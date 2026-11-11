# -*- coding: utf-8 -*-
"""详细日志的按钮记录、增量读取、轮替与清空测试。"""
import os
import queue
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from activity_log import ActivityLog  # noqa: E402
import webview_api as wa  # noqa: E402


class EventQueueTests(unittest.TestCase):
    def test_activity_events_are_batched_without_losing_control_events(self):
        events = queue.Queue()
        entries = [{"seq": 1, "message": "第一条"}, {"seq": 2, "message": "第二条"}]
        for entry in entries:
            events.put({"scope": "activity", "type": "entry", "entry": entry})
        done = {"scope": "meta", "type": "dl_done", "ok": True}
        events.put(done)
        events.put(None)
        sent = []
        api = types.SimpleNamespace(_emit_q=events, _send_event=sent.append)
        worker = threading.Thread(target=wa.LauncherApi._emit_sender_loop,
                                  args=(api,), daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "事件发送线程未响应停止标记")

        batches = [e for e in sent if e["type"] == "entries"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["entries"], entries)
        self.assertEqual(sent[-1], done)

    def test_only_log_events_are_droppable(self):
        self.assertTrue(wa.LauncherApi._droppable_event(
            {"scope": "activity", "type": "entry"}))
        self.assertFalse(wa.LauncherApi._droppable_event(
            {"scope": "meta", "type": "dl_done"}))


class ActivityLogTests(unittest.TestCase):
    def test_record_read_clear_and_rotate(self):
        with tempfile.TemporaryDirectory(prefix="activity_log_") as tmp:
            path = os.path.join(tmp, "logs", "detailed.log")
            pushed = []
            log = ActivityLog(path, max_entries=2, max_bytes=180, listener=pushed.append)
            first = log.add("按钮点击", "页面=meta 按钮=下载勾选项 #meta-download")
            second = log.add("meta", "开始下载 demo.safetensors")
            third = log.add("meta", "SHA-256 校验未通过", "ERROR")
            self.assertEqual([e["seq"] for e in pushed], [first["seq"], second["seq"], third["seq"]])
            self.assertEqual([e["message"] for e in log.entries()],
                             [second["message"], third["message"]])
            self.assertEqual(log.entries(after=second["seq"])[0]["seq"], third["seq"])
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(os.path.isfile(path + ".1"))
            with open(path + ".1", encoding="utf-8") as f:
                self.assertIn("按钮点击", f.read())

            log.clear()
            self.assertEqual(log.entries(), [])
            self.assertEqual(os.path.getsize(path), 0)
            # 清空不重置序号，前端不会把旧事件重新拉一遍。
            fourth = log.add("界面", "清空后新事件")
            self.assertGreater(fourth["seq"], third["seq"])


if __name__ == "__main__":
    unittest.main()
