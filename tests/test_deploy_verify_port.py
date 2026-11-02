"""
部署最后「首次运行验证」用的端口：必须是空闲端口，并通过 --port 指定给这次运行。
以前不指定端口、探测 7860–7869，用户自己开着的 WebUI 占着 7860 时会被误判成验证通过，依赖没装完就收尾。
运行：python -m unittest tests.test_deploy_verify_port
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import http.server
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest import mock

import webview_api as wa  # noqa: E402


class VerifyPortTest(unittest.TestCase):
    def test_free_config_port_is_used(self):
        self.assertEqual(wa._deploy_verify_port("7870", in_use=lambda p: False), 7870)

    def test_skip_ports_in_use(self):
        busy = {7860, 7861}
        self.assertEqual(wa._deploy_verify_port("", in_use=lambda p: p in busy), 7862)

    def test_busy_config_port_falls_back(self):
        busy = {7860, 7861}
        self.assertEqual(wa._deploy_verify_port("7860", in_use=lambda p: p in busy), 7862)

    def test_with_port_arg_replaces(self):
        self.assertEqual(wa._with_port_arg("--api --port 7860 --xformers", 7862), "--api --xformers --port 7862")
        self.assertEqual(wa._with_port_arg("--port=7860 --api", 7863), "--api --port 7863")
        self.assertEqual(wa._with_port_arg("", 7860), "--port 7860")


# 假的 webui.bat：按 COMMANDLINE_ARGS 里的 --port 起一个 HTTP 服务，先不吐「Running on local URL」，
# 逼验证走端口探测这条路；被验证通过后会被杀掉
FAKE_SERVER = r"""
import http.server, os, socketserver, sys, time
args = os.environ.get("COMMANDLINE_ARGS", "").split()
port = int(args[args.index("--port") + 1])
print("installing requirements ...", flush=True)
time.sleep(1.5)
socketserver.TCPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler).serve_forever()
"""


@unittest.skipUnless(_os.name == "nt", "webui.bat 只在 Windows 上")
class VerifyRunTest(unittest.TestCase):
    """端到端：别的 Web 服务占着一个端口时，验证只认自己拉起的那个"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(_os.path.join(self.tmp, "fake_webui.py"), "w", encoding="utf-8") as f:
            f.write(FAKE_SERVER)
        with open(_os.path.join(self.tmp, "webui.bat"), "w", encoding="utf-8", newline="\r\n") as f:
            f.write(f'@echo off\n"{sys.executable}" "%~dp0fake_webui.py"\n')
        # 占一个端口，模拟用户自己开着的 WebUI
        self.other = socketserver.TCPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        self.other_port = self.other.server_address[1]
        threading.Thread(target=self.other.serve_forever, daemon=True).start()

    def tearDown(self):
        self.other.shutdown()
        self.other.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ignores_other_server(self):
        api = mock.MagicMock()
        api._deploy_cfg = {"port": str(self.other_port)}   # 配置的端口恰好被占
        api._deploy_cancel = threading.Event()
        logs = []
        env = dict(_os.environ, COMMANDLINE_ARGS="--api")
        for k in [k for k in env if k.upper() == "NODEFAULTCURRENTDIRECTORYINEXEPATH"]:
            env.pop(k)   # 有这个变量时 cmd 不在当前目录找 webui.bat（部分终端 / 工具会设置）
        ready, _ = wa._deploy_run_webui_until_ready(api, self.tmp, logs.append, env, timeout=60)
        text = "".join(logs)
        self.assertTrue(ready, text[-2000:])
        used = int(text.split("首次运行验证使用端口 ")[1].split()[0])
        self.assertNotEqual(used, self.other_port)
        self.assertIn(f"http://127.0.0.1:{used}，首次运行验证通过", text)


if __name__ == "__main__":
    unittest.main()
