"""
界面风格「首次启动跟随系统深 / 浅色」的回归测试（cm.resolve_ui_theme）。

规则：配置里的 ui_theme 还是 "auto"（全新安装）或根本没有这一项时，按系统的深 / 浅色
模式定成 terminal-dark / terminal 并存盘；只判断这一次——已经写着具体风格的配置
（包括用户自己选回浅色的）一律不动。
用临时目录当配置目录，不碰真实的 launcher_config.json。运行：python -m unittest tests.test_ui_theme_auto
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import config_manager as cm  # noqa: E402


class UiThemeAutoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wwy-theme-")
        self.cfg_path = os.path.join(self.tmp, "launcher_config.json")
        p = mock.patch.object(cm, "CONFIG_PATH", self.cfg_path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _dark(self, value):
        p = mock.patch.object(cm, "system_prefers_dark", return_value=value)
        p.start()
        self.addCleanup(p.stop)
        return p

    def _saved(self):
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_default_is_auto(self):
        self.assertEqual(cm.DEFAULT_CONFIG["ui_theme"], "auto")

    def test_first_launch_dark_system(self):
        self._dark(True)
        cfg = cm.load_config()
        self.assertEqual(cfg["ui_theme"], "terminal-dark")
        self.assertEqual(self._saved()["ui_theme"], "terminal-dark")   # 已存盘

    def test_first_launch_light_system(self):
        self._dark(False)
        cfg = cm.load_config()
        self.assertEqual(cfg["ui_theme"], "terminal")
        self.assertEqual(self._saved()["ui_theme"], "terminal")

    def test_only_once(self):
        """首次判成深色后系统改回浅色：不再跟着变。"""
        self._dark(True)
        cm.load_config()
        with mock.patch.object(cm, "system_prefers_dark", return_value=False):
            self.assertEqual(cm.load_config()["ui_theme"], "terminal-dark")

    def test_explicit_choice_untouched(self):
        """已有配置里写着具体风格（老用户的 terminal、自己选的 rhine）：系统是深色也不动。"""
        self._dark(True)
        for theme in ("terminal", "rhine", "rhine-dark", "bauform"):
            cfg = cm.load_config()
            cfg["ui_theme"] = theme
            cm.save_config(cfg)
            self.assertEqual(cm.load_config()["ui_theme"], theme)

    def test_config_without_key(self):
        """很老的配置里没有 ui_theme 这一项：按首次启动处理。"""
        self._dark(True)
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"webui_branch": "neo2"}, f)
        self.assertEqual(cm.load_config()["ui_theme"], "terminal-dark")

    def _write_foreign(self, theme):
        """一份别的电脑写的配置（machine_id 对不上）。"""
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"ui_theme": theme, "machine_id": "someone-elses-pc"}, f)

    def test_copied_from_other_pc_default_theme_follows_system(self):
        """整包发给别人：带来的是默认风格，对方第一次打开跟他自己的系统走（深 → 浅、浅 → 深都算）。"""
        self._dark(False)
        self._write_foreign("terminal-dark")
        self.assertEqual(cm.load_config()["ui_theme"], "terminal")
        self._write_foreign("terminal")
        with mock.patch.object(cm, "system_prefers_dark", return_value=True):
            self.assertEqual(cm.load_config()["ui_theme"], "terminal-dark")
            # 之后（指纹已是本机）不再跟随
            with mock.patch.object(cm, "system_prefers_dark", return_value=False):
                self.assertEqual(cm.load_config()["ui_theme"], "terminal-dark")

    def test_copied_from_other_pc_custom_theme_kept(self):
        """打包的人特意选了别的风格：原样保留。"""
        self._dark(True)
        self._write_foreign("rhine")
        self.assertEqual(cm.load_config()["ui_theme"], "rhine")

    def test_theme_file_gets_resolved_id(self):
        """开屏读的 current_theme.js 写的是判断后的具体 id，不是 auto。"""
        self._dark(True)
        os.makedirs(os.path.join(self.tmp, "web", "js"))
        cm.load_config()
        with open(os.path.join(self.tmp, "web", "js", "current_theme.js"), "r", encoding="utf-8") as f:
            self.assertIn('"terminal-dark"', f.read())

    def test_registry_failure_is_light(self):
        """读不到注册表（非 Windows / 老系统）当浅色，不抛异常。"""
        with mock.patch.dict(_sys.modules, {"winreg": None}):
            self.assertFalse(cm.system_prefers_dark())


if __name__ == "__main__":
    unittest.main()
