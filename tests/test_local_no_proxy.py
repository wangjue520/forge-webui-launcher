"""
起 WebUI / ComfyUI 的环境里 NO_PROXY 一定带本机地址（proxy_manager.with_local_bypass）回归测试。

背景：用户开着 Clash / v2rayN 的「系统代理」、启动器里没开代理时，启动器以前不设 NO_PROXY。
WebUI 的 gradio 用 httpx 自检 http://127.0.0.1:7860，httpx 在环境变量里没找到代理就读注册表的系统代理，
自检被转给代理，报 "When localhost is not accessible, a shareable link must be created" 起不来。
系统里设了 HTTP_PROXY 的情况一样。运行：python -m unittest tests.test_local_no_proxy
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import config_manager as cm  # noqa: E402
import proxy_manager as pm  # noqa: E402

LOCAL = ["localhost", "127.0.0.1", "::1", "0.0.0.0"]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _dead_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WithLocalBypassTest(unittest.TestCase):
    def test_adds_local_when_missing(self):
        env = pm.with_local_bypass({"PATH": "x"})
        self.assertEqual(env["NO_PROXY"].split(","), LOCAL)

    def test_keeps_user_entries_and_dedupes(self):
        env = pm.with_local_bypass({"NO_PROXY": "corp.lan, 127.0.0.1", "no_proxy": "corp.lan"})
        self.assertEqual(env["NO_PROXY"].split(","), ["corp.lan", "127.0.0.1", "localhost", "::1", "0.0.0.0"])
        names = [k for k in env if k.upper() == "NO_PROXY"]
        self.assertEqual(names, ["NO_PROXY"] if os.name == "nt" else ["NO_PROXY", "no_proxy"])


class LaunchEnvTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cfg = dict(cm.DEFAULT_CONFIG, custom_python_path=sys.executable, proxy_enabled=False)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_webui_and_comfy_env_have_local_bypass_without_proxy(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("NO_PROXY", "no_proxy"):
                os.environ.pop(k, None)
            for build in (cm.build_subprocess_env, cm.build_comfy_env):
                env = build(self.cfg, self.root)
                for h in LOCAL:
                    self.assertIn(h, env["NO_PROXY"].split(","), build.__name__)

    def test_local_request_skips_dead_proxy(self):
        """系统里有个连不上的代理时，子进程访问本机照样通（等同 gradio 的启动自检）"""
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        dead = f"http://127.0.0.1:{_dead_port()}"
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        code = ("import sys, requests\n"
                "r = requests.get(sys.argv[1], timeout=5)\n"
                "print(r.status_code)\n")
        try:
            with mock.patch.dict(os.environ, {"HTTP_PROXY": dead, "HTTPS_PROXY": dead}, clear=False):
                for k in ("NO_PROXY", "no_proxy"):
                    os.environ.pop(k, None)
                env = cm.build_subprocess_env(self.cfg, self.root)
                bare = {k: v for k, v in env.items() if k.upper() != "NO_PROXY"}
            # 没有 NO_PROXY 时确实会被转给代理（不然这个测试测不出东西）
            r = subprocess.run([sys.executable, "-c", code, url], env=bare, capture_output=True, timeout=30)
            self.assertNotEqual(r.returncode, 0)
            r = subprocess.run([sys.executable, "-c", code, url], env=env, capture_output=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
            self.assertEqual(r.stdout.strip(), b"200")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
