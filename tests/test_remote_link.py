# -*- coding: utf-8 -*-
"""remote_link：局域网 / 广域网联动的密钥、签名、主机服务、端口隧道、上传下载"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import remote_link as rl  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Echo(BaseHTTPRequestHandler):
    """假装是 WebUI：回请求路径和 Host 头"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_GET(self):
        body = json.dumps({"path": self.path, "host": self.headers.get("Host")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class KeyTest(unittest.TestCase):
    def test_new_key_shape(self):
        k = rl.new_key()
        self.assertEqual(len(k), rl.KEY_LEN)
        self.assertTrue(all(c in rl.KEY_ALPHABET for c in k))
        self.assertEqual(rl.normalize_key(rl.format_key(k).lower()), k)
        self.assertEqual(rl.normalize_key(" " + rl.format_key(k).replace("-", " ") + " "), k)

    def test_bad_keys(self):
        self.assertIsNone(rl.normalize_key("ABCD"))
        self.assertIsNone(rl.normalize_key("O" * 24))          # 字母表里没有 O
        self.assertIsNone(rl.normalize_key(""))

    def test_auth(self):
        k = rl.new_key()
        nonces = rl._NonceCache()
        h = rl.make_auth(k, "POST", "/rpc", b"{}")
        self.assertEqual(rl.check_auth(k, h, "POST", "/rpc", rl.body_digest(b"{}"), nonces), (True, ""))
        # 原样重放
        self.assertEqual(rl.check_auth(k, h, "POST", "/rpc", rl.body_digest(b"{}"), nonces)[1], "replay")
        # 换了请求体 / 路径 / 密钥
        h = rl.make_auth(k, "POST", "/rpc", b"{}")
        self.assertEqual(rl.check_auth(k, h, "POST", "/rpc", rl.body_digest(b"{1}"), nonces)[1], "auth")
        h = rl.make_auth(k, "POST", "/rpc", b"{}")
        self.assertEqual(rl.check_auth(k, h, "POST", "/x", rl.body_digest(b"{}"), nonces)[1], "auth")
        h = rl.make_auth(rl.new_key(), "POST", "/rpc", b"{}")
        self.assertEqual(rl.check_auth(k, h, "POST", "/rpc", rl.body_digest(b"{}"), nonces)[1], "auth")
        # 时钟差太多
        h = rl.make_auth(k, "POST", "/rpc", b"{}", offset=-3600)
        self.assertEqual(rl.check_auth(k, h, "POST", "/rpc", rl.body_digest(b"{}"), nonces)[1], "clock")
        self.assertEqual(rl.check_auth(k, "garbage", "POST", "/rpc", "", nonces)[1], "auth")


class RewriteTest(unittest.TestCase):
    def test_ports_and_rewrite(self):
        obj = {"url": "http://127.0.0.1:7860/", "list": ["ws://localhost:8188/ws", "https://civitai.com/x"],
               "n": 3, "base": "http://0.0.0.0:7861"}
        self.assertEqual(rl.local_url_ports(obj), {7860, 8188, 7861})
        out = rl.rewrite_local_urls(obj, lambda p: p + 10000)
        self.assertEqual(out["url"], "http://127.0.0.1:17860/")
        self.assertEqual(out["list"], ["ws://127.0.0.1:18188/ws", "https://civitai.com/x"])
        self.assertEqual(out["base"], "http://127.0.0.1:17861")
        self.assertEqual(out["n"], 3)

    def test_parse_addr(self):
        self.assertEqual(rl.parse_addr("192.168.1.5"), ("192.168.1.5", rl.DEFAULT_PORT))
        self.assertEqual(rl.parse_addr("192.168.1.5:8000"), ("192.168.1.5", 8000))
        self.assertEqual(rl.parse_addr("http://a.example.com:9000/"), ("a.example.com", 9000))
        self.assertIsNone(rl.parse_addr(""))


class HostClientTest(unittest.TestCase):
    """真起一个主机服务，用 Peer 走一遍：接口、事件、隧道、下载、上传"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.inbox = mock.patch.object(rl, "INBOX_DIR", os.path.join(cls.tmp, "inbox"))
        cls.inbox.start()
        cls.files_dir = os.path.join(cls.tmp, "models")
        os.makedirs(cls.files_dir)
        cls.model = os.path.join(cls.files_dir, "a.safetensors")
        with open(cls.model, "wb") as f:
            f.write(os.urandom(3 * 1024 * 1024 + 17))
        cls.echo = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        threading.Thread(target=cls.echo.serve_forever, daemon=True).start()
        cls.echo_port = cls.echo.server_address[1]

        def dispatch(method, args):
            if method == "link_hello":
                return {"name": "host", "version": "t"}
            if method == "instances_list":
                return {"url": f"http://127.0.0.1:{cls.echo_port}/"}
            if method == "boom":
                raise RuntimeError("坏了")
            if method == "nope":
                raise PermissionError("不让调")
            return {"method": method, "args": args}

        def resolver(q):
            p = q.get("path", "")
            if not p.startswith(cls.files_dir):
                raise PermissionError("只能下载模型目录里的文件")
            return p

        cls.key = rl.new_key()
        cls.host = rl.LinkHost(dispatch, resolver)
        cls.port = _free_port()
        assert cls.host.start(cls.key, cls.port), cls.host.error

    @classmethod
    def tearDownClass(cls):
        cls.host.stop()
        cls.echo.shutdown()
        cls.inbox.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def peer(self, key=None):
        return rl.Peer("127.0.0.1", self.port, key or self.key)

    def test_rpc(self):
        p = self.peer()
        self.assertEqual(p.hello()["name"], "host")
        self.assertEqual(p.rpc("models_list", [1, "x"]), {"method": "models_list", "args": [1, "x"]})
        with self.assertRaisesRegex(rl.LinkError, "坏了"):
            p.rpc("boom")
        with self.assertRaisesRegex(rl.LinkError, "不让调"):
            p.rpc("nope")

    def test_wrong_key(self):
        with self.assertRaisesRegex(rl.LinkError, "密钥不对"):
            self.peer(rl.new_key()).hello()

    def test_clock_skew_is_corrected(self):
        p = self.peer()
        p.offset = -2000          # 本机时钟慢了半个多小时：第一次被拒，按主机时间校准后重试成功
        self.assertEqual(p.hello()["name"], "host")
        self.assertLess(abs(p.offset), 5)

    def test_events(self):
        p = self.peer()
        r = p.events(0, "", wait=1)          # 第一次：拿到 epoch，不补历史
        self.host.publish({"scope": "launch", "type": "log", "text": "hi"})
        r2 = p.events(r["seq"], r["epoch"], wait=2)
        self.assertEqual([e["text"] for e in r2["events"]], ["hi"])
        t0 = time.time()
        r3 = p.events(r2["seq"], r2["epoch"], wait=1)   # 没有新事件：长轮询等到超时
        self.assertEqual(r3["events"], [])
        self.assertGreater(time.time() - t0, 0.8)

    def test_tunnel_only_to_noted_ports(self):
        cli = rl.LinkClient(lambda e: None, lambda *a: None)
        cli.enter(self.peer())
        try:
            # 主机还没在结果里提到过这个端口：拒绝转发
            local = cli.forward_port(self.echo_port)
            with self.assertRaises(Exception):
                urllib.request.urlopen(f"http://127.0.0.1:{local}/x", timeout=5).read()
            # instances_list 的结果里带了 echo 的地址，换成本机转发端口后能直接访问
            res = cli.call("instances_list", [])
            url = res["url"]
            self.assertNotIn(str(self.echo_port), url)
            body = json.loads(urllib.request.urlopen(url + "hello?a=1", timeout=5).read())
            self.assertEqual(body["path"], "/hello?a=1")
            # 同一个转发端口连发几次（每个连接一条隧道）
            for i in range(5):
                json.loads(urllib.request.urlopen(url + f"n{i}", timeout=5).read())
        finally:
            cli.leave()

    def test_download_and_resume(self):
        p = self.peer()
        dest = os.path.join(self.tmp, "dl.safetensors")
        with open(dest + ".part", "wb") as f:       # 上次下到一半
            with open(self.model, "rb") as src:
                f.write(src.read(1000))
        seen = []
        p.download({"path": self.model}, dest, progress=lambda d, t: seen.append((d, t)))
        with open(dest, "rb") as a, open(self.model, "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(seen[-1][0], seen[-1][1])
        with self.assertRaisesRegex(rl.LinkError, "只能下载"):
            p.download({"path": os.path.join(self.tmp, "x")}, dest + "2")

    def test_upload(self):
        p = self.peer()
        got = p.upload(self.model, "batch1")
        self.assertTrue(got.startswith(rl.INBOX_DIR))
        with open(got, "rb") as a, open(self.model, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_discovery(self):
        if not self.host._udp:
            self.skipTest("UDP 7898 被占用")
        found = rl.discover(self.key, timeout=1.5)
        self.assertTrue(any(port == self.port for _ip, port, _n in found), found)
        self.assertEqual(rl.discover(rl.new_key(), timeout=0.8), [])


if __name__ == "__main__":
    unittest.main()
