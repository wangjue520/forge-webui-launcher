# -*- coding: utf-8 -*-
"""报错诊断的 AI 设置和 LoRA 训练器同步（app/ai_api_sync.py）。共用文件用临时路径（WWY_AI_API_FILE）。"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import ai_api_sync as s  # noqa: E402


def trainer_writes(path, **vals):
    """模拟训练器改了设置（训练器 app/aiconf.py 写的格式）。"""
    v = s._vals(vals)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"values": v, "updated": time.time(), "by": "lora_trainer"}, f)


class AiApiSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "WWY", "ai_api.json")
        self.env = mock.patch.dict(os.environ, {"WWY_AI_API_FILE": self.path})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_disabled_without_override_under_unittest(self):
        with mock.patch.dict(os.environ, {"WWY_AI_API_FILE": ""}):
            cfg = {"diag_llm_api_key": "sk-should-not-leak"}
            self.assertFalse(s.sync(cfg))
            self.assertNotIn(s.SEEN_KEY, cfg)

    def test_launcher_key_goes_to_shared(self):
        cfg = {"diag_llm_provider": "qwen", "diag_llm_api_key": "sk-launcher"}
        self.assertFalse(s.sync(cfg))
        sh = s.read_shared()
        self.assertEqual(sh["values"]["diag_llm_api_key"], "sk-launcher")
        self.assertEqual(sh["by"], "launcher")

    def test_no_key_no_file(self):
        cfg = {"diag_llm_provider": "deepseek"}
        s.sync(cfg)
        self.assertFalse(os.path.exists(self.path))

    def test_trainer_change_is_pulled(self):
        cfg = {"diag_llm_provider": "qwen", "diag_llm_api_key": "sk-old"}
        s.sync(cfg)
        trainer_writes(self.path, diag_llm_provider="deepseek", diag_llm_api_key="sk-new", diag_llm_model="deepseek-v4-flash")
        self.assertTrue(s.sync(cfg))
        self.assertEqual(cfg["diag_llm_api_key"], "sk-new")
        self.assertEqual(cfg["diag_llm_provider"], "deepseek")
        self.assertFalse(s.sync(cfg))   # 再同步一次没有变化

    def test_stale_launcher_does_not_overwrite_trainer(self):
        # 启动器开着、内存里是旧 Key；训练器改了新 Key 之后启动器存了别的设置（主题）：不能把新 Key 盖回去
        cfg = {"diag_llm_api_key": "sk-old", "theme": "a"}
        s.sync(cfg)
        trainer_writes(self.path, diag_llm_api_key="sk-new")
        cfg["theme"] = "b"
        s.sync(cfg)   # save_config 里调的
        self.assertEqual(s.read_shared()["values"]["diag_llm_api_key"], "sk-new")
        self.assertEqual(cfg["diag_llm_api_key"], "sk-new")

    def test_launcher_edit_wins_after_sync(self):
        cfg = {"diag_llm_api_key": "sk-a"}
        s.sync(cfg)
        cfg["diag_llm_api_key"] = "sk-b"   # 用户在启动器设置页改了
        self.assertFalse(s.sync(cfg))
        self.assertEqual(s.read_shared()["values"]["diag_llm_api_key"], "sk-b")

    def test_first_sync_newer_side_wins(self):
        trainer_writes(self.path, diag_llm_api_key="sk-trainer")
        cfg = {"diag_llm_api_key": "sk-launcher-old"}
        self.assertTrue(s.sync(cfg, local_mtime=time.time() - 3600))   # 启动器配置比共用文件旧
        self.assertEqual(cfg["diag_llm_api_key"], "sk-trainer")
        trainer_writes(self.path, diag_llm_api_key="sk-trainer2")
        cfg2 = {"diag_llm_api_key": "sk-launcher-new"}
        self.assertFalse(s.sync(cfg2, local_mtime=time.time() + 3600))   # 启动器配置更新
        self.assertEqual(s.read_shared()["values"]["diag_llm_api_key"], "sk-launcher-new")

    def test_broken_shared_file(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{坏的")
        cfg = {"diag_llm_api_key": "sk-x"}
        s.sync(cfg)
        self.assertEqual(s.read_shared()["values"]["diag_llm_api_key"], "sk-x")


if __name__ == "__main__":
    unittest.main()
