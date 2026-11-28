"""
Python 包变化记录 + 一键修复 + 输出对比：
  - pip_history：第一次只记基准、之后只记变化、没变化不记、超出条数丢最旧的；认出会改环境的 pip 命令、
    说成人话；around_pip 前后各拍一次；实例用哪个 Python（Forge 没建 venv 时不记基础解释器）
  - webview_api：插件管理 / 版本管理跑 pip 时记录；输出对比读详情 + 采样参数
  - diagnose_llm：AI 能查变化记录，环境信息里带最近的变化，系统说明里讲清楚不要靠回退解决
  - 一键修复：缺的模型全下、缺的节点合成一个安装任务、没找到地址的先让 AI 找；装东西前停实例、装完启动
运行：python -m unittest tests.test_pip_history
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import diagnose as dg  # noqa: E402
import diagnose_llm as dl  # noqa: E402
import pip_history as ph  # noqa: E402
import webview_api as wa  # noqa: E402
from test_comfy_bridge import bridge_line  # noqa: E402
from test_diag_takeover import FakeRunner, TakeoverTest, ai_result  # noqa: E402
from test_diagnose import _Base, _touch  # noqa: E402


class _HistDir:
    def setUp(self):
        super().setUp()
        self.hdir = tempfile.mkdtemp()
        self._ph = mock.patch.object(ph, "HISTORY_DIR", self.hdir)
        self._ph.start()
        self.py = os.path.join(self.hdir, "env", "Scripts", "python.exe")
        _touch(self.py)

    def tearDown(self):
        self._ph.stop()
        shutil.rmtree(self.hdir, ignore_errors=True)
        super().tearDown()


class RecordTest(_HistDir, unittest.TestCase):
    def test_baseline_then_changes_only(self):
        first = ph.record(self.py, "x", packages={"numpy": "1.26.4", "torch": "2.7.0"})
        self.assertTrue(first["baseline"])
        self.assertEqual(first["count"], 2)
        self.assertIsNone(ph.record(self.py, "没变", packages={"numpy": "1.26.4", "torch": "2.7.0"}))
        e = ph.record(self.py, "插件管理：安装 opencv-python",
                      packages={"numpy": "2.2.6", "opencv-python": "4.11.0", "torch": "2.7.0"})
        self.assertEqual(e["added"], {"opencv-python": "4.11.0"})
        self.assertEqual(e["changed"], {"numpy": ["1.26.4", "2.2.6"]})
        self.assertEqual(e["removed"], {})
        e2 = ph.record(self.py, "卸载", packages={"numpy": "2.2.6", "torch": "2.7.0"})
        self.assertEqual(e2["removed"], {"opencv-python": "4.11.0"})
        rows = ph.history(self.py)
        self.assertEqual([r["reason"] for r in rows][:2], ["卸载", "插件管理：安装 opencv-python"])  # 新的在前
        self.assertEqual(len(rows), 3)
        text = ph.summary_text(self.py)
        self.assertIn("numpy 1.26.4→2.2.6", text)
        self.assertIn("新装：opencv-python 4.11.0", text)
        self.assertIn("卸掉了：opencv-python 4.11.0", text)

    def test_trims_oldest(self):
        with mock.patch.object(ph, "MAX_ENTRIES", 3):
            for i in range(6):
                ph.record(self.py, f"第{i}次", packages={"a": str(i)})
        rows = ph.history(self.py, 10)
        self.assertEqual([r["reason"] for r in rows], ["第5次", "第4次", "第3次"])

    def test_names_normalized_and_bad_python(self):
        self.assertEqual(ph.canon("Typing_Extensions"), ph.canon("typing.extensions"))
        self.assertIsNone(ph.record(os.path.join(self.hdir, "nope.exe"), "x", packages={}))
        self.assertIsNone(ph.list_packages(os.path.join(self.hdir, "nope.exe")))

    def test_list_packages_real_python(self):
        pk = ph.list_packages(sys.executable)
        self.assertIsInstance(pk, dict)
        self.assertIn("requests", pk)          # 启动器自己的环境里一定有

    def test_pip_verb_and_describe(self):
        py = r"D:\c\python.exe"
        self.assertEqual(ph.pip_verb(py, ["-s", "-m", "pip", "install", "--disable-pip-version-check", "x"]), "install")
        self.assertEqual(ph.pip_verb(py, ["-m", "pip", "uninstall", "-y", "torchvision"]), "uninstall")
        self.assertEqual(ph.pip_verb(py, ["-m", "pip", "--version"]), "")
        self.assertEqual(ph.pip_verb(py, ["-m", "pip", "show", "torch"]), "")
        self.assertEqual(ph.pip_verb(py, ["-m", "pip", "install", "--dry-run", "x"]), "")
        self.assertEqual(ph.pip_verb(py, ["-c", "print(1)"]), "")
        self.assertEqual(ph.pip_verb("git", ["clone", "pip", "install"]), "")
        d = ph.describe(["-m", "pip", "install", "-r", "requirements.txt", "-i", "https://mirror/simple",
                         "--extra-index-url", "https://x"], r"D:\ComfyUI\custom_nodes\ComfyUI-Foo")
        self.assertEqual(d, "安装 ComfyUI-Foo/requirements.txt 里的依赖")
        self.assertEqual(ph.describe(["-m", "pip", "install", "numpy<2", "-c", "pin.txt", "D:\\w\\torch-2.7.0.whl"]),
                         "安装 numpy<2 torch-2.7.0.whl")
        self.assertEqual(ph.describe(["-m", "pip", "uninstall", "-y", "torchvision"]), "卸载 torchvision")

    def test_around_pip_records_before_and_after(self):
        calls = []
        with mock.patch.object(ph, "record", lambda py, reason, env=None: calls.append((py, reason))):
            with ph.around_pip(self.py, ["-m", "pip", "install", "numpy<2"], "", "诊断"):
                calls.append("run")
            with ph.around_pip(self.py, ["-m", "pip", "--version"], ""):
                calls.append("run2")
            with ph.around_pip("git", ["clone", "x"], ""):
                calls.append("run3")
        self.assertEqual(calls[1], "run")
        self.assertIn("不是启动器装的", calls[0][1])
        self.assertEqual(calls[2], (self.py, "诊断：安装 numpy<2"))
        self.assertEqual(calls[3:], ["run2", "run3"])


class InstanceTest(_HistDir, _Base):
    def test_instance_python_strict(self):
        self.assertEqual(ph.instance_python(self.api, "neo", strict=True), "")    # 还没建 venv：不记基础解释器
        vpy = os.path.join(self.neo, "venv", "Scripts", "python.exe")
        _touch(vpy)
        self.assertEqual(ph.instance_python(self.api, "neo", strict=True), vpy)
        self.assertEqual(dl.instance_python(self.api, "neo"), vpy)
        with mock.patch.object(ph, "record", lambda py, reason, env=None: None) as _r:
            t = ph.record_instance_async(self.api, "neo", "启动前")
            self.assertIsNotNone(t)
            t.join(5)
        self.assertIsNone(ph.record_instance_async(self.api, "nope", "x"))

    def test_stream_cmd_records_pip(self):
        reasons = []
        with mock.patch.object(ph, "record", lambda py, reason, env=None: reasons.append(reason)), \
                mock.patch.object(wa, "_stream_cmd_raw", lambda *a, **k: 0):
            self.assertEqual(wa._stream_cmd(self.api, "ext", self.py, ["-m", "pip", "install", "opencv-python"],
                                            self.tmp), 0)
            wa._stream_cmd(self.api, "ver", self.py, ["-m", "pip", "show", "x"], self.tmp)
        self.assertEqual(len(reasons), 2)
        self.assertEqual(reasons[1], "插件管理：安装 opencv-python")

    def test_deploy_cmd_records_pip(self):
        reasons = []
        with mock.patch.object(ph, "record", lambda py, reason, env=None: reasons.append(reason)), \
                mock.patch.object(wa, "_deploy_run_cmd_raw", lambda *a, **k: 0):
            wa._deploy_run_cmd(self.api, self.py, ["-m", "pip", "install", "-r", "requirements.txt"], self.neo,
                               lambda t: None)
        self.assertEqual(reasons[1], "环境部署：安装 neo/requirements.txt 里的依赖")

    def test_ai_sees_history(self):
        vpy = os.path.join(self.neo, "venv", "Scripts", "python.exe")
        _touch(vpy)
        ph.record(vpy, "x", packages={"numpy": "1.26.4"})
        ph.record(vpy, "插件管理：安装 ComfyUI-Foo/requirements.txt 里的依赖", packages={"numpy": "2.2.6"})
        tools = dl.Tools(self.api, dg.instance_ctx(self.api, "neo"))
        out = tools.call("pip_history", {"iid": "neo"})
        self.assertIn("numpy 1.26.4→2.2.6", out)
        self.assertIn("安装 ComfyUI-Foo", out)
        self.assertIn("没有跟 torch 有关的变化记录", tools.call("pip_history", {"iid": "neo", "package": "torch"}))
        self.assertIn("numpy", tools.call("pip_history", {"iid": "neo", "package": "NumPy"}))
        env = dl.environment(self.api, dg.instance_ctx(self.api, "neo"))
        self.assertIn("Python 包最近变化", env)
        self.assertIn("1.26.4→2.2.6", env)
        prompt = dl.system_prompt(self.api, dg.instance_ctx(self.api, "neo"))
        self.assertIn("不要把「卸掉新装的节点", prompt)
        self.assertIn("pip_history", [n for n, _d, _s in dl.TOOL_SPECS])


class FixAllPlanTest(unittest.TestCase):
    def test_plan_filters(self):
        a = lambda aid, label, risk="confirm", **kw: {"aid": aid, "label": label, "risk": risk, "params": kw}
        dl1 = a("download_model", "下载 x", iid="cf", name="x")
        done = a("set_instance_cfg", "已改", "low", iid="cf")
        done["applied"] = {"text": "已改", "undone": False}
        undone = a("add_launch_arg", "加参数", "low", iid="cf", arg="--x")
        undone["applied"] = {"text": "加参数", "undone": True}
        findings = [{"actions": [dl1, a("open_page", "去页面", "ui", page="x"), a("trash_file", "删", "danger"),
                                 a("restart_instance", "重启", iid="cf"), done, undone,
                                 a("ext_disable", "先停用 Foo", iid="cf", rel="Foo"),
                                 a("ai_ask", "让 AI 找", "ui", note="找 y")]},
                    {"actions": [dict(dl1), a("ai_ask", "让 AI 找", "ui", note="找 y")]}]
        acts, asks = wa.fixall_plan(findings, [a("download_model", "下载 y", iid="cf", name="y")])
        self.assertEqual([x["label"] for x in acts], ["下载 x", "加参数", "下载 y"])
        self.assertEqual(asks, ["找 y"])


class FixAllRunTest(TakeoverTest):
    def test_one_click_fixes_everything(self):
        self.runner = FakeRunner(self.api, "cf", [])
        self.runner._running, self.runner.state = True, "ready"
        mdir = os.path.join(self.comfy, "models", "loras")
        url_a = "https://huggingface.co/x/y/resolve/main/a.safetensors"
        url_b = "https://civitai.com/api/download/models/123"
        models = [{"name": n, "folder": "loras", "node_type": "LoraLoader", "input": "lora_name", "dirs": [mdir],
                   **({"url": url_a} if n == "a.safetensors" else {})} for n in ("a.safetensors", "b.safetensors")]
        self.runner.log("[启动器] 正在启动 ComfyUI ...\n" + bridge_line(nodes=["FooNode", "BarNode"], models=models))
        downloads, installs, ai_notes = [], [], []

        def fake_run(api, ctx, text, findings, on_step=None, cancelled=None, post=None, note="", takeover=None,
                     followup=None, fix=False):
            ai_notes.append((note, fix))
            return ai_result("找到了 b", [("download_model", "下载 b.safetensors",
                                          {"iid": "cf", "url": url_b, "dir": mdir, "name": "b.safetensors"})])

        repos = [("https://github.com/a/foo", "Foo", ["FooNode"]), ("https://github.com/b/bar", "Bar", ["BarNode"])]
        with mock.patch.object(dl, "run", fake_run), \
                mock.patch.object(dg, "load_model_list", lambda *a, **k: []), \
                mock.patch.object(dg, "load_node_map", lambda *a, **k: {"x": 1}), \
                mock.patch.object(dg, "resolve_node_types", lambda types, nm: (repos, [])), \
                mock.patch.object(wa, "_diag_civitai_exact", lambda *a: None), \
                mock.patch.object(wa, "_ext_busy", lambda self_: False), \
                mock.patch.object(wa, "_diag_download",
                                  lambda self_, p: downloads.append(p["name"]) or {"ok": True, "text": "已开始下载"}), \
                mock.patch.object(wa, "_api_ext_install",
                                  lambda self_, ids, iid=None, nodes=None, extra=None:
                                  installs.append((list(ids), [n["name"] for n in nodes or []])) or {"ok": True}):
            job = wa._diag_new_job(self.api)
            wa._diag_fixall_run(self.api, job, {"type": "inst", "iid": "cf"}, "")
        done = [k for s, t, k in self.api.events if (s, t) == ("diag", "fixall_done")]
        self.assertEqual(len(done), 1)
        self.assertTrue(done[0]["ok"], done[0])
        self.assertEqual(sorted(downloads), ["a.safetensors", "b.safetensors"])   # 地址现成的 + AI 找到的一起下
        self.assertEqual(installs, [([], ["Foo", "Bar"])])                        # 两个节点合成一个安装任务
        self.assertEqual(len(ai_notes), 1)
        self.assertIn("b.safetensors", ai_notes[0][0])
        self.assertTrue(ai_notes[0][1])                                           # 要 AI 给能执行的动作
        self.assertEqual((self.runner.stops, self.runner.starts), (1, 1))         # 装节点前停、装完启动
        self.assertNotIn(job, self.api._diag_jobs)

    def _stopped_then_cancelled(self, ext_busy_after):
        """实例在跑 → 一键修复为了装节点把它停掉 → 装的时候用户点了「停止」（还有一个模型在下）"""
        self.runner = FakeRunner(self.api, "cf", [])
        self.runner._running, self.runner.state = True, "ready"
        mdir = os.path.join(self.comfy, "models", "loras")
        models = [{"name": "a.safetensors", "folder": "loras", "node_type": "LoraLoader", "input": "lora_name",
                   "dirs": [mdir], "url": "https://huggingface.co/x/y/resolve/main/a.safetensors"}]
        self.runner.log("[启动器] 正在启动 ComfyUI ...\n" + bridge_line(nodes=["FooNode"], models=models))
        job = wa._diag_new_job(self.api)
        busy = []

        def install(self_, ids, iid=None, nodes=None, extra=None):
            self.api._diag_jobs[job].set()          # 用户点了停止
            busy.append(ext_busy_after)
            return {"ok": True}

        def download(self_, p):
            self_._diag_dl = {"a.safetensors"}      # 模型还在下：流程会停在「等模型下完」
            return {"ok": True, "text": "已开始下载"}

        try:
            with mock.patch.object(dg, "load_model_list", lambda *a, **k: []), \
                    mock.patch.object(dg, "load_node_map", lambda *a, **k: {"x": 1}), \
                    mock.patch.object(dg, "resolve_node_types",
                                      lambda types, nm: ([("https://github.com/a/foo", "Foo", ["FooNode"])], [])), \
                    mock.patch.object(wa, "_diag_civitai_exact", lambda *a: None), \
                    mock.patch.object(wa, "_ext_busy", lambda self_: bool(busy and busy[-1])), \
                    mock.patch.object(wa, "_diag_download", download), \
                    mock.patch.object(wa, "_api_ext_install", install):
                wa._diag_fixall_run(self.api, job, {"type": "inst", "iid": "cf"}, "")
        finally:
            self.api._diag_dl = set()
        return [k for s, t, k in self.api.events if (s, t) == ("diag", "fixall_done")][0]

    def test_cancel_after_stop_restarts_instance(self):
        # 以前：停掉实例之后点「停止」，实例就一直停着（stops=1、starts=0）
        done = self._stopped_then_cancelled(ext_busy_after=False)
        self.assertFalse(done["ok"])
        self.assertIn("已停止", done["text"])
        self.assertEqual((self.runner.stops, self.runner.starts), (1, 1))

    def test_cancel_while_plugin_task_running_tells_user(self):
        # 后台的插件任务还在装东西：这时启动会抢文件，不启动，但要明说实例停着
        done = self._stopped_then_cancelled(ext_busy_after=True)
        self.assertEqual((self.runner.stops, self.runner.starts), (1, 0))
        self.assertIn("请手动启动", done["text"])

    def test_nothing_to_fix(self):
        self.runner = FakeRunner(self.api, "cf", [])
        self.runner.log("[启动器] 正在启动 ComfyUI ...\n正常\n")
        job = wa._diag_new_job(self.api)
        wa._diag_fixall_run(self.api, job, {"type": "inst", "iid": "cf"}, "")
        done = [k for s, t, k in self.api.events if (s, t) == ("diag", "fixall_done")][0]
        self.assertTrue(done["ok"])
        self.assertEqual(done["text"], "没有问题需要修")


class CompareTest(_Base):
    def test_compare(self):
        img = os.path.join(self.tmp, "a.png")
        _touch(img)
        details = {i: {"ok": True, "id": i, "path": img, "kind": "image", "exists": True, "name": f"{i}.png"}
                   for i in range(1, 7)}
        params = mock.Mock(steps=28, cfg_scale=6.5, scheduler="karras", denoising_strength=None)
        with mock.patch.object(wa, "_api_outputs_detail", lambda self_, oid: dict(details[oid])), \
                mock.patch.object(wa.me, "read_metadata", lambda p, s: mock.Mock(params=params)):
            self.assertFalse(wa._api_outputs_compare(self.api, [1])["ok"])
            r = wa._api_outputs_compare(self.api, [1, 2, 3, 4, 5, 6])
        self.assertTrue(r["ok"])
        self.assertEqual([d["id"] for d in r["items"]], [1, 2, 3, 4])            # 最多 4 张
        self.assertEqual((r["items"][0]["steps"], r["items"][0]["cfg"], r["items"][0]["scheduler"],
                          r["items"][0]["denoise"]), ("28", "6.5", "karras", ""))


if __name__ == "__main__":
    unittest.main()
