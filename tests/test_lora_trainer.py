"""
LoRA 训练器一键部署 / 一键启动（app/lora_trainer.py）的回归测试。

部署：用假的 LauncherApi（不下载、不跑 git / pip），检查步骤顺序、目录检测、写入训练器配置、取消。
启动：用一个假的训练器（app/main.py 起个小 HTTP 服务，打印和真训练器一样的就绪行），检查就绪识别和停止。
运行：python -m unittest tests.test_lora_trainer
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

import lora_trainer as lt  # noqa: E402


def _touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def make_trainer(root, main_py=""):
    _touch(os.path.join(root, "app", "main.py"), main_py)
    _touch(os.path.join(root, "app", "envsetup.py"))
    _touch(os.path.join(root, "scripts", "setup_env.py"))
    _touch(os.path.join(root, "web", "index.html"), "<title>WWY LoRA 训练器</title>")
    _touch(os.path.join(root, "version.json"), json.dumps({"version": "0.1.0"}))


def link_python(root):
    """让 python_of(root) 找到当前解释器（Windows 上是 python/python.exe，其它系统 python/bin/python）"""
    if os.name == "nt":
        dst = os.path.join(root, "python", "python.exe")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(sys.executable, dst)
    else:
        dst = os.path.join(root, "python", "bin", "python")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.symlink(sys.executable, dst)


class FakeApi:
    """deploy_flow 用到的 LauncherApi 属性"""

    def __init__(self, cfg=None, setup_rc=0, cancel_at=None):
        self._deploy_cfg = dict(cfg or {}, mirror_mode=(cfg or {}).get("mirror_mode", "never"))
        self._deploy_cancel = threading.Event()
        self._deploy_running = True
        self.events, self.cmds = [], []
        self.setup_rc, self.cancel_at = setup_rc, cancel_at
        self.opened = []

    def _emit(self, scope, type_, **data):
        self.events.append((scope, type_, data))

    def _deploy_portable_env(self, target, branch, need_python, need_git, log, progress):
        self.cmds.append(("portable", branch, need_python, need_git))
        if need_python:
            link_python(target)
        if need_git:
            _touch(os.path.join(target, "git", "cmd", "git.exe"))

    def _deploy_run_cmd(self, program, args, cwd, log, env=None, timeout=None):
        self.cmds.append((os.path.basename(program), list(args)))
        if self.cancel_at and self.cancel_at in " ".join(args):
            self._deploy_cancel.set()
            return -9
        if "checkout" in args:   # 「检出」完源码就是一个训练器目录
            make_trainer(args[1])
        if any(a.endswith("setup_env.py") for a in args):
            log("[ 50%] 安装 PyTorch\n")
            if self.setup_rc == 0:
                _touch(os.path.join(cwd, "env", "Scripts" if os.name == "nt" else "bin",
                                    "python.exe" if os.name == "nt" else "python"))
                for n in ("sd-scripts", "musubi-tuner"):
                    os.makedirs(os.path.join(cwd, "engines", n), exist_ok=True)
            return self.setup_rc
        return 0

    def open_url(self, url):
        self.opened.append(url)
        return {"ok": True}


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_and_missing_dirs_are_deployable(self):
        self.assertTrue(lt.detect(os.path.join(self.tmp, "new"))["deployable"])
        os.makedirs(os.path.join(self.tmp, "half", "python"))   # 上次部署到一半留下的
        self.assertTrue(lt.detect(os.path.join(self.tmp, "half"))["deployable"])

    def test_dir_with_other_files_is_refused(self):
        _touch(os.path.join(self.tmp, "docs", "a.txt"))
        d = lt.detect(os.path.join(self.tmp, "docs"))
        self.assertFalse(d["deployable"])
        self.assertTrue(d["reason"])

    def test_installed_trainer(self):
        root = os.path.join(self.tmp, "t")
        make_trainer(root)
        d = lt.detect(root)
        self.assertTrue(d["installed"])
        self.assertFalse(d["ready"])   # 还没有 Python / 训练环境
        self.assertEqual(d["version"], "0.1.0")

    def test_default_root_prefers_existing_install(self):
        launcher = os.path.join(self.tmp, "WWY", "forge-webui-launcher")
        os.makedirs(launcher)
        self.assertEqual(lt.default_root(launcher, {}), os.path.join(self.tmp, "WWY", lt.DIR_NAME))
        existing = os.path.join(self.tmp, lt.DIR_NAME)   # 装在上一级文件夹旁边
        make_trainer(existing)
        self.assertEqual(lt.default_root(launcher, {}), existing)

    def test_recommend_torch(self):
        self.assertEqual(lt.recommend_torch(["NVIDIA GeForce GTX 1080 Ti"]), "cu126")
        self.assertEqual(lt.recommend_torch(["NVIDIA GeForce RTX 4090"]), "cu128")
        self.assertEqual(lt.recommend_torch([]), "cu128")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self):
        with open(os.path.join(self.tmp, "data", "settings.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_network_and_proxy_follow_launcher(self):
        lt.write_settings(self.tmp, {"mirror_mode": "always", "proxy_enabled": True, "proxy_url": "127.0.0.1:7890"}, lambda t: None)
        self.assertEqual(self.read(), {"network": "cn", "proxy_url": "http://127.0.0.1:7890"})

    def test_auto_mode_writes_nothing(self):
        lt.write_settings(self.tmp, {"mirror_mode": "auto"}, lambda t: None)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "data", "settings.json")))

    def test_user_choices_kept(self):
        _touch(os.path.join(self.tmp, "data", "settings.json"), json.dumps({"network": "global", "proxy_url": "http://x:1"}))
        lt.write_settings(self.tmp, {"mirror_mode": "always", "proxy_enabled": True, "proxy_url": "http://y:2"}, lambda t: None)
        self.assertEqual(self.read(), {"network": "global", "proxy_url": "http://x:1"})


class DeployFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, lt.DIR_NAME)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def done(self, api):
        return [d for s, t, d in api.events if t == "deploy_done"][-1]

    def test_fresh_deploy(self):
        api = FakeApi({"mirror_mode": "always"})
        lt.deploy_flow(api, self.root, "cu128")
        self.assertTrue(self.done(api)["ok"])
        self.assertFalse(api._deploy_running)
        self.assertEqual(api.cmds[0][0], "portable")
        self.assertEqual(api.cmds[0][1], "lora_trainer")
        flat = [" ".join(c[1]) for c in api.cmds[1:]]
        self.assertTrue(any("fetch" in c for c in flat))
        self.assertTrue(any(c.endswith(lt.REPO + ".git") for c in flat))   # 远程地址改回 GitHub 原地址
        setup = next(c for c in flat if "setup_env.py" in c)
        self.assertIn("--torch cu128", setup)
        self.assertTrue(lt.detect(self.root)["ready"])
        with open(os.path.join(self.root, "data", "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["network"], "cn")
        steps = [d["pct"] for s, t, d in api.events if t == "step"]
        self.assertTrue(any(15 < p < 99 for p in steps))   # setup_env 的「[ 50%]」换算成了部署进度

    def test_redeploy_skips_source_and_keeps_torch(self):
        make_trainer(self.root)
        link_python(self.root)
        api = FakeApi()
        lt.deploy_flow(api, self.root, "")
        self.assertTrue(self.done(api)["ok"])
        flat = [" ".join(c[1]) for c in api.cmds if c[0] != "portable"]
        self.assertFalse(any("fetch" in c for c in flat))
        self.assertNotIn("--torch", next(c for c in flat if "setup_env.py" in c))

    def test_setup_failure_reported(self):
        api = FakeApi(setup_rc=1)
        lt.deploy_flow(api, self.root, "cu128")
        self.assertFalse(self.done(api)["ok"])
        self.assertTrue(any(t == "error" for s, t, d in api.events))

    def test_cancel(self):
        api = FakeApi(cancel_at="fetch")
        lt.deploy_flow(api, self.root, "cu128")
        self.assertFalse(self.done(api)["ok"])
        self.assertFalse(any(t == "error" for s, t, d in api.events))   # 取消不算出错
        self.assertFalse(any("setup_env.py" in " ".join(c[1]) for c in api.cmds if c[0] != "portable"))


FAKE_MAIN = r'''
import http.server, sys, threading, time
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write("<title>WWY LoRA 训练器</title>".encode())
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
sys.stdout.reconfigure(encoding="utf-8")
print("=" * 20); print(f"  LoRA 训练器已启动：http://127.0.0.1:{port}/", flush=True)
while True: time.sleep(1)
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "t")
        make_trainer(self.root, FAKE_MAIN)
        link_python(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, fn, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.1)
        return False

    def test_start_ready_stop(self):
        api = FakeApi()
        r = lt.TrainerRunner(api)
        port = free_port()
        self.assertTrue(r.start(self.root, port, True)["ok"])
        self.assertTrue(self.wait(lambda: r.state == "ready"))
        url = f"http://127.0.0.1:{port}/"
        self.assertEqual(r.status()["url"], url)
        self.assertTrue(self.wait(lambda: api.opened == [url]))   # 就绪后打开一次浏览器
        self.assertTrue(r.managed())
        self.assertTrue(any("LoRA 训练器已启动" in d.get("text", "") for s, t, d in api.events if t == "log"))
        self.assertFalse(r.start(self.root, port, True)["ok"])   # 已经在跑
        r.stop()
        self.assertTrue(self.wait(lambda: not r.managed()))
        self.assertEqual(r.status()["state"], "stopped")
        self.assertFalse(lt.probe(url, 0.5))

    def test_not_deployed(self):
        r = lt.TrainerRunner(FakeApi())
        res = r.start(os.path.join(self.tmp, "nothing"), free_port(), False)
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("need_deploy"))


if __name__ == "__main__":
    unittest.main()
