"""
venv 版本错位与「配置冲突」误报的回归测试。

背景（用户反馈的「闪退」）：
  1) 启动前自动修复 venv\\pyvenv.cfg 的 home 时不比较 Python 大版本，把 3.12 建的 venv 指到了
     3.13 的便携 Python，venv 里 cp312 的 numpy / torch 整批加载失败 →「检测不到显卡」秒退；
  2) 启动器直接运行 webui.bat，webui-user.bat 根本不会被执行，却拿它报「配置冲突」，每次都弹窗。
用合成的假目录跑（python.exe 是空文件，版本靠同目录的 python3XX.dll 判断），不需要真的 Python。
运行：python -m unittest tests.test_venv_and_overrides
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import tempfile
import unittest

import config_manager as cm  # noqa: E402
import process_manager as pm  # noqa: E402

NEO_WEBUI_BAT = "@echo off\r\n\r\nif exist webui.settings.bat (\r\n    call webui.settings.bat\r\n)\r\n\r\nif not defined PYTHON (set PYTHON=python)\r\n"


def _touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _python(base, minor):
    """假的 Python 3.minor：空的 python.exe + python3XX.dll"""
    _touch(os.path.join(base, "python.exe"))
    _touch(os.path.join(base, f"python3{minor}.dll"))
    _touch(os.path.join(base, "python3.dll"))  # 稳定 ABI 的 dll，不能被当成版本号
    return os.path.join(base, "python.exe")


def _venv(root, home, version):
    _touch(os.path.join(root, "venv", "pyvenv.cfg"),
           f"home = {home}\ninclude-system-site-packages = false\nversion = {version}\n")


class VenvVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "webui")
        os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_version_from_dll(self):
        self.assertEqual(cm.python_version_of(_python(os.path.join(self.tmp, "py312"), 12)), "3.12")

    def test_healthy_venv_no_issue(self):
        py = _python(os.path.join(self.root, "python"), 12)
        _venv(self.root, os.path.dirname(py), "3.12.10")
        self.assertIsNone(cm.venv_version_issue(self.root, py))

    def test_home_points_to_other_major_version(self):
        """被旧版启动器改坏：pyvenv.cfg 记 3.12，home 却指向 3.13"""
        py313 = _python(os.path.join(self.root, "python"), 13)
        _venv(self.root, os.path.dirname(py313), "3.12.10")
        text = cm.venv_version_issue(self.root, py313)
        self.assertIn("3.12", text)
        self.assertIn("3.13", text)

    def test_home_still_valid_but_bundled_differs(self):
        """venv 自己的 home（3.12）还在，只是整合包自带的是 3.13：venv 能正常用，不报警"""
        py312 = _python(os.path.join(self.tmp, "sys312"), 12)
        py313 = _python(os.path.join(self.root, "python"), 13)
        _venv(self.root, os.path.dirname(py312), "3.12.10")
        self.assertIsNone(cm.venv_version_issue(self.root, py313))

    def test_moved_install_same_version_is_fixed_silently(self):
        """整个目录搬过家：home 失效但版本一致 → 不报警，并且自动改 home"""
        py = _python(os.path.join(self.root, "python"), 12)
        _venv(self.root, r"D:\old\place\python", "3.12.10")
        self.assertIsNone(cm.venv_version_issue(self.root, py))
        self.assertTrue(cm.sync_venv_pyvenv_cfg(self.root, py))
        with open(os.path.join(self.root, "venv", "pyvenv.cfg"), encoding="utf-8") as f:
            self.assertIn(f"home = {os.path.dirname(py)}", f.read())

    def test_sync_refuses_to_cross_major_version(self):
        """home 失效且只能找到 3.13：不能改 home（改了就是闪退的根因），并报警"""
        py313 = _python(os.path.join(self.root, "python"), 13)
        _venv(self.root, r"D:\old\place\python", "3.12.10")
        logs = []
        self.assertFalse(cm.sync_venv_pyvenv_cfg(self.root, py313, logs.append))
        with open(os.path.join(self.root, "venv", "pyvenv.cfg"), encoding="utf-8") as f:
            self.assertIn(r"home = D:\old\place\python", f.read())
        self.assertTrue(logs and "版本不同" in logs[0])
        self.assertIsNotNone(cm.venv_version_issue(self.root, py313))


class OverrideFilesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        _touch(os.path.join(self.root, "webui.bat"), NEO_WEBUI_BAT)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_webui_user_bat_is_not_executed(self):
        """Neo 的 webui.bat 不调用 webui-user.bat：里面写什么都不该报「配置冲突」，也不该被改"""
        user = os.path.join(self.root, "webui-user.bat")
        _touch(user, "set PYTHON=C:\\x\\python.exe\nset VENV_DIR=-\ncall webui.bat\n")
        self.assertEqual(pm._find_webui_user_bat_overrides(self.root), [])
        pm._clear_webui_user_bat_overrides(self.root)
        with open(user, encoding="utf-8") as f:
            self.assertIn("set VENV_DIR=-", f.read())

    def test_webui_settings_bat_is_checked(self):
        """webui.settings.bat 会被 webui.bat 执行：写死的变量要报，清空只动它"""
        settings = os.path.join(self.root, "webui.settings.bat")
        _touch(settings, "set VENV_DIR=-\nREM set PYTHON=ignored\n")
        hits = pm._find_webui_user_bat_overrides(self.root)
        self.assertEqual(hits, [("webui.settings.bat", "VENV_DIR", "-", 1)])
        pm._clear_webui_user_bat_overrides(self.root)
        with open(settings, encoding="utf-8") as f:
            self.assertIn("set VENV_DIR=\n", f.read())
        self.assertTrue(os.path.exists(settings + ".bak"))

    def test_custom_webui_bat_calling_user_bat(self):
        """改过的整合包 webui.bat 确实 call 了 webui-user.bat：这时才检查它"""
        _touch(os.path.join(self.root, "webui.bat"), "@echo off\ncall webui-user.bat\n")
        _touch(os.path.join(self.root, "webui-user.bat"), "set GIT=C:\\git\\git.exe\n")
        self.assertEqual(pm._find_webui_user_bat_overrides(self.root),
                         [("webui-user.bat", "GIT", "C:\\git\\git.exe", 1)])


if __name__ == "__main__":
    unittest.main()
