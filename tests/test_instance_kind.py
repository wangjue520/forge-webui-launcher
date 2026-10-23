"""
实例类型按目录重新检测（cm.reconcile_kind）的回归测试。

背景：ComfyUI 实例被记成 Forge Neo（webui_branch 错了）时，常用插件页会把 WebUI 扩展
往 ComfyUI 根目录的 extensions/ 里装。原因之一是「一键启动」页换根目录时 update_config
和 instance_update 两个调用并发、absorb_active 把旧分支盖了回去。
现在刷新页面 / 实例列表 / 扩展页 / 启动前都会按目录重新认一遍。
用合成的假目录跑，不需要真的 ComfyUI / WebUI。运行：python -m unittest tests.test_instance_kind
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import config_manager as cm  # noqa: E402
import webview_api as wa     # noqa: E402


def _touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def make_aki_comfy(base):
    """秋叶 ComfyUI 整合包：ComfyUI-aki-vX/{ComfyUI/{main.py, comfy/}, python/python.exe}"""
    root = os.path.join(base, "ComfyUI-aki-v3")
    _touch(os.path.join(root, "ComfyUI", "main.py"))
    os.makedirs(os.path.join(root, "ComfyUI", "comfy"), exist_ok=True)
    os.makedirs(os.path.join(root, "ComfyUI", "custom_nodes"), exist_ok=True)
    _touch(os.path.join(root, "python", "python.exe"))
    return root


def make_forge_neo(base):
    root = os.path.join(base, "forge-neo")
    _touch(os.path.join(root, "webui.bat"))
    _touch(os.path.join(root, "backend", "args.py"), "parser.add_argument('--sage')")
    return root


def make_cfg(insts, active):
    cfg = dict(cm.DEFAULT_CONFIG)
    cfg["instances"] = insts
    cfg["active_instance"] = active
    cm.project_active(cfg)
    return cfg


def fake_api(cfg):
    """只带本测试用到的属性的 LauncherApi（不跑 __init__，不起线程 / 窗口）"""
    api = wa.LauncherApi.__new__(wa.LauncherApi)
    api.cfg = cfg
    api._cfg_lock = threading.RLock()
    api._runners = {}
    api._runners_lock = threading.Lock()
    api.events = []
    api._emit = lambda *a, **k: api.events.append(a)
    return api


class ReconcileKindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.comfy = make_aki_comfy(self.tmp)
        self.forge = make_forge_neo(self.tmp)
        # cm.save_config 写到临时位置，不碰真配置
        self.cfg_path = os.path.join(self.tmp, "launcher_config.json")
        self.p1 = mock.patch.object(cm, "CONFIG_PATH", self.cfg_path)
        self.p2 = mock.patch.object(cm, "write_theme_file", lambda cfg: None)
        self.p1.start(); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_comfy_recorded_as_neo_is_fixed(self):
        """截图里的情况：名字 ComfyUI、目录是秋叶 ComfyUI，但分支记成了 neo2"""
        inst = cm.make_instance(None, id="a", name="ComfyUI", webui_root=self.comfy, webui_branch="neo2")
        self.assertEqual(cm.reconcile_kind(inst), ("neo2", "comfyui"))
        self.assertEqual(inst["webui_branch"], "comfyui")
        self.assertEqual(inst["name"], "ComfyUI")
        self.assertIsNone(cm.reconcile_kind(inst))  # 再跑一遍不再变

    def test_auto_name_follows_kind_but_custom_name_kept(self):
        a = cm.make_instance(None, id="a", name="Forge Neo 2", webui_root=self.comfy, webui_branch="neo2")
        b = cm.make_instance(None, id="b", name="我的出图机", webui_root=self.comfy, webui_branch="neo")
        cm.reconcile_kind(a); cm.reconcile_kind(b)
        self.assertEqual(a["name"], "ComfyUI")
        self.assertEqual(b["name"], "我的出图机")
        self.assertEqual(b["webui_branch"], "comfyui")

    def test_forge_recorded_as_comfy_is_fixed(self):
        inst = cm.make_instance(None, id="a", name="ComfyUI", webui_root=self.forge, webui_branch="comfyui")
        cm.reconcile_kind(inst)
        self.assertEqual(inst["webui_branch"], "neo2")
        self.assertEqual(inst["name"], "Forge Neo")

    def test_forge_sub_branch_choice_respected(self):
        """同属 Forge 的 classic / neo / neo2 是用户选的参数体系，不能被改"""
        inst = cm.make_instance(None, id="a", webui_root=self.forge, webui_branch="classic")
        self.assertIsNone(cm.reconcile_kind(inst))
        self.assertEqual(inst["webui_branch"], "classic")

    def test_invalid_root_untouched(self):
        inst = cm.make_instance(None, id="a", webui_root=os.path.join(self.tmp, "nope"), webui_branch="neo2")
        self.assertIsNone(cm.reconcile_kind(inst))
        inst["webui_root"] = ""
        self.assertIsNone(cm.reconcile_kind(inst))

    def test_ext_target_uses_custom_nodes(self):
        """常用插件页：错记的 ComfyUI 实例要装到 ComfyUI/custom_nodes，而不是根目录 extensions"""
        inst = cm.make_instance(None, id="a", name="ComfyUI", webui_root=self.comfy, webui_branch="neo2")
        api = fake_api(make_cfg([inst], "a"))
        _cfg, info, comfy_dir = wa._ext_target(api, None)
        self.assertEqual(info["kind"], "comfyui")
        self.assertEqual(info["kind_label"], "ComfyUI")
        self.assertEqual(info["target_dir"], os.path.join(self.comfy, "ComfyUI", "custom_nodes"))
        self.assertTrue(info["kind_fixed"] and info["active"])
        self.assertEqual(api.cfg["webui_branch"], "comfyui")      # 顶层投影也跟着改了
        self.assertIn(("instances", "changed"), api.events)

    def test_instances_list_and_non_active(self):
        a = cm.make_instance(None, id="a", webui_root=self.forge, webui_branch="neo2")
        b = cm.make_instance(None, id="b", name="ComfyUI", webui_root=self.comfy, webui_branch="neo2")
        api = fake_api(make_cfg([a, b], "a"))
        r = wa._api_instances_list(api)
        self.assertEqual(r["kind_fixed"], ["b"])
        kinds = {i["id"]: i["kind"] for i in r["instances"]}
        self.assertEqual(kinds, {"a": "forge", "b": "comfyui"})
        self.assertEqual(api.cfg["webui_branch"], "neo2")  # 当前实例没被误伤

    def test_update_config_root_change(self):
        """一键启动页的输入框走全局 update_config：换成 ComfyUI 目录，类型要跟着变"""
        inst = cm.make_instance(None, id="a", webui_root="", webui_branch="neo2")
        api = fake_api(make_cfg([inst], "a"))
        r = api.update_config({"webui_root": self.comfy})
        self.assertTrue(r["kind_fixed"])
        self.assertEqual(cm.find_instance(api.cfg, "a")["webui_branch"], "comfyui")

    def test_concurrent_root_change_race(self):
        """update_config 和 instance_update 同时到（同一个 change 事件触发的两个处理函数）"""
        for _ in range(30):
            inst = cm.make_instance(None, id="a", webui_root="", webui_branch="neo2")
            api = fake_api(make_cfg([inst], "a"))
            ts = [threading.Thread(target=api.update_config, args=({"webui_root": self.comfy},)),
                  threading.Thread(target=wa._api_instance_update, args=(api, "a", {"webui_root": self.comfy}))]
            for t in ts: t.start()
            for t in ts: t.join()
            self.assertEqual(cm.find_instance(api.cfg, "a")["webui_branch"], "comfyui")
            self.assertEqual(api.cfg["webui_branch"], "comfyui")

    def test_settings_wrong_branch_reverted(self):
        """高级选项把 ComfyUI 实例的分支误选成 Forge → 按目录改回，并告诉前端"""
        inst = cm.make_instance(None, id="a", webui_root=self.comfy, webui_branch="comfyui")
        api = fake_api(make_cfg([inst], "a"))
        r = wa._api_instance_config_update(api, "a", {"webui_branch": "neo2"})
        self.assertTrue(r["kind_fixed"])
        self.assertEqual(r["webui_branch"], "comfyui")
        self.assertEqual(api.cfg["webui_branch"], "comfyui")
        self.assertNotIn(("instances", "changed"), api.events)  # 原样改回，不广播


def make_trainer(base, name="lora"):
    root = os.path.join(base, name)
    for p in (("app", "main.py"), ("app", "envsetup.py"), ("web", "index.html")):
        _touch(os.path.join(root, *p))
    return root


class TrainerDirTest(unittest.TestCase):
    """LoRA 训练器装在不叫常用名字的文件夹里（比如 G:\\lora）也要能认出来"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.forge = make_forge_neo(self.tmp)
        self.trainer = make_trainer(self.tmp)
        self.p1 = mock.patch.object(cm, "CONFIG_PATH", os.path.join(self.tmp, "launcher_config.json"))
        self.p2 = mock.patch.object(cm, "write_theme_file", lambda cfg: None)
        self.p1.start(); self.p2.start()
        inst = cm.make_instance(None, id="a", webui_root=self.forge, webui_branch="neo2")
        self.api = fake_api(make_cfg([inst], "a"))

    def tearDown(self):
        self.p1.stop(); self.p2.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_existing_trainer(self):
        """「添加已有安装」选训练器目录：记成训练器，不变成实例"""
        r = wa._api_instance_add(self.api, self.trainer)
        self.assertTrue(r["ok"] and r["trainer"])
        self.assertEqual(self.api.cfg["lora_trainer_root"], self.trainer)
        self.assertEqual(len(self.api.cfg["instances"]), 1)
        self.assertIn(("trainer", "changed"), self.api.events)
        self.assertEqual(wa._trainer_root(self.api), self.trainer)

    def test_status_scans_once_in_background(self):
        self.api._spawn = lambda fn, name="": fn()   # 同步跑，方便断言
        with mock.patch.object(wa.lt, "found_root", lambda *a: ""), \
             mock.patch.object(wa.lt, "scan_root", mock.Mock(return_value=self.trainer)) as scan:
            wa._trainer_root(self.api)
            self.assertEqual(self.api.cfg["lora_trainer_root"], self.trainer)
            self.assertIn(("trainer", "changed"), self.api.events)
            self.api.cfg["lora_trainer_root"] = ""
            wa._trainer_root(self.api)
            self.assertEqual(scan.call_count, 1)            # 一次启动只扫一次


if __name__ == "__main__":
    unittest.main()
