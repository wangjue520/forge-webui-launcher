"""
ComfyUI 桌面版（新版 Comfy Desktop，多实例）的测试：认出实例和它的 Python、模型默认放共享目录
（ComfyUI-Shared\\models）而不是实例自己的 models、启动时把共享目录挂给 ComfyUI。
目录和设置文件按桌面版源码（src/main/settings.ts、installations.ts、lib/models.ts）伪造，不需要装桌面版。
运行：python -m unittest tests.test_comfy_desktop
"""
import os as _os, sys as _sys  # app_dir_for_tests：被测模块在 app/ 子目录里
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import comfy_desktop as cdk    # noqa: E402
import config_manager as cm    # noqa: E402
import model_library as ml     # noqa: E402


def _touch(p, text=""):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class DesktopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = self.tmp
        self.data_root = os.path.join(t, "LocalAppData", "Comfy-Desktop")
        self.install = os.path.join(self.data_root, "ComfyUI-Installs", "ComfyUI")
        _touch(os.path.join(self.install, "ComfyUI", "main.py"))
        os.makedirs(os.path.join(self.install, "ComfyUI", "comfy"))
        for sub in ("checkpoints", "loras"):
            os.makedirs(os.path.join(self.install, "ComfyUI", "models", sub))
        _touch(os.path.join(self.install, "standalone-env", "python.exe"))
        self.shared = os.path.join(self.data_root, "ComfyUI-Shared", "models")
        for sub in cdk.MODEL_FOLDER_TYPES:
            os.makedirs(os.path.join(self.shared, sub))
        self.appdata = os.path.join(t, "Roaming")
        self.cfgdir = os.path.join(self.appdata, "Comfy Desktop")
        self.env = mock.patch.dict(os.environ, {"APPDATA": self.appdata,
                                                "LOCALAPPDATA": os.path.join(t, "LocalAppData")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, settings=None, records=None):
        if settings is not None:
            _touch(os.path.join(self.cfgdir, "settings.json"), json.dumps(settings))
        if records is not None:
            _touch(os.path.join(self.cfgdir, "installations.json"), json.dumps(records))

    def rec(self, **kw):
        r = {"id": "inst-1", "name": "ComfyUI", "installPath": self.install, "sourceId": "standalone"}
        r.update(kw)
        return r

    def cfg(self, root=None):
        return {"webui_root": root or self.install, "webui_branch": "comfyui"}

    def test_layout_finds_standalone_python(self):
        comfy_dir, py = cm.comfy_layout(self.install)
        self.assertEqual(os.path.normcase(comfy_dir), os.path.normcase(os.path.join(self.install, "ComfyUI")))
        self.assertTrue(py.endswith(os.path.join("standalone-env", "python.exe")))
        # 根目录选到里面的 ComfyUI 也认
        _d, py2 = cm.comfy_layout(os.path.join(self.install, "ComfyUI"))
        self.assertEqual(py, py2)

    def test_shared_is_default(self):
        self.write({"modelsDirs": [self.shared]}, [self.rec()])
        primary, dirs = cdk.model_dirs(self.install)
        self.assertEqual(os.path.normcase(primary), os.path.normcase(self.shared))
        self.assertEqual([os.path.normcase(d) for d in dirs], [os.path.normcase(self.shared)])
        dirs = ml.instance_model_dirs(self.cfg(), cm.comfy_layout)
        self.assertEqual(os.path.normcase(dirs["loras"]), os.path.normcase(os.path.join(self.shared, "loras")))
        self.assertEqual(os.path.normcase(dirs["checkpoints"]),
                         os.path.normcase(os.path.join(self.shared, "checkpoints")))
        # 根目录选到里面的 ComfyUI 一样
        primary2, _ = cdk.model_dirs(os.path.join(self.install, "ComfyUI"))
        self.assertEqual(primary, primary2)

    def test_without_settings_uses_sibling_shared(self):
        # 桌面版没写过 settings.json / 没登记这个实例：按目录结构认，共享目录用默认位置
        primary, dirs = cdk.model_dirs(self.install)
        self.assertEqual(os.path.normcase(primary), os.path.normcase(self.shared))

    def test_shared_off_keeps_own_models(self):
        self.write({"modelsDirs": [self.shared]}, [self.rec(useSharedModels=False)])
        primary, dirs = cdk.model_dirs(self.install)
        self.assertIsNone(primary)
        self.assertEqual(dirs, [])
        dirs = ml.instance_model_dirs(self.cfg(), cm.comfy_layout)
        self.assertEqual(os.path.normcase(dirs["loras"]),
                         os.path.normcase(os.path.join(self.install, "ComfyUI", "models", "loras")))

    def test_primary_override(self):
        extra = os.path.join(self.tmp, "D", "my_models")
        os.makedirs(os.path.join(extra, "loras"))
        self.write({"modelsDirs": [self.shared]}, [self.rec(modelDirs=[extra], modelDirsPrimary=extra)])
        primary, dirs = cdk.model_dirs(self.install)
        self.assertEqual(os.path.normcase(primary), os.path.normcase(extra))
        self.assertEqual(len(dirs), 2)
        # 实例指定「默认放自己的 models」
        own = os.path.join(self.install, "ComfyUI", "models")
        self.write(records=[self.rec(modelDirsPrimary=own)])
        self.assertIsNone(cdk.model_dirs(self.install)[0])

    def test_yaml_and_launch_args(self):
        self.write({"modelsDirs": [self.shared]}, [self.rec()])
        primary, dirs = cdk.model_dirs(self.install)
        text = cdk.yaml_text(dirs, primary)
        self.assertIn('base_path: "' + self.shared.replace("\\", "/") + '"', text)
        self.assertIn("is_default: true", text)
        self.assertIn("    loras: loras/", text)
        try:
            import yaml
        except ImportError:
            yaml = None
        if yaml:
            data = yaml.safe_load(text)
            self.assertEqual(data["wwy_comfy_desktop_0"]["loras"], "loras/")
        cfg = dict(self.cfg(), _extra_model_paths="lib.yaml", _desktop_model_paths="desk.yaml",
                   comfy_enable_manager=False)
        args = cm.build_comfy_args(cfg)
        i = [k for k, a in enumerate(args) if a == "--extra-model-paths-config"]
        self.assertEqual([args[k + 1] for k in i], ["lib.yaml", "desk.yaml"])

    def test_not_desktop(self):
        portable = os.path.join(self.tmp, "ComfyUI_windows_portable")
        _touch(os.path.join(portable, "ComfyUI", "main.py"))
        os.makedirs(os.path.join(portable, "ComfyUI", "comfy"))
        os.makedirs(os.path.join(portable, "ComfyUI", "models", "loras"))
        _touch(os.path.join(portable, "python_embeded", "python.exe"))
        self.write({"modelsDirs": [self.shared]}, [self.rec()])
        self.assertEqual(cdk.model_dirs(portable), (None, []))
        dirs = ml.instance_model_dirs(self.cfg(portable), cm.comfy_layout)
        self.assertEqual(os.path.normcase(dirs["loras"]),
                         os.path.normcase(os.path.join(portable, "ComfyUI", "models", "loras")))


if __name__ == "__main__":
    unittest.main()
