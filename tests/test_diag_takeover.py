"""
AI 接管：AI 定修复 → 直接执行 → 重启 → 看新日志 → 还有报错就再来。
用假的实例（不真启动进程）和假的 AI（按剧本返回），只测循环本身：
  - 修完重启干净就结束、不再问 AI
  - 删文件 / 重启这类动作不自动做
  - 还有报错进入下一轮，带上前几轮做过的事
  - AI 说要用户动手就停；停止接管；给 AI 的说明按对象记住并带上
运行：python -m unittest tests.test_diag_takeover
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import unittest
from unittest import mock

import diagnose as dg  # noqa: E402
import diagnose_llm as dl  # noqa: E402
import webview_api as wa  # noqa: E402
from test_diagnose import _Base, _touch  # noqa: E402

VAE_NAN = "nodes.py:1595: RuntimeWarning: invalid value encountered in cast\n"


class FakeRunner:
    """只记录启停；每次 start 往实例日志里写一轮新的启动日志（内容按剧本）"""

    def __init__(self, api, iid, runs):
        self.api, self.iid, self.runs = api, iid, list(runs)
        self.state, self._running, self.url, self.status_text = "idle", False, None, ""
        self.starts = self.stops = 0

    def running(self):
        return self._running

    def log(self, text):
        self.api._emit("launch", "log", iid=self.iid, text=text)

    def stop(self):
        self.stops += 1
        self._running, self.state = False, "stopped"

    def start(self, root):
        self.starts += 1
        body = self.runs.pop(0) if self.runs else ""
        self.log("[启动器] 正在启动 ComfyUI ...\n" + body + "To see the GUI go to: http://127.0.0.1:8188\n")
        self._running, self.state, self.url = True, "ready", "http://127.0.0.1:8188"
        self.status_text = "运行中"
        return {"ok": True}


def ai_result(summary, actions=(), verdict="continue"):
    return {"summary": summary, "cause": "", "steps": ["自己去做 X"] if verdict == "need_user" else [],
            "actions": [{"aid": a, "label": label, "params": p, "risk": dg.ACTIONS[a]["risk"], "auto": False}
                        for a, label, p in actions], "dropped": [], "confidence": "high", "verdict": verdict}


class TakeoverTest(_Base):
    def setUp(self):
        super().setUp()
        self.api.cfg["diag_llm_api_key"] = "sk-test"
        dg.LOGS.clear(dg.inst_key("cf"))        # 日志缓冲是全局的，别吃到别的用例留下的
        self.runner = None
        self.patches = [mock.patch.object(wa, "_TK_SETTLE", 0),
                        mock.patch.object(wa, "_tk_sleep", lambda cancel, s: cancel.is_set())]
        for p in self.patches:
            p.start()
        self.api._runner = lambda iid=None: self.runner
        self.api._spawn = lambda fn, name=None: fn()
        self.calls = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def run_takeover(self, first_log, runs, script, note="", cancel_before=False):
        self.runner = FakeRunner(self.api, "cf", runs)
        if first_log is not None:
            self.runner._running, self.runner.state = True, "ready"
            self.runner.log("[启动器] 正在启动 ComfyUI ...\n" + first_log)
        script = list(script)

        def fake_run(api, ctx, text, findings, on_step=None, cancelled=None, post=None, note="", takeover=None):
            self.calls.append({"text": text, "findings": [f["rule"] for f in findings], "note": note,
                               "takeover": dict(takeover or {})})
            return script.pop(0)

        job = wa._diag_new_job(self.api)
        self.api._diag_tk["cf"] = job
        if cancel_before:
            self.api._diag_jobs[job].set()
        with mock.patch.object(dl, "run", fake_run):
            wa._diag_takeover_run(self.api, job, "cf", note)
        done = [k for s, t, k in self.api.events if (s, t) == ("diag", "takeover_done")]
        self.assertEqual(len(done), 1)
        self.assertNotIn("cf", self.api._diag_tk)
        return done[0], [k for s, t, k in self.api.events if (s, t) == ("diag", "takeover_step")]

    def test_fix_restart_clean_ends_without_more_ai(self):
        trash = os.path.join(self.comfy, "models", "x.safetensors")
        _touch(trash)
        done, steps = self.run_takeover(VAE_NAN, ["正常启动\n"], [ai_result("VAE 溢出", [
            ("set_instance_cfg", "VAE 改用 FP32", {"iid": "cf", "changes": {"vae_precision": "fp32"}}),
            ("restart_instance", "重启", {"iid": "cf"}),
            ("trash_file", "删掉坏模型", {"path": trash})])], note="缺模型不用管")
        self.assertTrue(done["ok"], done)
        self.assertEqual(len(self.calls), 1)                    # 重启后干净：不再问 AI
        self.assertEqual(self.calls[0]["note"], "缺模型不用管")
        self.assertIn("vae_nan", self.calls[0]["findings"])
        self.assertEqual(self.api.instance_cfg("cf")["vae_precision"], "fp32")
        self.assertTrue(os.path.isfile(trash))                  # 删文件不自动做
        self.assertEqual((self.runner.stops, self.runner.starts), (1, 1))
        kinds = [s["kind"] for s in steps]
        self.assertIn("warn", kinds)
        act = [s for s in steps if s["kind"] == "act"]
        self.assertEqual(len(act), 1)
        self.assertTrue(act[0]["undo"])                         # 改设置可以撤销
        log = dg.LOGS.tail(dg.inst_key("cf"))
        self.assertIn("[诊断] AI 接管结束", log)

    def test_second_round_gets_history(self):
        done, _ = self.run_takeover(VAE_NAN, [VAE_NAN], [
            ai_result("VAE 溢出", [("set_instance_cfg", "VAE FP32", {"iid": "cf", "changes": {"vae_precision": "fp32"}})]),
            ai_result("剩下的不影响", verdict="fixed")])
        self.assertTrue(done["ok"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["takeover"]["round"], 2)
        self.assertIn("第 1 轮", self.calls[1]["takeover"]["history"][0])
        self.assertIn("VAE FP32", self.calls[1]["takeover"]["history"][0])
        self.assertEqual(self.calls[1]["text"].count("正在启动 ComfyUI"), 1)   # 只看最新这一轮的日志

    def test_need_user_stops(self):
        done, _ = self.run_takeover(VAE_NAN, [], [ai_result("要换驱动", verdict="need_user")])
        self.assertFalse(done["ok"])
        self.assertEqual(done["steps"], ["自己去做 X"])
        self.assertEqual(self.runner.starts, 0)

    def test_not_started_yet_starts_first(self):
        done, _ = self.run_takeover(None, ["正常启动\n"], [ai_result("没问题", verdict="fixed")])
        self.assertTrue(done["ok"])
        self.assertEqual(self.runner.starts, 1)

    def test_rounds_run_out(self):
        script = [ai_result(f"第{i}次", [("set_instance_cfg", "改", {"iid": "cf", "changes": {"vae_precision": "fp32"}})])
                  for i in range(wa._TK_ROUNDS)]
        done, _ = self.run_takeover(VAE_NAN, [VAE_NAN] * wa._TK_ROUNDS, script)
        self.assertFalse(done["ok"])
        self.assertIn("还没好", done["text"])
        self.assertEqual(len(self.calls), wa._TK_ROUNDS)

    def test_cancel(self):
        done, _ = self.run_takeover(VAE_NAN, [], [], cancel_before=True)
        self.assertFalse(done["ok"])
        self.assertIn("停止", done["text"])
        self.assertEqual(self.calls, [])

    def test_saved_note_and_api_guards(self):
        t = {"type": "inst", "iid": "cf"}
        self.assertTrue(wa._api_diag_note_set(self.api, t, "模型我自己有")["ok"])
        self.assertEqual(wa._diag_saved_note(self.api, t), "模型我自己有")
        self.assertEqual(wa._diag_saved_note(self.api, {"type": "inst", "iid": "neo"}), "")
        self.assertFalse(wa._api_diag_takeover(self.api, {"type": "launcher"})["ok"])
        got = {}
        with mock.patch.object(wa, "_diag_takeover_run", lambda api, job, iid, note: got.update(note=note)):
            r = wa._api_diag_takeover(self.api, t)
            self.assertTrue(r["ok"])
            self.assertEqual(got["note"], "模型我自己有")
            self.api._diag_tk["cf"] = r["job"]
            self.assertFalse(wa._api_diag_takeover(self.api, t)["ok"])       # 同一实例不能接管两次
        wa._api_diag_note_set(self.api, t, "")
        self.assertNotIn("inst:cf", self.api.cfg["diag_notes"])


class TakeoverPromptTest(_Base):
    def test_takeover_text_and_verdict(self):
        self.api.cfg["diag_llm_api_key"] = "sk-test"
        from test_diagnose import _openai_reply
        import json
        sent = []

        def post(url, headers, body, timeout):
            sent.append(json.loads(json.dumps(body)))
            return _openai_reply(calls=[("finish", {"summary": "s", "cause": "c", "steps": [], "verdict": "fixed"})])

        res = dl.run(self.api, dg.instance_ctx(self.api, "cf"), VAE_NAN, dg.scan(VAE_NAN, dg.instance_ctx(self.api, "cf")),
                     post=post, takeover={"round": 2, "max": 5, "history": ["第 1 轮：改了 X"], "state": "运行中"})
        user = sent[0]["messages"][1]["content"]
        self.assertIn("AI 接管", user)
        self.assertIn("第 1 轮：改了 X", user)
        self.assertIn("规则建议", user)
        self.assertEqual(res["verdict"], "fixed")


if __name__ == "__main__":
    unittest.main()
