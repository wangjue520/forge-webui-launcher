"""
启动 WebUI 时的输出环境（config_manager.build_launch_env_overrides / build_subprocess_env）回归测试。

背景：以前只有 ComfyUI 带 PYTHONUNBUFFERED / PYTHONIOENCODING，WebUI 没有。WebUI 的输出接的是管道，
print 的内容攒满几 KB 才送出来：秋叶 A1111 启动时装依赖好几分钟，运行日志里一行都没有、认不出监听地址；
中文 Windows 上还会报 'gbk' codec can't encode。
这里用一个假的 WebUI（webui.bat → launch.py：先 print 一行再「装依赖」睡一会儿），按启动器的环境跑 webui.bat，
那一行必须在进程还没结束时就读到。运行：python -m unittest tests.test_webui_output_env
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import config_manager as cm  # noqa: E402

LAUNCH_PY = ('import sys, time\nprint("Python", sys.version)\nprint("Version: v1.10.1 中文 Ҫ")\n'
             'time.sleep(15)\n')


class WebuiOutputEnvTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, "webui.bat"), "w", encoding="ascii", newline="") as f:
            f.write('@echo off\r\nif not defined PYTHON (set PYTHON=python)\r\n%PYTHON% launch.py %*\r\nexit /b\r\n')
        with open(os.path.join(self.root, "launch.py"), "w", encoding="utf-8") as f:
            f.write(LAUNCH_PY)
        self.cfg = dict(cm.DEFAULT_CONFIG, custom_python_path=sys.executable, webui_branch="neo2")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_overrides_have_unbuffered_utf8(self):
        ov = cm.build_launch_env_overrides(self.cfg, self.root)
        self.assertEqual(ov["PYTHONUNBUFFERED"], "1")
        self.assertEqual(ov["PYTHONIOENCODING"], "utf-8")
        rocm = cm.build_launch_env_overrides(dict(self.cfg, gpu_backend="rocm"), self.root)   # A 卡分支提前返回
        self.assertEqual(rocm["PYTHONUNBUFFERED"], "1")

    @unittest.skipUnless(os.name == "nt", "webui.bat 只在 Windows 上跑")
    def test_print_reaches_launcher_while_running(self):
        env = cm.build_subprocess_env(self.cfg, self.root)
        env.pop("NoDefaultCurrentDirectoryInExePath", None)
        proc = subprocess.Popen(["cmd.exe", "/c", os.path.join(self.root, "webui.bat")], cwd=self.root, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        got = []
        threading.Thread(target=lambda: [got.append(cm.decode_process_output(l)) for l in proc.stdout],
                         daemon=True).start()
        try:
            end = time.time() + 10
            while time.time() < end and not any("Version: v1.10.1" in l for l in got):
                time.sleep(0.2)
            self.assertIsNone(proc.poll(), "假 WebUI 不该这么快结束")
            line = next((l for l in got if "Version" in l), "")
            self.assertIn("Version: v1.10.1 中文 Ҫ", line)   # 进程还在跑就收到了，中文和 GBK 里没有的字都对
        finally:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)


if __name__ == "__main__":
    unittest.main()
