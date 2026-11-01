"""
部署中的状态：开始部署时 deploy 的 state 事件带上部署的是什么（label / target），
界面重载后用 deploy_status 恢复「正在部署 …」的显示。
实例目录被删掉的检测：_root_missing。
运行：python -m unittest tests.test_deploy_status
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import webview_api as wa  # noqa: E402


class DeployStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.api = mock.MagicMock()
        self.api._deploy_lock = threading.Lock()
        self.api._deploy_cancel = threading.Event()
        self.api._deploy_running = False
        self.api._deploy_info = {}
        self.api.cfg = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _states(self):
        return [c.kwargs for c in self.api._emit.call_args_list if c.args[:2] == ("deploy", "state")]

    def test_idle(self):
        self.assertEqual(wa._api_deploy_status(self.api), {"running": False})

    def test_start_reports_what_is_being_deployed(self):
        target = _os.path.join(self.tmp, "forge-neo-h3")
        with mock.patch.object(wa, "_deploy_cfg_for", return_value={}):
            r = wa._api_deploy_start(self.api, target, "neo2h3", True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._states()[-1], {"running": True, "action": "deploy", "label": "Forge Neo H3", "target": target})
        st = wa._api_deploy_status(self.api)
        self.assertEqual((st["running"], st["label"], st["target"]), (True, "Forge Neo H3", target))
        # 部署线程结束（_deploy_flow 的 finally 里把 _deploy_running 置回 False）后不再显示
        self.api._deploy_running = False
        self.assertEqual(wa._api_deploy_status(self.api), {"running": False})

    def test_comfyui_label(self):
        with mock.patch.object(wa, "_deploy_cfg_for", return_value={}):
            wa._api_deploy_start(self.api, _os.path.join(self.tmp, "comfy"), "comfyui", True)
        self.assertEqual(self._states()[-1]["label"], "ComfyUI")

    def test_spawn_failure_resets_state(self):
        self.api._spawn.side_effect = RuntimeError("no thread")
        with mock.patch.object(wa, "_deploy_cfg_for", return_value={}):
            r = wa._api_deploy_start(self.api, _os.path.join(self.tmp, "x"), "neo2", True)
        self.assertFalse(r["ok"])
        self.assertEqual(self._states()[-1], {"running": False})
        self.assertEqual(wa._api_deploy_status(self.api), {"running": False})


class RootMissingTest(unittest.TestCase):
    """实例目录被删掉：不存在或只剩空文件夹算 missing；正在往这个目录部署时不算"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.api = mock.MagicMock()
        self.api._deploy_running = False
        self.api._deploy_info = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cases(self):
        full = _os.path.join(self.tmp, "full")
        _os.makedirs(full)
        open(_os.path.join(full, "webui.bat"), "w").close()
        empty = _os.path.join(self.tmp, "empty")
        _os.makedirs(empty)
        self.assertFalse(wa._root_missing(self.api, ""))
        self.assertFalse(wa._root_missing(self.api, full))
        self.assertTrue(wa._root_missing(self.api, _os.path.join(self.tmp, "gone")))
        self.assertTrue(wa._root_missing(self.api, empty))
        # 刚开始部署时目标目录还是空的
        self.api._deploy_running = True
        self.api._deploy_info = {"action": "deploy", "label": "Forge Neo", "target": empty}
        self.assertFalse(wa._root_missing(self.api, empty))
        self.assertTrue(wa._root_missing(self.api, _os.path.join(self.tmp, "gone")))


if __name__ == "__main__":
    unittest.main()
