"""
兜底规则：专门规则不认识的运行时报错也要报出来（新问题不能漏），但不重复、不把用户中断当报错。
运行：python -m unittest tests.test_diag_generic
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import unittest

from test_diagnose import _Base  # noqa: E402

COMFY_EXC = r"""[2026-10-31 02:56:24.968] got prompt
[2026-10-31 02:56:28.857] !!! Exception during processing !!! MiniMax H3 Block Cache cannot be combined with Spectrum Apply MiniMax H3; use only one acceleration node
[2026-10-31 02:56:28.876] Traceback (most recent call last):
  File "F:\ComfyUI\execution.py", line 548, in execute
    output_data = await get_output_data(prompt_id)
  File "F:\ComfyUI\custom_nodes\ComfyUI-Danbooru-Gallery\py\metadata_hook.py", line 194, in hook
    results = await original()
  File "F:\ComfyUI\custom_nodes\comfyui-minimax-h3-blockcache-T8-main\nodes.py", line 181, in execute
    raise ValueError(
        "MiniMax H3 Block Cache cannot be combined with Spectrum Apply MiniMax H3; use only one acceleration node"
    )
ValueError: MiniMax H3 Block Cache cannot be combined with Spectrum Apply MiniMax H3; use only one acceleration node

[2026-10-31 02:56:28.882] Prompt executed in 3.89 seconds
"""

FORGE_TB = r"""*** Error running process: D:\neo\extensions\sd-webui-foo\scripts\foo.py
    Traceback (most recent call last):
      File "D:\neo\modules\scripts.py", line 832, in process
        script.process(p, *script_args)
      File "D:\neo\extensions\sd-webui-foo\scripts\foo.py", line 40, in process
        x = p.extra["bar"]
    KeyError: 'bar'
Traceback (most recent call last):
  File "D:\neo\modules\call_queue.py", line 74, in f
    res = list(func(*args, **kwargs))
  File "D:\neo\extensions\sd-webui-foo\scripts\foo.py", line 40, in process
    x = p.extra["bar"]
KeyError: 'bar'
"""


class GenericTest(_Base):
    def test_comfy_node_exception(self):
        f = self.one(self.scan("cf", COMFY_EXC), "runtime_error")
        self.assertEqual(f["facts"]["exc_type"], "ValueError")
        self.assertEqual(f["facts"]["pack"], "comfyui-minimax-h3-blockcache-T8-main")   # 最深的那层，不是中间挂钩的
        self.assertIn("只", f["title"] + "只")
        self.assertTrue(f["evidence"].startswith("ValueError: MiniMax H3"))
        self.assertEqual(f["actions"][0]["aid"], "ai_ask")
        self.assertIn("comfyui-minimax-h3-blockcache-T8-main", f["actions"][0]["params"]["note"])
        # 不同的报错 key 不同（流式检测会重新提醒）
        other = self.one(self.scan("cf", COMFY_EXC.replace("Block Cache", "X Cache")), "runtime_error")
        self.assertNotEqual(other["key"], f["key"])

    def test_forge_traceback_and_extension(self):
        # 带「Error running process ... extensions\」的有专门规则（扩展报错），兜底不重复
        self.assertEqual([f["rule"] for f in self.scan("neo", FORGE_TB)], ["forge_ext_error"])
        bare = FORGE_TB[FORGE_TB.index("Traceback (most recent call last):\n  File \"D:\\neo\\modules\\call_queue"):]
        f = self.one(self.scan("neo", "Model loaded.\n" * 40 + bare), "runtime_error")
        self.assertEqual((f["facts"]["exc_type"], f["facts"]["pack"]), ("KeyError", "sd-webui-foo"))
        self.assertTrue(f["title"].startswith("WebUI"))

    def test_known_errors_not_duplicated(self):
        oom = ("!!! Exception during processing !!! Allocation on device\nTraceback (most recent call last):\n"
               '  File "x.py", line 1, in f\ntorch.OutOfMemoryError: Allocation on device\n')
        rules = [f["rule"] for f in self.scan("cf", oom)]
        self.assertIn("cuda_oom", rules)
        self.assertNotIn("runtime_error", rules)
        imp = ("Traceback (most recent call last):\n  File \"F:\\ComfyUI\\nodes.py\", line 2, in load\n"
               "FileNotFoundError: [Errno 2] No such file\n\nCannot import F:\\ComfyUI\\custom_nodes\\tp module for custom "
               "nodes: [Errno 2] No such file\n")
        self.assertEqual([f["rule"] for f in self.scan("cf", imp)], ["comfy_import_failed"])

    def test_interrupt_and_noise_ignored(self):
        text = ("Traceback (most recent call last):\n  File \"x.py\", line 1, in f\nKeyboardInterrupt\n"
                "Processing interrupted\nPrompt executed in 1.0 seconds\nsome Error word in a normal line\n")
        self.assertEqual(self.scan("cf", text), [])

    def test_validation_failure(self):
        text = ("got prompt\nFailed to validate prompt for output 9:\n* CheckpointLoaderSimple 4:\n"
                "  - Value not in list: ckpt_name: 'gone.safetensors' not in ['a.safetensors']\n"
                "Output will be ignored\ninvalid prompt: {'type': 'prompt_outputs_failed_validation'}\n")
        f = self.one(self.scan("cf", text), "runtime_error")
        self.assertIn("gone.safetensors", f["text"])

    def test_launcher_kind_not_affected(self):
        import diagnose as dg
        self.assertEqual(dg.scan(COMFY_EXC, dg.launcher_ctx(self.api)), [])


if __name__ == "__main__":
    unittest.main()
