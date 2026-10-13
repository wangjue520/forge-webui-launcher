# -*- coding: utf-8 -*-
"""
reader.py —— 元数据读取调度（不依赖 PIL 的版本）。

调度顺序和判定条件跟上游 image_io.read_metadata 保持一致，只是把
"用 Pillow 打开图片拿 info/exif" 这一步换成了 container_reader：

  1. ComfyUI PNG 块 (prompt / workflow)      —— Easy Diffusion、A1111 parameters 让路
  2. WebUI PNG 块 (parameters)               —— SwarmUI 让路
  3. NovelAI PNG 块 (Description / Comment)  —— Fooocus 让路
  4. EXIF UserComment                        —— NAI → ComfyUI JSON → WebUI 文本
  5. 隐写 PNG (alpha / RGB 通道 LSB)
  6. 其他生成器 (Fooocus / SwarmUI / InvokeAI / Draw Things / Easy Diffusion)
  7. AI 供应商标注 (Gemini / GPT Image)
  8. 同名 sidecar 文本 (.txt / .caption)

每一步的"让路"判定都是为了不让某个解析器吞掉别家的图——这些门禁是上游
踩了很多坑之后攒出来的，这里原样复用，不要图省事简化掉。
"""
import json
import os
import re

from .metadata_types import ImageMetadata
from .comfyui_parser import ComfyUIParser
from .webui_parser import WebUIParser, _PARAM_LINE_RE
from .novelai_parser import NovelAIParser
from .civitai_air import extract_from_text, text_has_marker
from .png_stealth import probe_stealth_signature, decode_stealth_text, payload_to_metadata
from .alt_generators import AltGenerators
from .sidecar_reader import SidecarReader
from .container_reader import read_container

_WEBUI_SIGNATURE_RE = re.compile(r"Steps[:：]\s*\d+|Negative prompt[:：]", re.IGNORECASE)

# 本模块自用的内部键（由 container_reader 塞进 info），不能混进给解析器的视图
_PRIVATE_KEYS = ("_usercomment", "_exif_tiff", "_jpeg_comment")


def read_webui_fast(file_path, file_size_bytes=0, container=None):
    """
    快路径：只按 WebUI/A1111 的形态试一次，不是就返回 None。

    做的事只有：读容器 → 看 PNG 的 parameters 块（或 EXIF UserComment）
    有没有 WebUI 签名 → 有就直接交给 WebUIParser。
    完全跳过 ComfyUI 图遍历、NovelAI、隐写像素扫描、其他生成器探测、sidecar。

    门禁一条都没放松：带 workflow 块的（ComfyUI 兼容载体）、
    带 sui_image_params 的（SwarmUI）一律让路给完整流程，
    不会出现"为了快而把别家的图误判成 WebUI"。
    """
    if container is None:
        container = read_container(file_path)
    if container is None:
        return None
    raw_info = container["info"]

    # 这几个键一出现就说明不是单纯的 WebUI 图，直接交给完整流程
    if any(k in raw_info for k in ("prompt", "workflow", "Description", "Comment")):
        return None

    text = raw_info.get("parameters")
    if isinstance(text, str) and "sui_image_params" in text:
        return None
    if not isinstance(text, str) or not text.strip():
        text = raw_info.get("_usercomment")
    if not isinstance(text, str) or not _WEBUI_SIGNATURE_RE.search(text):
        return None

    meta = ImageMetadata(file_path=file_path)
    meta.file_size_kb = round((file_size_bytes or 0) / 1024, 1)
    meta.width, meta.height = container["width"], container["height"]
    meta.file_format = container["format"]

    parsed = WebUIParser.parse_text(text)
    _attach_air(parsed, text)
    return _finish(parsed, meta)


def read_metadata(file_path, file_size_bytes=0, fast_first=True):
    """
    读取一张图片的生成元数据，恒返回 ImageMetadata（读不出来时 source_type 为 Plain）。

    fast_first=True 时先走 read_webui_fast 试一次 WebUI 形态，不中再走完整调度。
    """
    if fast_first:
        quick = read_webui_fast(file_path, file_size_bytes)
        if quick is not None:
            return quick
    return _read_metadata_full(file_path, file_size_bytes)


def parse_container(container, file_path="", file_size_bytes=0):
    """
    从**已经读好的**容器数据解析，不碰磁盘。

    存在的理由：网页端拖进来一个文件时，浏览器手里已经有完整字节了，
    但拿不到磁盘路径。与其绕一大圈去要路径（那条路慢得离谱），不如让前端
    自己把 PNG 文本块 / EXIF 解出来，只把这几 KB 文本送过来。
    调度逻辑和门禁跟按路径读完全共用，不存在"两套解析器"。

    没有 file_path 时，隐写 PNG 和同名 sidecar 这两条要读原文件的路径会跳过
    ——前端拿到真实路径之后可以再补一次完整解析。
    """
    return _read_metadata_full(file_path, file_size_bytes, container=container)


def _read_metadata_full(file_path, file_size_bytes=0, container=None):
    meta = ImageMetadata(file_path=file_path)
    meta.file_size_kb = round((file_size_bytes or 0) / 1024, 1)

    if container is None and file_path:
        container = read_container(file_path)
    if container is None:
        side = _try_sidecar(file_path, meta)
        if side is not None:
            return side
        meta.source_type = "Plain (无元数据)"
        return meta

    meta.width, meta.height = container["width"], container["height"]
    meta.file_format = container["format"]
    raw_info = container["info"]

    uc_str = raw_info.get("_usercomment")
    exif_tiff = raw_info.get("_exif_tiff")
    jpeg_comment = raw_info.get("_jpeg_comment")
    info = {k: v for k, v in raw_info.items() if k not in _PRIVATE_KEYS}

    # 1) ComfyUI PNG 块
    if "prompt" in info or "workflow" in info:
        if not (AltGenerators.easy_diffusion_should_yield(info)
                or _webui_params_should_yield(info)):
            p = ComfyUIParser.parse_comfy_data(info.get("prompt"), info.get("workflow"))
            return _finish(p, meta)

    # 2) WebUI PNG 块
    params_block = info.get("parameters")
    if params_block is not None and "sui_image_params" not in str(params_block):
        p = WebUIParser.parse_text(params_block)
        _attach_air(p, params_block)
        if "workflow" in info:
            # 带 workflow 块 = ComfyUI 生成，parameters 只是兼容载体
            p.source_type = "ComfyUI"
            try:
                wf = json.loads(info["workflow"]) if isinstance(info["workflow"], str) else info["workflow"]
                if isinstance(wf, dict) and isinstance(wf.get("nodes"), list):
                    p.raw_workflow_json = wf
            except Exception:
                pass
        return _finish(p, meta)

    # 3) NovelAI PNG 块
    if "Description" in info or "Comment" in info:
        if not AltGenerators.nai_should_yield_to_fooocus(info):
            nai = NovelAIParser.parse_png_info(info)
            if nai:
                _attach_air(nai, f"{info.get('Description') or ''}\n{info.get('Comment') or ''}")
                nai.source_chunks = {k: str(info[k]) for k in
                                     ("Source", "Title", "Generation time")
                                     if isinstance(info.get(k), str)}
                return _finish(nai, meta)

    # 4) EXIF UserComment
    if uc_str:
        p = _from_usercomment(uc_str, meta)
        if p is not None:
            return p

    # 4b) JPEG COM 注释段（少数工具把参数写这里）
    if jpeg_comment and _WEBUI_SIGNATURE_RE.search(jpeg_comment):
        p = WebUIParser.parse_text(jpeg_comment)
        _attach_air(p, jpeg_comment)
        return _finish(p, meta)

    # 5) 隐写 PNG（要读像素，没有磁盘路径时做不了）
    if container["format"] == "PNG" and file_path and os.path.isfile(file_path):
        s = _read_stealth(file_path)
        if s is not None:
            return _finish(s, meta)

    # 6) 其他生成器探测
    view = AltGenerators.build_metadata_view(info, exif_raw=exif_tiff, usercomment=uc_str)
    alt = AltGenerators.detect_alt_generator(view)
    if alt is not None:
        return _finish(alt, meta)

    # 7) AI 供应商标注
    prov = AltGenerators.detect_ai_provider(view, image_path=file_path, file_size=file_size_bytes)
    if prov is not None:
        return _finish(prov, meta)

    # 8) sidecar 文本（同样需要磁盘路径）
    if file_path:
        side = _try_sidecar(file_path, meta)
        if side is not None:
            return side

    meta.source_type = "Plain (无元数据)"
    return meta


# ============================================================

def _from_usercomment(uc_str, meta):
    # NAI 优先：裸 Comment JSON 里也有 "prompt" 键，放到 ComfyUI 后面会被误吞成空图
    if not AltGenerators.nai_should_yield_to_fooocus_text(uc_str):
        nai = NovelAIParser.parse_usercomment(uc_str)
        if nai:
            _attach_air(nai, uc_str)
            return _finish(nai, meta)
    brace = uc_str.find("{")
    if brace != -1 and '"prompt"' in uc_str:
        try:
            cj = json.JSONDecoder().raw_decode(uc_str[brace:])[0]
            if AltGenerators.looks_like_comfy_usercomment_payload(cj):
                p = ComfyUIParser.parse_comfy_data(cj.get("prompt"), cj.get("workflow"))
                return _finish(p, meta)
        except Exception:
            pass
    if _WEBUI_SIGNATURE_RE.search(uc_str):
        p = WebUIParser.parse_text(uc_str)
        _attach_air(p, uc_str)
        return _finish(p, meta)
    return None


def _webui_params_should_yield(info):
    params = info.get("parameters")
    return (isinstance(params, str)
            and "sui_image_params" not in params
            and _PARAM_LINE_RE.search(params) is not None)


def _attach_air(meta, text):
    if meta is None or not text_has_marker(text):
        return
    try:
        meta.civitai_resources = extract_from_text(text)
    except Exception:
        pass


def _read_stealth(file_path):
    try:
        sig = probe_stealth_signature(file_path)
        if not sig:
            return None
        text = decode_stealth_text(file_path, sig)
        if not text:
            return None
        payload = payload_to_metadata(text)
        if not payload:
            return None
        s = _dispatch_stealth(payload)
        if s is not None:
            _attach_air(s, text)
        return s
    except Exception:
        return None


def _dispatch_stealth(payload):
    nai = NovelAIParser.parse_json_dict(payload)
    if nai:
        return nai
    if isinstance(payload.get("parameters"), str):
        return WebUIParser.parse_text(payload["parameters"])
    if "prompt" in payload or "workflow" in payload:
        return ComfyUIParser.parse_comfy_data(payload.get("prompt"), payload.get("workflow"))
    if any(isinstance(v, dict) and "class_type" in v for v in payload.values()):
        return ComfyUIParser.parse_comfy_data(payload, None)
    return None


def _try_sidecar(file_path, meta):
    try:
        side = SidecarReader.read_sidecar_metadata(file_path)
    except Exception:
        return None
    if side is None:
        return None
    return _finish(side, meta)


def _finish(parsed, meta):
    """把容器层拿到的文件信息盖回解析结果（上游 _apply_file_info 的等价实现）。"""
    if parsed is None:
        meta.source_type = "Plain (无元数据)"
        return meta
    parsed.file_path = meta.file_path
    parsed.file_format = meta.file_format
    parsed.width = parsed.width or meta.width
    parsed.height = parsed.height or meta.height
    parsed.file_size_kb = meta.file_size_kb
    return parsed


def parse_workflow_json(file_path):
    """直接解析一个 ComfyUI 工作流 JSON 文件（不是图片）。"""
    with open(file_path, "r", encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)
    air_hint = text_has_marker(raw)
    if not isinstance(data, dict):
        raise ValueError("不是有效的工作流 JSON")
    prompt_obj = wf_obj = None
    if "prompt" in data or "workflow" in data:
        prompt_obj, wf_obj = data.get("prompt"), data.get("workflow")
    elif "nodes" in data and "links" in data:
        wf_obj = data
    elif any(isinstance(v, dict) and "class_type" in v for v in data.values()):
        prompt_obj = data
    else:
        wf_obj = data
    return ComfyUIParser.parse_comfy_data(prompt_obj, wf_obj, scan_air=air_hint)
