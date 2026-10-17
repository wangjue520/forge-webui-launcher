"""
一键启动会弹两个标签页：WebUI 设置「Launch the webui in browser on startup」默认 Local 会自己开浏览器，
启动器就绪后又开一次。WebUI 自己会开时启动器不再打开。
运行：python -m unittest tests.test_browser_open
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import json
import os
import shutil
import tempfile
import unittest

import config_manager as cm  # noqa: E402


class BrowserOpenTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cfg = dict(cm.DEFAULT_CONFIG, webui_branch="neo2", enable_listen=False, enable_share=False, autolaunch=False)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _setting(self, value):
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"auto_launch_browser": value}, f)

    def test_default_local_opens_itself(self):
        # 没有 config.json（首次启动）= 默认 Local
        self.assertTrue(cm.webui_opens_browser(self.cfg, self.root))

    def test_local_with_listen_does_not(self):
        self.cfg["enable_listen"] = True
        self.assertFalse(cm.webui_opens_browser(self.cfg, self.root))

    def test_disabled_setting(self):
        self._setting("Disable")
        self.assertFalse(cm.webui_opens_browser(self.cfg, self.root))

    def test_remote_always(self):
        self._setting("Remote")
        self.cfg["enable_listen"] = True
        self.assertTrue(cm.webui_opens_browser(self.cfg, self.root))

    def test_autolaunch_flag(self):
        self._setting("Disable")
        self.cfg["autolaunch"] = True
        self.assertTrue(cm.webui_opens_browser(self.cfg, self.root))


if __name__ == "__main__":
    unittest.main()
