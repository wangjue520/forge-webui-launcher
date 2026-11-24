# -*- coding: utf-8 -*-
"""图片信息页「AI 分析」和「汇总 / 分离」的后端部分。

AI 分析：把 ComfyUI 节点图交给用户配置的大模型，理出正向 / 负向 / LoRA。
这里不联网——用假的 post 函数顶替 HTTP 请求，验证发出去的材料、对各种
回复形态（工具调用 / 纯 JSON / 包在代码块里 / 报错）的处理，以及结果整理。
汇总：每个采样阶段各自拼出来的一条提示词（text_stages）。
"""
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import diagnose_llm as dl  # noqa: E402
import image_meta_core as imc  # noqa: E402
import meta_ai as mai  # noqa: E402
import webview_api as wa  # noqa: E402
from meta_engine.comfyui_parser import ComfyUIParser  # noqa: E402

CFG = {"diag_llm_provider": "deepseek", "diag_llm_api_key": "sk-test-not-a-real-key"}


def graph():
    """主采样 + 高清修复二次采样（正向不同）+ 一个 LoRA + 一个没接线的文本框。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 1, "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1,
            "model": ["10", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl_base.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ["12", 0], "clip": ["10", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "lowres, bad anatomy", "clip": ["10", 1]},
              "_meta": {"title": "负面"}},
        "10": {"class_type": "LoraLoader", "inputs": {
            "lora_name": "style\\ink_wash.safetensors", "strength_model": 0.8, "strength_clip": 0.8,
            "model": ["4", 0], "clip": ["4", 1]}},
        "12": {"class_type": "StringConcatenate", "inputs": {
            "string_a": ["13", 0], "string_b": ["14", 0], "delimiter": ", "}},
        "13": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "masterpiece, best quality"},
               "_meta": {"title": "质量词"}},
        "14": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "1girl, silver hair"},
               "_meta": {"title": "角色"}},
        "20": {"class_type": "LatentUpscaleBy", "inputs": {"samples": ["3", 0], "upscale_method": "bislerp", "scale_by": 1.5}},
        "21": {"class_type": "KSampler", "inputs": {
            "seed": 2, "steps": 12, "cfg": 6, "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 0.5,
            "model": ["10", 0], "positive": ["22", 0], "negative": ["7", 0], "latent_image": ["20", 0]}},
        "22": {"class_type": "CLIPTextEncode", "inputs": {"text": "highly detailed skin, sharp focus", "clip": ["10", 1]}},
        "30": {"class_type": "CLIPTextEncode", "inputs": {"text": "spare idea, never connected", "clip": ["10", 1]}},
    }


def parsed():
    m = ComfyUIParser.parse_comfy_data(graph())
    shown = imc.present(m)
    shown.pop("_meta")
    return m, shown


GOOD = {
    "summary": "SDXL 文生图，一次采样后 1.5 倍高清修复。",
    "positive": "masterpiece, best quality, 1girl, silver hair",
    "negative": "lowres, bad anatomy",
    "positive_parts": [
        {"text": "masterpiece, best quality", "node": "质量词 #13", "stage": "主采样"},
        {"text": "1girl, silver hair", "node": "角色 #14", "stage": "主采样"},
        {"text": "highly detailed skin, sharp focus", "node": "CLIPTextEncode #22", "stage": "二次采样"},
    ],
    "negative_parts": [{"text": "lowres, bad anatomy", "node": "负面 #7", "stage": "主采样"}],
    "loras": [{"name": "style\\ink_wash.safetensors", "weight": 0.8, "clip_weight": 0.8, "node": "LoraLoader #10"}],
    "model": "sdxl_base.safetensors",
    "params": [{"name": "采样器", "value": "euler"}, {"name": "步数", "value": 20}],
    "notes": ["节点 #30 的文本框没有接到采样器"],
}


def tool_reply(args):
    """OpenAI 格式：模型调用了 report 工具。"""
    return 200, {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "report", "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}, ""


def text_reply(text, finish="stop"):
    return 200, {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": text}}]}, ""


class Poster:
    """顶替 HTTP 请求：按顺序吐出准备好的回复，并记下每次发出去的请求体。"""

    def __init__(self, *replies):
        self.replies, self.bodies = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.bodies.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class StageSummaryTests(unittest.TestCase):
    def test_each_stage_gets_its_own_merged_prompt(self):
        m, shown = parsed()
        self.assertEqual(m.positive_prompt, "masterpiece, best quality, 1girl, silver hair")
        self.assertEqual(shown["text_stages"], [
            {"stage": "主采样", "main": True,
             "positive": "masterpiece, best quality, 1girl, silver hair", "negative": "lowres, bad anatomy"},
            {"stage": "二次采样", "main": False,
             "positive": "highly detailed skin, sharp focus", "negative": "lowres, bad anatomy"},
        ])
        self.assertTrue(shown["can_ai"])

    def test_detailer_stage_and_empty_stages(self):
        g = graph()
        g["50"] = {"class_type": "FaceDetailer", "inputs": {
            "image": ["60", 0], "model": ["10", 0], "clip": ["10", 1], "vae": ["4", 2],
            "positive": ["51", 0], "negative": ["7", 0], "steps": 20, "cfg": 7, "denoise": 0.4, "seed": 3}}
        g["51"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "detailed face", "clip": ["10", 1]}}
        stages = ComfyUIParser.parse_comfy_data(g).text_stages
        self.assertEqual([(s["stage"], s["positive"]) for s in stages], [
            ("主采样", "masterpiece, best quality, 1girl, silver hair"),
            ("二次采样", "highly detailed skin, sharp focus"),
            ("FaceDetailer", "detailed face"),
        ])
        # 没有采样器的图：没有阶段可汇总，也不报错
        m = ComfyUIParser.parse_comfy_data({"1": {"class_type": "Text Multiline", "inputs": {"text": "just a note, here"}}})
        self.assertEqual(m.text_stages, [])

    def test_non_comfy_image_cannot_use_ai(self):
        from meta_engine.webui_parser import WebUIParser
        shown = imc.present(WebUIParser.parse_text("1girl\nNegative prompt: lowres\nSteps: 20, Sampler: Euler a"))
        self.assertFalse(shown["can_ai"])
        self.assertEqual(shown["text_stages"], [])


class DigestTests(unittest.TestCase):
    def test_digest_contains_graph_and_rule_hints(self):
        m, shown = parsed()
        text = mai.digest(m, shown)
        for needle in ("CLIPTextEncode", "ink_wash.safetensors", "masterpiece, best quality", '["12",0]',
                       "spare idea, never connected", "没接到采样器", "质量词"):
            self.assertIn(needle, text)

    def test_long_strings_are_clipped_and_huge_graph_rejected(self):
        g = graph()
        g["99"] = {"class_type": "LoadImageBase64", "inputs": {"image": "A" * 60000}}
        m = ComfyUIParser.parse_comfy_data(g)
        text = mai.digest(m, {})
        self.assertLess(len(text), 30000)
        self.assertIn("已截断", text)
        big = {str(i): {"class_type": "CLIPTextEncode", "inputs": {"text": f"prompt number {i} " + "word " * 60}}
               for i in range(3000)}
        with self.assertRaises(mai.TooLarge):
            mai.digest(ComfyUIParser.parse_comfy_data(big), {})

    def test_private_paths_are_redacted(self):
        g = graph()
        home = os.path.expanduser("~")
        g["98"] = {"class_type": "LoadImage", "inputs": {"image": os.path.join(home, "Pictures", "ref.png")}}
        text = mai.digest(ComfyUIParser.parse_comfy_data(g), {})
        self.assertNotIn(home, text)

    def test_workflow_only_image(self):
        wf = {"nodes": [
            {"id": 3, "type": "KSampler", "inputs": [{"name": "positive", "link": 4}],
             "widgets_values": [1, "fixed", 20, 8, "euler", "normal", 1]},
            {"id": 6, "type": "CLIPTextEncode", "inputs": [], "widgets_values": ["a cozy cabin in the woods"]},
        ], "links": [[4, 6, 0, 3, 1, "CONDITIONING"]]}
        m = ComfyUIParser.parse_comfy_data(None, wf)
        self.assertTrue(mai.has_workflow(m))
        self.assertIn("a cozy cabin in the woods", mai.digest(m, {}))


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.m, self.shown = parsed()
        self.text = mai.digest(self.m, self.shown)

    def test_tool_call_reply(self):
        post = Poster(tool_reply(GOOD))
        r = mai.analyze(CFG, self.text, post=post)
        self.assertEqual(r["positive"], GOOD["positive"])
        self.assertEqual(r["negative"], GOOD["negative"])
        self.assertEqual([p["text"] for p in r["positive_parts"]], [p["text"] for p in GOOD["positive_parts"]])
        self.assertEqual(r["positive_parts"][2]["stage"], "二次采样")
        self.assertEqual(r["loras"], [{"name": "style\\ink_wash.safetensors", "weight": 0.8, "clip_weight": None,
                                       "enabled": True, "node": "LoraLoader #10"}])
        self.assertEqual(r["params"], [["采样器", "euler"], ["步数", "20"]])
        self.assertEqual((r["model"], r["by"]), ("sdxl_base.safetensors", "deepseek-v4-flash"))
        # 发出去的请求：带着节点图，并且要求调用 report
        body = post.bodies[0]
        self.assertIn("ink_wash.safetensors", body["messages"][1]["content"])
        self.assertEqual(body["tool_choice"], {"type": "function", "function": {"name": "report"}})

    def test_plain_json_reply_and_fenced_json(self):
        fenced = "好的，分析如下：\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
        r = mai.analyze(CFG, self.text, post=Poster(text_reply(fenced)))
        self.assertEqual(r["positive"], GOOD["positive"])
        self.assertEqual(len(r["loras"]), 1)

    def test_falls_back_when_forced_tool_call_is_rejected(self):
        # 个别服务商不支持强制调用工具：第一次 400，改成直接要 JSON 再问一次
        rejected = (400, {"error": {"message": "tool_choice is not supported"}}, "")
        post = Poster(rejected, text_reply(json.dumps(GOOD, ensure_ascii=False)))
        r = mai.analyze(CFG, self.text, post=post)
        self.assertEqual(r["summary"], GOOD["summary"])
        self.assertEqual(len(post.bodies), 2)
        self.assertNotIn("tools", post.bodies[1])
        self.assertIn("只输出一个 JSON 对象", post.bodies[1]["messages"][1]["content"])

    def test_auth_error_is_not_retried(self):
        post = Poster((401, {"error": {"message": "invalid key"}}, ""))
        with self.assertRaises(dl.LlmError) as cm:
            mai.analyze(CFG, self.text, post=post)
        self.assertIn("API Key", str(cm.exception))
        self.assertEqual(len(post.bodies), 1)

    def test_unusable_reply_raises(self):
        post = Poster(text_reply("我不太确定这个工作流……"), text_reply("还是不确定"))
        with self.assertRaises(dl.LlmError):
            mai.analyze(CFG, self.text, post=post)
        with self.assertRaises(dl.LlmError):
            mai.analyze({}, self.text, post=Poster())        # 没填 Key

    def test_normalize_tolerates_sloppy_output(self):
        r = mai.normalize({
            "positive_parts": ["a cat", {"text": "  on a sofa "}, {"text": ""}, 5],
            "negative": ["blurry", "lowres"],
            "loras": ["plain_name.safetensors",
                      {"name": "off.safetensors", "weight": "0.6", "enabled": False},
                      {"lora": "zero.safetensors", "strength": 0},
                      {"name": "None"}, {"name": "plain_name.safetensors"}],
            "params": {"步数": 20, "尺寸": "832x1216"},
            "notes": "只有一句话",
        }, "m")
        # 只给了分段：汇总那一条由分段拼出来；只给了整条：分段视图里就是一项
        self.assertEqual(r["positive"], "a cat, on a sofa")
        self.assertEqual(r["negative"], "blurry\nlowres")
        self.assertEqual([p["text"] for p in r["negative_parts"]], ["blurry\nlowres"])
        self.assertEqual([(l["name"], l["weight"], l["enabled"]) for l in r["loras"]], [
            ("plain_name.safetensors", None, True), ("off.safetensors", 0.6, False), ("zero.safetensors", 0.0, False)])
        self.assertEqual(r["params"], [["步数", "20"], ["尺寸", "832x1216"]])
        self.assertEqual(r["notes"], ["只有一句话"])
        empty = mai.normalize(None)
        self.assertEqual((empty["positive"], empty["loras"], empty["positive_parts"]), ("", [], []))


class ApiTests(unittest.TestCase):
    """接口层：没图 / 不是 ComfyUI / 没填 Key 的提示，后台线程 + 事件，同一张图不重复请求。"""

    def setUp(self):
        m, shown = parsed()
        self.events = []
        self.api = types.SimpleNamespace(cfg=dict(CFG), _meta_meta=m, _meta_current=shown)
        self.api._emit = lambda scope, type_, **data: self.events.append({"scope": scope, "type": type_, **data})
        self.api._spawn = lambda work, name="worker": work()
        patcher = mock.patch.object(wa, "_ai_sync", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, force=False):
        return wa._api_meta_ai_analyze(self.api, force)

    def test_guards(self):
        self.api._meta_meta = None
        self.assertFalse(self.call()["ok"])
        from meta_engine.webui_parser import WebUIParser
        m = WebUIParser.parse_text("1girl\nSteps: 20, Sampler: Euler a")
        shown = imc.present(m)
        shown.pop("_meta")
        self.api._meta_meta, self.api._meta_current = m, shown
        self.assertIn("ComfyUI", self.call()["error"])

    def test_missing_key_points_to_settings(self):
        self.api.cfg = {}
        r = self.call()
        self.assertEqual((r["ok"], r["need_key"]), (False, True))
        self.assertEqual(self.events, [])

    def test_result_comes_by_event_then_from_cache(self):
        done = mai.normalize(GOOD, "deepseek-v4-flash")
        with mock.patch.object(mai, "analyze", return_value=done) as analyze:
            r = self.call()
            self.assertEqual((r["ok"], r["model"]), (True, "deepseek-v4-flash"))
            self.assertEqual(self.events, [{"scope": "meta", "type": "ai_result", "job": r["job"], "ok": True, "result": done}])
            # 同一张图再点：直接给上次的结果，不再请求
            again = self.call()
            self.assertEqual((again["cached"], again["result"]), (True, done))
            self.assertEqual(analyze.call_count, 1)
            # 「重新分析」才会再请求一次
            self.call(force=True)
            self.assertEqual(analyze.call_count, 2)

    def test_llm_error_is_reported_not_cached(self):
        with mock.patch.object(mai, "analyze", side_effect=dl.LlmError("账户余额不足（402）")) as analyze:
            r = self.call()
            self.assertTrue(r["ok"])
            self.assertEqual((self.events[-1]["ok"], self.events[-1]["error"]), (False, "账户余额不足（402）"))
            self.call()
            self.assertEqual(analyze.call_count, 2)


if __name__ == "__main__":
    unittest.main()
