# -*- coding: utf-8 -*-
"""net_guard：代理（梯子）端口失效时自动换路"""
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import requests  # noqa: E402

import net_guard  # noqa: E402


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class _NotAProxy(BaseHTTPRequestHandler):
    """占着端口的普通网页服务：什么都回 404（就是截图里那种情况）"""
    def _404(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()
    do_GET = do_CONNECT = _404

    def log_message(self, *a):
        pass


class _GoodProxy(BaseHTTPRequestHandler):
    """能用的代理：CONNECT 回 200；普通 http 请求直接替目标站回个 ok"""
    def do_CONNECT(self):
        self.send_response(200, "Connection established")
        self.end_headers()
        self.close_connection = True

    def do_GET(self):
        body = b"via-proxy " + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProbeTest(unittest.TestCase):
    def test_refused(self):
        ok, why = net_guard.probe(f"127.0.0.1:{_free_port()}", "civitai.com")
        self.assertFalse(ok)
        self.assertIn("没有程序在监听", why)

    def test_not_a_proxy(self):
        srv = _serve(_NotAProxy)
        try:
            ok, why = net_guard.probe(f"http://127.0.0.1:{srv.server_port}", "civitai.com")
        finally:
            srv.shutdown()
        self.assertFalse(ok)
        self.assertIn("404", why)

    def test_good(self):
        srv = _serve(_GoodProxy)
        try:
            ok, why = net_guard.probe(f"127.0.0.1:{srv.server_port}", "civitai.com")
        finally:
            srv.shutdown()
        self.assertTrue(ok, why)


class GuardTest(unittest.TestCase):
    def setUp(self):
        net_guard.install()
        net_guard.reset()
        self.good = _serve(_GoodProxy)
        self.bad_port = _free_port()
        self.bad = f"http://127.0.0.1:{self.bad_port}"
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = self.bad
        self.patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(net_guard, "_registry_proxy", lambda: ""),
            mock.patch.object(net_guard, "_launcher_proxy", lambda: ""),
            mock.patch.object(net_guard, "_ladder_ports", lambda: []),
            mock.patch.object(net_guard, "_listening_ports", lambda: None),
            mock.patch.object(net_guard, "KNOWN_PORTS",
                              [(_free_port(), "关着的"), (self.good.server_port, "测试梯子")]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.good.shutdown()
        net_guard.reset()

    def test_falls_back_to_running_ladder(self):
        with mock.patch.object(net_guard, "_log") as log:
            r = requests.get("http://civitai.example/api/v1/x", timeout=5)
            self.assertEqual(r.text, "via-proxy http://civitai.example/api/v1/x")
            # 第二次直接走记住的路，不再探测、不再重复写日志
            with mock.patch.object(net_guard, "probe", side_effect=AssertionError("不该再探测")):
                r = requests.get("http://civitai.example/api/v1/y", timeout=5)
            self.assertIn("/api/v1/y", r.text)
        log.assert_called_once()
        self.assertIn(f"127.0.0.1:{self.good.server_port}", log.call_args[0][0])
        self.assertIn("环境变量", log.call_args[0][0])

    def test_friendly_error_when_nothing_works(self):
        self.good.shutdown()
        with mock.patch.object(net_guard, "_send",
                               side_effect=requests.exceptions.ConnectionError("直连也不通")):
            with self.assertRaises(requests.exceptions.ProxyError) as cm:
                requests.get("http://civitai.example/api", timeout=5)
        msg = str(cm.exception)
        self.assertIn(self.bad, msg)
        self.assertIn("没有程序在监听", msg)
        self.assertIn("Clash", msg)

    def test_explicit_proxies_untouched(self):
        with self.assertRaises(requests.exceptions.ProxyError):
            requests.get("http://civitai.example/", proxies={"http": self.bad}, timeout=5)

    def test_direct_route_survives_redirect(self):
        """直连换路要经得起重定向：requests 跟随重定向时会重新读环境变量里的坏代理"""
        hits = []

        class Redir(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path == "/a":
                    self.send_response(302)
                    self.send_header("Location", "/b")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        srv = _serve(Redir)
        try:
            r = net_guard._send(requests.Session(), "GET", f"http://127.0.0.1:{srv.server_port}/a",
                                net_guard.DIRECT, {"timeout": 5})
        finally:
            srv.shutdown()
        self.assertEqual(r.text, "ok")
        self.assertEqual(hits, ["/a", "/b"])


class SourceTest(unittest.TestCase):
    def test_registry_style_value(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(net_guard, "_launcher_proxy", lambda: ""):
            src, where = net_guard._source("http://127.0.0.1:7897")
        self.assertIn("Windows 系统代理", src)
        self.assertIn("网络和 Internet", where)


if __name__ == "__main__":
    unittest.main()
