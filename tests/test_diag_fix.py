"""
执行修复 / 跟 AI 说话：
  - 改 ComfyUI 网页里的工作流（绕过 / 静音节点、改参数）：校验、指令发到桥接插件、等回执、撤销
  - 接管时改工作流的动作不停实例、不重启
  - 追问带上上一次的结论；「执行修复」要 AI 给出能执行的动作；记住的说明 + 这次的话一起发
运行：python -m unittest tests.test_diag_fix
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import importlib.util
import json
import unittest
from unittest import mock

import comfy_bridge  # noqa: E402
import diagnose as dg  # noqa: E402
import diagnose_llm as dl  # noqa: E402
import webview_api as wa  # noqa: E402
from test_diagnose import _Base, _openai_reply  # noqa: E402
from test_diag_takeover import FakeRunner, TakeoverTest, ai_result  # noqa: E402


class FakeComfy:
    """假的 ComfyUI + 网页：收到指令就按 nodes 里的状态改，回执和桥接插件一样"""

    def __init__(self, clients=1, status=200):
        self.clients, self.status = clients, status
        self.nodes = {12: {"mode": "enable", "widgets": {"enabled": True}}, 23: {"mode": "enable", "widgets": {}}}
        self.cmds, self.acks, self.trust_env = [], {}, True

    def __call__(self):            # 当 requests.Session 用
        return self

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/wwy_bridge/command")
        self.cmds.append(json)
        if self.status == 200 and self.clients:
            self.acks[json["id"]] = self.apply(json)
        return mock.Mock(status_code=self.status, json=lambda: {"ok": True, "clients": self.clients})

    def get(self, url, timeout=None):
        return mock.Mock(json=lambda: {"ok": True, "ack": self.acks.get(url.rsplit("/", 1)[-1])})

    def apply(self, c):
        if c["op"] == "set_mode":
            if any(i not in self.nodes for i in c["node_ids"]):
                return {"ok": False, "text": "当前工作流里没有节点"}
            prev = {str(i): self.nodes[i]["mode"] for i in c["node_ids"]}
            for i in c["node_ids"]:
                self.nodes[i]["mode"] = c["mode"]
            return {"ok": True, "text": f"已{c['mode']} {c['node_ids']}", "prev": prev}
        n = self.nodes[c["node_id"]]["widgets"]
        prev, n[c["widget"]] = n.get(c["widget"]), c["value"]
        return {"ok": True, "text": "改了", "prev": prev}


class WorkflowEditTest(_Base):
    def setUp(self):
        super().setUp()
        self.comfy_web = FakeComfy()
        self.p = mock.patch("requests.Session", self.comfy_web)
        self.p.start()
        self.sleep = mock.patch.object(wa.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.p.stop()
        self.sleep.stop()
        super().tearDown()

    def test_validate(self):
        v = lambda aid, p: dg.validate_action(self.api, aid, {"iid": "cf", **p})[1]
        self.assertIsNone(v("comfy_set_mode", {"node_ids": [23], "mode": "bypass"}))
        self.assertIsNone(v("comfy_set_mode", {"node_ids": "23", "mode": "mute"}))       # 单个也行
        self.assertTrue(v("comfy_set_mode", {"node_ids": ["x"], "mode": "bypass"}))
        self.assertTrue(v("comfy_set_mode", {"node_ids": [23], "mode": "delete"}))
        self.assertIsNone(v("comfy_set_widget", {"node_id": 12, "widget": "enabled", "value": False}))
        self.assertTrue(v("comfy_set_widget", {"node_id": 12, "widget": "", "value": 1}))
        self.assertTrue(v("comfy_set_widget", {"node_id": 12, "widget": "a", "value": {"x": 1}}))
        self.assertTrue(dg.validate_action(self.api, "comfy_set_mode",
                                           {"iid": "neo", "node_ids": [1], "mode": "bypass"})[1])  # WebUI 不行
        self.assertEqual(dg.ACTIONS["comfy_set_mode"]["risk"], "low")

    def test_bypass_and_undo(self):
        r = wa._api_diag_fix(self.api, "comfy_set_mode", {"iid": "cf", "node_ids": [23], "mode": "bypass"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.comfy_web.nodes[23]["mode"], "bypass")
        self.assertEqual(self.comfy_web.cmds[0]["op"], "set_mode")
        self.assertFalse(self.comfy_web.trust_env)               # 本机地址不走代理
        self.assertTrue(wa._api_diag_undo(self.api, r["undo"])["ok"])
        self.assertEqual(self.comfy_web.nodes[23]["mode"], "enable")

    def test_set_widget_and_undo(self):
        r = wa._api_diag_fix(self.api, "comfy_set_widget", {"iid": "cf", "node_id": 12, "widget": "enabled",
                                                            "value": False})
        self.assertTrue(r["ok"], r)
        self.assertIs(self.comfy_web.nodes[12]["widgets"]["enabled"], False)
        wa._api_diag_undo(self.api, r["undo"])
        self.assertIs(self.comfy_web.nodes[12]["widgets"]["enabled"], True)

    def test_errors(self):
        r = wa._api_diag_fix(self.api, "comfy_set_mode", {"iid": "cf", "node_ids": [99], "mode": "bypass"})
        self.assertFalse(r["ok"])
        self.assertIn("没有节点", r["error"])
        self.comfy_web.clients = 0
        r = wa._api_diag_fix(self.api, "comfy_set_mode", {"iid": "cf", "node_ids": [23], "mode": "bypass"})
        self.assertIn("网页没打开", r["error"])
        self.comfy_web.clients, self.comfy_web.status = 1, 404
        r = wa._api_diag_fix(self.api, "comfy_set_mode", {"iid": "cf", "node_ids": [23], "mode": "bypass"})
        self.assertIn("旧版", r["error"])

    def test_undo_kept_when_page_closed(self):
        r = wa._api_diag_fix(self.api, "comfy_set_mode", {"iid": "cf", "node_ids": [23], "mode": "bypass"})
        self.comfy_web.clients = 0
        self.assertFalse(wa._api_diag_undo(self.api, r["undo"])["ok"])
        self.comfy_web.clients = 1
        self.assertTrue(wa._api_diag_undo(self.api, r["undo"])["ok"])           # 网页开了以后还能撤销


class TakeoverLiveTest(TakeoverTest):
    def test_workflow_edit_no_restart(self):
        web = FakeComfy()
        with mock.patch("requests.Session", web), mock.patch.object(wa.time, "sleep", lambda s: None):
            done, steps = self.run_takeover("!!! Exception during processing !!! A cannot be combined with B\n", [], [
                ai_result("两个加速节点冲突", [("comfy_set_mode", "绕过 #23", {"iid": "cf", "node_ids": [23],
                                                                         "mode": "bypass"})])])
        self.assertTrue(done["ok"], done)
        self.assertIn("运行", done["text"])
        self.assertEqual((self.runner.stops, self.runner.starts), (0, 0))      # 开着改，不重启
        self.assertEqual(web.nodes[23]["mode"], "bypass")
        self.assertTrue([s for s in steps if s["kind"] == "act"][0]["undo"])


for _name in [n for n in dir(TakeoverLiveTest) if n.startswith("test_") and n not in
              ("test_workflow_edit_no_restart",)]:
    setattr(TakeoverLiveTest, _name, None)          # 只跑这里新加的，父类的用例在 test_diag_takeover 里跑过


class PromptTest(_Base):
    def setUp(self):
        super().setUp()
        self.api.cfg["diag_llm_api_key"] = "sk-test"

    def ask(self, **kw):
        sent = []

        def post(url, headers, body, timeout):
            sent.append(json.loads(json.dumps(body)))
            return _openai_reply(calls=[("finish", {"summary": "s", "cause": "c", "steps": []})])

        dl.run(self.api, dg.instance_ctx(self.api, "cf"), "", post=post, **kw)
        return sent[0]["messages"][1]["content"]

    def test_followup_and_fix(self):
        prev = {"summary": "两个加速节点冲突", "cause": "c", "steps": ["绕过一个"], "actions": []}
        user = self.ask(note="按方案一改", followup=prev)
        self.assertIn("你上一次的诊断", user)
        self.assertIn("两个加速节点冲突", user)
        self.assertNotIn("执行修复", user)
        user = self.ask(followup=prev, fix=True)
        self.assertIn("执行修复", user)
        self.assertIn("comfy_set_mode", user)
        self.assertIn("comfy_set_mode", dl.system_prompt(self.api, dg.instance_ctx(self.api, "cf")))

    def test_compose_note(self):
        t = {"type": "inst", "iid": "cf"}
        self.assertEqual(wa._diag_compose_note(self.api, t, "出图全黑"), "出图全黑")
        wa._api_diag_note_set(self.api, t, "模型我自己有")
        self.assertEqual(wa._diag_compose_note(self.api, t, ""), "模型我自己有")
        both = wa._diag_compose_note(self.api, t, "出图全黑")
        self.assertIn("模型我自己有", both)
        self.assertIn("出图全黑", both)

    def test_api_passes_followup_and_fix(self):
        got = {}
        with mock.patch.object(wa, "_diag_run_ai", lambda api, job, target, note, followup, fix: got.update(
                note=note, followup=followup, fix=fix)):
            r = wa._api_diag_ai(self.api, {"type": "inst", "iid": "cf"}, "补充一句", {"summary": "x"}, True)
        self.assertTrue(r["ok"])
        self.assertEqual(got, {"note": "补充一句", "followup": {"summary": "x"}, "fix": True})


class BridgeAckTest(unittest.TestCase):
    def test_first_ok_wins(self):
        spec = importlib.util.spec_from_file_location(
            "wwy_bridge_ack_test", _os.path.join(comfy_bridge.SRC, "__init__.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.store_ack({"id": "a", "ok": False, "text": "这个标签页开的是别的工作流"})
        m.store_ack({"id": "a", "ok": True, "text": "已绕过", "prev": {"23": "enable"}})
        m.store_ack({"id": "a", "ok": False, "text": "又一个标签页"})
        self.assertEqual(m._acks["a"], {"ok": True, "text": "已绕过", "prev": {"23": "enable"}})
        self.assertEqual(m.VERSION, 2)


if __name__ == "__main__":
    unittest.main()
