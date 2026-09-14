"""
novelai_parser.py - NovelAI (V3 / V4 / V4.5 / V5) 元数据解析器

官方元数据规格 (NovelAI/novelai-image-metadata):
- PNG: tEXt 块 Description=整体提示词, Comment=JSON 参数 (Software=NovelAI, Source=模型名)
  - V3: Comment 含 prompt/uc/steps/sampler/seed/scale/strength/noise/width/height 等
  - V4/V4.5/V5: v4_prompt.caption.base_caption + char_captions、v4_negative_prompt、
    顶层 characterPrompts 数组
- JPEG/WebP: EXIF UserComment, 两种形态:
  1) PNG 包装对象 {Software, Description, Comment(内嵌 JSON 字符串), Source, ...}
  2) 裸 Comment JSON (含 uc / v4_prompt 键) —— WebP 导出常见
  (UserComment 还可能带 ASCII\\0\\0\\0 / UNICODE\\0 / charset="UTF-8" 等前缀,
   由 image_io._decode_user_comment 统一剥离后传入)

解析失败一律返回 None, 由调用方回退到原逻辑, 绝不抛异常。
"""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from .metadata_types import ImageMetadata, SamplerParameters
from .common import safe_int, safe_float

logger = logging.getLogger(__name__)

# Comment JSON 中视为 NovelAI 参数体的特征键 (裸 Comment JSON 判定, 参考 looks_like_comment_body)
_COMMENT_BODY_KEYS = ("uc", "v4_prompt", "v4_negative_prompt", "characterPrompts", "character_prompts")
# 记入 other_params["novelai"] 的 NAI 专有参数 (键名 → 展示键名)
_NAI_PARAM_KEYS = (
    "steps", "sampler", "seed", "strength", "noise", "scale", "cfg_rescale",
    "sm", "sm_dyn", "dynamic_thresholding", "noise_schedule", "legacy_v3_extend",
    "uncond_scale", "skip_cfg_above_sigma", "skip_cfg_above_sigmas", "controlnet_strength",
    "legacy", "add_original_image", "params_version", "ucPreset", "qualityToggle",
    "use_coords", "use_order", "request_type", "width", "height", "img2img",
)


def _flatten_text_value(value: Any) -> str:
    """把 NAI 的提示词值归一为纯文本 (str / 结构化 caption / 其他标量)"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _flatten_structured_prompt(value)
    if isinstance(value, (list, tuple)):
        parts = [_flatten_text_value(v) for v in value]
        return ", ".join(p for p in parts if p)
    return str(value).strip()


def _flatten_structured_prompt(blob: Any) -> str:
    """展平 V4/V4.5/V5 的 v4_prompt / v4_negative_prompt 结构 (caption.base_caption + char_captions)"""
    if isinstance(blob, dict):
        caption = blob.get("caption")
        if isinstance(caption, dict) and caption.get("base_caption"):
            return _flatten_text_value(caption.get("base_caption"))
        return _flatten_text_value(blob.get("prompt") or blob.get("caption") or blob)
    return _flatten_text_value(blob)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return _flatten_text_value(value)


def _prompts_from_comment(description: str, comment_data: Any) -> Tuple[str, str]:
    """从 Description 与 Comment JSON 提取 (正向, 负向): Description 优先,
    Comment 的 prompt / v4_prompt 结构兜底; 负向取 uc / v4_negative_prompt。"""
    prompt = description
    negative = ""
    if isinstance(comment_data, dict):
        if not prompt:
            prompt = _flatten_text_value(comment_data.get("prompt"))
        if "v4_prompt" in comment_data and not prompt:
            prompt = _flatten_structured_prompt(comment_data["v4_prompt"])
        negative = _flatten_text_value(comment_data.get("uc"))
        if not negative and "v4_negative_prompt" in comment_data:
            negative = _flatten_structured_prompt(comment_data["v4_negative_prompt"])
    return prompt, negative


def looks_like_comment_body(data: Any) -> bool:
    """裸 Comment JSON 判定: 含 NAI 特征键且不含 WebUI 风格的 negative_prompt 键"""
    if not isinstance(data, dict):
        return False
    return any(k in data for k in _COMMENT_BODY_KEYS) and "negative_prompt" not in data


def _extract_character_prompts(comment_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """提取 V4/V4.5/V5 角色提示词, 按 index 合并三个来源:
    v4_prompt.caption.char_captions / v4_prompt.character_prompts / 顶层 characterPrompts
    (+ v4_negative_prompt.caption.char_captions 的角色负面)
    """
    by_index: Dict[int, Dict[str, Any]] = {}

    def ensure(index: int) -> Dict[str, Any]:
        slot = by_index.get(index)
        if slot is None:
            slot = {"index": index, "prompt": "", "negative_prompt": ""}
            by_index[index] = slot
        return slot

    def set_center(slot: Dict[str, Any], center: Any) -> None:
        if isinstance(center, dict) and "center" not in slot:
            slot["center"] = {"x": center.get("x", 0.5), "y": center.get("y", 0.5)}

    structured = comment_data.get("v4_prompt")
    if isinstance(structured, dict):
        nested = structured.get("character_prompts")
        if isinstance(nested, list):
            for index, char in enumerate(nested):
                if not isinstance(char, dict):
                    continue
                slot = ensure(index)
                p = _as_text(char.get("prompt", ""))
                n = _as_text(char.get("ucPrompt", char.get("uc", "")))
                if p:
                    slot["prompt"] = p
                if n:
                    slot["negative_prompt"] = n
                set_center(slot, char.get("center"))
        caption = structured.get("caption")
        if isinstance(caption, dict):
            char_captions = caption.get("char_captions")
            if isinstance(char_captions, list):
                for index, item in enumerate(char_captions):
                    if not isinstance(item, dict):
                        continue
                    slot = ensure(index)
                    text = _as_text(item.get("char_caption") or item.get("prompt"))
                    if text and not slot["prompt"]:
                        slot["prompt"] = text
                    centers = item.get("centers")
                    if (isinstance(centers, list) and centers
                            and isinstance(centers[0], dict)):
                        set_center(slot, centers[0])

    v4_neg = comment_data.get("v4_negative_prompt")
    if isinstance(v4_neg, dict):
        caption = v4_neg.get("caption") if isinstance(v4_neg.get("caption"), dict) else {}
        char_captions = caption.get("char_captions")
        if isinstance(char_captions, list):
            for index, item in enumerate(char_captions):
                if not isinstance(item, dict):
                    continue
                slot = ensure(index)
                text = _as_text(item.get("char_caption") or item.get("uc"))
                if text and not slot["negative_prompt"]:
                    slot["negative_prompt"] = text

    top = comment_data.get("characterPrompts") or comment_data.get("character_prompts")
    if isinstance(top, list):
        for index, char in enumerate(top):
            if not isinstance(char, dict):
                continue
            slot = ensure(index)
            p = _as_text(char.get("prompt") or char.get("char_caption"))
            n = _as_text(char.get("uc") or char.get("ucPrompt"))
            if p:
                slot["prompt"] = p
            if n:
                slot["negative_prompt"] = n
            set_center(slot, char.get("center"))

    return [by_index[i] for i in sorted(by_index)
            if by_index[i].get("prompt") or by_index[i].get("negative_prompt")]


def _extract_gen_params(comment_data: Dict[str, Any]) -> Dict[str, Any]:
    """提取 NAI 专有生成参数 (仅供检视展示, 不参与 WebUI 回写)"""
    params: Dict[str, Any] = {}
    for key in _NAI_PARAM_KEYS:
        if key in comment_data and comment_data[key] is not None:
            params[key] = comment_data[key]
    return params


def _build_meta(prompt: str, negative: str, comment_data: Optional[Dict[str, Any]],
                raw_text: str = "") -> ImageMetadata:
    """由展平的正负提示词 + Comment JSON 构建 ImageMetadata (source_type="NovelAI")"""
    meta = ImageMetadata(source_type="NovelAI", raw_text=raw_text)
    meta.positive_prompt = prompt
    meta.negative_prompt = negative
    params = SamplerParameters()
    if isinstance(comment_data, dict):
        params.steps = safe_int(comment_data.get("steps"))
        params.seed = safe_int(comment_data.get("seed"))
        params.cfg_scale = safe_float(comment_data.get("scale"))
        sampler = comment_data.get("sampler")
        if isinstance(sampler, str) and sampler.strip():
            params.sampler_name = sampler.strip()
        width, height = safe_int(comment_data.get("width")), safe_int(comment_data.get("height"))
        if width and height:
            params.size = f"{width}x{height}"
        strength = safe_float(comment_data.get("strength"))
        if strength is not None and strength < 1.0:
            # img2img: NAI strength 即重绘幅度 (<1 才是 img2img; V3 txt2img 恒为 1)
            params.denoising_strength = strength
            params.other_params["img2img"] = {"denoising_strength": strength,
                                              "noise": safe_float(comment_data.get("noise"))}
        gen = _extract_gen_params(comment_data)
        if gen:
            params.other_params["novelai"] = gen
        chars = _extract_character_prompts(comment_data)
        if chars:
            meta.character_prompts = chars
        # 写回锚点: 保留 Comment JSON 原文 (NAI 形态就地保存时只改 prompt/uc, 其余键原样)
        meta.raw_comment_json = comment_data
    meta.params = params
    return meta


def _parse_nai_dict(data: Any) -> Optional[ImageMetadata]:
    """解析已 JSON 解码的 NAI 元数据 dict (PNG 包装对象 或 裸 Comment JSON)。

    返回 None 表示不是 NovelAI 或解析不出提示词 (调用方回退原逻辑)。
    """
    if not isinstance(data, dict):
        return None
    try:
        software = str(data.get("Software", "") or "")
        is_nai = "novelai" in software.lower()
        comment_body = looks_like_comment_body(data)
        # 包装对象判定: Software=NovelAI, 或 Description+Comment/Source 同现, 或裸 Comment 形态
        if not is_nai and not comment_body \
                and not ("Description" in data and ("Comment" in data or "Source" in data)):
            return None

        description = _flatten_text_value(data.get("Description"))
        nested = data.get("Comment")
        comment_data: Optional[Dict[str, Any]] = None
        if isinstance(nested, str) and nested.strip():
            try:
                parsed = json.loads(nested)
                comment_data = parsed if isinstance(parsed, dict) else None
            except (ValueError, TypeError):
                comment_data = None
        elif comment_body:
            comment_data = data  # 裸 Comment JSON: 参数体就是对象本身

        prompt, negative = _prompts_from_comment(description, comment_data)

        if not prompt and not negative:
            return None
        raw_text = description or _flatten_text_value(comment_data.get("prompt")) \
            if isinstance(comment_data, dict) else description
        return _build_meta(prompt, negative, comment_data, raw_text=raw_text)
    except Exception as e:  # 防御: 任何畸形输入都不得向上抛
        logger.debug("Failed to parse NovelAI metadata dict: %s", e)
        return None


def _loads_braced(text: str) -> Optional[Dict[str, Any]]:
    """从文本首个 '{' 起 raw_decode (容忍 charset 前缀/前后杂散), 失败返回 None"""
    brace = text.find("{")
    if brace < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[brace:])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


class NovelAIParser:
    """NovelAI 元数据解析入口"""

    @staticmethod
    def parse_png_info(info: Dict[str, Any]) -> Optional[ImageMetadata]:
        """解析 PNG tEXt 块 (Description/Comment/Software/Source)。非 NAI 返回 None。"""
        try:
            if not isinstance(info, dict):
                return None
            software = str(info.get("Software", "") or info.get("Source", "") or "")
            comment_data = _loads_braced(info.get("Comment") or "") \
                if isinstance(info.get("Comment"), str) else None
            is_nai = "novelai" in software.lower()
            body = comment_data is not None and looks_like_comment_body(comment_data)
            if not is_nai and not body:
                return None
            description = _flatten_text_value(info.get("Description"))
            prompt, negative = _prompts_from_comment(description, comment_data)
            if not prompt and not negative:
                return None
            return _build_meta(prompt, negative, comment_data, raw_text=description)
        except Exception as e:
            logger.debug("Failed to parse NovelAI PNG info: %s", e)
            return None

    @staticmethod
    def parse_usercomment(uc_str: str) -> Optional[ImageMetadata]:
        """解析 EXIF UserComment 文本 (PNG 包装对象 或 裸 Comment JSON)。非 NAI 返回 None。"""
        try:
            if not uc_str or "{" not in uc_str:
                return None
            return _parse_nai_dict(_loads_braced(uc_str))
        except Exception as e:
            logger.debug("Failed to parse NovelAI UserComment: %s", e)
            return None

    @staticmethod
    def parse_json_dict(data: Any) -> Optional[ImageMetadata]:
        """解析已解码的 JSON dict (隐写载荷等已无原始文本的场景)。非 NAI 返回 None。"""
        return _parse_nai_dict(data)
