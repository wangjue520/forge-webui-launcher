"""
ComfyUI 桥接插件 + 缺模型诊断：
  - comfy_bridge：放进 custom_nodes / 内容变了更新 / 用户停用了不放回
  - 插件本身（assets/comfyui/wwy_launcher_bridge）：按下拉框选项认出模型目录、同样的结果不重复打日志
  - diagnose：桥接日志 → 缺节点 / 缺模型发现，「检查通过」后消失；download_model 校验
  - webview_api：缺模型找下载地址（工作流自带 → Manager 模型表 → Civitai 同名）、下载走镜像顺序
  - diagnose_llm：用户描述带进对话、hf_files
运行：python -m unittest tests.test_comfy_bridge
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

import comfy_bridge  # noqa: E402
import diagnose as dg  # noqa: E402
import diagnose_llm as dl  # noqa: E402
import webview_api as wa  # noqa: E402
from test_diagnose import _Base, _touch, _openai_reply  # noqa: E402

PLUGIN = os.path.join(os.path.dirname(comfy_bridge.SRC), comfy_bridge.NAME, "__init__.py")


def bridge_line(wf="t.json", nodes=(), models=()):
    rep = {"wf": wf, "nodes": list(nodes), "cnr": {}, "models": list(models)}
    head = f"工作流「{wf}」缺 …" if nodes or models else f"工作流「{wf}」检查通过"
    return f"[WWY诊断] {head} {json.dumps(rep, ensure_ascii=False)}\n"


class EnsureTest(_Base):
    def test_install_update_disabled(self):
        cn = os.path.join(self.comfy, "custom_nodes")
        dst = os.path.join(cn, comfy_bridge.NAME)
        logs = []
        self.assertEqual(comfy_bridge.ensure(self.comfy, logs.append), "installed")
        self.assertTrue(os.path.isfile(os.path.join(dst, "web", "wwy_bridge.js")))
        self.assertEqual(comfy_bridge.ensure(self.comfy, logs.append), "ok")
        with open(os.path.join(dst, "__init__.py"), "a", encoding="utf-8") as f:
            f.write("# 旧版本\n")
        self.assertEqual(comfy_bridge.ensure(self.comfy, logs.append), "updated")
        self.assertEqual(comfy_bridge.status(self.comfy), "ok")
        self.assertEqual(len(logs), 2)
        os.rename(dst, dst + ".disabled")
        self.assertEqual(comfy_bridge.ensure(self.comfy), "disabled")
        self.assertFalse(os.path.isdir(dst))
        self.assertEqual(comfy_bridge.ensure(os.path.join(self.tmp, "nope")), "no_comfy")

    def test_launch_extras_respects_switch(self):
        self.api.cfg["diag_comfy_bridge"] = False
        self.api._instance_launch_extras("cf", self.api.instance_cfg("cf"), lambda m: None)
        self.assertEqual(comfy_bridge.status(self.comfy), "missing")
        self.api.cfg["diag_comfy_bridge"] = True
        self.api._instance_launch_extras("cf", self.api.instance_cfg("cf"), lambda m: None)
        self.assertEqual(comfy_bridge.status(self.comfy), "ok")


class PluginTest(_Base):
    """不起 ComfyUI：用假的 folder_paths / nodes 模块加载插件"""

    def setUp(self):
        super().setUp()
        models = os.path.join(self.tmp, "models")
        lists = {"loras": ["a.safetensors", "sub\\b.safetensors"], "checkpoints": ["sd.safetensors"],
                 "vae": [], "diffusion_models": []}
        fp = types.ModuleType("folder_paths")
        fp.folder_names_and_paths = {k: ([os.path.join(models, k)], set()) for k in lists}
        fp.get_filename_list = lambda k: list(lists[k])
        fp.get_folder_paths = lambda k: [os.path.join(models, k), os.path.join(self.tmp, "lib", k)]
        fp.get_user_directory = lambda: os.path.join(self.comfy, "user")

        class LoraLoader:
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"model": ("MODEL",), "lora_name": (list(lists["loras"]),)}}

        class OddLoader:     # 输入名猜不出、选项是新式 COMBO
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"pick": ("COMBO", {"options": list(lists["checkpoints"])})}}

        nd = types.ModuleType("nodes")
        nd.NODE_CLASS_MAPPINGS = {"LoraLoader": LoraLoader, "OddLoader": OddLoader}
        self.mods = mock.patch.dict(sys.modules, {"folder_paths": fp, "nodes": nd})
        self.mods.start()
        spec = importlib.util.spec_from_file_location("wwy_bridge_under_test", PLUGIN)
        self.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin)
        self.models = models

    def tearDown(self):
        self.mods.stop()
        super().tearDown()

    def report(self, **data):
        buf = io.StringIO()
        with redirect_stdout(buf):
            line = self.plugin.handle_report(data)
        return line, buf.getvalue()

    def test_folder_detection_and_dedupe(self):
        # 第一次就是干净的：不打日志
        self.assertIsNone(self.report(workflow_name="ok.json")[0])
        miss = [{"name": "Anima\\x.safetensors", "node_type": "LoraLoader", "input": "lora_name"},
                {"name": "y.safetensors", "node_type": "OddLoader", "input": "pick"},
                {"name": "z.safetensors", "node_type": "UNETLoader", "input": "unet_name"},
                {"name": "w.safetensors", "node_type": "X", "input": "w", "url": "https://h/w", "directory": "vae"}]
        line, out = self.report(workflow_name="t.json", missing_nodes=[{"type": "Foo", "cnr_id": "foo-pack"}, "Bar"],
                                missing_models=miss, workflow={"nodes": []})
        self.assertIn("缺 2 个节点、4 个模型", out)
        rep = json.loads(line[line.index("{"):])
        folders = {m["name"]: m["folder"] for m in rep["models"]}
        self.assertEqual(folders, {"Anima\\x.safetensors": "loras", "y.safetensors": "checkpoints",
                                   "z.safetensors": "diffusion_models", "w.safetensors": "vae"})
        self.assertEqual(rep["models"][0]["dirs"][0], os.path.join(self.models, "loras"))
        self.assertEqual(rep["cnr"], {"Foo": "foo-pack"})
        self.assertEqual(rep["nodes"], ["Bar", "Foo"])
        # 工作流另存了一份给 AI 读
        with open(os.path.join(self.comfy, "user", "wwy_bridge", "current_workflow.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["name"], "t.json")
        # 同样的结果（比如切回这个标签页）不再打
        self.assertIsNone(self.report(workflow_name="t.json", missing_nodes=["Foo", "Bar"], missing_models=miss)[0])
        # 补齐了：打一行「检查通过」，诊断随之清掉
        line, _ = self.report(workflow_name="t.json")
        self.assertIn("检查通过", line)
        self.assertEqual(dg.scan(line, dg.instance_ctx(self.api, "cf")), [])


class RuleTest(_Base):
    def test_bridge_findings_follow_last_report(self):
        m = [{"name": "x.safetensors", "node_type": "LoraLoader", "node_title": "加载LoRA", "input": "lora_name",
              "folder": "loras", "dirs": [os.path.join(self.comfy, "models", "loras")], "url": ""}]
        log = "got prompt\n" + bridge_line(nodes=["ImageResize+"], models=m)
        fs = self.scan("cf", log)
        node = self.one(fs, "comfy_missing_node")
        self.assertEqual(node["facts"]["node_types"], ["ImageResize+"])
        self.assertIn("拦着不让运行", node["text"])
        model = self.one(fs, "comfy_missing_model")
        self.assertIn("models\\loras", model["text"])
        self.assertEqual(model["facts"]["models"][0]["name"], "x.safetensors")
        # 只缺模型了：缺节点那条消失；全齐了：都消失
        self.assertEqual([f["rule"] for f in self.scan("cf", log + bridge_line(models=m))], ["comfy_missing_model"])
        self.assertEqual(self.scan("cf", log + bridge_line()), [])
        # 不同的缺失组合 key 不同（流式检测会重新提醒）
        other = self.one(self.scan("cf", bridge_line(nodes=["Other"])), "comfy_missing_node")
        self.assertNotEqual(other["key"], node["key"])
        # WebUI 实例不认这条
        self.assertEqual(self.scan("neo", log), [])
        self.assertTrue(dg.QUICK_TRIGGER.search(bridge_line()))

    def test_server_and_bridge_missing_node_keep_newest(self):
        server = "invalid prompt: {'type': 'missing_node_type', 'message': \"Node 'Foo' not found.\"}\n"
        fs = self.scan("cf", bridge_line(nodes=["Bar"]) + server)
        self.assertEqual(self.one(fs, "comfy_missing_node")["facts"]["node_types"], ["Foo"])

    def test_download_model_validation(self):
        d = os.path.join(self.comfy, "models", "loras")
        roots = [self.comfy]
        v = lambda p: dg.validate_action(self.api, "download_model", {"iid": "cf", **p}, roots)
        good = {"url": "https://huggingface.co/a/b/resolve/main/x.safetensors", "dir": d, "name": "x.safetensors"}
        p, err = v(good)
        self.assertIsNone(err)
        self.assertEqual(p["dir"], os.path.abspath(d))
        self.assertIsNone(v({**good, "url": "https://civitai.com/api/download/models/12345?type=Model"})[1])
        self.assertIsNone(v({**good, "url": "https://github.com/a/b/releases/download/v1/x.safetensors"})[1])
        self.assertTrue(v({**good, "url": "https://evil.com/x.safetensors"})[1])
        self.assertTrue(v({**good, "url": "https://civitai.com/models/123"})[1])         # 模型页不是下载地址
        self.assertTrue(v({**good, "name": "..\\..\\x.safetensors"})[1])
        self.assertTrue(v({**good, "name": "x.exe"})[1])
        self.assertTrue(v({**good, "dir": os.path.join(self.tmp, "elsewhere")})[1])
        self.assertTrue(dg.validate_action(self.api, "download_model", good, ())[1])   # 没给允许的目录就不让下


class EnrichAndDownloadTest(_Base):
    def setUp(self):
        super().setUp()
        self.api._diag_dl = set()
        self.api._spawn = lambda fn, name=None: fn()
        self.lib = os.path.join(self.tmp, "library")
        os.makedirs(self.lib)
        self.api.cfg["model_library_path"] = self.lib
        _touch(os.path.join(self.comfy, "custom_nodes", "ComfyUI-Manager.disabled", "model-list.json"),
               json.dumps({"models": [{"name": "4x-AnimeSharp", "filename": "4x-AnimeSharp.pth",
                                       "url": "https://huggingface.co/Kim2091/AnimeSharp/resolve/main/4x-AnimeSharp.pth",
                                       "save_path": "upscale_models"}]}))

    def finding(self, models):
        f = self.one(self.scan("cf", bridge_line(models=models)), "comfy_missing_model")
        return f, dg.instance_ctx(self.api, "cf")

    def test_sources_and_dirs(self):
        cdir = os.path.join(self.comfy, "models")
        models = [
            {"name": "wf.safetensors", "folder": "vae", "url": "https://huggingface.co/o/r/resolve/main/wf.safetensors",
             "dirs": [os.path.join(cdir, "vae"), os.path.join(self.lib, "vae")]},
            {"name": "4x-AnimeSharp.pth", "folder": "upscale_models", "dirs": [os.path.join(cdir, "upscale_models")]},
            {"name": "Anima\\civ.safetensors", "folder": "loras", "dirs": [os.path.join(cdir, "loras")]},
            {"name": "nowhere.safetensors", "folder": "loras", "dirs": [os.path.join(cdir, "loras")]},
        ]
        f, ctx = self.finding(models)

        def smart(name, role, base, key, limit=8):
            self.assertEqual(role, "LoRA")
            if name == "civ.safetensors":
                return [{"exact": True, "model_name": "Civ", "version_name": "v1",
                         "match_file": {"name": "civ.safetensors",
                                        "downloadUrl": "https://civitai.com/api/download/models/7"}}], False
            return [{"exact": False, "model_name": "像但不是", "match_file": {"name": "near.safetensors",
                                                                        "downloadUrl": "https://civitai.com/x"}}], False

        with mock.patch.object(wa.cd, "search_models_smart", smart):
            wa._diag_enrich(self.api, [f], ctx)
        acts = {a["params"].get("name"): a for a in f["actions"]}
        # 工作流自带地址；目标目录优先共享模型库
        self.assertEqual(acts["wf.safetensors"]["params"]["dir"], os.path.join(self.lib, "vae"))
        self.assertEqual(acts["4x-AnimeSharp.pth"]["params"]["url"],
                         "https://huggingface.co/Kim2091/AnimeSharp/resolve/main/4x-AnimeSharp.pth")
        # 子文件夹保留
        self.assertEqual(acts["civ.safetensors"]["params"]["dir"], os.path.join(cdir, "loras", "Anima"))
        self.assertEqual(acts["civ.safetensors"]["risk"], "confirm")
        # 近似结果不算：交给 AI
        ask = [a for a in f["actions"] if a["aid"] == "ai_ask"]
        self.assertEqual(len(ask), 1)
        self.assertIn("nowhere.safetensors", ask[0]["params"]["note"])
        self.assertNotIn("nowhere.safetensors", acts)
        # 给出的按钮都过得了校验
        for a in f["actions"]:
            self.assertIsNone(dg.validate_action(self.api, a["aid"], a["params"], dl.allowed_roots(self.api))[1],
                              a)

    def test_download_mirror_order_and_result(self):
        url = "https://huggingface.co/o/r/resolve/main/m.safetensors"
        d = os.path.join(self.comfy, "models", "vae")
        tried = []

        def dl_file(info, dest, api_key=None, progress_cb=None, **k):
            tried.append(info["downloadUrl"])
            if "hf-mirror" in info["downloadUrl"]:
                raise RuntimeError("镜像 502")
            path = os.path.join(dest, info["name"])
            _touch(path, "x")
            return path, None

        self.api.cfg["mirror_mode"] = "always"
        with mock.patch.object(wa.cd, "download_file", dl_file):
            r = wa._api_diag_fix(self.api, "download_model", {"iid": "cf", "url": url, "dir": d,
                                                              "name": "m.safetensors"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(tried, ["https://hf-mirror.com/o/r/resolve/main/m.safetensors", url])
        self.assertTrue(os.path.isfile(os.path.join(d, "m.safetensors")))
        logs = "".join(k.get("text", "") for s, t, k in self.api.events if t == "log")
        self.assertIn("镜像 502", logs)
        self.assertIn("按 R 刷新", logs)
        self.assertIn(("diag", "download_done"), [(s, t) for s, t, _k in self.api.events])
        # 已经有了就不再下
        r = wa._api_diag_fix(self.api, "download_model", {"iid": "cf", "url": url, "dir": d, "name": "m.safetensors"})
        self.assertIn("已经在", r["text"])
        self.assertEqual(len(tried), 2)


class AiNoteTest(_Base):
    def setUp(self):
        super().setUp()
        self.api.cfg["diag_llm_api_key"] = "sk-test"

    def test_note_goes_first_and_tools_listed(self):
        sent = []

        def post(url, headers, body, timeout):
            sent.append(json.loads(json.dumps(body)))
            return _openai_reply(calls=[("finish", {"summary": "s", "cause": "c", "steps": []})])

        dl.run(self.api, dg.instance_ctx(self.api, "cf"), "", note="出图全黑 sk-abcdefghijklmnopqrstu", post=post)
        user = sent[0]["messages"][1]["content"]
        self.assertTrue(user.startswith("用户的描述"))
        self.assertIn("出图全黑", user)
        self.assertNotIn("sk-abcdefghijklmnopqrstu", user)     # 描述里也脱敏
        names = [t["function"]["name"] for t in sent[0]["tools"]]
        self.assertIn("search_model", names)
        self.assertIn("hf_files", names)

    def test_hf_files(self):
        tools = dl.Tools(self.api, dg.instance_ctx(self.api, "cf"))
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"siblings": [{"rfilename": "split_files/vae/a.safetensors"},
                                               {"rfilename": "README.md"}]}
        with mock.patch("requests.get", return_value=resp) as g:
            out = tools.t_hf_files("Comfy-Org/x")
        self.assertIn("https://huggingface.co/Comfy-Org/x/resolve/main/split_files/vae/a.safetensors", out)
        self.assertNotIn("README", out)
        self.assertTrue(g.call_args[0][0].endswith("/api/models/Comfy-Org/x"))
        self.assertIn("格式", tools.t_hf_files("../etc"))


if __name__ == "__main__":
    unittest.main()
