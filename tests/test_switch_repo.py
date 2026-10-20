"""
实例记成 H3、源码却是官方 Neo 的修复：
  - version_manager.switch_repo：换远程仓库 + 检出分支，改过的配置文件保留，失败时远程地址改回去
  - 部署到已有安装时按目录里实际的源码决定登记成普通 Neo 还是 H3
运行：python -m unittest tests.test_switch_repo
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import version_manager as vm  # noqa: E402

GIT = shutil.which("git") or "git"


def _git(cwd, *args):
    subprocess.run([GIT, "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def _run(program, args, cwd, env, timeout):
    return subprocess.run([program] + list(args), cwd=cwd, env=env, capture_output=True).returncode


def _make_repo(path, branch, files):
    os.makedirs(path)
    _git(path, "init", "-q", "-b", branch)
    for rel, text in files.items():
        with open(os.path.join(path, rel), "w", encoding="utf-8") as f:
            f.write(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


class SwitchRepoTest(unittest.TestCase):
    def setUp(self):
        # 本地仓库地址用正斜杠：git config 里的反斜杠会被转义，读回来对不上
        self.tmp = tempfile.mkdtemp().replace("\\", "/")
        self.official = self.tmp + "/official"
        self.h3 = self.tmp + "/h3"
        _make_repo(self.official, "neo", {"webui-user.bat": "set ARGS=\n", "presets.py": "PRESETS = ['sd']\n"})
        _make_repo(self.h3, "minimax-h3", {"webui-user.bat": "set ARGS=\n", "presets.py": "PRESETS = ['sd', 'h3']\n"})
        self.inst = os.path.join(self.tmp, "inst")
        subprocess.run([GIT, "clone", "-q", "-b", "neo", self.official, self.inst], check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_switch_to_h3_keeps_user_settings(self):
        with open(os.path.join(self.inst, "webui-user.bat"), "w", encoding="utf-8") as f:
            f.write("set ARGS=--api\n")
        res = vm.switch_repo(_run, GIT, self.inst, self.h3, "minimax-h3", [""], lambda m: None,
                             keep_files=("webui-user.bat",), backup_root=os.path.join(self.tmp, "bak"))
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(vm.normalize_url(vm.read_remote_url(self.inst)), vm.normalize_url(self.h3))
        with open(os.path.join(self.inst, "presets.py"), encoding="utf-8") as f:
            self.assertIn("h3", f.read())
        with open(os.path.join(self.inst, "webui-user.bat"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "set ARGS=--api\n")
        info = vm.local_info(GIT, self.inst, with_dirty=False)
        self.assertEqual(info.get("branch"), "minimax-h3")

    def test_failed_switch_restores_remote(self):
        before = vm.read_remote_url(self.inst).strip()
        res = vm.switch_repo(_run, GIT, self.inst, self.h3, "no-such-branch", [""], lambda m: None)
        self.assertFalse(res["ok"])
        self.assertEqual(vm.read_remote_url(self.inst).strip(), before)


class DeployReconcileTest(unittest.TestCase):
    """_deploy_reconcile_variant：只测分支判断，不真的联网切换"""

    def setUp(self):
        import webview_api as wa
        self.wa = wa
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, ".git"))
        self.api = mock.MagicMock()
        self.api._deploy_cancel = threading.Event()
        self.api._deploy_cfg = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remote(self, url):
        with open(os.path.join(self.tmp, ".git", "config"), "w", encoding="utf-8") as f:
            f.write(f'[remote "origin"]\n\turl = {url}\n')

    def _call(self, variant):
        return self.wa._deploy_reconcile_variant(self.api, self.tmp, variant, GIT, None, lambda m: None)

    def test_h3_dir_registered_as_h3_even_if_neo_chosen(self):
        self._remote("https://github.com/wangjue520/sd-webui-forge-neo-h3.git")
        self.assertEqual(self._call(""), "h3")
        self.api._ask.assert_not_called()

    def test_h3_chosen_on_official_neo_keep(self):
        self._remote("https://github.com/Haoming02/sd-webui-forge-classic.git")
        self.api._ask.return_value = "keep"
        self.assertEqual(self._call("h3"), "")

    def test_h3_chosen_on_official_neo_cancel(self):
        self._remote("https://github.com/Haoming02/sd-webui-forge-classic.git")
        self.api._ask.return_value = "cancel"
        with self.assertRaises(self.wa._DeployCancelled):
            self._call("h3")

    def test_h3_chosen_on_official_neo_switch(self):
        self._remote("https://github.com/Haoming02/sd-webui-forge-classic.git")
        self.api._ask.return_value = "switch"
        with mock.patch.object(self.wa.vm, "switch_repo", return_value={"ok": True}) as sw, \
                mock.patch.object(self.wa, "_gh_sources", return_value=[""]):
            self.assertEqual(self._call("h3"), "h3")
        self.assertEqual(sw.call_args.args[3], self.wa.H3_REPO)
        self.assertEqual(sw.call_args.args[4], self.wa.H3_BRANCH)

    def test_switch_failure_falls_back_to_plain_neo(self):
        self._remote("https://github.com/Haoming02/sd-webui-forge-classic.git")
        self.api._ask.return_value = "switch"
        with mock.patch.object(self.wa.vm, "switch_repo", return_value={"ok": False, "error": "网络"}), \
                mock.patch.object(self.wa, "_gh_sources", return_value=[""]):
            self.assertEqual(self._call("h3"), "")


if __name__ == "__main__":
    unittest.main()
