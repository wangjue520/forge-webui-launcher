"""
报错诊断：
  - diagnose：日志缓冲、规则识别（ComfyUI 节点、网络、模型损坏、VAE 溢出……）、模型文件核对、动作校验、脱敏
  - diagnose_llm：两种接口格式的工具调用循环（用假的 HTTP 回复），AI 推荐的动作经白名单校验
  - webview_api 的接线：实例日志里出现 VAE NaN → 自动改 VAE 精度 → 撤销
运行：python -m unittest tests.test_diagnose
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import json
import os
import shutil
import struct
import tempfile
import threading
import unittest
from unittest import mock

import config_manager as cm  # noqa: E402
import diagnose as dg  # noqa: E402
import diagnose_llm as dl  # noqa: E402
import webview_api as wa  # noqa: E402


def _touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _safetensors(path, data_len, real_len=None):
    header = json.dumps({"w": {"dtype": "F16", "shape": [data_len // 2], "data_offsets": [0, data_len]}}).encode()
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(header)) + header + b"\0" * (data_len if real_len is None else real_len))


def make_cfg(insts, active):
    cfg = dict(cm.DEFAULT_CONFIG)
    cfg["instances"] = insts
    cfg["active_instance"] = active
    cm.project_active(cfg)
    return cfg


def fake_api(cfg):
    api = wa.LauncherApi.__new__(wa.LauncherApi)
    api.cfg = cfg
    api._cfg_lock = threading.RLock()
    api._runners = {}
    api._runners_lock = threading.Lock()
    api._window = None
    api.events = []
    api._emit_orig = api._emit
    api._emit = lambda scope, type_, **k: (api.events.append((scope, type_, k)),
                                          wa._diag_hook(api, scope, type_, k) if scope != "diag" else None)
    api._diag_lock = threading.Lock()
    api._diag_timers, api._diag_seen, api._diag_applied = {}, {}, {}
    api._diag_undo, api._diag_last_auto, api._diag_jobs = {}, {}, {}
    api._diag_seq = 0
    api._diag_by_key = {}
    return api


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p1 = mock.patch.object(cm, "CONFIG_PATH", os.path.join(self.tmp, "launcher_config.json"))
        self.p2 = mock.patch.object(cm, "write_theme_file", lambda cfg: None)
        self.p1.start(); self.p2.start()
        # Forge Neo：backend/args.py 声明参数
        self.neo = os.path.join(self.tmp, "neo")
        _touch(os.path.join(self.neo, "webui.bat"))
        _touch(os.path.join(self.neo, "launch.py"))
        _touch(os.path.join(self.neo, "backend", "args.py"), "parser.add_argument('--fp32-vae')")
        # A1111：modules/cmd_args.py
        self.a1111 = os.path.join(self.tmp, "a1111")
        _touch(os.path.join(self.a1111, "webui.bat"))
        _touch(os.path.join(self.a1111, "launch.py"))
        _touch(os.path.join(self.a1111, "modules", "cmd_args.py"),
               "parser.add_argument('--no-half-vae')\nparser.add_argument('--medvram')")
        # ComfyUI
        self.comfy = os.path.join(self.tmp, "ComfyUI")
        _touch(os.path.join(self.comfy, "main.py"))
        _touch(os.path.join(self.comfy, "comfy", "cli_args.py"), "parser.add_argument('--cpu-vae')")
        self.node = os.path.join(self.comfy, "custom_nodes", "ComfyUI-Foo")
        _touch(os.path.join(self.node, "requirements.txt"), "opencv-python\n")
        self.cfg = make_cfg([
            {"id": "neo", "name": "Neo", "webui_root": self.neo, "webui_branch": "neo2"},
            {"id": "old", "name": "A1111", "webui_root": self.a1111, "webui_branch": "classic"},
            {"id": "cf", "name": "Comfy", "webui_root": self.comfy, "webui_branch": "comfyui"},
        ], "neo")
        self.api = fake_api(self.cfg)

    def tearDown(self):
        self.p1.stop(); self.p2.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, iid, text):
        return dg.scan(text, dg.instance_ctx(self.api, iid))

    def one(self, findings, rule):
        hits = [f for f in findings if f["rule"] == rule]
        self.assertEqual(len(hits), 1, [f["rule"] for f in findings])
        return hits[0]


class LogBookTest(unittest.TestCase):
    def test_progress_bar_and_partial_lines(self):
        lb = dg.LogBook(max_lines=5)
        lb.add("k", "start\n 10%|#\r 50%|##\r100%|###\nhalf")
        lb.add("k", " line\n\x1b[31mred\x1b[0m\n")
        self.assertEqual(lb.tail("k"), "start\n100%|###\nhalf line\nred")
        for i in range(10):
            lb.add("k", f"{i}\n")
        self.assertEqual(lb.tail("k").splitlines(), ["5", "6", "7", "8", "9"])

    def test_current_run(self):
        text = "old NaNs was produced in VAE\n[启动器] 正在启动 webui.bat ...\nnew run"
        self.assertEqual(dg.current_run(text), "[启动器] 正在启动 webui.bat ...\nnew run")


class RuleTest(_Base):
    def test_comfy_import_failed(self):
        log = ("Traceback (most recent call last):\nModuleNotFoundError: No module named 'cv2'\n\n"
               f"Cannot import {self.node} module for custom nodes: No module named 'cv2'\n")
        f = self.one(self.scan("cf", log), "comfy_import_failed")
        aids = [a["aid"] for a in f["actions"]]
        self.assertEqual(aids, ["comfy_install_deps", "pip_install", "ext_disable"])
        self.assertEqual(f["actions"][1]["params"]["packages"], ["opencv-python"])
        self.assertIn("ComfyUI-Foo", f["title"])

    def test_comfy_missing_node_and_node_map(self):
        log = "invalid prompt: {'type': 'invalid_prompt', 'message': 'Cannot execute because node FooNode does not exist.'}"
        f = self.one(self.scan("cf", log), "comfy_missing_node")
        self.assertEqual(f["facts"]["node_types"], ["FooNode"])
        nm = {"https://github.com/a/foo": [["FooNode", "FooOther"], {"title_aux": "Foo Pack"}],
              "https://github.com/b/bar": [[], {"nodename_pattern": "^Bar", "title_aux": "Bar"}]}
        repos, missing = dg.resolve_node_types(["FooNode", "BarX", "Nope"], nm)
        self.assertEqual([(u, t) for u, t, _ in repos], [("https://github.com/a/foo", "Foo Pack"),
                                                         ("https://github.com/b/bar", "Bar")])
        self.assertEqual(missing, ["Nope"])

    def test_node_map_cache_and_fetch(self):
        nm = {"https://github.com/a/foo": [["FooNode"], {}]}
        cache = os.path.join(self.tmp, "map.json")
        with mock.patch.object(dg, "NODE_MAP_CACHE", cache):
            self.assertEqual(dg.load_node_map(self.comfy, fetch=lambda u: nm), nm)
            self.assertTrue(os.path.isfile(cache))
            self.assertEqual(dg.load_node_map(self.comfy, fetch=None), nm)   # 第二次读缓存

    def test_proxy_bad_hides_net_unreachable(self):
        self.api.cfg["proxy_enabled"] = True
        log = ("requests.exceptions.ProxyError: HTTPSConnectionPool(host='huggingface.co', port=443): "
               "Max retries exceeded with url: /x (Caused by ProxyError('Unable to connect to proxy', "
               "NewConnectionError('Failed to establish a new connection: [WinError 10061]')))\n"
               "proxy 127.0.0.1:7890")
        fs = self.scan("neo", log)
        self.assertEqual([f["rule"] for f in fs], ["proxy_bad"])
        self.assertEqual(fs[0]["actions"][0]["params"], {"changes": {"proxy_enabled": False}})

    def test_net_unreachable_mirror_auto(self):
        log = ("OSError: We couldn't connect to 'https://huggingface.co' to load this file\n"
               "requests.exceptions.ConnectTimeout: HTTPSConnectionPool(host='huggingface.co', port=443)")
        f = self.one(self.scan("cf", log), "net_unreachable")
        self.assertEqual(f["facts"]["sites"], ["HuggingFace"])
        a = f["actions"][0]
        self.assertEqual((a["aid"], a["auto"], a["params"]), ("set_launcher_cfg", True, {"changes": {"mirror_mode": "always"}}))
        # Civitai 没镜像：不给自动切镜像
        f = self.one(self.scan("neo", "civitai.com: Read timed out."), "net_unreachable")
        self.assertNotIn("set_launcher_cfg", [a["aid"] for a in f["actions"]])

    def test_vae_nan_per_branch(self):
        log = "modules.devices.NansException: A tensor with all NaNs was produced in VAE."
        a = self.one(self.scan("neo", log), "vae_nan")["actions"][0]
        self.assertEqual((a["aid"], a["params"]["changes"], a["auto"]), ("set_instance_cfg", {"vae_precision": "fp32"}, True))
        a = self.one(self.scan("old", log), "vae_nan")["actions"][0]
        self.assertEqual((a["aid"], a["params"]["arg"]), ("add_launch_arg", "--no-half-vae"))
        a = self.one(self.scan("cf", "nodes.py:1500: RuntimeWarning: invalid value encountered in cast"),
                     "vae_nan")["actions"][0]
        self.assertEqual(a["params"]["changes"], {"vae_precision": "fp32"})

    def test_oom_pagefile_numpy_fatal(self):
        fs = self.scan("cf", "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB\n"
                             "OSError: [WinError 1455] 页面文件太小，无法完成操作。\n"
                             "A module that was compiled using NumPy 1.x cannot be run in\nNumPy 2.0.2 as it may crash.")
        rules = {f["rule"] for f in fs}
        self.assertEqual(rules, {"cuda_oom", "pagefile", "numpy2"})
        self.assertEqual(self.one(fs, "numpy2")["actions"][0]["params"]["packages"], ["numpy<2"])
        f = self.one(self.scan("neo", "RuntimeError: The NVIDIA driver on your system is too old"), "fatal_hint")
        self.assertEqual(f["actions"][0]["aid"], "open_url")
        self.assertIn("驱动", f["title"])

    def test_model_corrupt(self):
        path = os.path.join(self.neo, "models", "Stable-diffusion", "bad model.safetensors")
        os.makedirs(os.path.dirname(path))
        _safetensors(path, 4096, real_len=100)
        _touch(os.path.splitext(path)[0] + ".civitai.info", json.dumps({"modelId": 12, "versionId": 34}))
        log = (f"Loading weights [abc] from {path}\n"
               "safetensors_rust.SafetensorError: Error while deserializing header: MetadataIncompleteBuffer")
        f = self.one(self.scan("neo", log), "model_corrupt")
        self.assertEqual(f["facts"]["state"], "corrupt")
        self.assertEqual([a["aid"] for a in f["actions"]], ["reveal_path", "civitai_redownload", "trash_file"])
        self.assertEqual(f["actions"][1]["params"]["url"], "https://civitai.com/models/12?modelVersionId=34")
        # 文件其实是好的：降级成「加载出错」
        _safetensors(path, 4096)
        self.assertEqual(self.scan("neo", log)[0]["rule"], "model_load_error")

    def test_kind_filter(self):
        self.assertEqual(self.scan("neo", f"Cannot import {self.node} module for custom nodes: x"), [])


class ModelFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_states(self):
        self.assertEqual(dg.check_model_file(os.path.join(self.tmp, "nope.safetensors"))[0], "missing")
        self.assertEqual(dg.check_model_file(self.p("e.safetensors", b""))[0], "corrupt")
        self.assertEqual(dg.check_model_file(self.p("h.safetensors", b"<!DOCTYPE html><html>"))[0], "corrupt")
        self.assertEqual(dg.check_model_file(self.p("g.gguf", b"GGUF\x03\0\0\0"))[0], "ok")
        self.assertEqual(dg.check_model_file(self.p("b.gguf", b"XXXX\x03\0\0\0"))[0], "corrupt")
        self.assertEqual(dg.check_model_file(self.p("z.ckpt", b"PK\x03\x04broken"))[0], "corrupt")
        self.assertEqual(dg.check_model_file(self.p("t.ckpt", b"<html>"))[0], "corrupt")
        ok = os.path.join(self.tmp, "ok.safetensors")
        _safetensors(ok, 64)
        self.assertEqual(dg.check_model_file(ok)[0], "ok")

    def test_find_model_path(self):
        text = "{'filename': 'D:\\\\ai\\\\models\\\\a b.safetensors', 'x': 1}\nother"
        self.assertEqual(dg.find_model_path(text), "D:\\ai\\models\\a b.safetensors")
        self.assertEqual(dg.find_model_path("Loading from E:/m/x.gguf now"), "E:/m/x.gguf")


class ValidateTest(_Base):
    def test_rejects(self):
        v = lambda aid, p, roots=(): dg.validate_action(self.api, aid, p, roots)[1]
        self.assertIsNone(v("set_instance_cfg", {"iid": "neo", "changes": {"vae_precision": "fp32"}}))
        self.assertTrue(v("set_instance_cfg", {"iid": "neo", "changes": {"webui_root": "C:\\"}}))
        self.assertTrue(v("set_instance_cfg", {"iid": "nope", "changes": {"vae_precision": "fp32"}}))
        self.assertTrue(v("pip_install", {"iid": "cf", "packages": ["torch==2.1"]}))
        self.assertTrue(v("pip_install", {"iid": "cf", "packages": ["x; rm -rf /"]}))
        self.assertIsNone(v("pip_install", {"iid": "cf", "packages": ["numpy<2", "opencv-python>=4.8"]}))
        self.assertTrue(v("comfy_install_node", {"iid": "cf", "url": "https://evil.com/a/b"}))
        self.assertTrue(v("add_launch_arg", {"iid": "neo", "arg": "--made-up"}))       # 这个版本不认识
        self.assertIsNone(v("add_launch_arg", {"iid": "old", "arg": "--no-half-vae"}))
        self.assertTrue(v("add_launch_arg", {"iid": "old", "arg": "--x & calc"}))
        self.assertTrue(v("set_launcher_cfg", {"changes": {"civitai_api_key": "x"}}))
        self.assertTrue(v("open_url", {"url": "javascript:alert(1)"}))
        self.assertTrue(v("comfy_install_deps", {"iid": "cf", "node_dir": self.tmp}))
        self.assertIsNone(v("comfy_install_deps", {"iid": "cf", "node_dir": self.node}))
        outside = os.path.join(self.tmp, "x.safetensors")
        _touch(outside)
        self.assertTrue(v("trash_file", {"path": outside}, [self.neo]))
        self.assertTrue(v("trash_file", {"path": os.path.join(self.neo, "webui.bat")}, [self.neo]))
        self.assertTrue(v("no_such", {}))

    def test_redact(self):
        home = os.path.expanduser("~")
        out = dg.redact(f"{home}\\x.txt api_key=sk-abcdefghijklmnop1234 Bearer abcdefghijklmnopq hf_" + "a" * 30)
        self.assertNotIn(home, out)
        self.assertNotIn("abcdefghijklmnop1234", out)
        self.assertNotIn("a" * 30, out)
        self.assertEqual(dg.redact_config({"civitai_api_key": "k", "n": {"token": "t"}, "a": 1}),
                         {"civitai_api_key": "***", "n": {"token": "***"}, "a": 1})


class AutoFixTest(_Base):
    """实例日志流 → 规则扫描 → 自动改 VAE 精度 → 撤销"""

    def test_stream_scan_auto_fix_and_undo(self):
        self.api._emit("launch", "log", iid="neo", text="[启动器] 正在启动 webui.bat ...\n")
        self.api._emit("launch", "log", iid="neo",
                       text="NansException: A tensor with all NaNs was produced in VAE.\n")
        self.assertIn("neo", self.api._diag_timers)
        self.api._diag_timers.pop("neo").cancel()
        wa._diag_stream_scan(self.api, "neo")
        self.assertEqual(cm.instance_cfg(self.api.cfg, "neo")["vae_precision"], "fp32")
        found = [e for e in self.api.events if e[:2] == ("diag", "found")]
        self.assertEqual(found[0][2]["titles"], ["VAE 溢出（出图全黑 / NaN）"])
        logs = "".join(e[2].get("text", "") for e in self.api.events if e[:2] == ("launch", "log"))
        self.assertIn("[诊断] 已自动处理", logs)
        # 诊断面板里能看到「已自动处理」和撤销记录
        rep = wa._api_diag_run(self.api, {"type": "inst", "iid": "neo"})
        acts = rep["findings"][0]["actions"]
        act = acts[0]
        self.assertEqual(act["params"]["changes"], {"vae_precision": "fp32"})
        self.assertTrue(act["applied"]["undo"])
        self.assertIn({"vae_precision": "cpu"}, [a["params"].get("changes") for a in acts])   # 下一步建议
        self.assertEqual(wa._api_diag_undo(self.api, act["applied"]["undo"])["ok"], True)
        self.assertEqual(cm.instance_cfg(self.api.cfg, "neo")["vae_precision"], "auto")
        # 撤销过的不再自动做
        wa._diag_auto_apply(self.api, dg.scan("NaNs was produced in VAE", dg.instance_ctx(self.api, "neo")))
        self.assertEqual(cm.instance_cfg(self.api.cfg, "neo")["vae_precision"], "auto")

    def test_add_arg_and_undo(self):
        r = wa._api_diag_fix(self.api, "add_launch_arg", {"iid": "old", "arg": "--no-half-vae"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(cm.instance_cfg(self.api.cfg, "old")["extra_args"], "--no-half-vae")
        wa._api_diag_undo(self.api, r["undo"])
        self.assertEqual(cm.instance_cfg(self.api.cfg, "old")["extra_args"], "")

    def test_launcher_error_triggers_auto(self):
        jobs = []
        with mock.patch.object(self.api, "_spawn", lambda fn, name="": jobs.append(fn), create=True):
            self.api._emit("deploy", "error", title="源码下载失败",
                           text="fatal: unable to access 'https://github.com/x/y/': Failed to connect to github.com port 443")
            self.api._emit("deploy", "error", title="源码下载失败",
                           text="fatal: unable to access 'https://github.com/x/y/': Failed to connect to github.com port 443")
        self.assertEqual(len(jobs), 1)        # 同一个错误 2 分钟内只诊断一次
        jobs[0]()
        auto = [e for e in self.api.events if e[:2] == ("diag", "auto")][0][2]
        self.assertEqual(auto["report"]["findings"][0]["rule"], "net_unreachable")
        self.assertEqual(auto["job"], "")     # 没配 AI
        self.assertEqual(self.api.cfg["mirror_mode"], "always")


def _openai_reply(content="", calls=()):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return 200, {"choices": [{"message": msg}]}, ""


class LlmTest(_Base):
    def setUp(self):
        super().setUp()
        self.api.cfg.update(diag_llm_provider="deepseek", diag_llm_api_key="sk-test")

    def test_resolve(self):
        self.assertEqual(dl.resolve(self.api.cfg), ("openai", "https://api.deepseek.com", "deepseek-v4-flash", "sk-test"))
        self.assertIsNone(dl.resolve({**self.api.cfg, "diag_llm_api_key": ""}))
        self.assertIsNone(dl.resolve({**self.api.cfg, "diag_llm_provider": "custom"}))   # 自定义没填地址

    def test_openai_agent_loop(self):
        sent = []
        replies = [
            _openai_reply(calls=[("list_dir", {"path": self.node})]),
            _openai_reply(calls=[("read_file", {"path": "C:\\Windows\\win.ini"})]),
            _openai_reply(calls=[("finish", {
                "summary": "节点缺依赖", "cause": "没装 cv2", "steps": ["停止实例", "点安装"],
                "actions": [{"action": "comfy_install_deps", "params": {"iid": "cf", "node_dir": self.node}},
                            {"action": "pip_install", "params": {"iid": "cf", "packages": ["torch"]}}]})]),
        ]

        def post(url, headers, body, timeout):
            sent.append((url, headers, json.loads(json.dumps(body))))
            return replies[len(sent) - 1]

        steps = []
        ctx = dg.instance_ctx(self.api, "cf")
        res = dl.run(self.api, ctx, "No module named 'cv2'", on_step=steps.append, post=post)
        self.assertEqual(sent[0][0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(sent[0][1]["Authorization"], "Bearer sk-test")
        self.assertIn("finish", [t["function"]["name"] for t in sent[0][2]["tools"]])
        # 第二轮带上了 list_dir 的结果
        tool_msgs = [m for m in sent[1][2]["messages"] if m["role"] == "tool"]
        self.assertIn("requirements.txt", tool_msgs[0]["content"])
        # 范围外的文件读不了
        tool_msgs = [m for m in sent[2][2]["messages"] if m["role"] == "tool"]
        self.assertIn("不在允许读取的范围", tool_msgs[-1]["content"])
        self.assertEqual(res["summary"], "节点缺依赖")
        self.assertEqual([a["aid"] for a in res["actions"]], ["comfy_install_deps"])
        self.assertEqual(len(res["dropped"]), 1)
        self.assertEqual(len(steps), 3)

    def test_anthropic_format(self):
        self.api.cfg.update(diag_llm_provider="claude")
        sent = []

        def post(url, headers, body, timeout):
            sent.append((url, headers, json.loads(json.dumps(body))))
            if len(sent) == 1:
                return 200, {"content": [{"type": "text", "text": "看看日志"},
                                         {"type": "tool_use", "id": "t1", "name": "get_log",
                                          "input": {"source": "launcher"}}], "stop_reason": "tool_use"}, ""
            return 200, {"content": [{"type": "tool_use", "id": "t2", "name": "finish",
                                      "input": {"summary": "s", "cause": "c", "steps": []}}]}, ""

        res = dl.run(self.api, dg.launcher_ctx(self.api), "", post=post)
        self.assertEqual(sent[0][0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(sent[0][1]["x-api-key"], "sk-test")
        self.assertEqual(sent[0][2]["model"], "claude-sonnet-5-5")
        last = sent[1][2]["messages"][-1]
        self.assertEqual(last["content"][0]["type"], "tool_result")
        self.assertEqual(last["content"][0]["tool_use_id"], "t1")
        self.assertEqual(res["summary"], "s")

    def test_http_errors(self):
        with self.assertRaises(dl.LlmError) as cm_:
            dl.run(self.api, dg.launcher_ctx(self.api), "",
                   post=lambda *a: (401, {"error": {"message": "bad key"}}, ""))
        self.assertIn("API Key 无效", str(cm_.exception))
        ok, text = dl.ping(self.api.cfg, post=lambda *a: (200, {"choices": [{"message": {"content": "正常"}}]}, ""))
        self.assertTrue(ok)
        self.assertIn("正常", text)

    def test_system_prompt_has_knowledge_and_actions(self):
        sp = dl.system_prompt(self.api, dg.launcher_ctx(self.api))
        self.assertIn("ComfyUI", sp)
        self.assertIn("set_instance_cfg", sp)
        self.assertIn(self.neo, sp)


if __name__ == "__main__":
    unittest.main()
