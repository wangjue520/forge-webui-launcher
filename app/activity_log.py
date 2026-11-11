# -*- coding: utf-8 -*-
"""启动器详细日志：线程安全的内存环形缓冲区 + 本地持久化。"""
from collections import deque
from datetime import datetime
import itertools
import os
import threading


class ActivityLog:
    def __init__(self, path, max_entries=2000, max_bytes=5 * 1024 * 1024, listener=None):
        self.path = path
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries = deque(maxlen=max_entries)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._listener = listener
        self._file = None           # 一直开着（行缓冲）：每条都开关一次文件，杀软逐次扫描时会拖慢调用方
        self._bytes = 0
        try:
            self._bytes = os.path.getsize(path)
        except OSError:
            pass

    @staticmethod
    def _line(entry):
        return (f"{entry['time']} [{entry['level']}] [{entry['source']}] "
                f"{entry['message']}")

    def _handle_locked(self):
        if self._file is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # 行缓冲：每条写完就落盘，界面卡死 / 进程被强杀时已经记下的还在
            self._file = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)
        return self._file

    def _close_locked(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def add(self, source, message, level="INFO"):
        text = str(message or "").strip()
        if not text:
            return None
        # 单条异常堆栈也不能把日志窗口和事件桥撑爆。
        text = text[:6000]
        entry = {
            "seq": next(self._seq),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "level": str(level or "INFO").upper(),
            "source": str(source or "app")[:80],
            "message": text,
        }
        with self._lock:
            self._entries.append(entry)
            try:
                line = self._line(entry) + "\n"
                self._handle_locked().write(line)
                self._bytes += len(line.encode("utf-8"))
                if self._bytes > self.max_bytes:
                    self._rotate_locked()
            except (OSError, ValueError):
                # 日志盘满/只读不能反过来让下载或界面操作失败。
                self._close_locked()
        if self._listener:
            try:
                self._listener(entry)
            except Exception:
                pass
        return entry

    def _rotate_locked(self):
        self._close_locked()
        backup = self.path + ".1"
        try:
            if os.path.exists(backup):
                os.remove(backup)
            if os.path.exists(self.path):
                os.replace(self.path, backup)
            # 「打开日志文件」在轮替后、下一条事件前也必须有效。
            with open(self.path, "a", encoding="utf-8"):
                pass
            self._bytes = 0
        except OSError:
            # 文件被占用时退化为截断，仍不让日志影响主流程。
            try:
                with open(self.path, "w", encoding="utf-8"):
                    pass
                self._bytes = 0
            except OSError:
                pass

    def entries(self, after=0, limit=1000):
        try:
            after = int(after or 0)
            limit = max(1, min(2000, int(limit or 1000)))
        except (TypeError, ValueError):
            after, limit = 0, 1000
        with self._lock:
            rows = [dict(e) for e in self._entries if e["seq"] > after]
        return rows[-limit:]

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._close_locked()
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "w", encoding="utf-8"):
                    pass
                self._bytes = 0
            except OSError:
                pass

    def close(self):
        with self._lock:
            self._close_locked()
