# -*- coding: utf-8 -*-
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import mirror_manager as mm  # noqa: E402
import torch_bootstrap as tb  # noqa: E402

OFF = mm.PYTORCH_OFFICIAL_WHL
SJTU, TUNA = mm.PYTORCH_MIRROR_BASES[0], mm.PYTORCH_MIRROR_BASES[1]


def _rank(pypi, torch):
    return {"time": time.time(), "pypi": pypi, "torch": torch}


class TorchIndexOrderTest(unittest.TestCase):
    def test_torch_rank_wins_over_pypi_rank(self):
        # 实测：PyPI 腾讯云最快（国内镜像），但 torch 官方 10 MB/s、交大镜像 250 KB/s → 先试官方
        cfg = {"mirror_mode": "auto", "source_rank": _rank(
            [{"id": "https://mirrors.cloud.tencent.com/pypi/simple", "bps": 10e6}, {"id": "", "bps": 7e6}],
            [{"id": OFF, "bps": 10.4e6}, {"id": SJTU, "bps": 250e3}, {"id": TUNA, "bps": 0}])}
        b = tb.index_bases(cfg, "cu130")
        self.assertEqual(b[0], f"{OFF}/cu130")
        self.assertEqual(b[1], f"{SJTU}/cu130")
        self.assertEqual(len(b), 3)   # 不通的也留在最后兜底

    def test_mirror_first_when_mirror_faster(self):
        cfg = {"mirror_mode": "auto", "source_rank": _rank(
            [{"id": "", "bps": 1e6}], [{"id": OFF, "bps": 100e3}, {"id": SJTU, "bps": 8e6}])}
        self.assertEqual(tb.index_bases(cfg, "cu128")[0], f"{SJTU}/cu128")

    def test_never_keeps_official_first(self):
        b = tb.index_bases({"mirror_mode": "never"}, "cu126")
        self.assertEqual(b[0], f"{OFF}/cu126")
        self.assertEqual(len(set(b)), len(b))


if __name__ == "__main__":
    unittest.main()
