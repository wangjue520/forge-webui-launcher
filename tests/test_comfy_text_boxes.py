# -*- coding: utf-8 -*-
"""图片信息页：ComfyUI 工作流里多个提示词文本框的读取。

用户反馈「工作流里拉了好几个文本框，解析图片时有些框的内容读不出来」。
这里用手写的节点图（API 格式 prompt / 界面格式 workflow）覆盖各种接法，
验证每个文本框都能列出来、正负归类正确、没接线的框不丢但和参与出图的分开，
同时原来就能解析的主提示词结果不变。不依赖真实图片和网络。
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import image_meta_core as imc  # noqa: E402
from meta_engine.comfyui_parser import ComfyUIParser  # noqa: E402
from meta_engine.comfy_text_boxes import collect_text_boxes, is_text_key, key_polarity, value_ok  # noqa: E402
from meta_engine.webui_parser import WebUIParser  # noqa: E402


def basic_graph():
    """最普通的文生图：一个采样器、一正一负两个 CLIPTextEncode。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 1, "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "masterpiece, 1girl", "clip": ["4", 1]},
              "_meta": {"title": "正面提示词"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "lowres, bad anatomy", "clip": ["4", 1]},
              "_meta": {"title": "CLIP Text Encode (Prompt)"}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]}},
    }


def parse(prompt=None, workflow=None):
    return ComfyUIParser.parse_comfy_data(prompt, workflow)


def by_text(boxes):
    return {b["text"]: b for b in boxes}


def brief(boxes):
    """(文本, 正负, 状态) 三元组列表，断言时好读。"""
    return [(b["text"], b["polarity"], b["status"]) for b in boxes]


class HelperTests(unittest.TestCase):
    def test_key_polarity(self):
        for k in ("positive", "text_positive", "base_positive", "pos_g", "positive_prompt", "正向提示词"):
            self.assertEqual(key_polarity(k), "positive", k)
        for k in ("negative", "text_negative", "neg_l", "refiner_negative", "uncond", "负面"):
            self.assertEqual(key_polarity(k), "negative", k)
        # position / pose / post 开头是 pos，但不是正向
        for k in ("position", "pose", "post_process", "text", "conditioning", "clip"):
            self.assertIsNone(key_polarity(k), k)

    def test_is_text_key(self):
        for k in ("text", "text_g", "text_l", "clip_l", "t5xxl", "prompt", "prompt_2", "string_a",
                  "positive_prompt", "wildcard_text", "populated_text", "value", "tags", "提示词"):
            self.assertTrue(is_text_key(k), k)
        for k in ("ckpt_name", "sampler_name", "scheduler", "filename_prefix", "delimiter",
                  "text_encoder_name", "prompt_type", "positive_token_normalization", "_widgets_values"):
            self.assertFalse(is_text_key(k), k)

    def test_value_ok(self):
        for v in ("1girl", "a cat, a dog", "猫", "一只猫", "embedding:easynegative.pt, bad"):
            self.assertTrue(value_ok(v), v)
        for v in ("", " ", ", ", "a", "true", "euler", "512 x 512", "42", "model.safetensors",
                  r"C:\models\a b.ckpt", "none"):
            self.assertFalse(value_ok(v), v)


class ApiGraphTests(unittest.TestCase):
    def test_basic_graph_unchanged(self):
        m = parse(basic_graph())
        self.assertEqual(m.positive_prompt, "masterpiece, 1girl")
        self.assertEqual(m.negative_prompt, "lowres, bad anatomy")
        self.assertEqual(brief(m.text_boxes), [
            ("masterpiece, 1girl", "positive", "used"),
            ("lowres, bad anatomy", "negative", "used"),
        ])
        pos = m.text_boxes[0]
        self.assertEqual((pos["node_id"], pos["class_type"], pos["title"], pos["field"]),
                         ("6", "CLIPTextEncode", "正面提示词", "text"))
        self.assertEqual(pos["stages"], ["主采样"])

    def test_concat_chain_lists_every_segment(self):
        # 三个文本框经合并节点拼成一条正向：每一段都要单独列出，顺序与拼接顺序一致
        g = basic_graph()
        g["6"]["inputs"]["text"] = ["10", 0]
        g["10"] = {"class_type": "Text Concatenate", "inputs": {
            "delimiter": ", ", "clean_whitespace": "true",
            "text_a": ["11", 0], "text_b": ["12", 0], "text_c": ["13", 0]}}
        g["11"] = {"class_type": "Text Multiline", "inputs": {"text": "best quality, masterpiece"},
                   "_meta": {"title": "质量词"}}
        g["12"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "1girl, silver hair"}}
        g["13"] = {"class_type": "easy positive", "inputs": {"positive": "forest, night"}}
        m = parse(g)
        self.assertEqual(m.positive_prompt, "best quality, masterpiece, 1girl, silver hair, forest, night")
        self.assertEqual(brief(m.text_boxes), [
            ("best quality, masterpiece", "positive", "used"),
            ("1girl, silver hair", "positive", "used"),
            ("forest, night", "positive", "used"),
            ("lowres, bad anatomy", "negative", "used"),
        ])
        self.assertEqual(m.text_boxes[0]["title"], "质量词")
        # 分隔符 ", " 和开关值 "true" 不是文本框
        self.assertNotIn("10", {b["node_id"] for b in m.text_boxes})

    def test_string_nodes_from_common_packs(self):
        # 各家节点包的字符串 / 提示词节点，字段名各不相同
        g = basic_graph()
        g["6"]["inputs"]["text"] = ["20", 0]
        g["20"] = {"class_type": "JoinStrings", "inputs": {          # KJNodes
            "string1": ["21", 0], "string2": ["22", 0], "delimiter": ", "}}
        g["21"] = {"class_type": "StringFunction|pysssss", "inputs": {  # comfyui-custom-scripts
            "action": "append", "tidy_tags": "yes",
            "text_a": "solo, smile", "text_b": ["23", 0], "text_c": ""}}
        g["22"] = {"class_type": "CR Text Concatenate", "inputs": {   # Comfyroll
            "text1": ["24", 0], "text2": ["25", 0], "separator": " "}}
        g["23"] = {"class_type": "Power Prompt - Simple (rgthree)", "inputs": {"prompt": "red eyes"}}
        g["24"] = {"class_type": "ImpactWildcardProcessor", "inputs": {  # Impact
            "wildcard_text": "__hair__ hair", "populated_text": "blue hair", "mode": "populate", "seed": 1,
            "Select to add Wildcard": "Select the Wildcard to add to the text"}}
        g["25"] = {"class_type": "StringConstantMultiline", "inputs": {"string": "city street\nrain", "strip_newlines": False}}
        texts = by_text(parse(g).text_boxes)
        for t in ("solo, smile", "red eyes", "__hair__ hair", "blue hair", "city street\nrain"):
            self.assertIn(t, texts)
            self.assertEqual((texts[t]["polarity"], texts[t]["status"]), ("positive", "used"), t)
        # 下拉选项 / 分隔符 / 占位提示语都不能混进来
        for junk in ("append", "yes", "populate", ", ", "Select the Wildcard to add to the text"):
            self.assertNotIn(junk, texts)

    def test_sdxl_dual_text_fields(self):
        g = basic_graph()
        g["6"] = {"class_type": "CLIPTextEncodeSDXL", "inputs": {
            "width": 1024, "height": 1024, "crop_w": 0, "crop_h": 0, "target_width": 1024, "target_height": 1024,
            "text_g": "a photo of a cat", "text_l": "cat, photo", "clip": ["4", 1]}}
        m = parse(g)
        self.assertEqual(m.positive_prompt, "a photo of a cat\ncat, photo")
        self.assertEqual([(b["field"], b["text"]) for b in m.text_boxes if b["polarity"] == "positive"],
                         [("text_g", "a photo of a cat"), ("text_l", "cat, photo")])
        # 两格填的一样（最常见的用法）时并成一条，不重复列
        g["6"]["inputs"]["text_l"] = "a photo of a cat"
        pos = [b for b in parse(g).text_boxes if b["polarity"] == "positive"]
        self.assertEqual([(b["field"], b["text"]) for b in pos], [("text_g / text_l", "a photo of a cat")])

    def test_flux_guider_and_multi_encoder_fields(self):
        g = {
            "1": {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": ["2", 0], "guider": ["3", 0], "sampler": ["4", 0], "sigmas": ["5", 0], "latent_image": ["6", 0]}},
            "2": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
            "3": {"class_type": "BasicGuider", "inputs": {"model": ["7", 0], "conditioning": ["8", 0]}},
            "4": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "5": {"class_type": "BasicScheduler", "inputs": {"scheduler": "simple", "steps": 20, "denoise": 1, "model": ["7", 0]}},
            "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
            "7": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-dev.safetensors", "weight_dtype": "default"}},
            "8": {"class_type": "FluxGuidance", "inputs": {"guidance": 3.5, "conditioning": ["9", 0]}},
            "9": {"class_type": "CLIPTextEncodeFlux", "inputs": {
                "clip_l": "a cat", "t5xxl": "a fluffy cat sitting on a sofa", "guidance": 3.5, "clip": ["10", 0]}},
            "10": {"class_type": "DualCLIPLoader", "inputs": {
                "clip_name1": "t5xxl_fp16.safetensors", "clip_name2": "clip_l.safetensors", "type": "flux"}},
        }
        m = parse(g)
        self.assertEqual(m.positive_prompt, "a cat\na fluffy cat sitting on a sofa")
        self.assertEqual([(b["field"], b["text"], b["polarity"], b["status"]) for b in m.text_boxes], [
            ("clip_l", "a cat", "positive", "used"),
            ("t5xxl", "a fluffy cat sitting on a sofa", "positive", "used"),
        ])

    def test_sd3_hidream_and_cfg_guider(self):
        g = {
            "1": {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": ["2", 0], "guider": ["3", 0], "sampler": ["4", 0], "sigmas": ["5", 0], "latent_image": ["6", 0]}},
            "2": {"class_type": "RandomNoise", "inputs": {"noise_seed": 7}},
            "3": {"class_type": "CFGGuider", "inputs": {"cfg": 5, "model": ["7", 0], "positive": ["8", 0], "negative": ["9", 0]}},
            "4": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "5": {"class_type": "BasicScheduler", "inputs": {"scheduler": "simple", "steps": 28, "denoise": 1, "model": ["7", 0]}},
            "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
            "7": {"class_type": "UNETLoader", "inputs": {"unet_name": "hidream_i1_dev.safetensors", "weight_dtype": "default"}},
            "8": {"class_type": "CLIPTextEncodeHiDream", "inputs": {
                "clip_l": "short tags", "clip_g": "more tags, here", "t5xxl": "a long natural sentence",
                "llama": "another long description for llama", "clip": ["10", 0]}},
            "9": {"class_type": "CLIPTextEncodeSD3", "inputs": {
                "clip_l": "ugly", "clip_g": "ugly", "t5xxl": "blurry, low quality", "empty_padding": "none", "clip": ["10", 0]}},
            "10": {"class_type": "QuadrupleCLIPLoader", "inputs": {"clip_name1": "a.safetensors"}},
        }
        boxes = parse(g).text_boxes
        self.assertEqual([(b["field"], b["polarity"]) for b in boxes], [
            ("clip_l", "positive"), ("clip_g", "positive"), ("t5xxl", "positive"), ("llama", "positive"),
            ("clip_l / clip_g", "negative"), ("t5xxl", "negative"),
        ])
        self.assertTrue(all(b["status"] == "used" for b in boxes))

    def test_dual_cfg_guider_uses_output_slots(self):
        # HiDream E1 这类图像编辑工作流：DualCFGGuider 的 cond1 / cond2 没写正负，
        # 分别接 InstructPixToPixConditioning 的 0 号（正向）和 1 号（负向）输出
        g = {
            "54": {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": ["47", 0], "guider": ["53", 0], "sampler": ["46", 0], "sigmas": ["49", 0], "latent_image": ["52", 2]}},
            "53": {"class_type": "DualCFGGuider", "inputs": {
                "cfg_conds": 5, "cfg_cond2_negative": 2, "style": "regular",
                "model": ["41", 0], "cond1": ["52", 0], "cond2": ["52", 1], "negative": ["8", 0]}},
            "52": {"class_type": "InstructPixToPixConditioning", "inputs": {
                "positive": ["6", 0], "negative": ["7", 0], "vae": ["43", 0], "pixels": ["45", 0]}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "let her hair fall loose", "clip": ["42", 0]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "low quality, blurry, distorted", "clip": ["42", 0]}},
            "8": {"class_type": "CLIPTextEncode", "inputs": {"text": "base negative words", "clip": ["42", 0]}},
            "41": {"class_type": "UNETLoader", "inputs": {"unet_name": "hidream_e1_1_bf16.safetensors"}},
            "46": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "47": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
            "49": {"class_type": "BasicScheduler", "inputs": {"scheduler": "simple", "steps": 28, "denoise": 1, "model": ["41", 0]}},
        }
        m = parse(g)
        self.assertEqual(m.positive_prompt, "let her hair fall loose")
        self.assertEqual(brief(m.text_boxes), [
            ("let her hair fall loose", "positive", "used"),
            ("low quality, blurry, distorted", "negative", "used"),
            ("base negative words", "negative", "used"),
        ])

    def test_zeroed_negative_is_not_filled_with_another_positive_box(self):
        # Flux / Z-Image 常见接法：负向接 ConditioningZeroOut（没有负向提示词）。
        # 工作流里另有别的文本框时，负向栏以前会被兜底扫描填进其中一个正向框的内容
        g = basic_graph()
        g["3"]["inputs"]["negative"] = ["15", 0]
        g["15"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["6", 0]}}
        del g["7"]
        g["6"]["inputs"]["text"] = ["16", 0]
        g["16"] = {"class_type": "StringConcatenate", "inputs": {
            "string_a": ["17", 0], "string_b": ["18", 0], "delimiter": ", "}}
        g["17"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "a man in a baseball uniform"}}
        g["18"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "pixel art style, retro"}}
        m = parse(g)
        self.assertEqual(m.positive_prompt, "a man in a baseball uniform, pixel art style, retro")
        self.assertEqual(m.negative_prompt, "")
        self.assertEqual(brief(m.text_boxes), [
            ("a man in a baseball uniform", "positive", "used"), ("pixel art style, retro", "positive", "used")])

    def test_second_sampler_and_detailer_have_their_own_prompts(self):
        # 高清修复的二次采样、FaceDetailer 各用各的正向：原来这些完全不显示
        g = basic_graph()
        g["20"] = {"class_type": "LatentUpscaleBy", "inputs": {"samples": ["3", 0], "upscale_method": "nearest-exact", "scale_by": 1.5}}
        g["21"] = {"class_type": "KSampler", "inputs": {
            "seed": 2, "steps": 12, "cfg": 6, "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 0.5,
            "model": ["4", 0], "positive": ["22", 0], "negative": ["7", 0], "latent_image": ["20", 0]}}
        g["22"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "highly detailed skin, sharp focus", "clip": ["4", 1]}}
        g["8"]["inputs"]["samples"] = ["21", 0]
        g["50"] = {"class_type": "FaceDetailer", "inputs": {
            "image": ["8", 0], "model": ["4", 0], "clip": ["4", 1], "vae": ["4", 2],
            "positive": ["51", 0], "negative": ["7", 0], "bbox_detector": ["52", 0],
            "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 0.4, "seed": 3,
            "guide_size": 512, "wildcard": "[SEP] blue eyes"}}
        g["51"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "detailed face, beautiful eyes", "clip": ["4", 1]}}
        g["52"] = {"class_type": "UltralyticsDetectorProvider", "inputs": {"model_name": "bbox/face_yolov8m.pt"}}
        m = parse(g)
        self.assertEqual(m.positive_prompt, "masterpiece, 1girl")       # 主提示词不被后面的阶段污染
        t = by_text(m.text_boxes)
        self.assertEqual(t["masterpiece, 1girl"]["stages"], ["主采样"])
        self.assertEqual(t["highly detailed skin, sharp focus"]["stages"], ["二次采样"])
        self.assertEqual(t["detailed face, beautiful eyes"]["stages"], ["FaceDetailer"])
        self.assertEqual(t["[SEP] blue eyes"]["polarity"], "positive")
        self.assertEqual(t["lowres, bad anatomy"]["stages"], ["主采样", "二次采样", "FaceDetailer"])
        self.assertEqual(t["lowres, bad anatomy"]["polarity"], "negative")
        # 排序：先主采样，再二次采样，再 Detailer
        order = [b["text"] for b in m.text_boxes]
        self.assertLess(order.index("masterpiece, 1girl"), order.index("highly detailed skin, sharp focus"))
        self.assertLess(order.index("highly detailed skin, sharp focus"), order.index("detailed face, beautiful eyes"))

    def test_conditioning_combine_area_and_controlnet(self):
        g = basic_graph()
        g["3"]["inputs"]["positive"] = ["40", 0]
        g["3"]["inputs"]["negative"] = ["40", 1]
        # ControlNetApplyAdvanced 同时带着正负两条线：不能串
        g["40"] = {"class_type": "ControlNetApplyAdvanced", "inputs": {
            "strength": 1, "start_percent": 0, "end_percent": 1,
            "positive": ["30", 0], "negative": ["7", 0], "control_net": ["41", 0], "image": ["42", 0]}}
        g["41"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "control v11p sd15 openpose.pth"}}
        g["42"] = {"class_type": "LoadImage", "inputs": {"image": "pose (1).png"}}
        g["30"] = {"class_type": "ConditioningCombine", "inputs": {"conditioning_1": ["6", 0], "conditioning_2": ["31", 0]}}
        g["31"] = {"class_type": "ConditioningSetArea", "inputs": {
            "width": 256, "height": 512, "x": 0, "y": 0, "strength": 1, "conditioning": ["32", 0]}}
        g["32"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "red dress girl on the left", "clip": ["4", 1]}}
        self.assertEqual(brief(parse(g).text_boxes), [
            ("masterpiece, 1girl", "positive", "used"),
            ("red dress girl on the left", "positive", "used"),
            ("lowres, bad anatomy", "negative", "used"),
        ])

    def test_pipe_and_loader_inline_prompts(self):
        # Efficient Loader：正负提示词直接填在加载器上，正向在 1 号输出槽
        g = {
            "1": {"class_type": "Efficient Loader", "inputs": {
                "ckpt_name": "a.safetensors", "vae_name": "Baked VAE", "clip_skip": -2, "lora_name": "None",
                "lora_model_strength": 1, "lora_clip_strength": 1,
                "positive": "1girl, smile", "negative": "bad hands",
                "token_normalization": "none", "weight_interpretation": "comfy",
                "empty_latent_width": 512, "empty_latent_height": 512, "batch_size": 1}},
            "2": {"class_type": "KSampler (Efficient)", "inputs": {
                "seed": 5, "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1,
                "preview_method": "auto", "vae_decode": "true (tiled)",
                "model": ["1", 0], "positive": ["1", 1], "negative": ["1", 2], "latent_image": ["1", 3], "optional_vae": ["1", 4]}},
        }
        m = parse(g)
        self.assertEqual((m.positive_prompt, m.negative_prompt), ("1girl, smile", "bad hands"))
        self.assertEqual(brief(m.text_boxes), [("1girl, smile", "positive", "used"), ("bad hands", "negative", "used")])

        # Impact 的 pipe：采样器只接了 basic_pipe，正负在 ToBasicPipe 上
        g = basic_graph()
        del g["3"]["inputs"]["positive"], g["3"]["inputs"]["negative"], g["3"]["inputs"]["model"]
        g["3"]["class_type"] = "ImpactKSamplerBasicPipe"
        g["3"]["inputs"]["basic_pipe"] = ["60", 0]
        g["60"] = {"class_type": "ToBasicPipe", "inputs": {
            "model": ["4", 0], "clip": ["4", 1], "vae": ["4", 2], "positive": ["6", 0], "negative": ["7", 0]}}
        self.assertEqual(brief(parse(g).text_boxes), [
            ("masterpiece, 1girl", "positive", "used"), ("lowres, bad anatomy", "negative", "used")])

    def test_prompt_styler_negative_is_not_positive(self):
        g = basic_graph()
        g["6"]["inputs"]["text"] = ["40", 0]
        g["7"]["inputs"]["text"] = ["40", 1]
        g["40"] = {"class_type": "SDXLPromptStyler", "inputs": {
            "text_positive": "a castle on a hill", "text_negative": "blurry, ugly",
            "style": "sai-cinematic", "log_prompt": "No"}}
        m = parse(g)
        self.assertEqual((m.positive_prompt, m.negative_prompt), ("a castle on a hill", "blurry, ugly"))
        self.assertEqual(brief(m.text_boxes), [
            ("a castle on a hill", "positive", "used"), ("blurry, ugly", "negative", "used")])

    def test_video_text_embeds_without_positive_negative_keys(self):
        # kijai 的 WanVideo：采样器吃的是 text_embeds，正负填在同一个编码节点上
        g = {
            "1": {"class_type": "WanVideoSampler", "inputs": {
                "steps": 30, "cfg": 6, "shift": 5, "seed": 9, "scheduler": "unipc",
                "model": ["2", 0], "text_embeds": ["3", 0], "image_embeds": ["4", 0]}},
            "2": {"class_type": "WanVideoModelLoader", "inputs": {"model": "wan2.1_t2v_14B.safetensors"}},
            "3": {"class_type": "WanVideoTextEncode", "inputs": {
                "positive_prompt": "a dog running on the beach", "negative_prompt": "static, blurry", "t5": ["5", 0]}},
            "4": {"class_type": "WanVideoEmptyEmbeds", "inputs": {"width": 832, "height": 480, "num_frames": 81}},
            "5": {"class_type": "LoadWanVideoT5TextEncoder", "inputs": {"model_name": "umt5-xxl-enc-bf16.safetensors"}},
        }
        self.assertEqual(brief(parse(g).text_boxes), [
            ("a dog running on the beach", "positive", "used"), ("static, blurry", "negative", "used")])

    def test_floating_boxes_are_listed_but_marked(self):
        g = basic_graph()
        # 没接线的备用文本框：一个编码节点、一个纯字符串节点、一个只接了显示节点的
        g["70"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "1boy, armor, standing", "clip": ["4", 1]},
                   "_meta": {"title": "备用"}}
        g["71"] = {"class_type": "CLIPTextEncode", "inputs": {
            "text": "worst quality, low quality, watermark", "clip": ["4", 1]}}
        g["72"] = {"class_type": "PrimitiveString", "inputs": {"value": "sunset"}}
        g["73"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "an old idea", "clip": ["4", 1]},
                   "_meta": {"title": "Negative (old)"}}
        # 备注节点里的字是写给人看的，不算
        g["74"] = {"class_type": "Note", "inputs": {"text": "remember to change the seed, please"}}
        m = parse(g)
        self.assertEqual((m.positive_prompt, m.negative_prompt), ("masterpiece, 1girl", "lowres, bad anatomy"))
        t = by_text(m.text_boxes)
        self.assertEqual(t["masterpiece, 1girl"]["status"], "used")
        for text in ("1boy, armor, standing", "worst quality, low quality, watermark", "sunset", "an old idea"):
            self.assertEqual(t[text]["status"], "floating", text)
            self.assertEqual(t[text]["stages"], [], text)
        # 正负：标题里写了的按标题，没线索的按内容猜（并标明是猜的），猜不出就是 unknown
        self.assertEqual((t["an old idea"]["polarity"], t["an old idea"]["guess"]), ("negative", False))
        self.assertEqual((t["worst quality, low quality, watermark"]["polarity"],
                          t["worst quality, low quality, watermark"]["guess"]), ("negative", True))
        self.assertEqual(t["sunset"]["polarity"], "unknown")
        self.assertNotIn("remember to change the seed, please", t)
        # 参与出图的排在前面
        statuses = [b["status"] for b in m.text_boxes]
        self.assertEqual(statuses, sorted(statuses, key=["used", "unknown", "floating", "disabled"].index))

    def test_generic_fallback_for_unknown_nodes(self):
        # 名单里没有的自定义节点、没见过的字段名：靠「连线走得到 + 内容像提示词」收进来
        g = basic_graph()
        g["6"]["inputs"]["text"] = ["80", 0]
        g["80"] = {"class_type": "MyPackFancyMixer", "inputs": {
            "blend": "average", "quality": 0.8, "upstream": ["81", 0],
            "extra_words": "cinematic lighting, depth of field, 85mm",
            "preview_mode": "true (tiled)", "layout": "1024 x 1024 (square)"}}
        g["81"] = {"class_type": "AcmeWordBank", "inputs": {"content_zh": "一个女孩站在雨中的街道上", "bank": "default"}}
        t = by_text(parse(g).text_boxes)
        for text in ("cinematic lighting, depth of field, 85mm", "一个女孩站在雨中的街道上"):
            self.assertEqual((t[text]["polarity"], t[text]["status"]), ("positive", "used"), text)
        for junk in ("average", "true (tiled)", "1024 x 1024 (square)", "default"):
            self.assertNotIn(junk, t)

    def test_non_prompt_strings_never_listed(self):
        g = basic_graph()
        g["90"] = {"class_type": "LoraLoader", "inputs": {
            "lora_name": "style\\my lora, v2.safetensors", "strength_model": 1, "strength_clip": 1,
            "model": ["4", 0], "clip": ["4", 1]}}
        g["91"] = {"class_type": "VHS_VideoCombine", "inputs": {
            "frame_rate": 16, "filename_prefix": "video/my cool clip, take 2", "format": "video/h264-mp4",
            "pix_fmt": "yuv420p", "images": ["8", 0]}}
        g["92"] = {"class_type": "IPAdapterAdvanced", "inputs": {
            "weight_type": "style transfer precise", "combine_embeds": "concat", "model": ["4", 0]}}
        self.assertEqual(brief(parse(g).text_boxes), [
            ("masterpiece, 1girl", "positive", "used"), ("lowres, bad anatomy", "negative", "used")])

    def test_switch_only_selected_branch_is_used(self):
        g = basic_graph()
        g["6"]["inputs"]["text"] = ["100", 0]
        g["100"] = {"class_type": "ImpactSwitch", "inputs": {
            "select": 2, "sel_mode": False, "input1": ["101", 0], "input2": ["102", 0]}}
        g["101"] = {"class_type": "Text Multiline", "inputs": {"text": "daytime, sunny"}}
        g["102"] = {"class_type": "Text Multiline", "inputs": {"text": "night, rain"}}
        t = by_text(parse(g).text_boxes)
        self.assertEqual(t["night, rain"]["status"], "used")
        self.assertEqual(t["daytime, sunny"]["status"], "floating")

    def test_runtime_generated_text(self):
        # 反推：Florence2 → ShowText → CLIPTextEncode，文本运行时才有
        g = basic_graph()
        g["6"]["inputs"]["text"] = ["110", 0]
        g["110"] = {"class_type": "ShowText|pysssss", "inputs": {"text": ["111", 2]}}
        g["111"] = {"class_type": "Florence2Run", "inputs": {
            "text_input": "", "task": "more_detailed_caption", "fill_mask": True,
            "image": ["112", 0], "florence2_model": ["113", 0]}}
        g["112"] = {"class_type": "LoadImage", "inputs": {"image": "ref.png"}}
        g["113"] = {"class_type": "DownloadAndLoadFlorence2Model", "inputs": {"model": "microsoft/Florence-2-large"}}
        boxes = parse(g).text_boxes
        rt = [b for b in boxes if b["runtime"]]
        self.assertEqual(len(rt), 1)
        self.assertEqual((rt[0]["class_type"], rt[0]["text"], rt[0]["polarity"], rt[0]["status"]),
                         ("Florence2Run", "", "positive", "used"))
        # workflow 里的显示节点留着上一次的输出：带上，但标明可能不是这张图的
        wf = {"nodes": [
            {"id": 110, "type": "ShowText|pysssss", "inputs": [{"name": "text", "link": 5}],
             "widgets_values": [["a woman standing in a field of flowers"]]},
            {"id": 111, "type": "Florence2Run", "inputs": [], "widgets_values": ["", "more_detailed_caption"]},
        ], "links": [[5, 111, 2, 110, 0, "STRING"]]}
        rt = [b for b in parse(g, wf).text_boxes if b["runtime"]]
        self.assertEqual((rt[0]["text"], rt[0]["stale"]), ("a woman standing in a field of flowers", True))
        note = [b for b in imc.present(parse(g, wf))["text_boxes"] if b["class_type"] == "Florence2Run"][0]["note"]
        self.assertIn("上一次运行", note)

        # 通配 / 动态提示词：填的模板照常列出，同时标明最终文本是运行时展开的
        g = basic_graph()
        g["6"]["inputs"]["text"] = ["120", 0]
        g["120"] = {"class_type": "DPRandomGenerator", "inputs": {
            "text": "{red|blue} dress, __place__", "seed": 3, "autorefresh": "No"}}
        b = by_text(parse(g).text_boxes)["{red|blue} dress, __place__"]
        self.assertEqual((b["polarity"], b["status"], b["runtime"]), ("positive", "used", True))

        # 翻译 / LLM 扩写：用户填的原文要列出来
        g = basic_graph()
        g["6"]["inputs"]["text"] = ["130", 0]
        g["130"] = {"class_type": "DeepTranslatorTextNode", "inputs": {
            "from_translate": "chinese (simplified)", "to_translate": "english", "text": "一只在屋顶上的猫"}}
        t = by_text(parse(g).text_boxes)
        self.assertEqual((t["一只在屋顶上的猫"]["status"], t["一只在屋顶上的猫"]["runtime"]), ("used", True))
        self.assertNotIn("chinese (simplified)", t)

    def test_flattened_subgraph_ids(self):
        # 子图在 prompt 块里已经被 ComfyUI 展开，内部节点 id 形如 "12:3"
        g = basic_graph()
        g["3"]["inputs"]["positive"] = ["12:2", 0]
        del g["6"]
        g["12:1"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "inside the subgraph"},
                     "_meta": {"title": "角色"}}
        g["12:2"] = {"class_type": "CLIPTextEncode", "inputs": {"text": ["12:1", 0], "clip": ["4", 1]}}
        b = by_text(parse(g).text_boxes)["inside the subgraph"]
        self.assertEqual((b["node_id"], b["title"], b["polarity"], b["status"]), ("12:1", "角色", "positive", "used"))

    def test_no_sampler_means_status_unknown(self):
        g = {
            "1": {"class_type": "SomeCloudText2Img", "inputs": {"prompt": "a red fox in the snow", "aspect": "1:1"}},
            "2": {"class_type": "Text Multiline", "inputs": {"text": "spare idea, not sure"}},
        }
        m = parse(g)
        self.assertEqual(m.positive_prompt, "a red fox in the snow")
        self.assertEqual({b["status"] for b in m.text_boxes}, {"unknown"})
        self.assertEqual(len(m.text_boxes), 2)

    def test_disabled_nodes_come_from_workflow(self):
        # 静音 / 旁路的节点不进 prompt 块，只有 workflow 里还留着
        wf = {"nodes": [
            {"id": 6, "type": "CLIPTextEncode", "mode": 0, "widgets_values": ["masterpiece, 1girl"]},
            {"id": 77, "type": "CLIPTextEncode", "mode": 2, "title": "备用正向", "widgets_values": ["2girls, holding hands"]},
            {"id": 78, "type": "LoraLoader", "mode": 4, "widgets_values": ["a b.safetensors", 1, 1]},
            {"id": 79, "type": "Note", "mode": 2, "widgets_values": ["todo: fix the hands, later"]},
        ], "links": []}
        m = parse(basic_graph(), wf)
        t = by_text(m.text_boxes)
        self.assertEqual((t["2girls, holding hands"]["status"], t["2girls, holding hands"]["title"]), ("disabled", "备用正向"))
        self.assertEqual(len([b for b in m.text_boxes if b["status"] == "disabled"]), 1)
        self.assertEqual(m.text_boxes[-1]["text"], "2girls, holding hands")     # 停用的排最后

    def test_malformed_graph_does_not_raise(self):
        g = basic_graph()
        g["x"] = None
        g["y"] = {"class_type": None, "inputs": None}
        g["z"] = {"class_type": 5, "inputs": {"text": ["nope", 0], 3: "numeric key, weird"}}
        g["6"]["inputs"]["text"] = ["6", 0]          # 自环
        m = parse(g)
        self.assertIsInstance(m.text_boxes, list)
        self.assertEqual(collect_text_boxes({}), [])
        self.assertEqual(collect_text_boxes(None), [])


class WorkflowOnlyTests(unittest.TestCase):
    """只有界面格式 workflow、没有 prompt 块（直接拖进来的工作流 .json）。
    Reroute / Primitive / Set·Get / 子图只在这种格式里出现。"""

    @staticmethod
    def wf():
        return {"nodes": [
            {"id": 3, "type": "KSampler", "mode": 0,
             "inputs": [{"name": "model", "link": 1}, {"name": "positive", "link": 4},
                        {"name": "negative", "link": 6}, {"name": "latent_image", "link": 2}],
             "widgets_values": [156680208700286, "randomize", 20, 8, "euler", "normal", 1]},
            {"id": 4, "type": "CheckpointLoaderSimple", "inputs": [], "widgets_values": ["v1-5-pruned-emaonly.safetensors"]},
            {"id": 5, "type": "EmptyLatentImage", "inputs": [], "widgets_values": [512, 512, 1]},
            {"id": 6, "type": "CLIPTextEncode", "inputs": [{"name": "clip", "link": 3}],
             "widgets_values": ["beautiful scenery nature glass bottle landscape"]},
            {"id": 7, "type": "CLIPTextEncode", "inputs": [{"name": "clip", "link": 5}],
             "widgets_values": ["text, watermark"]},
        ], "links": [[1, 4, 0, 3, 0, "MODEL"], [2, 5, 0, 3, 3, "LATENT"], [3, 4, 1, 6, 0, "CLIP"],
                     [4, 6, 0, 3, 1, "CONDITIONING"], [5, 4, 1, 7, 0, "CLIP"], [6, 7, 0, 3, 2, "CONDITIONING"]]}

    def test_plain_workflow_unchanged(self):
        m = parse(None, self.wf())
        self.assertEqual(m.positive_prompt, "beautiful scenery nature glass bottle landscape")
        self.assertEqual(m.negative_prompt, "text, watermark")
        self.assertEqual(brief(m.text_boxes), [
            ("beautiful scenery nature glass bottle landscape", "positive", "used"),
            ("text, watermark", "negative", "used")])

    def test_reroute_primitive_and_stale_widget(self):
        # 文本部件转成输入：Primitive → Reroute → CLIPTextEncode.text。
        # 编码节点部件里残留的旧文本不能再当成提示词
        wf = self.wf()
        wf["nodes"][3]["inputs"].append({"name": "text", "link": 11, "widget": {"name": "text"}})
        wf["nodes"][3]["widgets_values"] = ["stale text left in the widget"]
        wf["nodes"] += [
            {"id": 20, "type": "PrimitiveNode", "inputs": [], "widgets_values": ["a cozy cabin in the woods", "fixed"]},
            {"id": 21, "type": "Reroute", "inputs": [{"name": "", "link": 10}]},
        ]
        wf["links"] += [[10, 20, 0, 21, 0, "STRING"], [11, 21, 0, 6, 1, "STRING"]]
        m = parse(None, wf)
        self.assertEqual(m.positive_prompt, "a cozy cabin in the woods")
        self.assertEqual(brief(m.text_boxes), [
            ("a cozy cabin in the woods", "positive", "used"), ("text, watermark", "negative", "used")])

    def test_set_get_nodes(self):
        # KJNodes 的 SetNode / GetNode 靠名字配对，连线表里没有这条边
        wf = self.wf()
        wf["nodes"][0]["inputs"][1]["link"] = 21          # KSampler.positive ← GetNode
        wf["nodes"] += [
            {"id": 30, "type": "SetNode", "inputs": [{"name": "CONDITIONING", "link": 20}], "widgets_values": ["pos_cond"]},
            {"id": 31, "type": "GetNode", "inputs": [], "widgets_values": ["pos_cond"]},
        ]
        wf["links"] = [l for l in wf["links"] if l[0] != 4] + [
            [20, 6, 0, 30, 0, "CONDITIONING"], [21, 31, 0, 3, 1, "CONDITIONING"]]
        m = parse(None, wf)
        self.assertEqual(m.positive_prompt, "beautiful scenery nature glass bottle landscape")
        self.assertEqual(brief(m.text_boxes)[0],
                         ("beautiful scenery nature glass bottle landscape", "positive", "used"))

    def test_multi_text_widgets_and_muted_nodes(self):
        wf = self.wf()
        wf["nodes"][3] = {"id": 6, "type": "CLIPTextEncodeSDXL", "inputs": [{"name": "clip", "link": 3}],
                          "widgets_values": [1024, 1024, 0, 0, 1024, 1024, "global description", "local, tags"]}
        wf["nodes"] += [
            {"id": 40, "type": "CLIPTextEncode", "mode": 2, "inputs": [], "widgets_values": ["muted spare prompt, unused"]},
            {"id": 41, "type": "Note", "inputs": [], "widgets_values": ["this is only a note, ignore it"]},
        ]
        m = parse(None, wf)
        self.assertEqual(m.positive_prompt, "global description\nlocal, tags")
        # SDXL 编码节点的两格都读到（原来只留第一格）；静音的框列出来但标成停用；备注不算
        self.assertEqual(brief(m.text_boxes), [
            ("global description", "positive", "used"), ("local, tags", "positive", "used"),
            ("text, watermark", "negative", "used"),
            ("muted spare prompt, unused", "unknown", "disabled")])

    def test_subgraph_expansion(self):
        # 新版 ComfyUI 的子图：正向编码节点和它的文本框装在子图里，
        # 文本框提升成了子图实例上的部件；负向文本从外层连进子图
        sub_id = "11111111-2222-3333-4444-555555555555"
        wf = {
            "nodes": [
                {"id": 3, "type": "KSampler", "mode": 0,
                 "inputs": [{"name": "model", "link": 1}, {"name": "positive", "link": 30},
                            {"name": "negative", "link": 31}, {"name": "latent_image", "link": 2}],
                 "widgets_values": [1, "fixed", 20, 8, "euler", "normal", 1]},
                {"id": 4, "type": "CheckpointLoaderSimple", "inputs": [], "widgets_values": ["sdxl.safetensors"]},
                {"id": 5, "type": "EmptyLatentImage", "inputs": [], "widgets_values": [1024, 1024, 1]},
                {"id": 8, "type": "PrimitiveStringMultiline", "inputs": [], "widgets_values": ["ugly, deformed"]},
                # 实例节点的 inputs 顺序和定义里的不一样（真实模板里就是这样），得按名字对
                {"id": 12, "type": sub_id, "mode": 0,
                 "inputs": [{"name": "neg_text", "link": 32, "widget": {"name": "neg_text"}},
                            {"name": "text", "link": None, "widget": {"name": "text"}},
                            {"name": "clip", "link": 3}],
                 "outputs": [{"name": "CONDITIONING", "links": [30]}, {"name": "CONDITIONING", "links": [31]}],
                 "properties": {"proxyWidgets": [["-1", "text"], ["1", "seed"], ["-1", "neg_text"]]},
                 "widgets_values": ["promoted prompt typed on the subgraph node", None, ""]},
            ],
            "links": [[1, 4, 0, 3, 0, "MODEL"], [2, 5, 0, 3, 3, "LATENT"], [3, 4, 1, 12, 0, "CLIP"],
                      [30, 12, 0, 3, 1, "CONDITIONING"], [31, 12, 1, 3, 2, "CONDITIONING"],
                      [32, 8, 0, 12, 0, "STRING"]],
            "definitions": {"subgraphs": [{
                "id": sub_id, "name": "Prompts",
                "inputNode": {"id": -10}, "outputNode": {"id": -20},
                "inputs": [{"name": "clip", "type": "CLIP"}, {"name": "text", "type": "STRING"},
                           {"name": "neg_text", "type": "STRING"}],
                "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"},
                            {"name": "CONDITIONING", "type": "CONDITIONING"}],
                "nodes": [
                    {"id": 1, "type": "CLIPTextEncode",
                     "inputs": [{"name": "clip", "link": 1}, {"name": "text", "link": 2, "widget": {"name": "text"}}],
                     "widgets_values": ["inner default, should be overridden"]},
                    {"id": 2, "type": "CLIPTextEncode",
                     "inputs": [{"name": "clip", "link": 3}, {"name": "text", "link": 4, "widget": {"name": "text"}}],
                     "widgets_values": [""]},
                ],
                "links": [
                    {"id": 1, "origin_id": -10, "origin_slot": 0, "target_id": 1, "target_slot": 0, "type": "CLIP"},
                    {"id": 2, "origin_id": -10, "origin_slot": 1, "target_id": 1, "target_slot": 1, "type": "STRING"},
                    {"id": 3, "origin_id": -10, "origin_slot": 0, "target_id": 2, "target_slot": 0, "type": "CLIP"},
                    {"id": 4, "origin_id": -10, "origin_slot": 2, "target_id": 2, "target_slot": 1, "type": "STRING"},
                    {"id": 5, "origin_id": 1, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "CONDITIONING"},
                    {"id": 6, "origin_id": 2, "origin_slot": 0, "target_id": -20, "target_slot": 1, "type": "CONDITIONING"},
                ],
            }]},
        }
        original = copy.deepcopy(wf)
        m = parse(None, wf)
        self.assertEqual(m.positive_prompt, "promoted prompt typed on the subgraph node")
        self.assertEqual(m.negative_prompt, "ugly, deformed")
        self.assertEqual([(b["node_id"], b["text"], b["polarity"], b["status"]) for b in m.text_boxes], [
            ("12:1", "promoted prompt typed on the subgraph node", "positive", "used"),
            ("8", "ugly, deformed", "negative", "used"),
        ])
        self.assertEqual(m.params.model_name, "sdxl.safetensors")
        # 展开不能改动原始 workflow（它还要原样用于「复制全部原始参数」和写回）
        self.assertEqual(m.raw_workflow_json, original)

        # 老一点的前端不写 proxyWidgets：实例的 widgets_values 按「接到内部部件的输入口」顺序存值
        wf2 = copy.deepcopy(original)
        del wf2["nodes"][4]["properties"]
        wf2["nodes"][4]["widgets_values"] = ["typed without proxy list", ""]
        m2 = parse(None, wf2)
        self.assertEqual((m2.positive_prompt, m2.negative_prompt), ("typed without proxy list", "ugly, deformed"))

        # 子图实例被静音：里面的文本框列出来，但算停用
        wf3 = copy.deepcopy(original)
        wf3["nodes"][4]["mode"] = 2
        boxes = parse(None, wf3).text_boxes
        self.assertEqual([(b["node_id"], b["status"]) for b in boxes if b["node_id"].startswith("12:")],
                         [("12:1", "disabled")])


class PresentTests(unittest.TestCase):
    def test_present_exposes_text_boxes(self):
        g = basic_graph()
        g["70"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "spare, unused prompt", "clip": ["4", 1]}}
        r = imc.present(parse(g))
        self.assertEqual((r["prompt"], r["negative"]), ("masterpiece, 1girl", "lowres, bad anatomy"))
        self.assertEqual([(b["text"], b["polarity"], b["status"]) for b in r["text_boxes"]], [
            ("masterpiece, 1girl", "positive", "used"),
            ("lowres, bad anatomy", "negative", "used"),
            ("spare, unused prompt", "unknown", "floating"),
        ])
        first = r["text_boxes"][0]
        self.assertEqual(set(first), {"node_id", "class_type", "title", "field", "text", "polarity",
                                      "guess", "status", "stages", "note"})
        self.assertEqual((first["title"], first["stages"]), ("正面提示词", ["主采样"]))
        # 标题和类型名一样时不重复给
        self.assertEqual(r["text_boxes"][2]["title"], "")

    def test_webui_image_has_no_text_boxes_and_same_prompt(self):
        text = ("masterpiece, 1girl, <lora:foo:0.8>\nNegative prompt: lowres, bad hands\n"
                "Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 123, Size: 512x768, Model: sd15")
        r = imc.present(WebUIParser.parse_text(text))
        self.assertEqual(r["prompt"], "masterpiece, 1girl, <lora:foo:0.8>")
        self.assertEqual(r["negative"], "lowres, bad hands")
        self.assertEqual(r["text_boxes"], [])


if __name__ == "__main__":
    unittest.main()
