"""
netstat / tasklist 输出解码的回归测试（app/process_manager.py）。

背景：启动器的 bat 先 chcp 936，netstat 的表头按 GBK 输出；以前用 text=True 让 Python 按系统 ANSI 代码页解码，
日文 Windows、开了「UTF-8 Beta」的系统上解码失败，stdout 变成 None，
启动前检查报「'NoneType' object has no attribute 'splitlines'」。
运行：python -m unittest tests.test_process_output
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import subprocess
import unittest
from unittest import mock

import process_manager as pm  # noqa: E402

NETSTAT = ("\r\n活动连接\r\n\r\n  协议  本地地址          外部地址        状态           PID\r\n"
           "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234\r\n"
           "  TCP    127.0.0.1:7860         0.0.0.0:0              LISTENING       5678\r\n")
TASKLIST = '"python.exe","5678","Console","1","1,234 K"\r\n'


def fake_run(stdout_bytes):
    def run(cmd, **kw):
        assert "text" not in kw and "encoding" not in kw   # 一定要按字节读，自己解码
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout_bytes, stderr=b"")
    return run


@unittest.skipUnless(_os.name == "nt", "只在 Windows 上查端口 / 进程名")
class OutputDecodeTest(unittest.TestCase):
    def test_gbk_netstat(self):
        with mock.patch.object(pm.subprocess, "run", fake_run(NETSTAT.encode("gbk"))):
            self.assertEqual(pm.find_listening_pid("7860"), 5678)
            self.assertIsNone(pm.find_listening_pid("7861"))

    def test_utf8_netstat(self):
        with mock.patch.object(pm.subprocess, "run", fake_run(NETSTAT.encode("utf-8"))):
            self.assertEqual(pm.find_listening_pid("7860"), 5678)

    def test_empty_output(self):
        with mock.patch.object(pm.subprocess, "run", fake_run(None)):
            self.assertIsNone(pm.find_listening_pid("7860"))
            self.assertEqual(pm.process_name_of(5678), "")

    def test_process_name(self):
        with mock.patch.object(pm.subprocess, "run", fake_run(TASKLIST.encode("gbk"))):
            self.assertEqual(pm.process_name_of(5678), "python.exe")


if __name__ == "__main__":
    unittest.main()
