"""
LoRA 自动整理的底模分类（model_library.normalize_base）：和 LoRA 训练器送过来的 baseModel 落进同一个文件夹，
Civitai 上的各种写法也认得；不通用的底模分开放。运行：python -m unittest tests.test_lora_base
"""
import os as _os, sys as _sys  # app_dir_for_tests：被测模块在 app/ 子目录里
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import unittest

import model_library as ml   # noqa: E402


class NormalizeBaseTest(unittest.TestCase):
    def check(self, cases):
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(ml.normalize_base(raw), want)

    def test_trainer_labels_map_to_themselves(self):
        # 训练器 app/models/*.py 的 base_label
        self.check([("MiniMax H3", "MiniMax H3"), ("MiniMax H3 Image", "MiniMax H3 Image"),
                    ("Qwen-Image", "Qwen-Image"), ("Qwen-Image-Edit", "Qwen-Image-Edit"),
                    ("Qwen-Image-2.1", "Qwen-Image-2.1"), ("Z-Image", "Z-Image"), ("FLUX.2 Klein", "FLUX.2 Klein"),
                    ("Krea 2", "Krea 2"), ("MOSS", "MOSS"), ("Anima", "Anima"), ("SDXL", "SDXL")])

    def test_civitai_spellings(self):
        self.check([("Qwen", "Qwen-Image"), ("Flux.2 Klein 9B", "FLUX.2 Klein"), ("Flux.2 Klein 4B-base", "FLUX.2 Klein"),
                    ("Flux.2 D", "FLUX.2"), ("Flux.1 D", "Flux"), ("Flux.1 Krea", "Flux"), ("ZImageTurbo", "Z-Image"),
                    ("ZImageBase", "Z-Image"), ("Wan Video 2.2 T2V-A14B", "Wan"), ("Illustrious", "Illustrious"),
                    ("SDXL 1.0", "SDXL"), ("Pony", "Pony"), ("SD 1.5", "SD1.5"), ("MiniMax-H3", "MiniMax H3")])

    def test_unknown_and_empty(self):
        self.assertEqual(ml.normalize_base("Some New Model"), "Some New Model")
        self.assertEqual(ml.normalize_base("", "SDXL LoRA"), "SDXL")
        self.assertEqual(ml.normalize_base(""), "")


if __name__ == "__main__":
    unittest.main()
