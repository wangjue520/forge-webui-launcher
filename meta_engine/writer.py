# -*- coding: utf-8 -*-
"""
writer.py —— 元数据写回调度。

按图片的来源决定写回形态，这是这套东西跟"只读图库"的本质区别：

  ComfyUI  编辑提示词直接落进**工作流图的节点里**（不是另写一段说明文字），
           保存后的图拖回 ComfyUI 仍然能按新提示词重跑。多阶段流水线
           （二采 / Refiner）只写"跟读取时快照不同"的字段，没被编辑的
           参数不会被汇总值污染到别的采样器节点上。
  NovelAI  以 NAI 原生形态写回：Comment JSON 只更新 prompt / uc
           （及 v4 结构里的 base_caption），其余 50 多个专有键原样保留，
           Software / Source 身份块也不动。不这么做的话，"改一下提示词"
           会让一张 NAI 图降级成普通 WebUI 图，专有参数全丢。
  其他     按 A1111 infotext 标准文本写回（目标格式没有原生载体时的通用形态，
           专有参数不伪造）。

落盘由 container_writer 做块级手术，像素一个字节都不动。
"""
import copy
import json
import os
import re

from .comfyui_parser import ComfyUIParser
from .webui_parser import WebUIParser
from . import container_writer as cw

_SIZE_RE = re.compile(r"\s*(\d+)\s*[xX\u00d7*]\s*(\d+)\s*")

# 本编辑器"自有"的 PNG 文本块：保存时一律重建，源图同名旧块丢弃，
# 这样用户清空某个字段之后，旧块不会在下次读取时把旧值复活
_OWNED_PNG_KEYS = ("prompt", "workflow", "parameters",
                   "Description", "Comment", "Software", "Source",
                   "Title", "Generation time")


class SaveError(Exception):
    pass


# ============================================================
# 载荷构造
# ============================================================

def _comfy_payload(meta, include_workflow=True):
    updated = ComfyUIParser.update_comfy_prompt_text(
        meta.raw_prompt_json, meta.positive_prompt, meta.negative_prompt,
        meta.params, params_snapshot=meta.params_snapshot)
    texts = {"prompt": json.dumps(updated, ensure_ascii=False, separators=(",", ":"))}
    if include_workflow and meta.raw_workflow_json:
        # UI 工作流里的文本 widget 跟着同步，这样把图拖回 ComfyUI 画布，
        # 看到的就是编辑后的提示词。数值参数不同步 —— API prompt 块已经承载了
        try:
            ComfyUIParser.sync_workflow_prompt_widgets(
                meta.raw_workflow_json, meta.raw_prompt_json, updated)
            texts["workflow"] = json.dumps(meta.raw_workflow_json,
                                           ensure_ascii=False, separators=(",", ":"))
        except Exception:
            pass
    return texts, updated


def _nai_payload(meta):
    c = copy.deepcopy(meta.raw_comment_json)
    if meta.positive_prompt is not None:
        c["prompt"] = meta.positive_prompt
        v4 = c.get("v4_prompt")
        if isinstance(v4, dict) and isinstance(v4.get("caption"), dict):
            v4["caption"]["base_caption"] = meta.positive_prompt
    if meta.negative_prompt is not None:
        c["uc"] = meta.negative_prompt
        v4n = c.get("v4_negative_prompt")
        if isinstance(v4n, dict) and isinstance(v4n.get("caption"), dict):
            v4n["caption"]["base_caption"] = meta.negative_prompt

    # 参数只写用户真正改过的（跟读取时的快照比），而且键本来就存在才写 ——
    # Comment JSON 的键集是 NAI 那个版本的事实形态，不该由我们凭空造键
    snap = meta.params_snapshot
    p = meta.params

    def changed(attr, snap_val):
        return not (snap and snap_val == getattr(p, attr))

    if p.steps is not None and "steps" in c and changed("steps", snap.steps if snap else None):
        c["steps"] = int(p.steps)
    if p.seed is not None and "seed" in c and changed("seed", snap.seed if snap else None):
        c["seed"] = int(p.seed)
    if p.cfg_scale is not None and "scale" in c and changed("cfg_scale", snap.cfg_scale if snap else None):
        c["scale"] = float(p.cfg_scale)
    if p.sampler_name and "sampler" in c and changed("sampler_name", snap.sampler_name if snap else None):
        c["sampler"] = p.sampler_name
    if p.size and "width" in c and "height" in c and changed("size", snap.size if snap else None):
        m = _SIZE_RE.fullmatch(p.size)
        if m:
            c["width"], c["height"] = int(m.group(1)), int(m.group(2))

    texts = {
        "Comment": json.dumps(c, ensure_ascii=False),
        "Description": meta.positive_prompt or "",
        "Software": "NovelAI",
    }
    for key in ("Source", "Title", "Generation time"):
        val = (meta.source_chunks or {}).get(key)
        if val:
            texts[key] = val
    return texts


def build_webui_text(meta):
    return WebUIParser.build_text(meta.positive_prompt, meta.negative_prompt, meta.params)


def _png_payload(meta, include_workflow=True):
    """返回 (要写入的文本块 dict, 用到的写回形态名)。"""
    if meta.source_type == "ComfyUI" and meta.raw_prompt_json:
        texts, _ = _comfy_payload(meta, include_workflow)
        return texts, "comfy"
    if meta.source_type == "NovelAI" and meta.raw_comment_json:
        try:
            return _nai_payload(meta), "nai"
        except Exception:
            pass   # NAI 写回出问题就退回通用文本，别让保存直接失败
    return {"parameters": build_webui_text(meta)}, "webui"


# ============================================================
# 保存 / 清理
# ============================================================

def save_metadata(file_path, meta, output_path=None, include_comfy_workflow=True):
    """
    把 meta 写回图片。output_path 为 None 时就地保存。

    只支持"格式不变"的保存（PNG→PNG、JPEG→JPEG、WebP→WebP）。
    跨格式另存请走 image_convert.convert_and_save —— 那个需要真正重编码像素。
    """
    if not os.path.exists(file_path):
        raise SaveError("源文件不存在")
    target = output_path or file_path
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)

    ext = os.path.splitext(target)[1].lower()
    if ext == ".json":
        return _save_json(meta, target, include_comfy_workflow)

    src_fmt = cw.detect_format(file_path)
    if src_fmt is None:
        raise SaveError("无法识别源图片格式（只支持 PNG / JPEG / WebP）")
    want = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}.get(ext)
    if want and want != src_fmt:
        raise SaveError(f"跨格式另存（{src_fmt} → {want}）需要重新编码像素，请用「另存为…」")

    if src_fmt == "PNG":
        texts, mode = _png_payload(meta, include_comfy_workflow)
        cw.write_png(file_path, target, new_texts=texts, owned_keys=_OWNED_PNG_KEYS)
        return mode
    # JPEG / WebP 只有一个 UserComment 文本槽，一律按 A1111 标准文本写
    text = build_webui_text(meta)
    if src_fmt == "JPEG":
        cw.write_jpeg(file_path, target, user_comment=text)
    else:
        w = getattr(meta, "width", 0) or 0
        h = getattr(meta, "height", 0) or 0
        cw.write_webp(file_path, target, user_comment=text, width=w, height=h)
    return "webui"


def _save_json(meta, target, include_workflow=True):
    if meta.source_type == "ComfyUI" and meta.raw_prompt_json:
        _texts, updated = _comfy_payload(meta, include_workflow)
        data = ({"prompt": updated, "workflow": meta.raw_workflow_json}
                if include_workflow and meta.raw_workflow_json else updated)
    elif meta.raw_workflow_json:
        data = meta.raw_workflow_json
    else:
        data = {"positive_prompt": meta.positive_prompt,
                "negative_prompt": meta.negative_prompt}
    tmp = target + f".tmp_{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return "json"


def strip_metadata(file_path, output_path=None):
    """把图片里的元数据清干净（分享前去掉提示词等信息）。"""
    if not os.path.exists(file_path):
        raise SaveError("源文件不存在")
    target = output_path or file_path
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    fmt = cw.detect_format(file_path)
    if fmt == "PNG":
        cw.write_png(file_path, target, strip=True)
    elif fmt == "JPEG":
        cw.write_jpeg(file_path, target, strip=True)
    elif fmt == "WEBP":
        cw.write_webp(file_path, target, strip=True)
    else:
        raise SaveError("无法识别图片格式（只支持 PNG / JPEG / WebP）")
    return True


# ============================================================
# 损失预告
# ============================================================

_SETTING_LABEL_RE = re.compile(r"([A-Za-z][\w \-]*)\s*[:：]")


def _setting_labels(text):
    if not text:
        return set()
    lines = text.strip().split("\n")
    return {m.group(1).strip() for m in _SETTING_LABEL_RE.finditer(lines[-1])}


def collect_save_warnings(file_path, meta, output_path=None, include_comfy_workflow=True):
    """
    保存前的损失预告，返回 (fatal, warnings)。
    fatal 非 None 表示这次保存不该进行；warnings 是需要用户确认的具体损失清单。

    跟原项目相比这里少了两条警告，因为块级手术让它们不再成立：
      · 动图丢帧 —— 帧数据根本没被解码重写，PNG/WebP 动图保存后完全一致
      · 动图过大拒绝保存 —— 同理，不再有内存上限
    """
    warnings = []
    fmt = cw.detect_format(file_path)
    if fmt is None:
        return "无法识别图片格式（只支持 PNG / JPEG / WebP）", warnings

    ext = os.path.splitext(output_path or file_path)[1].lower()
    want = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}.get(ext, fmt)
    if want != fmt:
        return (f"跨格式另存（{fmt} → {want}）需要重新编码像素，"
                "请用「另存为…」，那条路会单独提示损失。"), warnings

    if fmt == "JPEG":
        frames = 1
    elif fmt == "PNG":
        frames = cw.count_png_frames(file_path)
    else:
        frames = cw.count_webp_frames(file_path)
    if frames > 1:
        warnings.append(f"这是一张 {frames} 帧的动图。只改元数据不会动帧数据，动画会完整保留。")

    # 参数块重建：编辑器没显示的设置项会丢
    if not (meta.source_type == "ComfyUI" and meta.raw_prompt_json):
        src_text = None
        if fmt == "PNG":
            from .container_reader import read_container
            info = (read_container(file_path) or {}).get("info") or {}
            src_text = info.get("parameters")
        else:
            from .container_reader import read_container
            info = (read_container(file_path) or {}).get("info") or {}
            src_text = info.get("_usercomment")
        if src_text:
            dropped = _setting_labels(src_text) - _setting_labels(build_webui_text(meta))
            if dropped:
                warnings.append(
                    "编辑器只显示了部分设置项，源参数块里这些设置不会写回："
                    + "、".join(sorted(dropped)) + "。")

    if fmt in ("JPEG", "WEBP"):
        text = build_webui_text(meta)
        if fmt == "JPEG" and len(text.encode("utf-16le")) > 60000:
            warnings.append("参数文本较长，JPEG 元数据段有 64KB 上限，"
                            "会自动改用 UTF-8 编码；如仍超限保存会失败，建议另存为 PNG。")
    if meta.source_type == "NovelAI" and meta.raw_comment_json and fmt != "PNG":
        warnings.append("NovelAI 的专有参数只有 PNG 能原生承载。"
                        f"存成 {fmt} 会转成 WebUI 标准文本，多角色插槽等信息会丢失。")
    return None, warnings
