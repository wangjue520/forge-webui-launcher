"""
显卡识别和 torch 选型（cuda_compat）的回归测试。

背景：老卡以前按型号名的正则判断（GTX 9xx/10xx、TITAN X/V、Tesla P/M），笔记本的 980M、Quadro、
MX 系列、750 Ti、Tesla V100 都漏掉——驱动一升到 580 就给它们装 cu130，一出图报 no kernel image；
RTX 5000 Ada / Quadro RTX 5000 又被 "RTX 50" 当成 50 系。现在统一按显卡的计算能力
（nvidia-smi 的 compute_cap）判断，型号名只在查不到计算能力时兜底。

各 CUDA 版本的官方 Windows wheel 带的架构（PyTorch 仓库 .ci/wheel/windows/build_env_setup.py）：
  cu126：sm_50 ~ sm_90；cu128 及以后：sm_75 ~ sm_120。
全部用假的显卡 / 驱动 / 索引跑，不联网、不调 nvidia-smi。
运行：python -m unittest tests.test_cuda_compat
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import config_manager as cm  # noqa: E402
import cuda_compat as cc  # noqa: E402
import lora_trainer as lt  # noqa: E402
import model_catalog as mc  # noqa: E402
import process_manager as pm  # noqa: E402
import torch_bootstrap as tb  # noqa: E402

# (nvidia-smi 报的型号名, 真实计算能力)
KEPLER = [("NVIDIA GeForce GTX 780 Ti", 3.5), ("GeForce GTX 760", 3.0), ("GeForce GT 730", 3.5),
          ("GeForce GTX TITAN Black", 3.5), ("Tesla K80", 3.7), ("Quadro K4200", 3.0),
          ("GeForce GTX 880M", 3.0)]
LEGACY = [("NVIDIA GeForce GTX 750 Ti", 5.0), ("NVIDIA GeForce GTX 960", 5.2),
          ("NVIDIA GeForce GTX 980 Ti", 5.2), ("NVIDIA GeForce GTX 980M", 5.2),
          ("NVIDIA GeForce GTX 960M", 5.0), ("NVIDIA GeForce 940MX", 5.0), ("NVIDIA GeForce MX130", 5.0),
          ("Quadro K2200", 5.0), ("Quadro M4000", 5.2), ("Tesla M40 24GB", 5.2),
          ("GeForce GTX TITAN X", 5.2),
          ("NVIDIA GeForce GT 1030", 6.1), ("NVIDIA GeForce GTX 1050 Ti", 6.1),
          ("NVIDIA GeForce GTX 1060 6GB", 6.1), ("NVIDIA GeForce GTX 1070 with Max-Q Design", 6.1),
          ("NVIDIA GeForce GTX 1080 Ti", 6.1), ("NVIDIA GeForce MX150", 6.1), ("NVIDIA GeForce MX350", 6.1),
          ("Quadro P2000", 6.1), ("Quadro P4000", 6.1), ("Quadro P6000", 6.1), ("NVIDIA TITAN Xp", 6.1),
          ("NVIDIA TITAN X (Pascal)", 6.1), ("Tesla P4", 6.1), ("Tesla P40", 6.1),
          ("Tesla P100-PCIE-16GB", 6.0),
          ("NVIDIA TITAN V", 7.0), ("Tesla V100-PCIE-16GB", 7.0), ("Quadro GV100", 7.0)]
MODERN = [("NVIDIA GeForce GTX 1650", 7.5), ("NVIDIA GeForce GTX 1660 SUPER", 7.5),
          ("NVIDIA GeForce MX450", 7.5), ("NVIDIA GeForce RTX 2060", 7.5),
          ("NVIDIA GeForce RTX 2080 Ti", 7.5), ("NVIDIA TITAN RTX", 7.5), ("Quadro RTX 4000", 7.5),
          ("Quadro RTX 5000", 7.5), ("Tesla T4", 7.5), ("NVIDIA T1000 8GB", 7.5),
          ("NVIDIA GeForce RTX 3060", 8.6), ("NVIDIA RTX A5000", 8.6), ("NVIDIA A10G", 8.6),
          ("NVIDIA GeForce RTX 4090", 8.9), ("NVIDIA GeForce RTX 4090 D", 8.9),
          ("NVIDIA GeForce RTX 4070 Laptop GPU", 8.9), ("NVIDIA RTX 2000 Ada Generation", 8.9),
          ("NVIDIA RTX 5000 Ada Generation", 8.9), ("NVIDIA RTX 5000 Ada Generation Laptop GPU", 8.9),
          ("NVIDIA RTX 6000 Ada Generation", 8.9), ("NVIDIA L40S", 8.9)]
BLACKWELL = [("NVIDIA GeForce RTX 5090", 12.0), ("NVIDIA GeForce RTX 5090 D v2", 12.0),
             ("NVIDIA GeForce RTX 5070 Ti", 12.0), ("NVIDIA GeForce RTX 5060", 12.0),
             ("NVIDIA GeForce RTX 5050 Laptop GPU", 12.0),
             ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition", 12.0),
             ("NVIDIA RTX PRO 5000 Blackwell Generation Laptop GPU", 12.0)]

NEO = [("torch", "2.13.0+cu130"), ("torchvision", "0.28.0+cu130")]
NEO_LAUNCH_UTILS = '''
def prepare_environment():
    torch_index_url = os.environ.get("TORCH_INDEX_URL", "https://download.pytorch.org/whl/cu130")
    torch_command = os.environ.get("TORCH_COMMAND", f"pip install torch==2.13.0+cu130 torchvision==0.28.0+cu130 --extra-index-url {torch_index_url}")
'''
CLASSIC_LAUNCH_UTILS = '''
def prepare_environment():
    torch_index_url = os.environ.get('TORCH_INDEX_URL', "https://download.pytorch.org/whl/cu121")
    torch_command = os.environ.get('TORCH_COMMAND', f"pip install torch==2.3.1 torchvision==0.18.1 --extra-index-url {torch_index_url}")
'''


def gpus(*items):
    """假的 cc.gpu_info() 结果：items 是 (型号名, 计算能力) 或只有型号名（按名字估）"""
    out = []
    for it in items:
        name, sm = it if isinstance(it, tuple) else (it, None)
        out.append({"name": name, "sm": sm if sm is not None else cc.sm_from_name(name)})
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 换装记录、索引缓存都写到临时目录，别动真的 launcher_data
        for name in ("_FALLBACK_PATH", "_CACHE_PATH"):
            p = patch.object(cc, name, os.path.join(self.tmp, name + ".json"))
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("TORCH_COMMAND", None)

    def set_gpu(self, *items, driver=(13, 0)):
        for target, value in (("gpu_info", gpus(*items)), ("driver_cuda_version", driver)):
            p = patch.object(cc, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)


class NameTableTest(unittest.TestCase):
    """查不到 compute_cap 时按型号名估架构：估出来的档位要和真实的一致"""

    def bucket(self, sm):
        if sm is None:
            return None
        if sm < cc.SM_MIN:
            return "unsupported"
        if sm < cc.SM_MODERN:
            return "legacy"
        return "blackwell" if sm >= cc.SM_BLACKWELL else "modern"

    def test_buckets(self):
        for group in (KEPLER, LEGACY, MODERN, BLACKWELL):
            for name, real in group:
                self.assertEqual(self.bucket(cc.sm_from_name(name)), self.bucket(real), name)

    def test_lookalikes(self):
        # 名字像 50 系 / 40 系 / 20 系，实际不是
        self.assertEqual(cc.sm_from_name("Quadro RTX 5000"), 7.5)
        self.assertEqual(cc.sm_from_name("NVIDIA RTX 5000 Ada Generation"), 8.9)
        self.assertEqual(cc.sm_from_name("Quadro RTX 4000"), 7.5)
        self.assertEqual(cc.sm_from_name("NVIDIA RTX 2000 Ada Generation"), 8.9)
        self.assertEqual(cc.sm_from_name("NVIDIA GeForce GTX 1650"), 7.5)   # 不是 GTX 10 系
        self.assertEqual(cc.sm_from_name("NVIDIA TITAN RTX"), 7.5)         # 不是 TITAN X

    def test_unknown(self):
        for name in ("AMD Radeon RX 9070 XT", "Intel(R) Arc(TM) A770 Graphics", "", None):
            self.assertIsNone(cc.sm_from_name(name), name)

    def test_model_catalog_shares_table(self):
        self.assertEqual(mc.sm_from_name("NVIDIA RTX 5000 Ada Generation"), 8.9)
        self.assertFalse(mc.make_ctx("neo", sm=mc.sm_from_name("Quadro RTX 5000"))["blackwell"])
        self.assertTrue(mc.make_ctx("neo", sm=mc.sm_from_name("NVIDIA GeForce RTX 5080"))["blackwell"])


class ClassifyTest(_Base):
    def test_by_compute_cap(self):
        for group, legacy, blackwell, unsupported in ((KEPLER, False, False, True), (LEGACY, True, False, False),
                                                      (MODERN, False, False, False),
                                                      (BLACKWELL, False, True, False)):
            for item in group:
                with patch.object(cc, "gpu_info", return_value=gpus(item)):
                    self.assertEqual((cc.is_legacy_gpu(), cc.is_blackwell_gpu(), cc.is_unsupported_gpu()),
                                     (legacy, blackwell, unsupported), item[0])

    def test_compute_cap_beats_name(self):
        # 型号名认不出也没关系，有计算能力就按它来
        with patch.object(cc, "gpu_info", return_value=gpus(("NVIDIA Some Future Card", 6.1))):
            self.assertTrue(cc.is_legacy_gpu())
        with patch.object(cc, "gpu_info", return_value=gpus(("NVIDIA Some Future Card", 12.0))):
            self.assertTrue(cc.is_blackwell_gpu())

    def test_unknown_gpu_is_left_alone(self):
        with patch.object(cc, "gpu_info", return_value=gpus("NVIDIA Mystery")):
            self.assertEqual((cc.is_legacy_gpu(), cc.is_blackwell_gpu(), cc.is_unsupported_gpu()),
                             (False, False, False))
            self.assertIsNone(cc.gpu_tag_mismatch("cu130"))
        with patch.object(cc, "gpu_info", return_value=[]):
            self.assertFalse(cc.is_unsupported_gpu())

    def test_mixed_cards(self):
        with patch.object(cc, "gpu_info", return_value=gpus(("GTX 1080 Ti", 6.1), ("RTX 3090", 8.6))):
            self.assertTrue(cc.is_legacy_gpu())        # cu126 两张都能跑
        with patch.object(cc, "gpu_info", return_value=gpus(("GTX 1080 Ti", 6.1), ("RTX 5090", 12.0))):
            self.assertFalse(cc.is_legacy_gpu())       # 没有两张都能跑的版本：按新卡来
            self.assertTrue(cc.is_blackwell_gpu())
        with patch.object(cc, "gpu_info", return_value=gpus(("GTX 780", 3.5), ("RTX 3060", 8.6))):
            self.assertFalse(cc.is_unsupported_gpu())  # 有一张能用就不算

    def test_tag_mismatch(self):
        cases = [(("GTX 1080 Ti", 6.1), "cu130", "legacy"), (("GTX 1080 Ti", 6.1), "cu128", "legacy"),
                 (("GTX 1080 Ti", 6.1), "cu126", None), (("GTX 1080 Ti", 6.1), "cu121", None),
                 (("RTX 5090", 12.0), "cu126", "blackwell"), (("RTX 5090", 12.0), "cu121", "blackwell"),
                 (("RTX 5090", 12.0), "cu128", None), (("RTX 5090", 12.0), "cu130", None),
                 (("RTX 4090", 8.9), "cu126", None), (("RTX 4090", 8.9), "cu130", None),
                 (("RTX 2060", 7.5), "cu130", None), (("GTX 780", 3.5), "cu126", "unsupported"),
                 (("RTX 5090", 12.0), "cpu", None)]
        for gpu, tag, want in cases:
            with patch.object(cc, "gpu_info", return_value=gpus(gpu)):
                self.assertEqual(cc.gpu_tag_mismatch(tag), want, (gpu, tag))


class EffectiveTagTest(_Base):
    def test_legacy_always_cu126(self):
        for item in LEGACY:
            for driver in ((13, 0), (12, 6), None):
                with patch.object(cc, "gpu_info", return_value=gpus(item)):
                    self.assertEqual(cc.effective_tag("cu130", driver or (13, 0)), "cu126", item[0])
                    self.assertEqual(cc.candidate_tags("cu130", driver or (13, 0)), ["cu126"], item[0])

    def test_modern_follows_driver(self):
        for item in MODERN:
            with patch.object(cc, "gpu_info", return_value=gpus(item)):
                self.assertEqual(cc.effective_tag("cu130", (13, 2)), "cu130", item[0])
                self.assertEqual(cc.candidate_tags("cu130", (12, 6)), ["cu128", "cu126", "cu124", "cu121"], item[0])

    def test_blackwell_never_below_cu128(self):
        for item in BLACKWELL:
            with patch.object(cc, "gpu_info", return_value=gpus(item)):
                self.assertEqual(cc.effective_tag("cu130", (13, 2)), "cu130", item[0])
                self.assertEqual(cc.candidate_tags("cu130", (12, 8)), ["cu128"], item[0])

    def test_gpu_info_parsing(self):
        class R:
            def __init__(self, out, rc=0):
                self.stdout, self.returncode = out.encode(), rc

        def run_ok(cmd, **kw):
            return R("NVIDIA GeForce GTX 980M, 5.2\nNVIDIA GeForce RTX 3060, 8.6\n")

        def run_old_smi(cmd, **kw):   # 老驱动的 nvidia-smi 不认 compute_cap
            if "compute_cap" in cmd[1]:
                return R("Field \"compute_cap\" is not a valid field to query.\n", 2)
            return R("GeForce GTX 780 Ti\n")

        def run_broken(cmd, **kw):
            return R("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n", 9)

        for run, wmi, want, ttl in (
                (run_ok, [], [("NVIDIA GeForce GTX 980M", 5.2), ("NVIDIA GeForce RTX 3060", 8.6)], 600),
                (run_old_smi, [], [("GeForce GTX 780 Ti", 3.5)], 600),
                (run_broken, ["NVIDIA GeForce GTX 1060 6GB"], [("NVIDIA GeForce GTX 1060 6GB", 6.1)], 30)):
            cache = {"t": 0.0, "v": [], "ttl": 600}
            with patch.object(cc, "_gpu_cache", cache), patch.object(cc, "find_nvidia_smi", return_value="smi"), \
                    patch.object(cc.subprocess, "run", side_effect=run), \
                    patch.object(cc, "_wmi_nvidia_names", return_value=wmi):
                self.assertEqual([(g["name"], g["sm"]) for g in cc.gpu_info()], want)
                self.assertEqual(cache["ttl"], ttl)   # 出错的结果不能缓存 10 分钟


class DriverIssueTest(_Base):
    def ids(self, gpu, tag, driver=(13, 0), **kw):
        with patch.object(cc, "gpu_info", return_value=gpus(gpu)), \
                patch.object(cc, "driver_cuda_version", return_value=driver):
            issue = cc.driver_issue(tag, "这个 WebUI ", True, **kw)
            return issue and (issue["id"], issue["text"])

    def test_ids(self):
        self.assertEqual(self.ids(("Quadro P4000", 6.1), "cu130")[0], "gpu_too_old")
        self.assertEqual(self.ids(("GTX 780", 3.5), "cu130", (11, 4))[0], "gpu_unsupported")
        self.assertEqual(self.ids(("RTX 4090", 8.9), "cu130", (12, 6))[0], "driver_outdated")
        self.assertIsNone(self.ids(("RTX 4090", 8.9), "cu130", (13, 2)))
        self.assertIsNone(self.ids(("RTX 5090", 12.0), "cu130", (13, 2)))
        self.assertIsNone(self.ids(("GTX 1080 Ti", 6.1), "cu126", (13, 0)))

    def test_kepler_is_not_told_to_upgrade_driver(self):
        _id, text = self.ids(("GTX 780", 3.5), "cu130", (11, 4))
        self.assertNotIn("580", text)
        self.assertIn("CPU", text)

    def test_blackwell_with_old_torch(self):
        _id, text = self.ids(("RTX 5090", 12.0), "cu126")
        self.assertEqual(_id, "gpu_too_new")
        self.assertIn("自动换装", text)
        _id, text = self.ids(("RTX 5090", 12.0), "cu121", gpu_fix=False)          # Forge Classic
        self.assertIn("Forge Neo", text)
        _id, text = self.ids(("RTX 5090", 12.0), "cu126", gpu_fix=False, redeploy_fixes=True)   # ComfyUI
        self.assertIn("开始部署", text)

    def test_driver_number_matches_cuda_major(self):
        self.assertIn("528", self.ids(("GTX 1080 Ti", 6.1), "cu126", (11, 4))[1])
        self.assertIn("580", self.ids(("RTX 3060", 8.6), "cu130", (12, 6))[1])


class _ForgeRoot(_Base):
    """假的 WebUI 目录：只有 launch_utils.py 和 torch 的 dist-info"""

    def make_root(self, launch_utils=NEO_LAUNCH_UTILS, installed=None):
        root = os.path.join(self.tmp, "webui")
        os.makedirs(os.path.join(root, "modules"))
        with open(os.path.join(root, "modules", "launch_utils.py"), "w", encoding="utf-8") as f:
            f.write(launch_utils)
        sp = os.path.join(root, "python", "Lib", "site-packages")
        os.makedirs(sp)
        if installed:
            os.makedirs(os.path.join(sp, f"torch-{installed}.dist-info"))
        return root

    def overrides(self, root, resolved=None):
        """torch_compat_overrides 的 (覆盖, 是否重装, 提示)；索引查询用 resolved 顶替"""
        notes = []
        with patch.object(cc, "resolve_compat", return_value=resolved or (None, "cu126", "x")), \
                patch.object(cc, "python_tag", return_value="cp313"):
            out, reinstall = cm.torch_compat_overrides({"mirror_mode": "never"}, root, notes)
        return out, reinstall, "\n".join(notes)


class ForgeOverridesTest(_ForgeRoot):
    CU126 = ([("torch", "2.13.0+cu126"), ("torchvision", "0.28.0+cu126")], "cu126",
             "https://download.pytorch.org/whl/cu126")

    def test_modern_card_new_driver_untouched(self):
        self.set_gpu(("RTX 4090", 8.9), driver=(13, 2))
        self.assertEqual(self.overrides(self.make_root(installed="2.13.0+cu130")), ({}, False, ""))

    def test_missed_legacy_cards_now_get_cu126(self):
        # 以前这些卡在 580 驱动下照装 cu130
        for item in (("Quadro P4000", 6.1), ("NVIDIA GeForce GTX 980M", 5.2), ("Tesla V100-PCIE-16GB", 7.0),
                     ("NVIDIA GeForce GTX 750 Ti", 5.0), ("NVIDIA GeForce MX350", 6.1)):
            with patch.object(cc, "gpu_info", return_value=gpus(item)), \
                    patch.object(cc, "driver_cuda_version", return_value=(13, 0)):
                root = os.path.join(self.tmp, "webui")
                shutil.rmtree(root, ignore_errors=True)
                out, reinstall, notes = self.overrides(self.make_root(installed="2.13.0+cu130"), self.CU126)
                self.assertIn("torch==2.13.0+cu126", out.get("TORCH_COMMAND", ""), item[0])
                self.assertTrue(reinstall, item[0])
                self.assertIn("架构较老", notes)
                self.assertEqual(cc._fb_load(), {})   # 老卡换 cu126 是永久的，不记换装

    def test_blackwell_with_stale_torch_reinstalls_default(self):
        self.set_gpu(("RTX 5090", 12.0), driver=(13, 2))
        out, reinstall, notes = self.overrides(self.make_root(installed="2.13.0+cu126"))
        self.assertEqual((out, reinstall), ({}, True))
        self.assertIn("不支持这张显卡", notes)

    def test_classic_on_blackwell_is_explained_not_reinstalled(self):
        self.set_gpu(("RTX 5090", 12.0), driver=(13, 2))
        out, reinstall, notes = self.overrides(self.make_root(CLASSIC_LAUNCH_UTILS, installed="2.3.1+cu121"))
        self.assertEqual((out, reinstall), ({}, False))
        self.assertIn("Forge Neo", notes)

    def test_kepler_message(self):
        self.set_gpu(("GTX 780 Ti", 3.5), driver=(11, 4))
        out, reinstall, notes = self.overrides(self.make_root())
        self.assertEqual((out, reinstall), ({}, False))
        self.assertIn("太老了", notes)
        self.assertNotIn("580", notes)

    def test_driver_fallback_then_restore(self):
        root = self.make_root(installed="2.13.0+cu126")
        with patch.object(cc, "gpu_info", return_value=gpus(("RTX 4090", 8.9))):
            # 驱动旧：换装 cu126，并记下来
            with patch.object(cc, "driver_cuda_version", return_value=(12, 6)):
                out, reinstall, notes = self.overrides(root, self.CU126)
                self.assertIn("2.13.0+cu126", out["TORCH_COMMAND"])
                self.assertFalse(reinstall)   # 已经是这一版
                self.assertIn("升级", notes)
                self.assertFalse(cc.fallback_restorable(root, "2.13.0+cu126", "cu130"))   # 驱动还没升
            # 驱动升上去：这次启动换回默认版本
            with patch.object(cc, "driver_cuda_version", return_value=(13, 2)):
                issue = pm._driver_issue_forge(root, {})
                self.assertEqual(issue and issue["id"], "torch_restore")
                out, reinstall, notes = self.overrides(root)
                self.assertEqual((out, reinstall), ({}, True))
                self.assertIn("换回默认版本", notes)
                # 用户选了「保持现状」：不再提示、不再重装
                cc.dismiss_fallback(root)
                self.assertIsNone(pm._driver_issue_forge(root, {}))
                self.assertEqual(self.overrides(root)[:2], ({}, False))

    def test_restore_needs_known_driver_and_same_torch(self):
        root = self.make_root(installed="2.13.0+cu126")
        cc.note_fallback(root, "2.13.0+cu126", "cu130")
        with patch.object(cc, "gpu_info", return_value=gpus(("RTX 4090", 8.9))):
            self.assertTrue(cc.fallback_restorable(root, "2.13.0+cu126", "cu130", (13, 0)))
            with patch.object(cc, "driver_cuda_version", return_value=None):   # 读不到驱动：不动
                self.assertFalse(cc.fallback_restorable(root, "2.13.0+cu126", "cu130"))
            self.assertFalse(cc.fallback_restorable(root, "2.12.1+cu126", "cu130", (13, 0)))   # 用户自己换过
            self.assertFalse(cc.fallback_restorable(os.path.join(self.tmp, "other"), "2.13.0+cu126", "cu130", (13, 0)))

    def test_user_torch_command_wins(self):
        self.set_gpu(("Quadro P4000", 6.1), driver=(13, 0))
        with patch.dict(os.environ, {"TORCH_COMMAND": "pip install torch"}):
            self.assertEqual(cm.torch_compat_overrides({}, self.make_root(), []), ({}, False))


class LaunchPrecheckTest(_ForgeRoot):
    def issue(self, root):
        return pm._driver_issue_forge(root, {})

    def test_classic_on_blackwell(self):
        self.set_gpu(("RTX 5090", 12.0), driver=(13, 2))
        issue = self.issue(self.make_root(CLASSIC_LAUNCH_UTILS))
        self.assertEqual(issue["id"], "gpu_too_new")
        self.assertIn("Forge Neo", issue["text"])
        self.assertNotIn("自动换装", issue["text"])

    def test_neo_on_blackwell_with_stale_torch(self):
        self.set_gpu(("RTX 5090", 12.0), driver=(13, 2))
        issue = self.issue(self.make_root(installed="2.13.0+cu126"))
        self.assertEqual(issue["id"], "gpu_too_new")
        self.assertIn("自动换装", issue["text"])

    def test_no_kernel_hint_names_the_direction(self):
        text = "RuntimeError: CUDA error: no kernel image is available for execution on the device"
        with patch.object(cc, "gpu_info", return_value=gpus(("RTX 5090", 12.0))):
            self.assertIn("RTX 50 系", pm._fatal_hint_for(text))
        with patch.object(cc, "gpu_info", return_value=gpus(("Quadro P4000", 6.1))):
            hint = pm._fatal_hint_for(text)
            self.assertIn("架构较老", hint)
            self.assertIn("cu126", hint)
        with patch.object(cc, "gpu_info", return_value=[]):
            self.assertIn("两种情况", pm._fatal_hint_for(text))


class DeployTorchTest(_ForgeRoot):
    def test_bootstrap_parses_both_webuis(self):
        self.assertEqual(tb._parse_torch_spec(self.make_root()), (NEO, "cu130"))
        shutil.rmtree(os.path.join(self.tmp, "webui"))
        self.assertEqual(tb._parse_torch_spec(self.make_root(CLASSIC_LAUNCH_UTILS)),
                         ([("torch", "2.3.1"), ("torchvision", "0.18.1")], "cu121"))

    def test_trainer_recommendation(self):
        for item in LEGACY:
            self.assertEqual(lt.recommend_torch(gpus(item)), "cu126", item[0])
            self.assertEqual(lt.recommend_torch([item[0]]), "cu126", item[0])   # 只有型号名也行
        for item in MODERN + BLACKWELL:
            self.assertEqual(lt.recommend_torch(gpus(item)), "cu128", item[0])
        self.assertEqual(lt.recommend_torch([]), "cu128")

    def test_trainer_warning(self):
        self.set_gpu(("RTX 4090", 8.9), driver=(11, 8))
        self.assertIn("528", lt.torch_warning())
        with patch.object(cc, "driver_cuda_version", return_value=(13, 2)):
            self.assertEqual(lt.torch_warning(), "")


class MultiGpuTest(_ForgeRoot):
    """多显卡：实例指定了用哪张卡（gpu_device_id → --device-id / --cuda-device），就只按那张卡选 torch"""

    OLD_NEW = [("NVIDIA GeForce GTX 1080 Ti", 6.1), ("NVIDIA GeForce RTX 3090", 8.6)]     # nvidia-smi 的顺序
    OLD_50 = [("NVIDIA GeForce GTX 1080 Ti", 6.1), ("NVIDIA GeForce RTX 5090", 12.0)]
    CU126 = ForgeOverridesTest.CU126

    def test_cuda_order_is_fastest_first(self):
        info = gpus(*self.OLD_NEW)
        with patch.dict(os.environ, {"CUDA_DEVICE_ORDER": ""}):
            self.assertEqual([g["name"] for g in cc.cuda_order(info)],
                             ["NVIDIA GeForce RTX 3090", "NVIDIA GeForce GTX 1080 Ti"])
        with patch.dict(os.environ, {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"}):     # 用户让 CUDA 按插槽排：和 nvidia-smi 一样
            self.assertEqual([g["name"] for g in cc.cuda_order(info)],
                             ["NVIDIA GeForce GTX 1080 Ti", "NVIDIA GeForce RTX 3090"])

    def flags(self, device):
        with cc.device_scope(device):
            return cc.is_legacy_gpu(), cc.is_blackwell_gpu(), cc.gpu_names()

    def test_selected_card_decides(self):
        with patch.dict(os.environ, {"CUDA_DEVICE_ORDER": ""}):
            with patch.object(cc, "gpu_info", return_value=gpus(*self.OLD_NEW)):
                self.assertEqual(self.flags("0"), (False, False, ["NVIDIA GeForce RTX 3090"]))   # 0 号 = 最快的
                self.assertEqual(self.flags("1"), (True, False, ["NVIDIA GeForce GTX 1080 Ti"]))
                self.assertEqual(self.flags("0,1")[0], True)                 # 两张都用：cu126 两张都能跑
                for unknown in ("", None, "auto", "7"):                      # 没指定 / 填了别的 / 编号不存在：看全部
                    self.assertEqual(len(self.flags(unknown)[2]), 2, unknown)
                with cc.device_scope("0"):
                    self.assertEqual(cc.effective_tag("cu130", (13, 0)), "cu130")   # 以前：cu126
                with cc.device_scope("1"):
                    self.assertEqual(cc.effective_tag("cu130", (13, 0)), "cu126")
            with patch.object(cc, "gpu_info", return_value=gpus(*self.OLD_50)):
                self.assertEqual(self.flags("0")[:2], (False, True))
                self.assertEqual(self.flags("1")[:2], (True, False))         # 以前：因为机器里有 50 系按 cu130 选
                with cc.device_scope("1"):
                    self.assertEqual(cc.effective_tag("cu130", (13, 0)), "cu126")
                    self.assertEqual(cc.gpu_tag_mismatch("cu130"), "legacy")
        self.assertIsNone(getattr(cc._scope, "ids", None))                   # with 块出来之后恢复原样

    def test_single_card_ignores_device_id(self):
        with patch.object(cc, "gpu_info", return_value=gpus(("GTX 1080 Ti", 6.1))):
            self.assertEqual(self.flags("3")[0], True)

    def test_forge_launch_uses_instance_card(self):
        root = self.make_root(installed="2.13.0+cu130")
        with patch.dict(os.environ, {"CUDA_DEVICE_ORDER": ""}), \
                patch.object(cc, "gpu_info", return_value=gpus(*self.OLD_NEW)), \
                patch.object(cc, "driver_cuda_version", return_value=(13, 0)), \
                patch.object(cc, "resolve_compat", return_value=self.CU126), \
                patch.object(cc, "python_tag", return_value="cp313"):
            out, reinstall = cm.torch_compat_overrides({"mirror_mode": "never", "gpu_device_id": "0"}, root, [])
            self.assertEqual((out, reinstall), ({}, False))                  # 用 3090：默认的 cu130 不动
            out, reinstall = cm.torch_compat_overrides({"mirror_mode": "never", "gpu_device_id": "1"}, root, [])
            self.assertIn("cu126", out["TORCH_COMMAND"])                     # 用 1080 Ti：换 cu126
            self.assertTrue(reinstall)
            self.assertIsNone(pm._driver_issue_forge(root, {"gpu_device_id": "0"}))
            self.assertEqual(pm._driver_issue_forge(root, {"gpu_device_id": "1"})["id"], "gpu_too_old")


class ReviewFixesTest(_Base):
    """外部代码审查查出来的几处"""

    def test_gpu_name_with_comma(self):
        class R:
            returncode = 0
            stdout = b'"NVIDIA Foo, Bar", 8.6\nNVIDIA GeForce RTX 3060, 8.6\nNVIDIA Odd Card, [N/A]\n'

        cache = {"t": 0.0, "v": [], "ttl": 600}
        with patch.object(cc, "_gpu_cache", cache), patch.object(cc, "find_nvidia_smi", return_value="smi"), \
                patch.object(cc.subprocess, "run", return_value=R()):
            self.assertEqual([(g["name"], g["sm"]) for g in cc.gpu_info()],
                             [("NVIDIA Foo, Bar", 8.6), ("NVIDIA GeForce RTX 3060", 8.6), ("NVIDIA Odd Card", None)])

    def test_blackwell_never_gets_cu118(self):
        # 50 系 + 只到 CUDA 11 的驱动：以前过滤完又把 cu118 放了回去
        self.set_gpu(("RTX 5090", 12.0), driver=(11, 8))
        self.assertEqual(cc.candidate_tags("cu130", (11, 8)), [])
        called = []
        got = cc.resolve_compat([NEO], "cu130", lambda t: called.append(t) or ["x"], "cp313", (11, 8))
        self.assertEqual((got[0], called), (None, []))                       # 不去查任何索引，直接按「没有可用版本」
        import webview_api as wa
        logs = []
        self.assertEqual(wa._pick_comfy_torch_tag(logs.append), "cu128")     # ComfyUI：也不装 cu118
        self.assertIn("580", "".join(logs))

    def test_deploy_respects_user_torch_command(self):
        called = []
        with patch.dict(os.environ, {"TORCH_COMMAND": "pip install torch==2.9.1"}), \
                patch.object(tb, "_ensure_torch", lambda *a, **k: called.append(1) or True):
            logs = []
            self.assertFalse(tb.ensure_torch(self.tmp, {}, logs.append))
        self.assertEqual(called, [])
        self.assertIn("TORCH_COMMAND", "".join(logs))

    def test_fallback_file_is_replaced_atomically(self):
        root = os.path.join(self.tmp, "w")
        cc.note_fallback(root, "2.13.0+cu126", "cu130")
        cc.dismiss_fallback(root)
        folder = os.path.dirname(cc._FALLBACK_PATH)
        self.assertEqual([n for n in os.listdir(folder) if n.endswith(".tmp")], [])
        self.assertTrue(cc._fb_load()[cc._fb_key(root)]["dismissed"])
        with patch.object(cc.json, "dump", side_effect=OSError("disk full")):   # 写失败：原来的记录还在
            cc.note_fallback(root, "2.11.0+cu128", "cu130")
        self.assertEqual(cc._fb_load()[cc._fb_key(root)]["torch"], "2.13.0+cu126")
        self.assertEqual([n for n in os.listdir(folder) if n.endswith(".tmp")], [])


class ComfyAndPrecheckTest(_Base):
    """ComfyUI 选 tag、已装 torch 的核对、部署预检（webview_api）"""

    @classmethod
    def setUpClass(cls):
        import webview_api
        cls.wa = webview_api

    def pick(self, gpu, driver):
        logs = []
        with patch.object(cc, "gpu_info", return_value=gpus(gpu)), \
                patch.object(cc, "driver_cuda_version", return_value=driver):
            return self.wa._pick_comfy_torch_tag(logs.append), "".join(logs)

    def test_pick_tag(self):
        for item in LEGACY:   # 驱动再新也是 cu126
            self.assertEqual(self.pick(item, (13, 0))[0], "cu126", item[0])
            self.assertEqual(self.pick(item, (12, 6))[0], "cu126", item[0])
        for item in MODERN + BLACKWELL:
            self.assertEqual(self.pick(item, (13, 2))[0], "cu130", item[0])
            self.assertEqual(self.pick(item, (12, 9))[0], "cu128", item[0])
        tag, log = self.pick(("GTX 780 Ti", 3.5), (11, 4))
        self.assertIn("太老了", log)

    def env_problem(self, gpu, installed, driver=(13, 2), venv=False):
        base = os.path.join(self.tmp, "env")
        shutil.rmtree(base, ignore_errors=True)
        if venv:
            py = os.path.join(base, "venv", "Scripts", "python.exe")
            sp = os.path.join(base, "venv", "Lib", "site-packages")
        else:
            py = os.path.join(base, "python", "python.exe")
            sp = os.path.join(base, "python", "Lib", "site-packages")
        os.makedirs(os.path.join(sp, f"torch-{installed}.dist-info"))
        os.makedirs(os.path.dirname(py), exist_ok=True)
        with patch.object(cc, "gpu_info", return_value=gpus(gpu)), \
                patch.object(cc, "driver_cuda_version", return_value=driver):
            return self.wa._env_torch_problem(py)

    def test_installed_torch_is_checked_against_the_card(self):
        # 这几种 torch.cuda.is_available() 都是 True，以前部署会「已安装，跳过」
        self.assertIn("老架构", self.env_problem(("Quadro P4000", 6.1), "2.13.0+cu130"))
        self.assertIn("老架构", self.env_problem(("GTX 1080 Ti", 6.1), "2.11.0+cu128", venv=True))
        self.assertIn("50 系", self.env_problem(("RTX 5090", 12.0), "2.13.0+cu126"))
        self.assertIn("驱动", self.env_problem(("RTX 3060", 8.6), "2.13.0+cu130", driver=(12, 6)))
        for gpu, installed in ((("RTX 4090", 8.9), "2.13.0+cu130"), (("RTX 4090", 8.9), "2.11.0+cu128"),
                               (("RTX 5090", 12.0), "2.11.0+cu128"), (("GTX 1080 Ti", 6.1), "2.13.0+cu126"),
                               (("RTX 2060", 7.5), "2.13.0+cu130"), (("RTX 4090", 8.9), "2.13.0+cpu")):
            self.assertEqual(self.env_problem(gpu, installed), "", (gpu, installed))

    def precheck_ids(self, gpu, branch, driver=(13, 2), issues=()):
        with patch.object(cc, "gpu_info", return_value=gpus(gpu)), \
                patch.object(cc, "driver_cuda_version", return_value=driver):
            return [i["id"] for i in self.wa._nvidia_precheck_issues(branch, list(issues))]

    def test_deploy_precheck(self):
        old = [{"level": "warn", "id": "old_driver", "text": "x"}]
        self.assertEqual(self.precheck_ids(("RTX 5090", 12.0), "classic"), ["classic_rtx50"])
        self.assertEqual(self.precheck_ids(("RTX 5090", 12.0), "neo2"), [])
        self.assertEqual(self.precheck_ids(("RTX 4090", 8.9), "classic"), [])
        self.assertEqual(self.precheck_ids(("RTX 4090", 8.9), "neo2", (12, 6), old), ["old_driver"])
        # 老卡：说明会自动装 cu126；驱动带得动 CUDA 12 就别再提「驱动太旧」（那是 cu130 的要求）
        self.assertEqual(self.precheck_ids(("Quadro P4000", 6.1), "neo2", (12, 6), old), ["gpu_legacy"])
        self.assertEqual(self.precheck_ids(("Quadro P4000", 6.1), "comfyui", (11, 4), old),
                         ["old_driver", "gpu_legacy"])
        self.assertEqual(self.precheck_ids(("GTX 1080 Ti", 6.1), "classic", (13, 0)), [])
        # Kepler：没有新驱动可升，只说显卡不支持
        self.assertEqual(self.precheck_ids(("GTX 780 Ti", 3.5), "neo2", (11, 4), old), ["gpu_unsupported"])


if __name__ == "__main__":
    unittest.main()
