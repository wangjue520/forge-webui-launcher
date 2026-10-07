"""不依赖 A 卡或 AMD 网络的回归测试。"""
import functools
import http.server
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch
import zipfile

import amd_rocm
import process_manager as pm
import webview_api as api


class ProbeTests(unittest.TestCase):
    def probe(self, tv_source):
        # 用真正的子进程导入假模块，覆盖探测脚本而非只模拟返回 JSON。
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "torch.py").write_text(
                "from types import SimpleNamespace\n"
                "__version__='2.9.1+rocm7.1'\n"
                "version=SimpleNamespace(hip='7.1', cuda=None)\n"
                "cuda=SimpleNamespace(is_available=lambda: True, get_device_name=lambda i: 'AMD')\n"
                "def zeros(*shape, device=None):\n"
                "    assert device == 'cpu', 'NMS 必须在 CPU 检查'\n"
                "    return shape\n", encoding="utf-8")
            Path(tmp, "torchvision.py").write_text(tv_source, encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=tmp)
            return amd_rocm.probe_torch(sys.executable, env=env)

    def test_import_error_preserves_healthy_torch(self):
        info = self.probe("raise RuntimeError('operator torchvision::nms does not exist')")
        self.assertEqual(info["backend"], "rocm")
        self.assertTrue(info["ok"])
        self.assertIs(info.get("tv_ok"), False)
        self.assertIn("torchvision::nms", info["tv_error"])

    def test_nms_is_really_called(self):
        info = self.probe("from types import SimpleNamespace\n__version__='0.24.1'\n"
                          "def nms(*args): raise RuntimeError('NMS 内核损坏')\n"
                          "ops=SimpleNamespace(nms=nms)\n")
        self.assertIs(info.get("tv_ok"), False)
        self.assertEqual(info["tv_version"], "0.24.1")
        self.assertIn("NMS 内核损坏", info["tv_error"])

    def test_working_cpu_nms(self):
        info = self.probe("from types import SimpleNamespace\n__version__='0.24.1+rocm7.1'\n"
                          "def nms(boxes, scores, threshold):\n"
                          "    assert (boxes, scores, threshold) == ((1,4), (1,), 0.5)\n"
                          "    return [0]\nops=SimpleNamespace(nms=nms)\n")
        self.assertIs(info.get("tv_ok"), True)
        self.assertEqual(info["tv_error"], "")

    def test_missing_torchvision_is_not_a_torch_failure(self):
        info = self.probe("raise ModuleNotFoundError(\"No module named 'torchvision'\")")
        self.assertTrue(info["ok"])
        self.assertIs(info["tv_ok"], False)
        self.assertIn("No module named 'torchvision'", info["tv_error"])

    def test_failed_torch_probe_still_has_tv_fields(self):
        with patch.object(amd_rocm.subprocess, "run", side_effect=OSError("missing python")):
            info = amd_rocm.probe_torch("missing.exe")
        self.assertIs(info["tv_ok"], False)
        self.assertEqual(info["tv_version"], "")
        self.assertTrue(info["tv_error"])


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        # 标准 http.server 不处理 Range，补齐 wheel 尾部元数据读取所需的行为。
        if "Range" not in self.headers:
            return super().do_GET()
        data = Path(self.translate_path(self.path)).read_bytes()
        start, end = map(int, self.headers["Range"].split("=")[1].split("-"))
        body = data[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        self.wfile.write(body)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        handler = functools.partial(RangeHandler, directory=self.tmp.name)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.index = f"http://127.0.0.1:{self.server.server_port}/"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def wheel(self, name, version, requires=()):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        filename = f"{name}-{version}-cp313-cp313-win_amd64.whl"
        with zipfile.ZipFile(directory / filename, "w") as z:
            z.writestr(f"{name}-{version}.dist-info/METADATA",
                       f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" +
                       "".join(f"Requires-Dist: {r}\n" for r in requires))
        with (directory / "index.html").open("a", encoding="utf-8") as f:
            f.write(f'<a href="{filename}">{filename}</a>\n')

    def populate(self, paired=True, dependency="torch"):
        self.wheel("torch", "2.9.1+rocm7.1")
        self.wheel("torchvision", "0.25.0+rocm7.1", [dependency])
        self.wheel("torchaudio", "2.10.0+rocm7.1", ["torch"])
        if paired:
            self.wheel("torchvision", "0.24.1+rocm7.1", [dependency])
            self.wheel("torchaudio", "2.9.1+rocm7.1", ["torch"])
        self.wheel("rocm", "7.1.0")

    def test_unpinned_metadata_still_selects_matched_family(self):
        self.populate()
        roots = amd_rocm.root_requirements("gfx1101")
        # 根依赖顺序不应决定版本是否配套。
        for roots in (roots, list(reversed(roots))):
            plan = amd_rocm.Planner(self.index, "cp313").plan(roots)
            found = {item["name"]: item for item in plan}
            self.assertEqual(found["torchvision"]["version"], "0.24.1+rocm7.1")
            self.assertEqual(found["torchaudio"]["version"], "2.9.1+rocm7.1")
            self.assertEqual(found["torchvision"]["extras"], ["device-gfx1101"])

    def test_missing_matched_wheel_is_error(self):
        self.populate(paired=False)
        with self.assertRaises(amd_rocm.PlanError):
            amd_rocm.Planner(self.index, "cp313").plan(amd_rocm.root_requirements("gfx1101"))

    def test_conflicting_metadata_is_error(self):
        self.populate(dependency="torch==2.10.0")
        with self.assertRaises(amd_rocm.PlanError):
            amd_rocm.Planner(self.index, "cp313").plan(amd_rocm.root_requirements("gfx1101"))

    def test_nightly_versions_keep_release_suffix(self):
        for name, version in (("torch", "2.10.0.dev20261007+rocm7.1"),
                              ("torchvision", "0.25.0.dev20261007+rocm7.1"),
                              ("torchvision", "0.26.0.dev20261007+rocm7.1"),
                              ("torchaudio", "2.10.0.dev20261007+rocm7.1"), ("rocm", "7.1.0")):
            self.wheel(name, version)
        plan = amd_rocm.Planner(self.index, "cp313", prerelease=True).plan(amd_rocm.root_requirements("gfx1101"))
        versions = {item["name"]: item["version"] for item in plan}
        self.assertEqual(versions["torchvision"], "0.25.0.dev20261007+rocm7.1")

    def test_metadata_pins_are_not_ignored(self):
        self.populate(dependency="torch==2.9.1")
        plan = amd_rocm.Planner(self.index, "cp313").plan(amd_rocm.root_requirements("gfx1101"))
        self.assertEqual(next(i["version"] for i in plan if i["name"] == "torchvision"), "0.24.1+rocm7.1")


    def test_newest_torch_without_pair_falls_back_to_older_set(self):
        # AMD 源先发了 torch 2.11、torchvision 0.26 还没跟上：要退回有配套的 2.9.1，而不是整个失败
        self.wheel("torch", "2.11.0+rocm7.2")
        self.wheel("torch", "2.9.1+rocm7.1")
        self.wheel("torchvision", "0.24.1+rocm7.1", ["torch"])
        self.wheel("torchaudio", "2.9.1+rocm7.1", ["torch"])
        self.wheel("torchaudio", "2.11.0+rocm7.2", ["torch"])
        self.wheel("rocm", "7.1.0")
        plan = amd_rocm.Planner(self.index, "cp313").plan(amd_rocm.root_requirements("gfx1101"))
        found = {item["name"]: item["version"] for item in plan}
        self.assertEqual(found["torch"], "2.9.1+rocm7.1")
        self.assertEqual(found["torchvision"], "0.24.1+rocm7.1")
        self.assertEqual(found["torchaudio"], "2.9.1+rocm7.1")

    def test_same_series_pair_accepted_when_exact_patch_missing(self):
        self.wheel("torch", "2.9.1+rocm7.1")
        self.wheel("torchvision", "0.24.0+rocm7.1", ["torch"])
        self.wheel("torchaudio", "2.9.0+rocm7.1", ["torch"])
        self.wheel("rocm", "7.1.0")
        torch_ver, pins = amd_rocm.Planner(self.index, "cp313").pick_torch_set()
        self.assertEqual(torch_ver, "2.9.1+rocm7.1")
        self.assertEqual(pins, {"torchvision": "==0.24.*", "torchaudio": "==2.9.*"})

    def test_series_pair_rejected_when_metadata_pins_other_torch(self):
        # torchvision 0.24.0 元数据钉死 torch==2.9.0：不能配给 2.9.1，应改选 torch 2.9.0
        self.wheel("torch", "2.9.1+rocm7.1")
        self.wheel("torch", "2.9.0+rocm7.1")
        self.wheel("torchvision", "0.24.0+rocm7.1", ["torch==2.9.0"])
        self.wheel("torchaudio", "2.9.0+rocm7.1", ["torch==2.9.0"])
        self.wheel("rocm", "7.1.0")
        torch_ver, pins = amd_rocm.Planner(self.index, "cp313").pick_torch_set()
        self.assertEqual(torch_ver, "2.9.0+rocm7.1")
        self.assertEqual(pins["torchvision"], "==0.24.0")


class PurgeTests(unittest.TestCase):
    def test_leftover_dirs_removed_after_pip(self):
        with tempfile.TemporaryDirectory() as sp:
            for d in ("torchvision", "torchvision-0.25.0+cu128.dist-info", "torch", "torchaudio-x.egg-info"):
                os.makedirs(os.path.join(sp, d))
            Path(sp, "torchvision", "_C.pyd").write_bytes(b"cuda")
            os.makedirs(os.path.join(sp, "torchsde"))   # 名字相近的其他包不能误删
            fake = types.SimpleNamespace(stdout=f'["{sp.replace(chr(92), "/")}"]\n', returncode=0)
            log = Mock()
            with patch.object(api.subprocess, "run", return_value=fake):
                api._purge_leftover_pkgs("python.exe", ["torchvision"], log)
            left = sorted(os.listdir(sp))
            self.assertEqual(left, ["torch", "torchaudio-x.egg-info", "torchsde"])
            self.assertIn("残留", "".join(c.args[0] for c in log.call_args_list))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.host = types.SimpleNamespace(_deploy_cancel=threading.Event(), _deploy_run_cmd=Mock(return_value=0))
        self.log = Mock()
        self.bad = dict(installed=True, backend="rocm", ok=True, version="2.9.1+rocm7.1",
                        name="AMD", tv_ok=False, tv_version="0.25.0+cu128", tv_error="nms missing")
        self.good = dict(self.bad, tv_ok=True, tv_version="0.24.1+rocm7.1", tv_error="")
        # 直装回退只需查 torch 索引，测试中禁止访问真实 AMD 源。
        self.addCleanup(patch.stopall)
        patch("cuda_compat.python_tag", return_value="cp313").start()
        patch.object(amd_rocm.Planner, "pick_torch_set", return_value=(
            "2.9.1+rocm7.1", {"torchvision": "==0.24.1", "torchaudio": "==2.9.1"})).start()
        patch.object(amd_rocm.Planner, "pair_pins", return_value={"torchvision": "==0.24.1"}).start()

    def install(self):
        return api._install_rocm_torch(self.host, "python.exe", "gfx1101", ".", self.log, {})

    def test_broken_tv_repaired_without_removing_torch(self):
        with patch.object(amd_rocm, "probe_torch", side_effect=[self.bad, self.good]) as probe:
            self.assertTrue(self.install())
        cmds = [call.args[1] for call in self.host._deploy_run_cmd.call_args_list]
        self.assertIn(["-m", "pip", "uninstall", "-y", "torchvision"], cmds)
        installs = [cmd for cmd in cmds if "install" in cmd]
        self.assertTrue(installs)
        for cmd in installs:
            self.assertIn("torchvision[device-gfx1101]==0.24.1", cmd)
            self.assertIn("torch[device-gfx1101]==2.9.1+rocm7.1", cmd)
            self.assertIn("--isolated", cmd)
            self.assertEqual(cmd[cmd.index("--index-url") + 1], amd_rocm.ROCM_INDEX)
            self.assertNotIn("--extra-index-url", cmd)
        self.assertEqual(probe.call_count, 2)
        self.assertIn("修复成功", "".join(c.args[0] for c in self.log.call_args_list))

    def test_healthy_pair_skips(self):
        with patch.object(amd_rocm, "probe_torch", return_value=self.good):
            self.assertTrue(self.install())
        self.host._deploy_run_cmd.assert_not_called()

    def test_failed_repair_is_not_success(self):
        with patch.object(amd_rocm, "probe_torch", return_value=self.bad):
            self.assertFalse(self.install())

    def test_cancelled_repair_does_not_uninstall(self):
        self.host._deploy_cancel.set()
        with patch.object(amd_rocm, "probe_torch", return_value=self.bad):
            with self.assertRaises(api._DeployCancelled):
                self.install()
        self.host._deploy_run_cmd.assert_not_called()

    def test_nightly_retry_replaces_failed_stable_wheel(self):
        with patch.object(amd_rocm, "probe_torch", side_effect=[self.bad, self.bad, self.good]):
            self.assertTrue(self.install())
        cmds = [call.args[1] for call in self.host._deploy_run_cmd.call_args_list]
        # 同版本的正式源 wheel 若能安装但 NMS 仍坏，pip 会认为已经满足，必须先卸掉再试 nightly。
        self.assertEqual(cmds.count(["-m", "pip", "uninstall", "-y", "torchvision"]), 2)
        installs = [cmd for cmd in cmds if "install" in cmd]
        self.assertEqual(installs[1][installs[1].index("--index-url") + 1], amd_rocm.ROCM_NIGHTLY_INDEX)
        self.assertIn("--pre", installs[1])
        self.assertIn("torchvision[device-gfx1101]==0.24.1", installs[1])
        self.assertNotIn("--extra-index-url", installs[1])

    def test_failed_uninstall_stops_repair(self):
        self.host._deploy_run_cmd.return_value = 1
        with patch.object(amd_rocm, "probe_torch", return_value=self.bad):
            self.assertFalse(self.install())
        self.assertEqual(self.host._deploy_run_cmd.call_count, 1)

    def test_unknown_torch_version_does_not_uninstall(self):
        with patch.object(amd_rocm, "probe_torch", return_value=dict(self.bad, version="unknown")):
            self.assertFalse(self.install())
        self.host._deploy_run_cmd.assert_not_called()

    def test_full_install_rejects_bad_torchvision(self):
        with patch.object(amd_rocm, "probe_torch", side_effect=[{"installed": False}, self.bad]), \
                patch.object(api, "_predownload_rocm", return_value=(None, None)):
            self.assertFalse(self.install())

    def test_pip_fallback_keeps_pairing_without_a_download_plan(self):
        with patch.object(amd_rocm, "probe_torch", side_effect=[{"installed": False}, self.good]), \
                patch.object(api, "_predownload_rocm", return_value=(None, None)):
            self.assertTrue(self.install())
        cmd = next(c.args[1] for c in self.host._deploy_run_cmd.call_args_list if "install" in c.args[1])
        self.assertIn("torchvision[device-gfx1101]==0.24.1", cmd)
        self.assertIn("torchaudio==2.9.1", cmd)
        self.assertIn("torch[device-gfx1101]==2.9.1+rocm7.1", cmd)
        self.assertNotIn("--extra-index-url", cmd)

    def test_pip_fallback_preserves_the_downloaded_torch_version(self):
        plan = [{"name": "torch", "version": "2.8.0+rocm7.0", "extras": ["device-gfx1101"]}]
        self.host._deploy_run_cmd.side_effect = [1, 0, 0]
        with patch.object(amd_rocm, "probe_torch", side_effect=[{"installed": False}, self.good]), \
                patch.object(api, "_predownload_rocm", return_value=(plan, ["torch.whl"])):
            self.assertTrue(self.install())
        cmd = self.host._deploy_run_cmd.call_args_list[1].args[1]
        self.assertIn("torch[device-gfx1101]==2.8.0+rocm7.0", cmd)
        self.assertIn("torchvision[device-gfx1101]==0.23.0", cmd)
        self.assertIn("torchaudio==2.8.0", cmd)

    def test_cannot_resolve_pair_does_not_install_unpinned_packages(self):
        with patch.object(amd_rocm, "probe_torch", return_value={"installed": False}), \
                patch.object(api, "_predownload_rocm", return_value=(None, None)), \
                patch.object(amd_rocm.Planner, "pick_torch_set", side_effect=amd_rocm.PlanError("无配套")):
            self.assertFalse(self.install())
        self.host._deploy_run_cmd.assert_not_called()

    def test_cancel_during_probe_does_not_report_success(self):
        for initial in (False, True):
            self.host._deploy_cancel.clear()
            self.host._deploy_run_cmd.reset_mock()
            def cancel_probe(*args, **kwargs):
                self.host._deploy_cancel.set()
                return self.good
            effects = cancel_probe if initial else iter([self.bad])
            def probe(*args, **kwargs):
                if initial:
                    return cancel_probe()
                return next(effects, None) or cancel_probe()
            with self.subTest(initial=initial), patch.object(amd_rocm, "probe_torch", side_effect=probe):
                with self.assertRaises(api._DeployCancelled):
                    self.install()


class ExitTests(unittest.TestCase):
    def runner(self, chunks, rocm=False):
        host = Mock()
        host.instance_cfg.return_value = {"gpu_backend": "rocm" if rocm else ""}
        runner = pm.InstanceRunner(host, "test")
        runner.proc = Mock()
        runner.proc.stdout.read1.side_effect = [s.encode() for s in chunks] + [b""]
        runner.proc.wait.return_value = 0
        runner._reader(runner.gen, False, False)
        return runner

    def test_zero_exit_after_runtime_error_before_ready_fails(self):
        runner = self.runner(["RuntimeError: backend broken\n", "x" * 6000])
        self.assertIn("启动失败", runner.status_text)
        self.assertTrue(runner.fatal_hint)

    def test_nms_hint_distinguishes_gpu(self):
        for rocm in (False, True):
            runner = self.runner(["Traceback (most recent call last):\n",
                                  "RuntimeError: operator torchvision::nms does not exist\n"], rocm)
            self.assertIn("启动失败", runner.status_text)
            self.assertIn("torchvision", runner.fatal_hint)
            self.assertIn("把安装目录的环境切换成所选显卡" if rocm else "重新启动", runner.fatal_hint)
            self.assertIn("环境部署" if rocm else "Forge", runner.fatal_hint)

    def test_ready_process_exits_normally_even_with_previous_error(self):
        runner = self.runner(["RuntimeError: recovered\n", "Running on local URL: http://127.0.0.1:7860\n"])
        self.assertEqual(runner.status_text, "已正常退出")

    def test_clean_exit_stays_normal(self):
        self.assertEqual(self.runner(["hello\n"]).status_text, "已正常退出")

    def test_traceback_and_error_split_across_chunks(self):
        for chunks in (["Trace", "back (most recent call last):\n"], ["Er", "ror: broken\n"]):
            self.assertIn("启动失败", self.runner(chunks).status_text)

    def test_normal_automatic_restart(self):
        self.assertEqual(self.runner(["Restarting...\n"]).status_text, "已正常退出（自动重启）")


if __name__ == "__main__":
    unittest.main()
