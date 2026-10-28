# -*- coding: utf-8 -*-
"""dotnet_config：给基础 Python 写 loadFromRemoteSources 配置"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import dotnet_config  # noqa: E402


@unittest.skipUnless(os.name == "nt", "只在 Windows 上生效")
class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        for name in ("python.exe", "pythonw.exe"):
            open(os.path.join(self.base, name), "wb").close()
        p = mock.patch.object(dotnet_config, "_process_exe", lambda: os.path.join(self.base, "python.exe"))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _read(self, name):
        with open(os.path.join(self.base, name), encoding="utf-8") as f:
            return f.read()

    def test_writes_both(self):
        written = dotnet_config.ensure()
        self.assertEqual(sorted(os.path.basename(p) for p in written), ["python.exe.config", "pythonw.exe.config"])
        self.assertIn('<loadFromRemoteSources enabled="true" />', self._read("pythonw.exe.config"))

    def test_idempotent(self):
        dotnet_config.ensure()
        self.assertEqual(dotnet_config.ensure(), [])

    def test_backs_up_foreign_config(self):
        with open(os.path.join(self.base, "python.exe.config"), "w", encoding="utf-8") as f:
            f.write("<configuration><startup/></configuration>")
        dotnet_config.ensure()
        self.assertEqual(self._read("python.exe.config.bak"), "<configuration><startup/></configuration>")
        self.assertIn("loadFromRemoteSources", self._read("python.exe.config"))

    def test_skips_missing_exe(self):
        os.remove(os.path.join(self.base, "pythonw.exe"))
        written = dotnet_config.ensure()
        self.assertEqual([os.path.basename(p) for p in written], ["python.exe.config"])


if __name__ == "__main__":
    unittest.main()
