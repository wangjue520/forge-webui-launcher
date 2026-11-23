"""
匿名使用计数：每台电脑只算一次用户；升到新版本算一次更新；同版本不重复；失败下次再试；可关闭。
运行：python -m unittest tests.test_usage_ping
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import json
import os
import tempfile
import unittest
from unittest import mock

import usage_ping  # noqa: E402


class _Resp:
    status_code = 200
    content = b"ok\n"


class UsagePingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.marker = os.path.join(self.dir, "WWY", "launcher_usage.json")
        self.hits = []
        self.version = "3.1.030"
        self.online = True

        def fake_get(url, **kw):
            if not self.online:
                raise usage_ping.requests.ConnectionError("down")
            self.hits.append(url)
            return _Resp()

        p1 = mock.patch.object(usage_ping.requests, "get", side_effect=fake_get)
        p2 = mock.patch.object(usage_ping, "_current_version", side_effect=lambda cfg: self.version)
        p3 = mock.patch.object(usage_ping, "_candidates", side_effect=lambda url, cfg: [url])
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def run_ping(self, cfg=None):
        return usage_ping.ping(cfg or {}, marker=self.marker)

    def test_first_run_counts_user_once(self):
        self.assertEqual(self.run_ping(), {"user": True, "update": False})
        self.assertEqual(self.run_ping(), {"user": False, "update": False})
        self.assertEqual(self.hits, [usage_ping.USER_URL])

    def test_new_version_counts_update_once(self):
        self.run_ping()
        self.version = "3.1.031"
        self.assertEqual(self.run_ping(), {"user": False, "update": True})
        self.assertEqual(self.run_ping(), {"user": False, "update": False})
        self.assertEqual(self.hits, [usage_ping.USER_URL, usage_ping.UPDATE_URL])
        with open(self.marker, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], "3.1.031")

    def test_offline_retries_next_time(self):
        self.online = False
        self.assertEqual(self.run_ping(), {"user": False, "update": False})
        with open(self.marker, encoding="utf-8") as f:
            self.assertNotIn("user_counted", json.load(f))
        self.online = True
        self.assertEqual(self.run_ping(), {"user": True, "update": False})

    def test_unwritable_marker_never_counts(self):
        blocker = os.path.join(self.dir, "afile")
        open(blocker, "w").close()
        bad = os.path.join(blocker, "WWY", "launcher_usage.json")   # 父目录是个文件，写不进去
        for _ in range(3):
            usage_ping.ping({}, marker=bad)
        self.assertEqual(self.hits, [])

    def test_two_launchers_at_once_count_once(self):
        os.makedirs(os.path.dirname(self.marker), exist_ok=True)
        open(self.marker + ".lock", "w").close()      # 另一个启动器正在计
        self.assertEqual(self.run_ping(), {"user": False, "update": False})
        old = usage_ping.time.time() - usage_ping.LOCK_STALE - 10
        os.utime(self.marker + ".lock", (old, old))   # 锁过期了（那个进程中途没了）
        self.assertEqual(self.run_ping(), {"user": True, "update": False})
        self.assertFalse(os.path.exists(self.marker + ".lock"))

    def test_switching_git_and_zip_copies_counts_update_once(self):
        self.run_ping()
        for v in ["3.1.033", "3.1.032", "3.1.033", "3.1.032"]:
            self.version = v
            self.run_ping()
        self.assertEqual(self.hits.count(usage_ping.UPDATE_URL), 1)

    def test_read_timeout_does_not_try_next_mirror(self):
        calls = []

        def slow(url, **kw):
            calls.append(url)
            if url.startswith("https://a/"):
                raise usage_ping.requests.ReadTimeout("slow")
            return _Resp()

        cands = lambda url, cfg: ["https://a/" + url, "https://b/" + url]
        with mock.patch.object(usage_ping.requests, "get", side_effect=slow), \
                mock.patch.object(usage_ping, "_candidates", side_effect=cands):
            self.assertEqual(self.run_ping(), {"user": False, "update": False})
        self.assertEqual(len(calls), 1)

    def test_connect_error_tries_next_mirror(self):
        calls = []

        def down(url, **kw):
            calls.append(url)
            if url.startswith("https://a/"):
                raise usage_ping.requests.ConnectTimeout("no route")
            return _Resp()

        cands = lambda url, cfg: ["https://a/" + url, "https://b/" + url]
        with mock.patch.object(usage_ping.requests, "get", side_effect=down), \
                mock.patch.object(usage_ping, "_candidates", side_effect=cands):
            self.assertEqual(self.run_ping(), {"user": True, "update": False})
        self.assertEqual(len(calls), 2)

    def test_opt_out(self):
        self.assertEqual(self.run_ping({"usage_ping": False}), {"user": False, "update": False})
        self.assertEqual(self.hits, [])


if __name__ == "__main__":
    unittest.main()
