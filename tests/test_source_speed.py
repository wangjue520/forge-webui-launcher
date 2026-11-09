"""
部署前测速选源（app/source_speed.py、mirror_manager 的排序函数、webview_api._deploy_speedtest、
scripts/speedtest_sources.ps1、启动WWY启动器.bat 的 :speedtest）的回归测试。

核心：
  - 有新鲜的测速结果时，GitHub / PyPI / torch 都按实测速度从快到慢选源，连不上的放最后；
    直连 / 官方源最快就不走镜像；测速过期或没测过时行为和以前一样（按地区）。
  - 所有源都很慢：先提示开 / 关梯子后重测；还慢问「预计部署时间过长，是否继续部署？（Y/N）」，
    选 Y 照常部署，选 N 取消——不直接报错。
用本机 HTTP 服务模拟下载源，不访问外网。
运行：python -m unittest tests.test_source_speed
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import http.server
import os
import re
import threading
import time
import unittest
from unittest import mock

import mirror_manager as mm  # noqa: E402
import source_speed as ss  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS1 = os.path.join(ROOT, "scripts", "speedtest_sources.ps1")
BAT = os.path.join(ROOT, "启动WWY启动器.bat")


def _rank(**groups):
    out = {"time": time.time()}
    for g, items in groups.items():
        out[g] = [{"id": i, "name": i or "direct", "bps": b, "err": ""} for i, b in items]
    return out


class RankOrderTest(unittest.TestCase):
    def test_no_rank_keeps_old_behavior(self):
        cfg = {"mirror_mode": "auto", "mirror_detected": "cn", "github_mirror_detected": "blocked"}
        self.assertEqual(mm.pypi_argsets(cfg), [mm.pip_index_args(True, i) for i in range(len(mm.PYPI_MIRRORS))])
        url = "https://github.com/a/b"
        self.assertEqual(mm.github_candidates(url, cfg),
                         [f"{p}/{url}" for p in mm.GITHUB_PROXIES] + [url])
        cfg = {"mirror_mode": "auto", "mirror_detected": "global", "github_mirror_detected": "ok"}
        self.assertEqual(mm.pypi_argsets(cfg), [])
        self.assertEqual(mm.github_candidates(url, cfg), [url])
        self.assertEqual(mm.torch_bases(cfg), [mm.PYTORCH_OFFICIAL_WHL])

    def test_pypi_follows_speed(self):
        tencent = mm.PYPI_MIRRORS[2][1]
        aliyun = mm.PYPI_MIRRORS[1][1]
        cfg = {"mirror_mode": "auto", "mirror_detected": "global",
               "source_rank": _rank(pypi=[(tencent, 5e6), ("", 1e6), (aliyun, 2e6), (mm.PYPI_MIRRORS[0][1], 0)])}
        self.assertTrue(mm.resolve_mode(cfg))          # 测速说镜像最快，盖过老的地区检测
        sets = mm.pypi_argsets(cfg)
        self.assertEqual(sets[0], ["-i", tencent])
        self.assertEqual(sets[1], ["-i", aliyun])
        self.assertEqual(sets[-1], ["-i", mm.PYPI_MIRRORS[0][1]])   # 连不上的排最后
        self.assertEqual(mm.fastest_pypi_index(cfg), tencent)
        self.assertEqual(mm.pip_env_overrides(True, cfg)["PIP_INDEX_URL"], tencent)

    def test_official_fastest_means_no_mirror(self):
        cfg = {"mirror_mode": "auto", "mirror_detected": "cn",
               "source_rank": _rank(pypi=[("", 8e6), (mm.PYPI_MIRRORS[0][1], 5e5)])}
        self.assertFalse(mm.resolve_mode(cfg))
        self.assertEqual(mm.pypi_argsets(cfg), [])

    def test_github_direct_ranked_with_proxies(self):
        url = "https://github.com/a/b"
        p = mm.GITHUB_PROXIES
        cfg = {"mirror_mode": "auto", "github_mirror_detected": "blocked",
               "source_rank": _rank(github=[(p[4], 6e6), ("", 3e6), (p[1], 4e6), (p[0], 0)])}
        cands = mm.github_candidates(url, cfg)
        self.assertEqual(cands[:3], [f"{p[4]}/{url}", f"{p[1]}/{url}", url])
        self.assertEqual(len(cands), len(p) + 1)
        self.assertTrue(mm.resolve_github_mode(cfg))
        cfg["source_rank"] = _rank(github=[("", 9e6), (p[0], 1e5)])
        self.assertEqual(mm.github_candidates(url, cfg)[0], url)
        self.assertFalse(mm.resolve_github_mode(cfg))
        self.assertEqual(mm.github_candidates("https://example.com/x", cfg), ["https://example.com/x"])

    def test_torch_bases(self):
        sjtu = mm.PYTORCH_MIRROR_BASES[0]
        cfg = {"mirror_mode": "auto",
               "source_rank": _rank(torch=[(mm.PYTORCH_OFFICIAL_WHL, 9e6), (sjtu, 1e6)])}
        self.assertEqual(mm.torch_bases(cfg)[0], mm.PYTORCH_OFFICIAL_WHL)
        cfg["source_rank"] = _rank(torch=[(sjtu, 9e6), (mm.PYTORCH_OFFICIAL_WHL, 1e6)])
        self.assertEqual(mm.torch_bases(cfg), [sjtu, mm.PYTORCH_OFFICIAL_WHL] +
                         [b for b in mm.PYTORCH_MIRROR_BASES if b != sjtu])

    def test_never_and_always(self):
        url = "https://github.com/a/b"
        rank = _rank(github=[("", 9e6)], pypi=[("", 9e6)], torch=[(mm.PYTORCH_OFFICIAL_WHL, 9e6)])
        cfg = {"mirror_mode": "never", "source_rank": rank}
        self.assertEqual(mm.github_candidates(url, cfg), [url])
        self.assertEqual(mm.pypi_argsets(cfg), [])
        self.assertEqual(mm.torch_bases(cfg), [mm.PYTORCH_OFFICIAL_WHL])
        cfg = {"mirror_mode": "always", "source_rank": rank}
        self.assertEqual(mm.github_candidates(url, cfg)[-1], url)     # 总是用镜像：直连只兜底
        self.assertEqual(len(mm.pypi_argsets(cfg)), len(mm.PYPI_MIRRORS))
        self.assertEqual(mm.torch_bases(cfg)[-1], mm.PYTORCH_OFFICIAL_WHL)

    def test_stale_rank_ignored(self):
        rank = _rank(pypi=[(mm.PYPI_MIRRORS[0][1], 9e6)])
        rank["time"] = time.time() - mm.SOURCE_RANK_TTL - 10
        cfg = {"mirror_mode": "auto", "mirror_detected": "global", "source_rank": rank}
        self.assertFalse(mm.resolve_mode(cfg))
        self.assertIsNone(mm.speed_rank(cfg, "pypi"))

    def test_all_dead_rank_ignored(self):
        cfg = {"mirror_mode": "auto", "mirror_detected": "cn",
               "source_rank": _rank(pypi=[("", 0), (mm.PYPI_MIRRORS[0][1], 0)])}
        self.assertIsNone(mm.speed_rank(cfg, "pypi"))
        self.assertTrue(mm.resolve_mode(cfg))

    def test_store_updates_old_caches(self):
        cfg = {}
        ss.store(cfg, _rank(pypi=[("", 9e6)], github=[(mm.GITHUB_PROXIES[0], 9e6), ("", 1)]))
        self.assertEqual(cfg["mirror_detected"], "global")
        self.assertEqual(cfg["github_mirror_detected"], "blocked")


class EstimateTest(unittest.TestCase):
    def test_estimate_and_slow(self):
        fast = _rank(github=[("", 10 * 1024 * 1024)], pypi=[("", 10 * 1024 * 1024)],
                     torch=[(mm.PYTORCH_OFFICIAL_WHL, 10 * 1024 * 1024)])
        self.assertLess(ss.estimate_seconds(fast, "comfyui"), 600)
        self.assertFalse(ss.is_slow(fast, "comfyui"))
        slow = _rank(github=[("", 200 * 1024)], pypi=[("", 200 * 1024)],
                     torch=[(mm.PYTORCH_OFFICIAL_WHL, 200 * 1024)])
        self.assertTrue(ss.is_slow(slow, "comfyui"))
        dead = _rank(github=[("", 0)], pypi=[("", 9e6)])
        self.assertEqual(ss.estimate_seconds(dead), float("inf"))
        self.assertTrue(ss.is_slow(dead))

    def test_fmt(self):
        self.assertEqual(ss.fmt_duration(30), "不到 1 分钟")
        self.assertEqual(ss.fmt_duration(61 * 60), "1 小时 1 分钟")
        self.assertEqual(ss.fmt_duration(7200), "2 小时")
        self.assertIn("MB/s", ss.fmt_speed(5 * 1024 * 1024))
        self.assertIn("KB/s", ss.fmt_speed(500 * 1024))

    def test_pypi_files_base(self):
        self.assertEqual(ss.pypi_files_base("https://mirrors.aliyun.com/pypi/simple"), "https://mirrors.aliyun.com/pypi/")
        self.assertEqual(ss.pypi_files_base("https://pypi.tuna.tsinghua.edu.cn/simple/"),
                         "https://pypi.tuna.tsinghua.edu.cn/")


# ---------------- 本机 HTTP 服务：快 / 慢 / 404 ----------------

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        kind = self.path.strip("/").split("/")[0]
        if kind == "missing":
            self.send_response(404)
            self.end_headers()
            return
        if kind == "index":
            body = b'<a href="../files/fast/torch-2.9.1%2Bcu128-cp313-cp313-win_amd64.whl#sha256=1">x</a>'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(206)
        self.send_header("Content-Length", str(ss.PROBE_BYTES))
        self.end_headers()
        chunk = b"x" * 8192
        try:
            if kind in ("fast", "files"):
                for _ in range(ss.PROBE_BYTES // len(chunk)):
                    self.wfile.write(chunk)
            else:   # slow：几乎不动，测速要按截止时间收尾，不能等它
                for _ in range(200):
                    self.wfile.write(b"x" * 100)
                    self.wfile.flush()
                    time.sleep(0.5)
        except (ConnectionError, OSError):
            pass


class MeasureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.srv.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_measure_ranks_and_respects_deadline(self):
        urls = [f"{self.base}/slow/a", f"{self.base}/fast/a", f"{self.base}/missing/a",
                "http://127.0.0.1:9/never"]
        t0 = time.monotonic()
        with mock.patch.object(ss, "CONNECT_TIMEOUT", 1.0):
            res = ss.measure_urls(urls, seconds=1.0)
        self.assertLess(time.monotonic() - t0, 5)          # 慢源不拖住测速
        self.assertGreater(res[urls[1]][0], res[urls[0]][0] * 10)
        self.assertEqual(res[urls[2]][0], 0)
        self.assertIn("404", res[urls[2]][1])
        self.assertEqual(res[urls[3]][0], 0)

    def test_run_groups_with_local_sources(self):
        b = self.base
        with mock.patch.object(ss, "github_sources", lambda cfg: [("", "直连", f"{b}/slow/g"),
                                                                 ("https://p1", "p1", f"{b}/fast/g")]), \
                mock.patch.object(ss, "pypi_sources", lambda cfg: [("", "官方", f"{b}/missing/p"),
                                                                   ("https://m/simple", "m", f"{b}/fast/p")]), \
                mock.patch.object(ss, "torch_sources", lambda cfg: [(f"{b}/whl", "官方", f"{b}/whl")]), \
                mock.patch.object(ss, "_torch_probe_url", lambda base, tag, s: f"{b}/files/fast/torch.whl"), \
                mock.patch.object(ss, "CONNECT_TIMEOUT", 1.0):
            logs = []
            rank = ss.run({}, torch_tag="cu128", log=logs.append, seconds=1.0)
        self.assertEqual(rank["github"][0]["id"], "https://p1")
        self.assertEqual(rank["pypi"][0]["id"], "https://m/simple")
        self.assertGreater(rank["torch"][0]["bps"], 0)
        self.assertEqual(len(logs), 3)

    def test_torch_probe_url_from_index(self):
        import requests
        with requests.Session() as s:
            u = ss._torch_probe_url(f"{self.base}/index", "cu128", s)
        self.assertTrue(u.endswith("win_amd64.whl"))
        self.assertNotIn("#", u)


# ---------------- 部署流程里的提示 ----------------

class _FakeApi:
    def __init__(self, answers):
        import threading as th
        self.answers = list(answers)
        self.asked = []
        self._deploy_cancel = th.Event()
        self._cfg_lock = th.RLock()
        self._deploy_cfg = {"mirror_mode": "auto"}
        self.cfg = {"mirror_mode": "auto"}

    def _ask(self, scope, title, body, buttons, cancel_event=None):
        self.asked.append((title, body, [b["id"] for b in buttons]))
        return self.answers.pop(0) if self.answers else None


class DeployPromptTest(unittest.TestCase):
    SLOW = _rank(github=[("", 100 * 1024)], pypi=[("", 100 * 1024)],
                 torch=[(mm.PYTORCH_OFFICIAL_WHL, 100 * 1024)])
    FAST = _rank(github=[("", 9e6)], pypi=[("", 9e6)], torch=[(mm.PYTORCH_OFFICIAL_WHL, 9e6)])

    def _run(self, answers, ranks):
        import webview_api as wa
        api = _FakeApi(answers)
        seq = list(ranks)
        logs = []
        with mock.patch.object(ss, "run", lambda *a, **k: dict(seq.pop(0), time=time.time())), \
                mock.patch.object(wa.cm, "save_config", lambda cfg: None):
            try:
                wa._deploy_speedtest(api, "comfyui", logs.append)
                result = "continue"
            except wa._DeployCancelled:
                result = "cancelled"
        return api, result, "".join(logs)

    def test_fast_no_prompt(self):
        api, result, _ = self._run([], [self.FAST])
        self.assertEqual(result, "continue")
        self.assertEqual(api.asked, [])
        self.assertIn("source_rank", api.cfg)             # 部署完装插件也按这次测速选源
        self.assertIn("source_rank", api._deploy_cfg)

    def test_retest_becomes_fast(self):
        api, result, log = self._run(["retest"], [self.SLOW, self.FAST])
        self.assertEqual(result, "continue")
        self.assertEqual(len(api.asked), 1)
        self.assertIn("梯子", api.asked[0][1])
        self.assertIn("速度正常", log)

    def test_still_slow_yes_continues(self):
        api, result, log = self._run(["retest", "yes"], [self.SLOW, self.SLOW])
        self.assertEqual(result, "continue")
        self.assertEqual([a[0] for a in api.asked], ["所有下载源都很慢", "预计部署时间过长"])
        self.assertIn("是否继续部署？（Y/N）", api.asked[1][1])
        self.assertIn("继续部署", log)

    def test_still_slow_no_cancels(self):
        _api, result, _ = self._run(["retest", "no"], [self.SLOW, self.SLOW])
        self.assertEqual(result, "cancelled")

    def test_skip_goes_to_yes_no(self):
        api, result, _ = self._run(["skip", "yes"], [self.SLOW])
        self.assertEqual(result, "continue")
        self.assertEqual(len(api.asked), 2)

    def test_cancel(self):
        _api, result, _ = self._run(["cancel"], [self.SLOW])
        self.assertEqual(result, "cancelled")

    def test_speedtest_error_does_not_block(self):
        import webview_api as wa
        api = _FakeApi([])
        logs = []

        def boom(*a, **k):
            raise RuntimeError("网络炸了")
        with mock.patch.object(ss, "run", boom):
            wa._deploy_speedtest(api, "comfyui", logs.append)
        self.assertIn("按原来的规则选源", "".join(logs))


# ---------------- 首次启动脚本 ----------------

class ScriptConsistencyTest(unittest.TestCase):
    def test_ps1_sources_match_mirror_manager(self):
        with open(PS1, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "ps1 必须是 UTF-8 带 BOM")
        text = raw.decode("utf-8-sig")
        indexes = re.findall(r"Index = '([^']+)'", text)
        self.assertEqual(indexes[0], "https://pypi.org/simple")
        self.assertEqual(indexes[1:], [u for _n, u in mm.PYPI_MIRRORS])
        files = re.findall(r"Files = '([^']+)'", text)
        self.assertEqual(files[1:], [ss.pypi_files_base(u) for _n, u in mm.PYPI_MIRRORS])
        m = re.search(r"\$githubProxies = @\(([^)]*)\)", text)
        proxies = [p.rstrip("/") for p in re.findall(r"'([^']+)'", m.group(1))]
        self.assertEqual(proxies, mm.GITHUB_PROXIES)
        self.assertIn(ss.PYPI_PROBE_PATH, text)
        self.assertIn(ss.GITHUB_PROBE_FILE, text)
        self.assertIn("是否继续部署？（Y/N）", text)

    def test_bat_labels_exist(self):
        with open(BAT, "rb") as f:
            text = f.read().decode("gbk")
        labels = set(re.findall(r"^:([A-Za-z_]+)", text, re.M))
        for target in re.findall(r"(?:goto|call) :([A-Za-z_]+)", text):
            if target != "eof":
                self.assertIn(target, labels, f"bat 里跳到不存在的标签 :{target}")
        for lab in ("speedtest", "order_ranked", "try_ranked", "user_cancel"):
            self.assertIn(lab, labels)
        self.assertIn("speedtest_sources.ps1", text)
        self.assertIn("venv_relocate.py", text)
        # 用户选 N（返回 3）要先于「本地环境问题」（返回 2）判断
        i3 = text.index("if errorlevel 3 goto :user_cancel")
        i2 = text.index("if errorlevel 2 goto :local_fail", i3 - 200)
        self.assertLess(i3, i2)


if __name__ == "__main__":
    unittest.main()
