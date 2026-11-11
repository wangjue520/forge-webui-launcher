# -*- coding: utf-8 -*-
"""图片信息页缺失模型下载的完成判定与事件回归测试。"""
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import civitai_downloader as cd  # noqa: E402
import webview_api as wa  # noqa: E402


class FinishedThread:
    @staticmethod
    def is_alive():
        return False


class MetaDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="meta_download_")
        self.events = []
        self.api = types.SimpleNamespace(
            cfg={"webui_root": self.tmp.name, "webui_branch": "classic",
                 "model_library_enabled": False, "civitai_api_key": ""},
            _meta_refs=[{"role": "LoRA", "name": "demo"}],
            _meta_lookup_result={0: {
                "ok": True,
                "version_info": {"model_type": "LORA", "base_model": "SD 1.5", "raw": {}},
                "file_info": {"name": "demo.safetensors", "sizeKB": 1,
                              "downloadUrl": "https://example.invalid/demo", "hashes": {}},
            }},
            _meta_local={}, _meta_dl_thread=None, _meta_dl_cancel=False,
            _meta_dl_lock=threading.Lock(),
        )
        self.api._emit = lambda scope, type_, **data: self.events.append(
            {"scope": scope, "type": type_, **data})
        self.api._spawn = self.spawn

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def spawn(work, name="worker"):
        work()
        return FinishedThread()

    def events_of(self, type_, scope="meta"):
        return [e for e in self.events if e["scope"] == scope and e["type"] == type_]

    def test_hash_failure_is_never_reported_as_success(self):
        broken = os.path.join(self.tmp.name, "models", "Lora", "demo.safetensors.broken")
        os.makedirs(os.path.dirname(broken), exist_ok=True)
        with open(broken, "wb") as f:
            f.write(b"bad")
        with mock.patch.object(cd, "download_file", return_value=(broken, False)), \
                mock.patch.object(cd, "save_sidecar_metadata") as save_meta, \
                mock.patch.object(cd, "download_preview_image") as save_preview:
            result = wa._api_meta_download(self.api, [0])
        self.assertTrue(result["ok"])
        item = self.events_of("dl_item")[-1]
        done = self.events_of("dl_done")[-1]
        self.assertFalse(item["ok"])
        self.assertIn("SHA-256", item["error"])
        self.assertEqual(done["failed"], 1)
        self.assertFalse(done["ok"])
        save_meta.assert_not_called()
        save_preview.assert_not_called()
        self.assertFalse(self.events_of("changed", "models"))

    def test_success_requires_real_file_and_refreshes_models(self):
        def download(_info, dest, _key, progress_cb, cancel_flag):
            os.makedirs(dest, exist_ok=True)
            path = os.path.join(dest, "demo.safetensors")
            with open(path, "wb") as f:
                f.write(b"complete-model")
            progress_cb(14, 14)
            return path, True

        with mock.patch.object(cd, "download_file", side_effect=download), \
                mock.patch.object(wa, "_relocate_by_content", side_effect=lambda _api, path, *args: path), \
                mock.patch.object(cd, "save_sidecar_metadata"), \
                mock.patch.object(cd, "download_preview_image"):
            wa._api_meta_download(self.api, [0])

        item = self.events_of("dl_item")[-1]
        done = self.events_of("dl_done")[-1]
        self.assertTrue(item["ok"])
        self.assertTrue(os.path.isfile(item["path"]))
        self.assertEqual(item["file_name"], "demo.safetensors")
        self.assertEqual(self.api._meta_local[0], item["path"])
        self.assertTrue(done["ok"])
        self.assertEqual(done["success"], 1)
        self.assertEqual(self.events_of("changed", "models")[-1]["paths"], [item["path"]])

    def test_missing_final_file_is_failure(self):
        missing = os.path.join(self.tmp.name, "models", "Lora", "missing.safetensors")
        with mock.patch.object(cd, "download_file", return_value=(missing, None)):
            wa._api_meta_download(self.api, [0])
        self.assertFalse(self.events_of("dl_item")[-1]["ok"])
        self.assertIn("没有找到有效的模型文件", self.events_of("dl_item")[-1]["error"])
        self.assertEqual(self.events_of("dl_done")[-1]["failed"], 1)


class IncompleteResponse:
    status_code = 200
    url = "https://example.invalid/demo"
    headers = {"content-length": "10", "content-type": "application/octet-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield b"abc"


class DownloadIntegrityTests(unittest.TestCase):
    def test_short_response_stays_part_and_never_becomes_model(self):
        with tempfile.TemporaryDirectory(prefix="short_download_") as tmp:
            info = {"name": "short.safetensors", "downloadUrl": "https://example.invalid/demo",
                    "sizeKB": 1, "hashes": {}}
            with mock.patch.object(cd.requests, "get", return_value=IncompleteResponse()):
                with self.assertRaises(cd.CivitaiError) as ctx:
                    cd.download_file(info, tmp)
            self.assertIn("下载不完整", str(ctx.exception))
            self.assertFalse(os.path.exists(os.path.join(tmp, "short.safetensors")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "short.safetensors.part")))


if __name__ == "__main__":
    unittest.main()
