# -*- coding: utf-8 -*-
"""
图片生成元数据读取 —— 适配层。

真正的解析活儿现在交给 meta_engine（移植自独立项目「AI 元数据编辑器」的 core），
这个文件只负责两件事：
  1. 把 meta_engine 的 ImageMetadata 翻译成「图片信息」页要的展示结构
     （提示词 / 参数表 / 角色卡 / Civitai 资源 / LoRA 列表）
  2. 从解析结果里提取「这张图引用了哪些模型」，喂给缺失模型检测那条链路

为什么换掉旧实现：旧的那版是自己按正则和节点类型名硬猜的，
  · ComfyUI 只认几个常见节点类名，遇到 Efficiency / Impact / 各种 pipe 封装
    就抓不到提示词，更别提追 LoRA 链
  · NovelAI 只能读到 Comment 原文，V4/V4.5/V5 的多角色插槽完全没有
  · Forge / reForge / Fooocus / SwarmUI / InvokeAI / Draw Things 一律落到
    「未识别的元数据格式」
  · 隐写 PNG（微信/推特那种被剥了文本块但像素里还藏着参数的图）读不出来
新引擎把这些全补上了，而且每个格式都有专属门禁，不会把别家的图误判成自家的。

对外 API（webview_api.py 依赖的）保持不变：
    ImageMetaError / extract_image_metadata / ROLE_TO_LOCAL_FOLDER /
    find_local_model_file
extract_referenced_models* 这两个函数不再需要外部调用——引用清单现在直接放在
extract_image_metadata 返回结果的 "refs" 键里，但函数本身保留着，老代码不会炸。
"""
import os
import re

import meta_engine as me

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".avif")


class ImageMetaError(Exception):
    pass


# ============================================================
# 展示层：把 ImageMetadata 翻译成页面要的结构
# ============================================================

# 参数表里优先展示、且顺序固定的几项
_PRIMARY_ORDER = [
    "模型", "模型哈希", "VAE", "采样器", "调度器", "步数",
    "CFG", "种子", "尺寸", "重绘幅度", "Clip skip",
]

# other_params 的分组名 -> 中文显示名
_GROUP_LABELS = {
    "hires": "高分辨率修复",
    "adetailer": "ADetailer",
    "controlnet": "ControlNet",
    "core_extras": "核心参数",
    "dynamic_threshold": "Dynamic Thresholding",
    "freeu": "FreeU",
    "multidiffusion": "MultiDiffusion",
    "spectrum": "Spectrum",
    "soft_inpainting": "软重绘",
    "novelai": "NovelAI 参数",
    "other": "其他参数",
}

# ComfyUI 关键节点分类 -> 中文显示名
_NODE_CATEGORY_LABELS = {
    "facedetailer": "FaceDetailer",
    "adetailer": "ADetailer",
    "upscale": "放大",
    "controlnet": "ControlNet",
    "ipadapter": "IPAdapter",
    "vae": "VAE",
    "text_encoder": "文本编码器",
    "inpaint": "重绘",
    "multidiffusion": "分块扩散",
    "freeu": "FreeU",
    "video": "视频",
    "faceid": "人脸 ID",
    "preprocessor": "预处理器",
}

# 参数行解析的副产物：JSON 片段被当成键名（"modelName" 之类）。
# 这类碎片不该出现在参数表里，Civitai 资源有专门的卡片展示。
_JUNK_KEY_RE = re.compile(r'^["\'\[\]{}]|^\s*$')
_JUNK_KEYS = {"Civitai resources", "Hashes", "Lora hashes", "TI hashes"}


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, bool):
        return "是" if v else "否"
    return str(v)


def _flatten_group(label, value, rows):
    """把一个参数分组摊平成若干展示行。"""
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).startswith("_") or _JUNK_KEY_RE.match(str(k)) or k in _JUNK_KEYS:
                continue
            if isinstance(v, (dict, list)):
                _flatten_group(f"{label} · {k}", v, rows)
            elif v not in (None, ""):
                rows.append([f"{label} · {k}", _fmt(v)])
    elif isinstance(value, list):
        for i, item in enumerate(value, 1):
            sub = f"{label} {i}" if len(value) > 1 else label
            _flatten_group(sub, item, rows)
    elif value not in (None, ""):
        rows.append([label, _fmt(value)])


def _build_rows(m):
    """生成参数表的 [[键, 值], ...]。"""
    p = m.params
    primary = {
        "模型": p.model_name,
        "模型哈希": p.model_hash,
        "采样器": p.sampler_name,
        "调度器": p.scheduler,
        "步数": p.steps,
        "CFG": p.cfg_scale,
        "种子": p.seed,
        "尺寸": p.size or (f"{m.width}x{m.height}" if m.width and m.height else None),
        "重绘幅度": p.denoising_strength,
        "Clip skip": p.clip_skip,
    }
    # VAE 可能藏在 other_params.other 里（A1111）或 post_processing 里（ComfyUI）
    other = p.other_params if isinstance(p.other_params, dict) else {}
    vae = (other.get("other") or {}).get("VAE") if isinstance(other.get("other"), dict) else None
    if not vae:
        # ComfyUI 的 VAE 在关键节点里，提到主参数行，别让它挤在一堆节点信息中间
        for block in (m.post_processing or []):
            if isinstance(block, dict) and block.get("type") == "vae":
                for node in (block.get("nodes") or []):
                    if node.get("vae_name"):
                        vae = node["vae_name"]
                        break
            if vae:
                break
    if vae:
        primary["VAE"] = vae

    rows = [[k, _fmt(primary[k])] for k in _PRIMARY_ORDER
            if primary.get(k) not in (None, "")]

    # LoRA
    for i, lora in enumerate(p.loras or [], 1):
        name = lora.get("name") or ""
        w = lora.get("weight")
        if w is None:
            w = lora.get("strength_model")
        label = f"LoRA {i}" if len(p.loras) > 1 else "LoRA"
        rows.append([label, name + (f"  （强度 {_fmt(w)}）" if w is not None else "")])

    # Refiner / 二次采样
    for i, ref in enumerate(p.refiners or [], 1):
        bits = [f"{k}={_fmt(v)}" for k, v in ref.items()
                if k not in ("positive_prompt", "negative_prompt", "loras") and v not in (None, "")]
        if bits:
            rows.append([f"二次采样 {i}" if len(p.refiners) > 1 else "二次采样", "，".join(bits)])

    # 分组参数
    for key, value in other.items():
        if key.startswith("_") or key == "other":
            continue
        _flatten_group(_GROUP_LABELS.get(key, key), value, rows)
    if isinstance(other.get("other"), dict):
        _flatten_group(_GROUP_LABELS["other"],
                       {k: v for k, v in other["other"].items() if k != "VAE"}, rows)

    # ComfyUI 关键节点
    for block in (m.post_processing or []):
        if not isinstance(block, dict):
            continue
        cat = block.get("type", "")
        if cat == "vae" and primary.get("VAE"):
            continue  # 已经提到主参数行了
        label = _NODE_CATEGORY_LABELS.get(cat, cat)
        for node in (block.get("nodes") or []):
            bits = [f"{k}={_fmt(v)}" for k, v in node.items()
                    if k not in ("node_id", "class_type") and v not in (None, "")]
            head = node.get("class_type", "")
            rows.append([label, (head + "：" if head else "") + "，".join(bits)])

    return rows


def _collect_loras(m):
    """LoRA 列表（给前端单独渲染，带权重）。"""
    out = []
    for lora in (m.params.loras or []):
        w = lora.get("weight")
        if w is None:
            w = lora.get("strength_model")
        out.append({
            "name": lora.get("name") or "",
            "weight": w,
            "hash": lora.get("hash"),
        })
    return out


# ============================================================
# 模型引用提取（喂给「缺失模型检测」）
# ============================================================

_LORA_TAG_RE = re.compile(r"<lora:([^:>]+)(?::([^:>]+))?>", re.IGNORECASE)

# 缺失模型往哪放 —— 按分支决定，因为各家目录名根本对不上。
#
# 特别注意 Forge Neo：它的改动清单里有一条「Move embeddings folder into
# models folder」，也就是 embeddings 从根目录挪进了 models/。
# 沿用 A1111 的老位置，检测到缺 embedding 就会下到 Neo 根本不读的地方。
#
# 输出目录 Neo 也改了（outputs → output，单数），不过那条走的是读
# config.json 的路子，不在这张表里。

_FOLDERS_A1111 = {
    "Checkpoint":  "models/Stable-diffusion",
    "LoRA":        "models/Lora",
    "Embedding":   "embeddings",              # A1111 / Forge Classic：根目录
    "VAE":         "models/VAE",
    "ControlNet":  "models/ControlNet",
    "Upscaler":    "models/ESRGAN",
    "TextEncoder": "models/text_encoder",
}

_FOLDERS_NEO = dict(_FOLDERS_A1111)
_FOLDERS_NEO["Embedding"] = "models/embeddings"   # Neo 挪进 models/ 了

_FOLDERS_COMFY = {
    "Checkpoint":  "models/checkpoints",
    "LoRA":        "models/loras",
    "Embedding":   "models/embeddings",
    "VAE":         "models/vae",
    "ControlNet":  "models/controlnet",
    "Upscaler":    "models/upscale_models",
    "TextEncoder": "models/text_encoders",
}

_BRANCH_FOLDERS = {
    "classic": _FOLDERS_A1111,
    "a1111":   _FOLDERS_A1111,
    "neo":     _FOLDERS_NEO,
    "neo2":    _FOLDERS_NEO,
    "comfy":   _FOLDERS_COMFY,
    "comfyui": _FOLDERS_COMFY,
}

# 兼容旧代码里直接引用这个名字的地方，默认给 Classic 那套
ROLE_TO_LOCAL_FOLDER = _FOLDERS_A1111


def role_folders(branch=""):
    """按分支返回 {role: 相对目录}。认不出的分支退回 Classic。"""
    return _BRANCH_FOLDERS.get(str(branch or "").strip().lower(), _FOLDERS_A1111)


def role_folder(role, branch=""):
    return role_folders(branch).get(role)


_MODEL_FILE_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")


def _add_ref(refs, seen, role, name, hash_=None):
    name = (name or "").strip()
    if not name:
        return
    key = (role, name.lower())
    if key in seen:
        # 已经有同名条目了：如果这次带哈希而之前没有，补上（哈希能精确匹配，优先）
        if hash_:
            for r in refs:
                if (r["role"], r["name"].lower()) == key and not r["hash"]:
                    r["hash"] = hash_
        return
    seen.add(key)
    refs.append({"role": role, "name": name, "hash": (hash_ or None)})


def extract_refs(m):
    """
    从解析结果里提取「这张图用到了哪些模型」，每项 {"role","name","hash"}。

    哈希是能不能自动精确下载的分水岭：A1111/Forge 会写 Model hash、
    Lora hashes、TI hashes（都是 AutoV2 十位十六进制），能直接拿去 Civitai
    按哈希查；ComfyUI / NovelAI 通常只有文件名，只能走「按名字搜索 + 人工确认」。
    所以这里绝不瞎编哈希——没有就是 None，让上层老老实实走人工确认那条路。
    """
    refs, seen = [], set()
    p = m.params

    _add_ref(refs, seen, "Checkpoint", p.model_name, p.model_hash)

    for lora in (p.loras or []):
        _add_ref(refs, seen, "LoRA", lora.get("name"), lora.get("hash"))

    # 提示词里手写的 <lora:名字:权重>：Lora hashes 字段只记录通过网页 LoRA 选择器
    # 插进去的那些，手写或从别处复制的标签即使真的生效也完全不会被记录
    for text in (m.positive_prompt, m.negative_prompt):
        for mt in _LORA_TAG_RE.finditer(text or ""):
            _add_ref(refs, seen, "LoRA", mt.group(1))

    other = p.other_params if isinstance(p.other_params, dict) else {}
    flat_other = other.get("other") if isinstance(other.get("other"), dict) else {}

    _add_ref(refs, seen, "VAE", flat_other.get("VAE"))

    # TI hashes: "名字: 哈希, 名字: 哈希"
    ti = flat_other.get("TI hashes") or ""
    for part in str(ti).strip('"').split(","):
        part = part.strip()
        if ":" in part:
            name, _, h = part.rpartition(":")
            _add_ref(refs, seen, "Embedding", name.strip(), h.strip())

    # Hires 放大器
    hires = other.get("hires") if isinstance(other.get("hires"), dict) else {}
    up = hires.get("upscaler")
    if up and str(up).lower() not in ("latent", "none"):
        _add_ref(refs, seen, "Upscaler", up)

    # ControlNet（A1111 分组格式）
    for unit in (other.get("controlnet") or []):
        if isinstance(unit, dict):
            _add_ref(refs, seen, "ControlNet", unit.get("Model") or unit.get("model"))

    # ADetailer 检测模型
    for unit in (other.get("adetailer") or []):
        if isinstance(unit, dict):
            _add_ref(refs, seen, "Checkpoint", unit.get("checkpoint"))

    # ComfyUI 关键节点里的模型文件
    for block in (m.post_processing or []):
        if not isinstance(block, dict):
            continue
        cat = block.get("type", "")
        for node in (block.get("nodes") or []):
            if not isinstance(node, dict):
                continue
            if cat == "vae":
                _add_ref(refs, seen, "VAE", node.get("vae_name"))
            elif cat == "controlnet":
                _add_ref(refs, seen, "ControlNet", node.get("model"))
            elif cat == "upscale":
                _add_ref(refs, seen, "Upscaler", node.get("model"))
            elif cat == "text_encoder":
                for name in str(node.get("text_encoder") or "").split(","):
                    _add_ref(refs, seen, "TextEncoder", name.strip())
            elif cat in ("facedetailer", "adetailer"):
                _add_ref(refs, seen, "Checkpoint", node.get("model"))

    # 只留下本地真有对应目录的类型，免得界面上出来一堆没法处理的条目
    return [r for r in refs if r["role"] in ROLE_TO_LOCAL_FOLDER]


def find_local_model_file(webui_root, role, name, branch=""):
    """
    在对应的模型文件夹里按文件名（不含扩展名，忽略大小写）递归查找。
    找到返回完整路径，否则 None。

    元数据里的名字可能带子目录前缀（"角色/xxx.safetensors"）也可能不带，
    还可能带或不带扩展名，所以统一只比对「去掉路径和扩展名之后的主干」。

    embeddings 这一项两个位置都会看（根目录的 embeddings/ 和 models/embeddings）：
    Neo 把它挪进了 models/，但从老版本升上来的人机器上两个目录可能都在，
    只认一个就会误判成"缺失"然后重复下载。
    """
    if not webui_root or not os.path.isdir(webui_root):
        return None

    folders = []
    f = role_folder(role, branch)
    if f:
        folders.append(f)
    if role == "Embedding":
        for alt in ("embeddings", "models/embeddings"):
            if alt not in folders:
                folders.append(alt)
    if not folders:
        return None

    target_stem = os.path.splitext(os.path.basename(str(name).replace("\\", "/")))[0].strip().lower()
    if not target_stem:
        return None

    for folder in folders:
        target_dir = os.path.join(webui_root, *folder.split("/"))
        if not os.path.isdir(target_dir):
            continue
        for dirpath, _dirs, files in os.walk(target_dir):
            for fn in files:
                stem, ext = os.path.splitext(fn)
                if ext.lower() in _MODEL_FILE_EXTS and stem.strip().lower() == target_stem:
                    return os.path.join(dirpath, fn)
    return None


# ============================================================
# 主入口
# ============================================================

# 来源名 -> 给前端的徽章配色（与 style.css 里的 .src-badge 变体对应）
_SOURCE_TONE = {
    "ComfyUI": "comfy",
    "NovelAI": "nai",
    "Plain": "plain",
    "同名 txt 标注": "sidecar",
    "Plain (无元数据)": "plain",
}


def _tone_for(source_type):
    if source_type in _SOURCE_TONE:
        return _SOURCE_TONE[source_type]
    if source_type.startswith("WebUI"):
        return "webui"
    return "alt"


def extract_image_metadata(path):
    """
    解析一张图片（或一个 ComfyUI 工作流 JSON），返回展示用结构：

    {
      "ok": True,
      "source": "WebUI (Forge)",     # 来源显示名
      "tone": "webui",               # 徽章配色
      "has_meta": True,              # 是否读出了生成参数
      "prompt": str, "negative": str,
      "rows": [[键, 值], ...],       # 参数表
      "loras": [{"name","weight","hash"}],
      "characters": [{"index","prompt","negative_prompt","center"}],   # NovelAI 多角色
      "civitai": [{"model_name","version_name","weight","civitai_url",...}],
      "refs": [{"role","name","hash"}],
      "raw_text": str,               # 「复制全部原始参数」用
      "from_sidecar": bool,
      "file": {"width","height","format","size_kb"},
    }
    """
    if not path or not os.path.isfile(path):
        raise ImageMetaError("文件不存在")

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".json":
            m = me.parse_workflow_json(path)
            m.file_format, m.file_size_kb = "JSON", round(size / 1024, 1)
        else:
            m = me.read_metadata(path, size)
    except Exception as e:
        raise ImageMetaError(f"解析失败：{type(e).__name__}: {e}")

    return present(m)


def present(m):
    """把 ImageMetadata 翻译成展示结构。按路径读和按容器解析共用这一份。"""
    import copy as _copy
    if m.params_snapshot is None:
        try:
            m.params_snapshot = _copy.deepcopy(m.params)
        except Exception:
            pass

    source = m.source_type or "Unknown"
    if m.from_sidecar:
        # 图片本身没有嵌入元数据，内容来自同名 .txt / .caption
        # （LoRA 训练集里的图基本都是这种情况），说清楚免得用户以为是图里读出来的
        source = "同名 txt 标注"
    has_meta = bool(
        m.positive_prompt or m.negative_prompt or m.params.model_name
        or m.params.steps is not None or m.params.seed is not None
        or m.character_prompts or m.raw_prompt_json or m.civitai_resources
    )

    return {
        "ok": True,
        "source": source,
        "tone": _tone_for(source),
        "has_meta": has_meta,
        "prompt": m.positive_prompt or "",
        "negative": m.negative_prompt or "",
        "rows": _build_rows(m) if has_meta else [],
        "loras": _collect_loras(m),
        "characters": list(m.character_prompts or []),
        "civitai": list(m.civitai_resources or []),
        "refs": extract_refs(m) if has_meta else [],
        "raw_text": _raw_text_of(m),
        "from_sidecar": bool(m.from_sidecar),
        "file": {
            "width": m.width, "height": m.height,
            "format": m.file_format, "size_kb": m.file_size_kb,
        },
        # 可编辑对象本身。写回要用它（ComfyUI 的原始图、NAI 的 Comment JSON
        # 都在里面），调用方取走后应当从返回结构里 pop 掉，别往前端送。
        "_meta": m,
    }


def _raw_text_of(m):
    """「复制全部原始参数」的内容：有原文用原文，ComfyUI 给完整工作流 JSON。"""
    if m.raw_text:
        return m.raw_text
    if m.raw_comment_json:
        import json
        return json.dumps(m.raw_comment_json, ensure_ascii=False, indent=2)
    if m.raw_prompt_json or m.raw_workflow_json:
        import json
        return json.dumps(
            {"prompt": m.raw_prompt_json, "workflow": m.raw_workflow_json},
            ensure_ascii=False, indent=2)
    # 兜底：按 A1111 infotext 的样子重建一份，方便直接粘回 WebUI
    try:
        from meta_engine.webui_parser import WebUIParser
        return WebUIParser.build_text(m.positive_prompt, m.negative_prompt, m.params)
    except Exception:
        return m.positive_prompt or ""


# ============================================================
# 兼容垫片：老代码里这两个名字还在被引用
# ============================================================

def extract_referenced_models(settings, prompt_text="", negative_text=""):  # noqa: ARG001
    """旧签名保留。新代码请直接用 extract_image_metadata() 结果里的 "refs"。"""
    return []


def extract_referenced_models_from_comfy(summary):  # noqa: ARG001
    """旧签名保留。新代码请直接用 extract_image_metadata() 结果里的 "refs"。"""
    return []
