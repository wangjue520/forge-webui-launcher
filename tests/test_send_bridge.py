"""
「发送到 WebUI / ComfyUI」：
  - send_bridge 的转换：ComfyUI 图 → infotext、WebUI 图 → 默认工作流、LoRA 调用格式去掉文件夹
  - 两个桥接插件的「先存着、网页打开再取」和回执
  - webview_api.send_to 整条链路（用本机假服务器扮 WebUI / ComfyUI 和网页）
运行：python -m unittest tests.test_send_bridge
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import importlib.util
import json
import os
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import comfy_bridge  # noqa: E402
import model_catalog as mc  # noqa: E402
import send_bridge as sb  # noqa: E402
from meta_engine.metadata_types import ImageMetadata, SamplerParameters  # noqa: E402
from meta_engine.webui_parser import WebUIParser  # noqa: E402
from test_diagnose import _Base  # noqa: E402

LORA_PATH = "Anima\\画风\\c站候选\\Artistic_Anima_update_v2_epoch_20.safetensors"

COMFY_API = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "IL\\waiNSFW_v140.safetensors"}},
    "10": {"class_type": "LoraLoader", "inputs": {"lora_name": LORA_PATH, "strength_model": 0.8,
                                                  "strength_clip": 0.8, "model": ["4", 0], "clip": ["4", 1]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "1girl, solo, embedding:easynegative", "clip": ["10", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "lowres", "clip": ["10", 1]}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
    "3": {"class_type": "KSampler", "inputs": {"seed": 7, "steps": 28, "cfg": 5.5, "sampler_name": "dpmpp_2m",
                                               "scheduler": "karras", "denoise": 1.0, "model": ["10", 0],
                                               "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x", "images": ["8", 0]}},
}

WEBUI_TEXT = ("masterpiece, 1girl, <lora:Artistic_Anima_update_v2_epoch_20:0.8>, smile BREAK solo\n"
              "Negative prompt: easynegative, lowres\n"
              "Steps: 30, Sampler: DPM++ 2M, Schedule type: Automatic, CFG scale: 5, Seed: 42, Size: 832x1216, "
              "Model hash: abcdef1234, Model: waiNSFW_v140, Denoising strength: 0.4, Clip skip: 2, "
              "ADetailer model: face_yolov8n.pt, Hires upscale: 1.5, Hires steps: 15, "
              "Hires upscaler: R-ESRGAN 4x+ Anime6B, Version: f2.0.1v1.10.1")


def comfy_meta():
    # 启动器不带 Pillow：用标准库手写一张 8×8 的 PNG，prompt 放在 tEXt 块里（ComfyUI 就是这么存的）
    import meta_engine as me
    import struct
    import tempfile
    import zlib

    def chunk(ctype, body):
        return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)

    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.png")
    with open(p, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
                + chunk(b"tEXt", b"prompt\x00" + json.dumps(COMFY_API).encode("ascii"))
                + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00" * 24) * 8))
                + chunk(b"IEND", b""))
    return me.read_metadata(p)


def check_graph(t, wf):
    """UI 格式工作流自洽：每条连线两头的节点、槽位、类型都对得上"""
    nodes = {n["id"]: n for n in wf["nodes"]}
    t.assertEqual(wf["last_node_id"], max(nodes))
    for lid, src, sslot, dst, dslot, typ in wf["links"]:
        t.assertIn(lid, nodes[src]["outputs"][sslot]["links"])
        t.assertEqual(nodes[dst]["inputs"][dslot]["link"], lid)
        t.assertEqual(nodes[src]["outputs"][sslot]["type"], typ)
        t.assertEqual(nodes[dst]["inputs"][dslot]["type"], typ)
    for n in wf["nodes"]:
        for i in n["inputs"]:
            t.assertIsNotNone(i["link"], f"{n['type']} 的输入 {i['name']} 没连上")
    return nodes


class ConvertTest(unittest.TestCase):
    def test_lora_stem(self):
        self.assertEqual(sb.lora_stem(LORA_PATH), "Artistic_Anima_update_v2_epoch_20")
        self.assertEqual(sb.lora_stem("Anima/画风/x.y.safetensors"), "x.y")
        self.assertEqual(sb.lora_tag(LORA_PATH, 1.0), "<lora:Artistic_Anima_update_v2_epoch_20:1>")
        self.assertEqual(sb.lora_tags([{"name": LORA_PATH, "strength_model": 0.8}, {"name": "a\\b.safetensors"},
                                       {"name": "Artistic_Anima_update_v2_epoch_20"}]),
                         "<lora:Artistic_Anima_update_v2_epoch_20:0.8>, <lora:b:1>")

    def test_samplers(self):
        self.assertEqual(sb.comfy_sampler("DPM++ 2M Karras"), ("dpmpp_2m", "karras"))
        self.assertEqual(sb.comfy_sampler("DPM++ 2M", "Automatic"), ("dpmpp_2m", "karras"))
        self.assertEqual(sb.comfy_sampler("Euler a", "Automatic"), ("euler_ancestral", "normal"))
        self.assertEqual(sb.comfy_sampler("什么鬼", "Simple"), ("euler", "simple"))
        self.assertEqual(sb.comfy_sampler("什么鬼", strict=True), (None, None))
        self.assertEqual(sb.webui_sampler("euler_ancestral", "karras"), ("Euler a", "Karras"))
        self.assertEqual(sb.webui_sampler("k_dpmpp_2m", "native"), ("DPM++ 2M", None))
        self.assertEqual(sb.webui_sampler("Euler a", "Karras"), ("Euler a", "Karras"))
        self.assertEqual(sb.webui_sampler("res_multistep", "beta"), (None, "Beta"))

    def test_comfy_to_infotext(self):
        r = sb.to_infotext(comfy_meta())
        m = WebUIParser.parse_text(r["text"])
        self.assertIn("<lora:Artistic_Anima_update_v2_epoch_20:0.8>", m.positive_prompt)
        self.assertNotIn("Anima\\", m.positive_prompt)
        self.assertIn("easynegative", m.positive_prompt)
        self.assertNotIn("embedding:", m.positive_prompt)
        self.assertEqual((m.params.sampler_name, m.params.scheduler), ("DPM++ 2M", "Karras"))
        self.assertEqual((m.params.steps, m.params.seed, m.params.size), (28, 7, "832x1216"))
        self.assertEqual(m.params.model_name, "waiNSFW_v140")

    def test_webui_raw_untouched(self):
        m = WebUIParser.parse_text(WEBUI_TEXT)
        r = sb.to_infotext(m)
        self.assertEqual(r["mode"], "raw")
        self.assertEqual(r["text"], WEBUI_TEXT)

    def test_unknown_sampler_left_out(self):
        m = ImageMetadata(source_type="ComfyUI", positive_prompt="a")
        m.params = SamplerParameters(steps=10, sampler_name="res_multistep", scheduler="karras", seed=1)
        r = sb.to_infotext(m)
        self.assertNotIn("Sampler:", r["text"])
        self.assertTrue(any("res_multistep" in n for n in r["notes"]))

    def test_webui_to_workflow(self):
        m = WebUIParser.parse_text(WEBUI_TEXT)
        lists = {"checkpoints": ["IL\\waiNSFW_v140.safetensors"], "loras": [LORA_PATH],
                 "upscale": ["RealESRGAN_x4plus_anime_6B.pth"], "embeddings": ["easynegative"]}
        r = sb.comfy_payload(m, lists, mc.load())
        self.assertEqual(r["mode"], "built")
        self.assertEqual(r["missing"], [])
        nodes = check_graph(self, r["workflow"])
        by = {}
        for n in nodes.values():
            by.setdefault(n["type"], []).append(n)
        self.assertEqual(by["CheckpointLoaderSimple"][0]["widgets_values"], ["IL\\waiNSFW_v140.safetensors"])
        self.assertEqual(by["LoraLoader"][0]["widgets_values"], [LORA_PATH, 0.8, 0.8])
        self.assertEqual(by["CLIPSetLastLayer"][0]["widgets_values"], [-2])
        texts = [n["widgets_values"][0] for n in by["CLIPTextEncode"]]
        self.assertEqual(texts[0], "masterpiece, 1girl, smile, solo")
        self.assertEqual(texts[1], "embedding:easynegative, lowres")
        ks = by["KSampler"]
        self.assertEqual(ks[0]["widgets_values"], [42, "fixed", 30, 5.0, "dpmpp_2m", "karras", 1.0])
        self.assertEqual(ks[1]["widgets_values"][2], 15)
        self.assertEqual(ks[1]["widgets_values"][6], 0.4)
        self.assertEqual(by["UpscaleModelLoader"][0]["widgets_values"], ["RealESRGAN_x4plus_anime_6B.pth"])
        self.assertEqual(by["ImageScaleBy"][0]["widgets_values"], ["lanczos", 0.375])
        self.assertTrue(any("ADetailer" in n for n in r["notes"]))
        self.assertTrue(any("BREAK" in n for n in r["notes"]))
        self.assertIn("Note", by)

    def test_anima_split_loaders(self):
        m = WebUIParser.parse_text("1girl\nNegative prompt: bad\nSteps: 30, Sampler: Euler a, Schedule type: Simple, "
                                   "CFG scale: 4, Seed: -1, Size: 1024x1024, Model: anima-aesthetic-v1.1, "
                                   "Module 1: qwen_image_vae, Module 2: qwen_3_06b_base")
        lists = {"unet": ["anima\\anima-aesthetic-v1.1.safetensors"], "clip": ["qwen_3_06b_base.safetensors"],
                 "vae": ["qwen_image_vae.safetensors"]}
        r = sb.build_workflow(m, lists, mc.load())
        nodes = check_graph(self, r["workflow"])
        types = {n["type"]: n["widgets_values"] for n in nodes.values()}
        self.assertEqual(types["UNETLoader"], ["anima\\anima-aesthetic-v1.1.safetensors", "default"])
        self.assertEqual(types["CLIPLoader"], ["qwen_3_06b_base.safetensors", "stable_diffusion", "default"])
        self.assertEqual(types["VAELoader"], ["qwen_image_vae.safetensors"])
        self.assertEqual(types["KSampler"][:2], [0, "randomize"])
        self.assertNotIn("CheckpointLoaderSimple", types)
        self.assertEqual(r["missing"], [])

    def test_missing_models_flagged(self):
        m = WebUIParser.parse_text("a, <lora:nope:0.5>\nSteps: 20, Sampler: Euler, CFG scale: 7, Seed: 1, "
                                   "Size: 512x512, Model: something")
        r = sb.build_workflow(m, {"checkpoints": [], "loras": []}, mc.load())
        check_graph(self, r["workflow"])
        self.assertIn("something", r["missing"])
        self.assertIn("nope", r["missing"])

    def test_comfy_image_keeps_original(self):
        m = comfy_meta()
        self.assertFalse(sb.needs_lists(m))
        self.assertEqual(sb.comfy_payload(m)["mode"], "api")
        m.raw_workflow_json = {"nodes": [{"id": 1}], "links": []}
        r = sb.comfy_payload(m)
        self.assertEqual(r["mode"], "original")
        self.assertIs(r["workflow"], m.raw_workflow_json)

    def test_no_params(self):
        with self.assertRaises(ValueError):
            sb.comfy_payload(ImageMetadata())


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


WEBUI_EXT = os.path.join(comfy_bridge.WEBUI_SRC, "scripts", "wwy_launcher_bridge.py")
COMFY_PLUGIN = os.path.join(comfy_bridge.SRC, "__init__.py")


class PluginStateTest(unittest.TestCase):
    def test_webui_pending(self):
        m = _load(WEBUI_EXT, "wwy_webui_ext_test")
        self.assertIsNone(m.take())
        m.put({"id": "a"}, now=100)
        self.assertEqual(m.take(now=101), {"id": "a"})
        self.assertIsNone(m.take(now=101))           # 取一次就没了：两个标签页不会都套一遍
        m.put({"id": "b"}, now=100)
        self.assertIsNone(m.take(now=100 + m.PENDING_TTL + 1))
        self.assertEqual(m.clients(now=time.time() + 100), 0)
        m._state["waiting"] = 2
        self.assertEqual(m.clients(), 2)
        m.store_ack({"id": "x", "ok": True, "text": "好"})
        self.assertEqual(m._acks["x"], {"ok": True, "text": "好"})

    def test_comfy_pending(self):
        m = _load(COMFY_PLUGIN, "wwy_comfy_plugin_test")
        m.put_pending({"id": "a"}, now=10)
        self.assertEqual(m.take_pending(now=11), {"id": "a"})
        self.assertIsNone(m.take_pending(now=11))
        m.put_pending({"id": "b"}, now=10)
        self.assertIsNone(m.take_pending(now=10 + m.PENDING_TTL + 1))


class _Fake(BaseHTTPRequestHandler):
    """扮演装了桥接插件的 WebUI / ComfyUI，外加一个开着的网页（收到就回执）"""
    srv = None

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        st = self.server.st
        if self.path.startswith("/wwy_bridge/ack/"):
            return self._json({"ok": True, "ack": st["acks"].get(self.path.rsplit("/", 1)[-1])})
        if self.path.startswith("/object_info/"):
            cls = self.path.rsplit("/", 1)[-1]
            spec = {"LoraLoader": {"lora_name": [[LORA_PATH]]},
                    "CheckpointLoaderSimple": {"ckpt_name": ["COMBO", {"options": ["IL\\waiNSFW_v140.safetensors"]}]}}
            if cls in spec:
                return self._json({cls: {"input": {"required": spec[cls]}}})
            return self._json({})
        if self.path == "/embeddings":
            return self._json(["easynegative"])
        self._json({}, 404)

    def do_POST(self):
        st = self.server.st
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != st["endpoint"]:
            return self._json({}, 404)
        st["got"].append(data)
        if st["clients"]:
            st["acks"][data["id"]] = {"ok": st["ack_ok"], "text": "网页已套用" if st["ack_ok"] else "找不到按钮"}
        self._json({"ok": True, "clients": st["clients"], "queued": not st["clients"]})


class _Runner:
    def __init__(self, url):
        self.url, self.port = url, None

    def running(self):
        return True

    def status(self):
        return {"running": True, "state": "ready", "url": self.url}


class SendToTest(_Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
        self.srv.st = {"got": [], "acks": {}, "clients": 1, "ack_ok": True, "endpoint": "/wwy_bridge/send"}
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.opened = []
        self.api.open_url = lambda u: self.opened.append(u) or {"ok": True}
        self.api._meta_meta = WebUIParser.parse_text(WEBUI_TEXT)
        sb_wait = sb.wait_ack
        self._p = mock.patch.object(sb, "wait_ack", lambda b, c, w=8.0, s=None: sb_wait(b, c, 1.5, s, 0.05))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.srv.shutdown()
        super().tearDown()

    def test_not_running(self):
        r = self.api.send_to("webui", {"from": "meta"})
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("not_running"))

    def test_webui_delivered(self):
        self.api._runners["neo"] = _Runner(self.base + "/")
        r = self.api.send_to("webui", {"from": "meta"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["text"], "网页已套用")
        self.assertEqual(self.srv.st["got"][0]["infotext"], WEBUI_TEXT)
        self.assertEqual(self.srv.st["got"][0]["tab"], "txt2img")

    def test_webui_ack_failure(self):
        self.api._runners["neo"] = _Runner(self.base)
        self.srv.st["ack_ok"] = False
        r = self.api.send_to("webui", {"from": "meta"})
        self.assertFalse(r["ok"])
        self.assertIn("找不到按钮", r["error"])

    def test_page_closed_opens_browser(self):
        self.api._runners["neo"] = _Runner(self.base)
        self.srv.st["clients"] = 0
        r = self.api.send_to("webui", {"from": "meta"})
        self.assertTrue(r["ok"] and r["queued"])
        self.assertEqual(self.opened, [self.base])

    def test_choose_when_several_running(self):
        self.api._runners["neo"] = _Runner(self.base)
        self.api._runners["old"] = _Runner(self.base)
        r = self.api.send_to("webui", {"from": "meta"})
        self.assertEqual({t["id"] for t in r["choose"]}, {"neo", "old"})
        r = self.api.send_to("webui", {"from": "meta"}, "old")
        self.assertTrue(r["ok"], r)

    def test_old_plugin(self):
        self.api._runners["neo"] = _Runner(self.base)
        self.srv.st["endpoint"] = "/nothing"
        r = self.api.send_to("webui", {"from": "meta"})
        self.assertFalse(r["ok"])
        self.assertIn("重启", r["error"])

    def test_comfy_builds_workflow(self):
        self.api._runners["cf"] = _Runner(self.base)
        self.srv.st["endpoint"] = "/wwy_bridge/load"
        r = self.api.send_to("comfy", {"from": "meta", "name": "demo.png"})
        self.assertTrue(r["ok"], r)
        item = self.srv.st["got"][0]
        self.assertEqual(item["name"], "WWY-demo")
        nodes = check_graph(self, item["workflow"])
        lora = next(n for n in nodes.values() if n["type"] == "LoraLoader")
        self.assertEqual(lora["widgets_values"][0], LORA_PATH)      # 按 ComfyUI 下拉框里的写法填
        self.assertEqual(r["mode"], "built")

    def test_comfy_image_to_webui(self):
        self.api._meta_meta = comfy_meta()
        self.api._runners["neo"] = _Runner(self.base)
        r = self.api.send_to("webui", {"from": "meta"})
        self.assertTrue(r["ok"], r)
        self.assertIn("<lora:Artistic_Anima_update_v2_epoch_20:0.8>", self.srv.st["got"][0]["infotext"])


if __name__ == "__main__":
    unittest.main()
