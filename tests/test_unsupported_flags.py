"""
启动器生成的参数要按 WebUI 实际支持的参数过滤（用户反馈：新版 Neo 删掉了 --normalvram，
选了「普通显存模式」后启动报 unrecognized arguments: --normalvram）。
运行：python -m unittest tests.test_unsupported_flags
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import tempfile
import unittest

import config_manager as cm  # noqa: E402

NEW_NEO_ARGS = """
vram_group = parser.add_mutually_exclusive_group()
vram_group.add_argument("--gpu-only", action="store_true")
vram_group.add_argument("--highvram", action="store_true")
vram_group.add_argument("--lowvram", action="store_true")
parser.add_argument("--reserve-vram", type=float, default=None)
parser.add_argument("--bf16-unet", action="store_true")
parser.add_argument("--sage", action="store_true")
"""
CMD_ARGS = 'parser.add_argument("--port", type=int)\nparser.add_argument("--listen", action="store_true")\n'


def _touch(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class UnsupportedFlagsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        _touch(os.path.join(self.root, "backend", "args.py"), NEW_NEO_ARGS)
        _touch(os.path.join(self.root, "modules", "cmd_args.py"), CMD_ARGS)
        self.cfg = dict(cm.DEFAULT_CONFIG, webui_branch="neo2", vram_mode="normal", unet_precision="bf16",
                        install_sage=True, enable_listen=True, port="7860", extra_args="--normalvram-custom x")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_removed_flag_is_dropped_with_note(self):
        notes = []
        args = cm.build_commandline_args(self.cfg, self.root, notes).split()
        self.assertNotIn("--normalvram", args)
        self.assertIn("--bf16-unet", args)
        self.assertIn("--sage", args)
        self.assertIn("--port", args)
        self.assertTrue(notes and "--normalvram" in notes[0])

    def test_extra_args_untouched(self):
        self.assertIn("--normalvram-custom x", cm.build_commandline_args(self.cfg, self.root))

    def test_supported_flag_kept(self):
        self.cfg["vram_mode"] = "low"
        self.assertIn("--lowvram", cm.build_commandline_args(self.cfg, self.root).split())

    def test_old_neo_keeps_normalvram(self):
        _touch(os.path.join(self.root, "backend", "args.py"),
               NEW_NEO_ARGS + 'vram_group.add_argument("--normalvram", action="store_true")\n')
        self.assertIn("--normalvram", cm.build_commandline_args(self.cfg, self.root).split())

    def test_without_root_or_arg_files_no_filtering(self):
        self.assertIn("--normalvram", cm.build_commandline_args(self.cfg).split())
        empty = tempfile.mkdtemp()
        try:
            self.assertIn("--normalvram", cm.build_commandline_args(self.cfg, empty).split())
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_env_overrides_filtered(self):
        notes = []
        env = cm.build_launch_env_overrides(self.cfg, self.root, notes)
        self.assertNotIn("--normalvram ", env["COMMANDLINE_ARGS"] + " ")
        self.assertTrue(any("--normalvram" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
