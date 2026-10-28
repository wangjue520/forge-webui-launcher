# -*- coding: utf-8 -*-
"""adetailer_models：替 ADetailer-Neo 备好检测模型"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import adetailer_models as adm  # noqa: E402


class _Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        common = os.path.join(self.root, "extensions", "ADetailer-Neo", "lib_adetailer", "detection")
        os.makedirs(common)
        open(os.path.join(common, "common.py"), "w").close()
        self.ad = adm.model_dir(self.root)
        os.makedirs(self.ad)
        self.logs = []

    def log(self, text):
        self.logs.append(text)

    def touch(self, name, size=1, age=0):
        p = os.path.join(self.ad, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def ui_config(self, **kw):
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump(kw, f)


class FindNeoTest(_Root):
    def test_enabled(self):
        self.assertTrue(adm.find_neo(self.root).endswith("ADetailer-Neo"))

    def test_disabled_in_webui(self):
        self.ui_config(disabled_extensions=["ADetailer-Neo"])
        self.assertIsNone(adm.find_neo(self.root))

    def test_all_extensions_disabled(self):
        self.ui_config(disable_all_extensions="extra")
        self.assertIsNone(adm.find_neo(self.root))

    def test_original_adetailer_is_not_neo(self):
        other = tempfile.mkdtemp(dir=self.root)
        os.makedirs(os.path.join(other, "extensions", "adetailer", "adetailer"))
        open(os.path.join(other, "extensions", "adetailer", "adetailer", "common.py"), "w").close()
        self.assertIsNone(adm.find_neo(other))


class CleanupTest(_Root):
    def test_removes_old_partials_only(self):
        old_new_style = self.touch("yolov8x-worldv2.pt.0798dc71830b435998adba078930039d.partial", 100, age=600)
        old_tmp = self.touch("tmpk3j9x2ab", 50, age=600)
        fresh = self.touch("face_yolov8n.pt.ad646f4234af4cbdbfe258436c1ce568.partial", 10)
        ours = self.touch("face_yolov8s.pt.part", 10, age=600)      # 启动器自己的续传文件，要留着
        model = self.touch("hand_yolov8n.pt", 10, age=600)
        n, size = adm.cleanup_partials(self.ad)
        self.assertEqual((n, size), (2, 150))
        self.assertFalse(os.path.exists(old_new_style))
        self.assertFalse(os.path.exists(old_tmp))
        for p in (fresh, ours, model):
            self.assertTrue(os.path.exists(p))


class SourcesTest(unittest.TestCase):
    def test_hf_order(self):
        f = next(x for x in adm.FILES if x["name"] == "face_yolov8n.pt")
        self.assertIn("hf-mirror.com", adm.sources(f, True, False)[0][0])
        self.assertIn("huggingface.co", adm.sources(f, False, True)[0][0])

    def test_github_proxies(self):
        f = next(x for x in adm.FILES if x["name"] == "yolov8x-worldv2.pt")
        s = adm.sources(f, False, True, ["https://ghfast.top", "https://gh-proxy.com/"])
        self.assertEqual([u for u, _ in s], [
            "https://ghfast.top/" + adm.GHA, "https://gh-proxy.com/" + adm.GHA, adm.GHA])

    def test_mediapipe_copies_require_hash(self):
        f = next(x for x in adm.FILES if x["name"] == "face_landmarker.task")
        s = adm.sources(f, True, True)
        self.assertTrue(all(sha for u, sha in s if "hf-mirror" in u))
        self.assertIsNone(s[-1][1])   # Google 官方地址不校验（latest 会更新）


class PrepareTest(_Root):
    def setUp(self):
        super().setUp()
        self.started = threading.Event()
        p = mock.patch.object(adm, "_download_all", side_effect=self._fake_download)
        p.start()
        self.addCleanup(p.stop)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _fake_download(self, d, todo, cfg, log):
        self.started.set()
        self.release.wait(5)
        with adm._lock:
            adm._busy.discard(d)

    def all_present(self):
        for f in adm.FILES:
            self.touch(f["name"])

    def test_nothing_missing(self):
        self.all_present()
        self.assertEqual(adm.prepare(self.root, {}, self.log), "")
        self.assertFalse(self.started.is_set())

    def test_missing_adds_flag_and_downloads(self):
        self.all_present()
        os.remove(os.path.join(self.ad, "yolov8x-worldv2.pt"))
        self.touch("yolov8x-worldv2.pt.44b9f54061c44147a2400c8cc2754fc7.partial", 5, age=600)
        self.assertEqual(adm.prepare(self.root, {}, self.log), adm.FLAG)
        self.assertTrue(self.started.wait(2))
        text = "".join(self.logs)
        self.assertIn("清掉了", text)
        self.assertIn("yolov8x-worldv2.pt", text)
        # 第二次启动时还在下：不重复开线程，照样加参数
        self.started.clear()
        self.assertEqual(adm.prepare(self.root, {}, self.log), adm.FLAG)
        self.assertFalse(self.started.wait(0.3))
        self.assertIn("还在后台下载", "".join(self.logs))

    def test_user_flag_not_duplicated(self):
        self.assertEqual(adm.prepare(self.root, {"extra_args": "--xformers --ad-no-huggingface"}, self.log), "")
        self.assertTrue(self.started.wait(2))   # 已经不让它自己下了，后台照样补齐

    def test_extensions_disabled_by_args(self):
        self.assertEqual(adm.prepare(self.root, {"extra_args": "--disable-extra-extensions"}, self.log), "")
        self.assertFalse(self.started.is_set())

    def test_neo_disabled(self):
        self.ui_config(disabled_extensions=["ADetailer-Neo"])
        self.assertEqual(adm.prepare(self.root, {}, self.log), "")


class DownloadOneTest(_Root):
    def test_falls_through_sources(self):
        f = {"name": "face_landmarker.task", "size": 100}
        calls = []

        def fake(info, dest_dir, progress_cb=None):
            calls.append(info["downloadUrl"])
            path = os.path.join(dest_dir, info["name"])
            if len(calls) == 1:
                raise OSError("connection timed out")
            if len(calls) == 2:
                open(path + ".broken", "wb").close()
                return path + ".broken", False             # 内容和官方不一致
            if len(calls) == 3:
                with open(path, "wb") as fp:
                    fp.write(b"x" * 10)                    # 提前断开、没报错
                return path, None
            with open(path, "wb") as fp:
                fp.write(b"x" * 100)
            return path, True

        import civitai_downloader as cd
        with mock.patch.object(cd, "download_file", side_effect=fake):
            ok = adm.download_one(self.ad, f, [("https://a/1", None), ("https://b/2", "ab"),
                                               ("https://c/3", None), ("https://d/4", "cd")], self.log)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sorted(os.listdir(self.ad)), ["face_landmarker.task"])   # .broken 和不完整的都清掉了

    def test_all_fail(self):
        import civitai_downloader as cd
        with mock.patch.object(cd, "download_file", side_effect=OSError("timed out")):
            self.assertFalse(adm.download_one(self.ad, {"name": "x.pt", "size": 1}, [("https://a/1", None)], self.log))


if __name__ == "__main__":
    unittest.main()
