"""
torch → triton-windows 版本对应：按官方 torch 锁定的 triton 版本，不再用 3.(m-4) 公式
（torch 2.13 以前会被算成不存在的 triton 3.9，部署时报 No matching distribution）。
运行：python -m unittest tests.test_triton_spec
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import unittest

import webview_api as wa  # noqa: E402


class TritonSpecTest(unittest.TestCase):
    def test_known_versions(self):
        cases = {"2.6.0": 2, "2.9.1+cu128": 5, "2.10.0": 6, "2.11.0": 6, "2.12.0": 7, "2.13.0+cu130": 7, "2.14.1": 8}
        for torch_ver, minor in cases.items():
            self.assertEqual(wa._triton_spec_for_torch(torch_ver), f"triton-windows>=3.{minor},<3.{minor + 1}", torch_ver)

    def test_unknown_versions_skip(self):
        for torch_ver in ("2.5.1", "2.99.0", "3.0.0", "", None, "abc"):
            self.assertIsNone(wa._triton_spec_for_torch(torch_ver), torch_ver)


if __name__ == "__main__":
    unittest.main()
