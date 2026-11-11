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
            # 文件一直开着（行缓冲）：写完不用等关闭，别的程序马上能读到
            with open(path, encoding="utf-8") as f:
                self.assertIn("清空后新事件", f.read())
            log.close()      # Windows 上开着的文件删不掉临时目录

    def test_instance_output_is_not_copied_except_launcher_lines_and_errors(self):
        added = []
        api = types.SimpleNamespace(_activity=types.SimpleNamespace(
            add=lambda src, msg, level="INFO": added.append((src, msg, level))))
        wa.LauncherApi._record_activity_event(api, "launch", "log", {
            "iid": "a", "text": "Collecting torch\n[启动器] 已挂载共享模型库\nDownloading 3.2 MB\n"
                                "RuntimeError: CUDA out of memory"})
        wa.LauncherApi._record_activity_event(api, "launch", "log", {"iid": "a", "text": "100%|####| 20/20"})
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0][1], "[启动器] 已挂载共享模型库\nRuntimeError: CUDA out of memory")
        wa.LauncherApi._record_activity_event(api, "civitai", "log", {"text": "开始下载 a.safetensors"})
        self.assertEqual(added[-1][1], "开始下载 a.safetensors")


class ProgressThrottleTests(unittest.TestCase):
    """下载进度每 256KB 一条，不限流的话一秒几百次 evaluate_js，窗口会卡死"""

    def make_api(self):
        sent = []
        api = wa.LauncherApi.__new__(wa.LauncherApi)
        api._prog_lock = threading.Lock()
        api._prog_last, api._prog_pending = {}, {}
        api._emit_now = lambda scope, type_, data, direct=False: sent.append((scope, type_, dict(data)))
        return api, sent

    def test_burst_is_throttled_and_last_value_is_flushed(self):
        api, sent = self.make_api()
        for i in range(1, 501):
            wa.LauncherApi._emit(api, "civitai", "progress", downloaded=i * 1024, total=10 ** 9)
        self.assertLess(len(sent), 5)                      # 500 条压成一两条
        wa.LauncherApi._emit(api, "civitai", "done", ok=True)
        # 「完成」之前先把最后一条进度补发出去，顺序不能乱
        self.assertEqual(sent[-2][2]["downloaded"], 500 * 1024)
        self.assertEqual(sent[-1][1], "done")

    def test_final_chunk_and_separate_files_always_go_through(self):
        api, sent = self.make_api()
        wa.LauncherApi._emit(api, "meta", "dl_progress", idx=0, downloaded=1, total=10)
        wa.LauncherApi._emit(api, "meta", "dl_progress", idx=0, downloaded=10, total=10)
        wa.LauncherApi._emit(api, "meta", "dl_progress", idx=1, downloaded=1, total=10)
        self.assertEqual([(d["idx"], d["downloaded"]) for _s, _t, d in sent], [(0, 1), (0, 10), (1, 1)])

    def test_idle_flush_sends_only_due_progress(self):
        api, sent = self.make_api()
        wa.LauncherApi._emit(api, "deploy", "progress", downloaded=1, total=100)
        wa.LauncherApi._emit(api, "deploy", "progress", downloaded=2, total=100)
        self.assertEqual(len(sent), 1)
        api._progress_flush(direct=True)                   # 还没到间隔：不发
        self.assertEqual(len(sent), 1)
        api._prog_last = {k: 0.0 for k in api._prog_last}
        api._progress_flush(direct=True)
        self.assertEqual(sent[-1][2]["downloaded"], 2)


if __name__ == "__main__":
    unittest.main()
