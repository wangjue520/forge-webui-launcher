"""
「图片反推」页 WD14 / PixAI 标签模型（app/wd14_tagger_core.py）的回归测试。

核心：按 ONNX 输入布局区分 WD 系列（NHWC、BGR、0~255、补白边）和 PixAI（NCHW、RGB、[-1,1]、直接拉伸），
     PixAI 取 prediction 输出、认出角色时补作品名，WD 系列行为不变。
不需要真模型：用假的 onnxruntime.InferenceSession 记录喂进去的数组。
运行：wd14_venv\\Scripts\\python -m unittest tests.test_wd14_tagger（需要 numpy / pillow）
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import sys
import tempfile
import types
import unittest

try:
    import numpy as np
    from PIL import Image
except ImportError:   # 启动器主环境没有 numpy / pillow（它们只装在 wd14_venv 里）
    raise unittest.SkipTest("需要 numpy / pillow，请用 wd14_venv 的 python 运行")

import wd14_tagger_core as wt  # noqa: E402

_WD_CSV = (
    "tag_id,name,category,count\n"
    "1,general,9,1\n"
    "2,explicit,9,1\n"
    "3,1girl,0,1\n"
    "4,^_^,0,1\n"
    "5,long_hair,0,1\n"
    "6,hatsune_miku,4,1\n"
)

_PIXAI_CSV = (
    "id,tag_id,name,category,count,ips\n"
    "0,1,1girl,0,1,[]\n"
    "1,2,long_hair,0,1,[]\n"
    "2,3,hu_tao_(genshin_impact),4,1,\"[\"\"genshin_impact\"\"]\"\n"
    "3,4,boo_tao_(genshin_impact),4,1,\"[\"\"genshin_impact\"\"]\"\n"
    "4,5,hatsune_miku,4,1,\"[\"\"vocaloid\"\"]\"\n"
)


class _Node:
    def __init__(self, name, shape=None):
        self.name, self.shape = name, shape


class _FakeSession:
    def __init__(self, input_shape, outputs, probs):
        self._in = [_Node("input", input_shape)]
        self._out = [_Node(n) for n in outputs]
        self._probs = np.asarray(probs, dtype=np.float32)
        self.fed = None
        self.asked = None

    def get_inputs(self):
        return self._in

    def get_outputs(self):
        return self._out

    def run(self, names, feed):
        self.asked = names
        self.fed = next(iter(feed.values()))
        return [self._probs[None, :]]


class TaggerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.session = None
        fake = types.ModuleType("onnxruntime")
        fake.InferenceSession = lambda *a, **k: self.session
        self._old = sys.modules.get("onnxruntime")
        sys.modules["onnxruntime"] = fake
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            sys.modules.pop("onnxruntime", None)
        else:
            sys.modules["onnxruntime"] = self._old

    def _make(self, csv_text, session):
        with open(os.path.join(self.dir, "selected_tags.csv"), "w", encoding="utf-8") as f:
            f.write(csv_text)
        self.session = session
        return wt.WD14Tagger(self.dir)

    def test_wd_unchanged(self):
        s = _FakeSession(["batch", 448, 448, 3], ["output0"], [0.9, 0.1, 0.8, 0.5, 0.2, 0.95])
        t = self._make(_WD_CSV, s)
        self.assertFalse(t.nchw)
        self.assertEqual(t.target_size, 448)
        img = Image.new("RGB", (40, 20), (255, 0, 0))
        r = t.tag_image(img, 0.35, 0.85, include_rating=True)
        self.assertEqual(s.fed.shape, (1, 448, 448, 3))
        # BGR、0~255、上下补白边
        self.assertEqual(s.fed[0, 224, 224].tolist(), [0.0, 0.0, 255.0])
        self.assertEqual(s.fed[0, 0, 224].tolist(), [255.0, 255.0, 255.0])
        self.assertEqual(r["tags"], ["hatsune miku", "1girl", "^_^"])
        self.assertEqual(r["rating"][0], "general")
        self.assertEqual(r["copyright"], [])

    def test_pixai_preprocess_and_output(self):
        s = _FakeSession(["batch", 3, 448, 448], ["embedding", "logits", "prediction"],
                         [0.95, 0.31, 0.99, 0.9, 0.2])
        t = self._make(_PIXAI_CSV, s)
        self.assertTrue(t.nchw)
        self.assertEqual(t.target_size, 448)
        self.assertEqual(t.output_name, "prediction")
        img = Image.new("RGB", (40, 20), (255, 0, 0))
        r = t.tag_image(img, 0.3, 0.85, include_rating=True)
        self.assertEqual(s.asked, ["prediction"])
        self.assertEqual(s.fed.shape, (1, 3, 448, 448))
        self.assertEqual(s.fed.dtype, np.float32)
        # RGB、拉伸不补边（角上也是红色）、归一化到 [-1, 1]
        self.assertEqual(s.fed[0, :, 0, 0].tolist(), [1.0, -1.0, -1.0])
        self.assertEqual(s.fed[0, :, 224, 224].tolist(), [1.0, -1.0, -1.0])
        # 角色 -> 作品名（去重）-> 常规；没有评分类
        self.assertEqual(r["tags"], ["hu tao (genshin impact)", "boo tao (genshin impact)",
                                     "genshin impact", "1girl", "long hair"])
        self.assertEqual(r["copyright"], ["genshin_impact"])
        self.assertNotIn("rating", r)

    def test_pixai_transparent_gets_white_background(self):
        s = _FakeSession(["batch", 3, 448, 448], ["prediction"], [0, 0, 0, 0, 0])
        t = self._make(_PIXAI_CSV, s)
        t.tag_image(Image.new("RGBA", (16, 16), (0, 0, 0, 0)))
        self.assertTrue(np.allclose(s.fed, 1.0))

    def test_dynamic_shape_falls_back_to_ips_column(self):
        s = _FakeSession(["batch", "c", "h", "w"], ["prediction"], [0, 0, 0, 0, 0])
        t = self._make(_PIXAI_CSV, s)
        self.assertTrue(t.nchw)
        self.assertEqual(t.target_size, 448)

    def test_catalog_entry(self):
        self.assertEqual(wt.MODEL_CATALOG["pixai-tagger-v0.9"]["repo"], "deepghs/pixai-tagger-v0.9-onnx")
        self.assertIn("https://hf-mirror.com/deepghs/pixai-tagger-v0.9-onnx/tree/main",
                      wt.model_repo_urls("pixai-tagger-v0.9"))


if __name__ == "__main__":
    unittest.main()
