"""
下载慢 / 卡死时自动换源（app/portable_env.py、scripts/bootstrap_python.ps1）的回归测试。

核心：连上了但几乎不动的地址（国内加速代理常见，100 多 B/s）要被发现并换掉，已下载的部分接着用；
     连不上的地址不在原地重试；所有地址都慢时放宽下限，仍然能下完。
用本机 HTTP 服务模拟各种下载源，不访问外网。
运行：python -m unittest tests.test_portable_download
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import http.server
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import mirror_manager as mm  # noqa: E402
import portable_env as pe  # noqa: E402

DATA = bytes(range(256)) * 4096   # 1 MB
FAST_PART = 256 * 1024
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "bootstrap_python.ps1")


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        kind = self.path.strip("/").split("/")[0]
        rng = self.headers.get("Range")
        self.server.requests.append((kind, rng))
        if kind == "denied":
            self.send_error(403)
            return
        start = int(re.match(r"bytes=(\d+)-", rng).group(1)) if rng else 0
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(DATA) - start))
        self.end_headers()
        try:
            if kind == "good":
                self.wfile.write(DATA[start:])
            elif kind == "trickle":      # 先快一小段，然后几乎不动（和实际卡住的代理一样）
                pos = start
                if pos < FAST_PART:
                    self.wfile.write(DATA[pos:FAST_PART])
                    self.wfile.flush()
                    pos = FAST_PART
                while pos < len(DATA):
                    self.wfile.write(DATA[pos:pos + 1024])
                    self.wfile.flush()
                    pos += 1024
                    time.sleep(0.2)     # 约 5 KB/s
        except OSError:
            pass


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.dest = os.path.join(self.dir, "file.bin")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.requests = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        patcher = mock.patch.object(pe, "_SPEED_ROUNDS", ((50 * 1024, 1), (1024, 2)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _download(self, *kinds, **kwargs):
        urls = [f"{self.base}/{k}/file" for k in kinds]
        with mock.patch.object(mm, "resolve_github_mode", return_value=True), \
                mock.patch.object(mm, "GITHUB_PROXIES", urls), \
                mock.patch.object(mm, "github_url", side_effect=lambda u, m, i=0: urls[i]):
            # 原始地址也指向本机（403），测试不碰外网
            return pe.download_file(f"{self.base}/denied/origin", self.dest, cfg={}, **kwargs)

    def _content(self):
        with open(self.dest, "rb") as f:
            return f.read()

    def test_stalled_source_switches_and_resumes(self):
        self._download("trickle", "good")
        self.assertEqual(self._content(), DATA)
        kind, rng = self.server.requests[-1]
        self.assertEqual(kind, "good")
        # 换源后接着下，而不是从头来
        self.assertGreaterEqual(int(re.match(r"bytes=(\d+)-", rng).group(1)), FAST_PART)
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_only_slow_source_still_finishes_in_second_round(self):
        # 第一轮 50 KB/s 不达标；第二轮下限放宽到 1 KB/s，留在这个源上慢慢下
        logs = []
        with mock.patch.object(pe, "_SPEED_ROUNDS", ((1024 * 1024, 1), (1024, 2))), \
                mock.patch("time.sleep", new=lambda s: _REAL_SLEEP(s / 20)):   # 服务端加速，别让测试跑几分钟
            self._download("trickle", log_cb=logs.append)
        self.assertEqual(self._content(), DATA)
        self.assertTrue(any("放宽" in x for x in logs), logs)

    def test_refused_source_fails_fast_without_retries(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}/file"
        s.close()      # 没有人监听：连接被拒
        started = time.monotonic()
        with self.assertRaises(pe.PortableEnvError) as ctx:
            pe._download_once(dead, self.dest)
        self.assertIn("连接失败", str(ctx.exception))
        self.assertLess(time.monotonic() - started, 5)   # 以前要原地重试 5 次

    def test_denied_source_falls_through(self):
        self._download("denied", "good")
        self.assertEqual(self._content(), DATA)

    def test_cancel_is_not_retried(self):
        with self.assertRaises(pe.PortableEnvError) as ctx:
            self._download("good", "good", cancel_flag=lambda: True)
        self.assertIn("已取消", str(ctx.exception))
        self.assertEqual(len(self.server.requests), 1)

    def test_progress_reaches_total_but_is_throttled(self):
        calls = []
        self._download("good", progress_cb=lambda d, t: calls.append((d, t)))
        self.assertEqual(calls[-1], (len(DATA), len(DATA)))
        self.assertLess(len(calls), 10)   # 8KB 一块读，但界面不用每块刷新


_REAL_SLEEP = time.sleep


class SignatureTest(unittest.TestCase):
    """自解压包执行前的 Authenticode 校验：不靠 PowerShell，失败时要说出真实原因"""

    def _verify(self, code, signer="", log_cb=None):
        with mock.patch.object(pe.os, "name", "nt"), \
                mock.patch.object(pe, "_authenticode_check", return_value=(code, signer)):
            return pe.verify_pe_signature(r"D:\x\PortableGit.7z.exe", log_cb=log_cb)

    def test_official_signer_passes(self):
        self.assertIs(self._verify(0, "Johannes Schindelin"), True)

    def test_other_signer_rejected(self):
        with self.assertRaises(pe.PortableEnvError) as ctx:
            self._verify(0, "Mallory")
        self.assertIn("Mallory", str(ctx.exception))

    def test_proven_bad_file_is_blocked_with_reason(self):
        for code, word in ((0x80096010, "改动"), (0x800B0100, "没有数字签名"), (0x800B010C, "吊销")):
            with self.assertRaises(pe.PortableEnvError) as ctx:
                self._verify(code)
            self.assertIn(word, str(ctx.exception))

    def test_unprovable_result_warns_and_continues(self):
        # 系统证书旧、其它错误码：这台电脑证明不了，不代表文件有问题
        for code, word in ((0x800B0109, "根证书"), (0x800B0101, "过期"), (0x80004005, "0x80004005")):
            logs = []
            self.assertIs(self._verify(code, log_cb=logs.append), False)
            self.assertIn(word, logs[0])
            self.assertIn("继续部署", logs[0])

    def test_missing_signer_name_warns_and_continues(self):
        self.assertIs(self._verify(0, ""), False)

    def test_check_that_cannot_run_does_not_block(self):
        # 用户实际遇到的报错：校验没跑起来，不该说成「文件被篡改」、也不该挡住部署
        logs = []
        with mock.patch.object(pe.os, "name", "nt"), \
                mock.patch.object(pe, "_authenticode_check", side_effect=OSError("no dll")):
            self.assertIs(pe.verify_pe_signature(r"D:\x\a.exe", log_cb=logs.append), False)
        self.assertIn("no dll", logs[0])
        self.assertNotIn("篡改", logs[0])

    @unittest.skipUnless(os.name == "nt", "只有 Windows 有 WinVerifyTrust")
    def test_real_api_rejects_unsigned_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "假的 Git 中文路径.exe")
        with open(path, "wb") as f:
            f.write(b"MZ" + b"\0" * 4096)
        with self.assertRaises(pe.PortableEnvError):
            pe.verify_pe_signature(path)


def _release(tag, ver):
    names = [f"PortableGit-{ver}-64-bit.7z.exe", f"MinGit-{ver}-64-bit.zip", f"MinGit-{ver}-busybox-64-bit.zip"]
    assets = [{"name": n, "url": f"https://x/{n}", "digest": ""} for n in names]
    body = "\n".join(f"{n} | {'a' * 64}" for n in names)
    return tag, assets, body


class CandidatesTest(unittest.TestCase):
    def test_portable_then_mingit(self):
        releases = [_release(f"v{i}", f"2.5{i}.0") for i in range(4, 0, -1)]
        names = [c["name"] for c in pe.git_candidates(releases)]
        self.assertEqual(names, ["PortableGit-2.54.0-64-bit.7z.exe", "PortableGit-2.53.0-64-bit.7z.exe",
                                 "PortableGit-2.52.0-64-bit.7z.exe", "MinGit-2.54.0-64-bit.zip"])

    def test_release_without_checksum_is_skipped(self):
        tag, assets, _body = _release("v1", "2.50.0")
        self.assertEqual(pe.git_candidates([(tag, assets, "")]), [])

    def test_pinned_entries_are_complete(self):
        for branch_prefix in set(pe.PYTHON_VERSION_BY_BRANCH.values()):
            item = pe.pinned_python_asset(branch_prefix)
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(branch_prefix + ".", item["name"])
        self.assertIsNone(pe.pinned_python_asset("2.7"))
        names = [c["name"] for c in pe.pinned_git_assets()]
        self.assertTrue(any(n.endswith(".zip") for n in names) and any(n.endswith(".exe") for n in names))


class _FakeApi:
    def __init__(self):
        self._deploy_cfg = {}
        self._deploy_fallback_paths = {}
        self._deploy_cancel = threading.Event()


class DeployChainTest(unittest.TestCase):
    """部署时准备 Git / Python：任何一步失败都自动走下一条路，全部走不通才报错"""

    def setUp(self):
        import webview_api as wa
        self.wa = wa
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.api = _FakeApi()
        self.logs = []
        self.fetched = []

    def _log(self, text):
        self.logs.append(text)

    def _git(self, releases, fetch_fails=(), run_fails=(), system=(False, "没有"), which=None, sign_error=None):
        def fetch(api, item, tmp, log, progress):
            self.fetched.append(item["name"])
            if item["name"] in fetch_fails:
                raise pe.PortableEnvError("下载失败")
            return os.path.join(tmp, item["name"])

        def extract(archive, target, log_cb=None):
            if os.path.basename(archive) in run_fails:
                raise OSError("被杀软拦截")
            return os.path.join(target, "git", "cmd", "git.exe")

        patches = [
            mock.patch.object(pe, "list_recent_releases", side_effect=releases if isinstance(releases, Exception) else None,
                              return_value=None if isinstance(releases, Exception) else releases),
            mock.patch.object(pe, "list_release_assets_with_meta", side_effect=pe.PortableEnvError("API 不通")),
            mock.patch.object(self.wa, "_portable_fetch", side_effect=fetch),
            mock.patch.object(pe, "verify_pe_signature", side_effect=sign_error),
            mock.patch.object(pe, "extract_portable_git", side_effect=extract),
            mock.patch.object(pe, "extract_min_git", side_effect=extract),
            mock.patch.object(pe, "check_portable_git", return_value=(True, "git version x")),
            mock.patch.object(pe, "system_git_ok", return_value=system),
            mock.patch.object(self.wa.shutil, "which", return_value=which),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.wa._deploy_portable_git(self.api, self.dir, self.dir, self._log, lambda d, t: None)

    def test_first_version_works(self):
        self._git([_release("v2", "2.52.0"), _release("v1", "2.51.0")])
        self.assertEqual(self.fetched, ["PortableGit-2.52.0-64-bit.7z.exe"])

    def test_blocked_signature_moves_on_instead_of_stopping(self):
        self._git([_release("v2", "2.52.0")], sign_error=pe.PortableEnvError("签名不对"))
        self.assertEqual(self.fetched, ["PortableGit-2.52.0-64-bit.7z.exe", "MinGit-2.52.0-64-bit.zip"])

    def test_extract_blocked_falls_to_mingit(self):
        self._git([_release("v1", "2.51.0")], run_fails=("PortableGit-2.51.0-64-bit.7z.exe",))
        self.assertEqual(self.fetched[-1], "MinGit-2.51.0-64-bit.zip")

    def test_api_blocked_uses_pinned_version(self):
        self._git(pe.PortableEnvError("API 被限流"))
        self.assertEqual(self.fetched, [pe.PINNED_GIT[0][1]])
        self.assertTrue(any("固定版本" in x for x in self.logs))

    def test_system_git_used_after_first_failure(self):
        name = "PortableGit-2.52.0-64-bit.7z.exe"
        self._git([_release("v2", "2.52.0"), _release("v1", "2.51.0")], fetch_fails=(name,),
                  system=(True, r"C:\Git\cmd\git.exe"), which=r"C:\Git\cmd\git.exe")
        self.assertEqual(self.fetched, [name])     # 不再继续下载别的版本
        self.assertEqual(self.api._deploy_fallback_paths, {})    # 在 PATH 里，不用记路径

    def test_system_git_off_path_is_remembered(self):
        name = "PortableGit-2.52.0-64-bit.7z.exe"
        self._git([_release("v2", "2.52.0")], fetch_fails=(name,), system=(True, r"D:\G\cmd\git.exe"), which=None)
        self.assertEqual(self.api._deploy_cfg["custom_git_path"], r"D:\G\cmd\git.exe")
        self.assertEqual(self.api._deploy_fallback_paths["custom_git_path"], r"D:\G\cmd\git.exe")

    def test_network_down_stops_after_two_downloads_and_explains(self):
        names = tuple(c["name"] for c in pe.pinned_git_assets())
        with self.assertRaises(pe.PortableEnvError) as ctx:
            self._git(pe.PortableEnvError("不通"), fetch_fails=names)
        self.assertEqual(len(self.fetched), 2)
        self.assertIn("代理", str(ctx.exception))

    def test_cancel_during_download_is_not_reported_as_failure(self):
        def fetch(*a):
            raise self.wa._DeployCancelled()
        with mock.patch.object(pe, "list_recent_releases", return_value=[_release("v1", "2.51.0")]), \
                mock.patch.object(pe, "system_git_ok", return_value=(True, "git")), \
                mock.patch.object(self.wa, "_portable_fetch", side_effect=fetch):
            with self.assertRaises(self.wa._DeployCancelled):
                self.wa._deploy_portable_git(self.api, self.dir, self.dir, self._log, None)

    def _python(self, branch, online, fetch_fails=(), can_copy=False, system=None, copy_error=None):
        def fetch(api, item, tmp, log, progress):
            self.fetched.append(item["name"])
            if item["name"] in fetch_fails:
                raise pe.PortableEnvError("下载失败")
            return os.path.join(tmp, item["name"])

        tag = mock.patch.object(pe, "get_latest_pbs_tag", return_value="20261026") if online is not None \
            else mock.patch.object(pe, "get_latest_pbs_tag", side_effect=pe.PortableEnvError("raw 被墙"))
        patches = [
            tag,
            mock.patch.object(pe, "fetch_pbs_sha256sums", return_value=online or {}),
            mock.patch.object(self.wa, "_portable_fetch", side_effect=fetch),
            mock.patch.object(pe, "extract_python_tar", return_value="python.exe"),
            mock.patch.object(pe, "launcher_python_matches", return_value=can_copy),
            mock.patch.object(pe, "copy_launcher_python", return_value="copied.exe", side_effect=copy_error),
            mock.patch.object(pe, "find_system_python", return_value=system),
        ]
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.wa._deploy_portable_python(self.api, self.dir, branch, self.dir, self._log, lambda d, t: None)
        return started[5]   # copy_launcher_python 的 mock

    LATEST_313 = {"cpython-3.13.18+20261026-x86_64-pc-windows-msvc-install_only.tar.gz": "b" * 64}

    def test_classic_python_310_gone_from_latest_uses_pinned(self):
        # 真实情况：20261026 的清单里已经没有 3.10，Classic 以前会直接报错
        self._python("classic", self.LATEST_313)
        self.assertEqual(self.fetched, [pe.PINNED_PYTHON["3.10"][1]])

    def test_lookup_failure_uses_pinned(self):
        self._python("neo", None)
        self.assertEqual(self.fetched, [pe.PINNED_PYTHON["3.13"][1]])

    def test_download_failure_copies_launcher_python(self):
        name = next(iter(self.LATEST_313))
        copy = self._python("neo", self.LATEST_313, fetch_fails=(name,), can_copy=True)
        self.assertEqual(self.fetched, [name])     # 不再继续下载固定版本
        copy.assert_called_once()

    def test_system_python_when_nothing_else(self):
        names = (next(iter(self.LATEST_313)), pe.PINNED_PYTHON["3.13"][1])
        self._python("neo", self.LATEST_313, fetch_fails=names, system=r"D:\Py313\python.exe")
        self.assertEqual(self.api._deploy_cfg["custom_python_path"], r"D:\Py313\python.exe")
        self.assertEqual(self.api._deploy_fallback_paths["custom_python_path"], r"D:\Py313\python.exe")

    def test_copy_failure_still_tries_system_python(self):
        name = next(iter(self.LATEST_313))
        self._python("neo", self.LATEST_313, fetch_fails=(name,), can_copy=True,
                     copy_error=OSError("磁盘满"), system=r"D:\Py313\python.exe")
        self.assertEqual(self.api._deploy_cfg["custom_python_path"], r"D:\Py313\python.exe")

    def test_everything_failed_explains(self):
        names = (next(iter(self.LATEST_313)), pe.PINNED_PYTHON["3.13"][1])
        with self.assertRaises(pe.PortableEnvError) as ctx:
            self._python("neo", self.LATEST_313, fetch_fails=names)
        self.assertIn("代理", str(ctx.exception))


class SystemToolsTest(unittest.TestCase):
    def test_git_found_outside_path(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        exe = os.path.join(d, "Git", "cmd", "git.exe")
        os.makedirs(os.path.dirname(exe))
        open(exe, "wb").close()
        ok = mock.Mock(returncode=0)
        with mock.patch.dict(os.environ, {"ProgramFiles": d}), \
                mock.patch("shutil.which", return_value=None), \
                mock.patch.object(pe.subprocess, "run", return_value=ok):
            self.assertEqual(pe.system_git_ok(), (True, exe))

    def test_python_must_match_version(self):
        def fake(cmd, **kw):
            if cmd[0] == "py":
                raise FileNotFoundError
            ver = "3.13" if "13" in cmd[0] else "3.12"
            return mock.Mock(returncode=0, stdout=f"{cmd[0]}\n{ver}\n".encode())
        with mock.patch.object(pe.subprocess, "run", side_effect=fake), \
                mock.patch("shutil.which", side_effect=lambda n: r"C:\Py312\python.exe" if n == "python" else None), \
                mock.patch.dict(os.environ, {"ProgramFiles": r"C:\PF", "LOCALAPPDATA": r"C:\LA"}):
            found = pe.find_system_python("3.13")
            self.assertIn("Python313", found)
            self.assertIsNone(pe.find_system_python("3.10"))

    def test_store_stub_python_is_rejected(self):
        # 微软商店占位 python：退出码非 0，没有输出
        with mock.patch.object(pe.subprocess, "run", return_value=mock.Mock(returncode=9009, stdout=b"")):
            self.assertIsNone(pe._python_version_of(["python"]))

    def test_chinese_path_in_output_does_not_break_detection(self):
        # 启动器目录带中文时，Python 输出的路径可能是 UTF-8，也可能是 GBK
        for enc in ("utf-8", "gbk"):
            out = "D:\\启动器\\python.exe\n3.13\n".encode(enc)
            with mock.patch.object(pe.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=out)):
                self.assertEqual(pe._python_version_of(["python"])[1], "3.13")


class BootstrapScriptTest(unittest.TestCase):
    def _read(self):
        with open(SCRIPT, "rb") as f:
            return f.read()

    def test_bom_kept(self):
        # 没有 BOM 时 PowerShell 5.1 按 GBK 读，整个脚本语法错误
        self.assertTrue(self._read().startswith(b"\xef\xbb\xbf"))

    def test_proxies_match_mirror_manager(self):
        text = self._read().decode("utf-8")
        line = re.search(r"\$proxyPrefixes = @\((.*)\)", text).group(1)
        prefixes = [p.rstrip("/") for p in re.findall(r"'([^']+)'", line)]
        self.assertEqual(prefixes, mm.GITHUB_PROXIES)

    def test_curl_has_speed_limit(self):
        # 只设连接超时的话，100 多 B/s 的连接永远不算失败
        self.assertIn("--speed-limit", self._read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
