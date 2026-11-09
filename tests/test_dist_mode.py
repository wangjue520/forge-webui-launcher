"""
分发模式（config_manager 的相对路径换算、换电脑不清实例，app/venv_relocate.py）的回归测试。

核心：打开分发模式后，包里的路径（启动器目录里面的、和启动器文件夹并排的）存成相对路径；
整个文件夹换目录 / 换盘符后读配置，实例路径按启动器现在的位置换算；拷到别的电脑只清 Key。
运行：python -m unittest tests.test_dist_mode
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
import venv_relocate  # noqa: E402


class PathRuleTest(unittest.TestCase):
    BASE = r"G:\pkg\WWY启动器"

    def test_inside_and_sibling_become_relative(self):
        self.assertEqual(cm.to_portable_path(r"G:\pkg\WWY启动器\python\python.exe", self.BASE),
                         r"python\python.exe")
        self.assertEqual(cm.to_portable_path(r"G:\pkg\ComfyUI", self.BASE), r"..\ComfyUI")
        self.assertEqual(cm.to_portable_path(r"G:\pkg\ComfyUI\python\python.exe", self.BASE),
                         r"..\ComfyUI\python\python.exe")

    def test_outside_stays_absolute(self):
        for p in (r"G:\other\models", r"G:\pkg", r"F:\mod", r"G:\x\y\z"):
            self.assertEqual(cm.to_portable_path(p, self.BASE), p)
        self.assertEqual(cm.to_portable_path("", self.BASE), "")
        self.assertEqual(cm.to_portable_path(r"..\ComfyUI", self.BASE), r"..\ComfyUI")

    def test_resolve(self):
        self.assertEqual(cm.from_portable_path(r"..\ComfyUI", r"D:\新位置\WWY启动器"), r"D:\新位置\ComfyUI")
        self.assertEqual(cm.from_portable_path(r"python\python.exe", r"D:\a\b"), r"D:\a\b\python\python.exe")
        for p in (r"F:\mod", "", "https://x/y", "C:foo"):
            self.assertEqual(cm.from_portable_path(p, r"D:\a"), p)


class ConfigRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _at(self, launcher_dir):
        os.makedirs(os.path.join(launcher_dir, "web", "js"), exist_ok=True)
        path = os.path.join(launcher_dir, "launcher_config.json")
        return mock.patch.multiple(cm, CONFIG_PATH=path, LAUNCHER_DIR=launcher_dir)

    def _make_cfg(self, launcher_dir, comfy):
        cfg = dict(cm.DEFAULT_CONFIG)
        inst = cm.make_instance(cfg, webui_root=comfy, webui_branch="comfyui",
                                custom_python_path=os.path.join(comfy, "python", "python.exe"))
        cfg["instances"] = [inst]
        cfg["active_instance"] = inst["id"]
        cfg["config_version"] = cm.CONFIG_VERSION
        cfg["dist_mode"] = True
        cfg["civitai_api_key"] = "secret"
        cfg["civitai_last_dir"] = os.path.join(launcher_dir, "downloads")
        cm.project_active(cfg)
        return cfg

    def test_move_whole_package(self):
        old = os.path.join(self.tmp, "old", "WWY启动器")
        comfy = os.path.join(self.tmp, "old", "ComfyUI")
        os.makedirs(os.path.join(comfy, "python"))
        open(os.path.join(comfy, "python", "python.exe"), "wb").close()
        with self._at(old), mock.patch.object(cm, "machine_id", lambda: "AAA"):
            cfg = self._make_cfg(old, comfy)
            cfg["machine_id"] = "AAA"
            cm.save_config(cfg)
            with open(cm.CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
        self.assertEqual(saved["instances"][0]["webui_root"], r"..\ComfyUI")
        self.assertEqual(saved["instances"][0]["custom_python_path"], r"..\ComfyUI\python\python.exe")
        self.assertEqual(saved["webui_root"], r"..\ComfyUI")
        self.assertEqual(saved["civitai_last_dir"], "downloads")
        self.assertNotIn(self.tmp, json.dumps(saved, ensure_ascii=False))

        # 整个文件夹搬到别处 + 换了电脑
        new_parent = os.path.join(self.tmp, "换了个盘", "整合包")
        shutil.move(os.path.join(self.tmp, "old"), new_parent)
        new = os.path.join(new_parent, "WWY启动器")
        with self._at(new), mock.patch.object(cm, "machine_id", lambda: "BBB"):
            cfg = cm.load_config()
        self.assertEqual(cfg["webui_root"], os.path.join(new_parent, "ComfyUI"))
        self.assertEqual(cfg["instances"][0]["custom_python_path"],
                         os.path.join(new_parent, "ComfyUI", "python", "python.exe"))
        self.assertEqual(cfg["civitai_api_key"], "")          # 原主人的 Key 清掉
        self.assertFalse(cfg.get("_new_machine"))             # 不弹「从别的电脑复制过来，已清空目录」
        self.assertTrue(cfg.get("_dist_moved"))
        self.assertEqual(len(cfg["instances"]), 1)

    def test_without_dist_mode_paths_stay_absolute(self):
        d = os.path.join(self.tmp, "L")
        comfy = os.path.join(self.tmp, "ComfyUI")
        with self._at(d), mock.patch.object(cm, "machine_id", lambda: "AAA"):
            cfg = self._make_cfg(d, comfy)
            cfg["dist_mode"] = False
            cm.save_config(cfg)
            with open(cm.CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
        self.assertEqual(saved["webui_root"], comfy)

    def test_new_machine_without_dist_mode_still_resets(self):
        d = os.path.join(self.tmp, "L")
        comfy = os.path.join(self.tmp, "ComfyUI")
        with self._at(d), mock.patch.object(cm, "machine_id", lambda: "AAA"):
            cfg = self._make_cfg(d, comfy)
            cfg["dist_mode"] = False
            cfg["machine_id"] = "AAA"
            cm.save_config(cfg)
        with self._at(d), mock.patch.object(cm, "machine_id", lambda: "BBB"):
            cfg = cm.load_config()
        self.assertEqual(cfg["webui_root"], "")
        self.assertTrue(cfg.get("_new_machine"))


class VenvRelocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.launcher = os.path.join(self.tmp, "启动器 带空格")
        os.makedirs(os.path.join(self.launcher, "python"))
        os.makedirs(os.path.join(self.launcher, ".venv"))
        open(os.path.join(self.launcher, "python", "python.exe"), "wb").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, home):
        with open(os.path.join(self.launcher, ".venv", "pyvenv.cfg"), "w", encoding="utf-8") as f:
            f.write(f"home = {home}\ninclude-system-site-packages = false\nversion = 3.13.15\n"
                    f"executable = {home}\\python.exe\ncommand = {home}\\python.exe -m venv X:\\old\\.venv\n")

    def _read(self):
        with open(os.path.join(self.launcher, ".venv", "pyvenv.cfg"), encoding="utf-8") as f:
            return f.read()

    def test_moved_launcher_points_venv_back(self):
        self._write(r"X:\不存在的旧位置\python")
        self.assertTrue(venv_relocate.relocate(self.launcher))
        text = self._read()
        py_dir = os.path.join(self.launcher, "python")
        self.assertIn(f"home = {py_dir}\n", text)
        self.assertIn(f"executable = {os.path.join(py_dir, 'python.exe')}\n", text)
        self.assertIn("include-system-site-packages = false", text)
        self.assertIn("version = 3.13.15", text)
        self.assertFalse(venv_relocate.relocate(self.launcher))      # 再跑一次不用改

    def test_other_existing_python_untouched(self):
        other = os.path.join(self.tmp, "系统Python")
        os.makedirs(other)
        open(os.path.join(other, "python.exe"), "wb").close()
        self._write(other)
        before = self._read()
        self.assertFalse(venv_relocate.relocate(self.launcher))
        self.assertEqual(self._read(), before)

    def test_no_venv(self):
        os.rmdir(os.path.join(self.launcher, ".venv"))
        self.assertFalse(venv_relocate.relocate(self.launcher))


if __name__ == "__main__":
    unittest.main()
