"""
send_bridge.py — 把一张图的生成参数「发送到」WebUI / ComfyUI

图片信息页、输出管理器里点「发送到 WebUI / 发送到 ComfyUI」走这里。分两半：

转换（纯函数，方便单测）
  - to_infotext(meta)            任何来源 → A1111/Forge 的 infotext（WebUI 自己的 ↙ 粘贴按钮认的那种文本）
                                 WebUI 的图原样用原文（插件参数 ADetailer / ControlNet / 高清修复全在里面）；
                                 ComfyUI / NovelAI 的图提取提示词、LoRA、采样参数、高清修复重新拼一份
  - comfy_payload(meta, lists)   任何来源 → 交给 ComfyUI 网页载入的东西：
                                 ComfyUI 的图优先用图里存的原工作流；别的来源按参数生成一个默认工作流
                                 （模型加载 → LoRA 串 → 提示词 → 采样 → [高清修复] → 保存）
  - lora_stem / lora_tag / lora_tags   LoRA 调用格式：只要文件名，不带文件夹和后缀
                                 （<lora:Anima\\画风\\xxx:1> WebUI 认不出，<lora:xxx:1> 才行）

送达（HTTP，打本机实例的桥接插件）
  - comfy_lists(base)            问 ComfyUI 它能看到哪些模型（/object_info），默认工作流里的文件名按它填
  - post_comfy / post_webui      交给桥接插件；网页没打开就先存着，网页一打开自动载入
  - wait_ack                     等网页回执，告诉用户成没成
"""
import os
import re
import time

try:
    from meta_engine.metadata_types import SamplerParameters
    from meta_engine.webui_parser import WebUIParser
except Exception:  # pragma: no cover - 单独导入本模块时
    SamplerParameters = None
    WebUIParser = None


# ============================================================
# 采样器 / 调度器名字对照（ComfyUI 内部名 ↔ WebUI 下拉框显示名）
# ============================================================

# (ComfyUI, WebUI)。WebUI 下拉框里没有的名字不能硬塞：新版 Gradio 遇到不在选项里的值会让整次粘贴报错，
# 所以对不上的宁可不写（WebUI 保持当前选择），并在提示里说一声
_SAMPLERS = [
    ("euler", "Euler"), ("euler_ancestral", "Euler a"), ("heun", "Heun"),
    ("dpm_2", "DPM2"), ("dpm_2_ancestral", "DPM2 a"), ("lms", "LMS"),
    ("dpm_fast", "DPM fast"), ("dpm_adaptive", "DPM adaptive"),
    ("dpmpp_2s_ancestral", "DPM++ 2S a"), ("dpmpp_sde", "DPM++ SDE"),
    ("dpmpp_2m", "DPM++ 2M"), ("dpmpp_2m_sde", "DPM++ 2M SDE"),
    ("dpmpp_2m_sde_heun", "DPM++ 2M SDE Heun"), ("dpmpp_3m_sde", "DPM++ 3M SDE"),
    ("ddim", "DDIM"), ("uni_pc", "UniPC"), ("lcm", "LCM"), ("restart", "Restart"),
    ("deis", "DEIS"), ("ipndm", "iPNDM"), ("ipndm_v", "iPNDM_v"),
    ("euler_cfg_pp", "Euler CFG++"), ("euler_ancestral_cfg_pp", "Euler a CFG++"),
    ("dpmpp_2m_cfg_pp", "DPM++ 2M CFG++"), ("er_sde", "ER SDE"),
]
_C2W_SAMPLER = {c: w for c, w in _SAMPLERS}
_W2C_SAMPLER = {w.lower(): c for c, w in _SAMPLERS}
_W2C_SAMPLER.update({"dpm++ 2m sde gpu": "dpmpp_2m_sde_gpu", "unipc bh2": "uni_pc_bh2", "plms": "ddim"})
# ComfyUI 有、意思跟 WebUI 某个一样的变体
_C2W_SAMPLER.update({"dpmpp_2m_sde_gpu": "DPM++ 2M SDE", "dpmpp_sde_gpu": "DPM++ SDE",
                     "dpmpp_3m_sde_gpu": "DPM++ 3M SDE", "uni_pc_bh2": "UniPC"})

_SCHEDULERS = [
    ("normal", "Normal"), ("karras", "Karras"), ("exponential", "Exponential"),
    ("sgm_uniform", "SGM Uniform"), ("simple", "Simple"), ("ddim_uniform", "DDIM"),
    ("beta", "Beta"), ("kl_optimal", "KL Optimal"),
]
_C2W_SCHED = {c: w for c, w in _SCHEDULERS}
_W2C_SCHED = {w.lower(): c for c, w in _SCHEDULERS}
_W2C_SCHED.update({"uniform": "normal", "polyexponential": "exponential", "align your steps": "sgm_uniform",
                   "native": "normal", "turbo": "simple"})

# WebUI 调度器选「Automatic」时各采样器实际用的调度（A1111 1.9+ 的采样器定义里写死的）
_AUTO_SCHED = {"dpmpp_2m": "karras", "dpmpp_sde": "karras", "dpmpp_2s_ancestral": "karras",
               "dpm_2": "karras", "dpm_2_ancestral": "karras", "dpmpp_2m_sde": "exponential",
               "dpmpp_2m_sde_heun": "exponential", "dpmpp_3m_sde": "exponential"}

# 老版 WebUI 把调度器写进采样器名里：「DPM++ 2M Karras」
_SCHED_SUFFIX = re.compile(r"\s+(Karras|Exponential|SGM Uniform|Uniform|Simple|Normal|Beta|KL Optimal)$", re.I)


def webui_sampler(sampler, scheduler=None):
    """ComfyUI / NovelAI 的采样器、调度器 → WebUI 的 (Sampler, Schedule type)，认不出的给 None"""
    s = str(sampler or "").strip()
    if s.lower().startswith("k_"):          # NovelAI：k_euler_ancestral
        s = s[2:]
    low = s.lower()
    # 本来就是 WebUI 的名字就原样用，否则按 ComfyUI 内部名查
    ws = next((w for _c, w in _SAMPLERS if w.lower() == low), None) or _C2W_SAMPLER.get(low)
    sc = str(scheduler or "").strip().lower()
    wsc = _C2W_SCHED.get(sc) or next((w for _c, w in _SCHEDULERS if w.lower() == sc), None)
    return ws, wsc


def comfy_sampler(sampler, scheduler=None, strict=False):
    """WebUI 的 (Sampler, Schedule type) → ComfyUI 的 (sampler_name, scheduler)。
    认不出的采样器给 euler（strict=True 时给 None）"""
    s = str(sampler or "").strip()
    sched = str(scheduler or "").strip()
    m = _SCHED_SUFFIX.search(s)
    if m:                                   # 「DPM++ 2M Karras」→ DPM++ 2M + Karras
        if not sched or sched.lower() == "automatic":
            sched = m.group(1)
        s = s[:m.start()]
    if s.lower().startswith("k_"):
        s = s[2:]
    cs = _W2C_SAMPLER.get(s.lower()) or (s.lower() if s.lower() in _C2W_SAMPLER else None)
    if cs is None:
        if strict:
            return None, None
        cs = "euler"
    if not sched or sched.lower() == "automatic":
        csch = _AUTO_SCHED.get(cs, "normal")
    else:
        csch = _W2C_SCHED.get(sched.lower()) or (sched.lower() if sched.lower() in _C2W_SCHED else "normal")
    return cs, csch


# ============================================================
# LoRA 名字
# ============================================================

_MODEL_EXT = re.compile(r"\.(safetensors|ckpt|pt|pth|bin|gguf|sft)$", re.I)
_LORA_TAG_RE = re.compile(r"<(lora|lyco|hypernet):([^:>]+)(?::([^:>]*))?(?::([^:>]*))?>", re.I)


def lora_stem(name):
    """「Anima\\画风\\xxx.safetensors」→「xxx」：WebUI 的 <lora:> 只认文件名，带文件夹认不出"""
    n = str(name or "").replace("\\", "/").strip().rstrip("/")
    n = n.rsplit("/", 1)[-1]
    return _MODEL_EXT.sub("", n)


def _num(v, default=1.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return int(f) if f.is_integer() else round(f, 4)


def lora_weight(l):
    for k in ("weight", "strength_model", "strength"):
        if isinstance(l, dict) and l.get(k) is not None:
            return _num(l[k])
    return 1


def lora_tag(name, weight=1):
    return f"<lora:{lora_stem(name)}:{_num(weight)}>"


def lora_tags(loras):
    """一组 LoRA → 「<lora:a:0.8>, <lora:b:1>」（同名只出一次）"""
    out, seen = [], set()
    for l in loras or []:
        name = l.get("name") if isinstance(l, dict) else l
        st = lora_stem(name)
        if not st or st.lower() in seen:
            continue
        seen.add(st.lower())
        out.append(lora_tag(st, lora_weight(l) if isinstance(l, dict) else 1))
    return ", ".join(out)


def strip_lora_tags(text):
    """把 <lora:..> <lyco:..> <hypernet:..> 从提示词里拿掉（ComfyUI 不认，留着就是一段普通文字）"""
    t = _LORA_TAG_RE.sub("", text or "")
    t = re.sub(r"[ \t]*,[ \t]*(,[ \t]*)+", ", ", t)      # 拿掉后剩下的「, ,」
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"^[\s,]+|[\s,]+$", "", t)
    return re.sub(r"\n[ \t]*,[ \t]*", "\n", t)


def prompt_lora_tags(text):
    """提示词里手写的 LoRA：[{"name","weight","clip"}]"""
    out = []
    for kind, name, w, cw in _LORA_TAG_RE.findall(text or ""):
        if kind.lower() == "hypernet":
            continue
        out.append({"name": name.strip(), "weight": _num(w) if w else 1,
                    "clip": _num(cw) if cw else None})
    return out


# ============================================================
# 任意来源 → WebUI infotext
# ============================================================

def _is_webui(meta):
    return str(getattr(meta, "source_type", "") or "").startswith("WebUI")


def _other(params):
    """WebUI 参数里的杂项键（结构化分组里的 other / core_extras / hires）"""
    op = getattr(params, "other_params", None) or {}
    out = {}
    for g in ("other", "core_extras"):
        if isinstance(op.get(g), dict):
            out.update(op[g])
    return out


def _comfy_nodes(meta):
    api = getattr(meta, "raw_prompt_json", None)
    if isinstance(api, dict):
        return [n for n in api.values() if isinstance(n, dict) and n.get("class_type")]
    return []


def _hires_from_comfy(meta, base_size):
    """ComfyUI 图的第二个采样器（parser 记在 refiners 里）当成高清修复"""
    refs = getattr(meta.params, "refiners", None) or []
    if not refs:
        return None
    r = refs[0] if isinstance(refs[0], dict) else {}
    h = {"steps": r.get("steps"), "denoise": r.get("denoise")}
    # 放大倍率：工作流里的放大节点 → 成品尺寸 / 第一次采样尺寸
    scale, latent = None, False
    for n in _comfy_nodes(meta):
        ct = str(n.get("class_type"))
        inp = n.get("inputs") or {}
        if "upscale" in ct.lower() or "scale" in ct.lower():
            if ct.lower().startswith("latentupscale") or ct in ("NNLatentUpscale", "LatentUpscale"):
                latent = True
            for k in ("scale_by", "upscale_by", "scale"):
                if isinstance(inp.get(k), (int, float)) and 1 < float(inp[k]) <= 8:
                    scale = float(inp[k])
    if scale is None and base_size and meta.width and base_size[0]:
        s = meta.width / base_size[0]
        if s > 1.01:
            scale = round(s, 2)
    if scale is None and not h["steps"]:
        return None
    h["upscale"] = scale or 1.5
    h["upscaler"] = "Latent" if latent else None
    return h


def _parse_size(size):
    m = re.match(r"\s*(\d+)\s*[x×*]\s*(\d+)", str(size or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def to_infotext(meta):
    """
    返回 {"text": infotext, "notes": [给用户看的说明]}。
    WebUI 的图：原文（一个字都不改，插件参数也都在）；别的来源：重新拼。
    """
    notes = []
    if _is_webui(meta) and (meta.raw_text or "").strip():
        return {"text": meta.raw_text.strip(), "notes": notes, "mode": "raw"}
    if WebUIParser is None:
        raise RuntimeError("meta_engine 不可用")

    src = getattr(meta, "source_type", "") or "未知来源"
    p = meta.params
    prompt = (meta.positive_prompt or "").strip()
    neg = (meta.negative_prompt or "").strip()
    # ComfyUI 的 embedding:名字 → WebUI 直接写名字
    prompt = re.sub(r"embedding:([\w.\-]+?)(?:\.(?:pt|safetensors|bin))?\b", r"\1", prompt)
    neg = re.sub(r"embedding:([\w.\-]+?)(?:\.(?:pt|safetensors|bin))?\b", r"\1", neg)

    chars = [c for c in (getattr(meta, "character_prompts", None) or []) if (c.get("prompt") or "").strip()]
    if chars:
        prompt = ", ".join([prompt] + [c["prompt"].strip() for c in chars] if prompt else [c["prompt"].strip() for c in chars])
        notes.append(f"NovelAI 的 {len(chars)} 个角色提示词按顺序接在了正向提示词后面（WebUI 没有分角色的插槽）")

    # LoRA：工作流里 LoraLoader 加载的写成 <lora:名字:权重>，提示词里已经有的不重复
    have = {lora_stem(t["name"]).lower() for t in prompt_lora_tags(prompt)}
    add = [l for l in (p.loras or []) if isinstance(l, dict) and lora_stem(l.get("name")).lower() not in have]
    tags = lora_tags(add)
    if tags:
        prompt = f"{prompt}, {tags}" if prompt else tags

    ws, wsc = webui_sampler(p.sampler_name, p.scheduler)
    if p.sampler_name and not ws:
        notes.append(f"采样器「{p.sampler_name}」WebUI 里没有对应的，保留 WebUI 当前的采样器")
    if p.scheduler and not wsc and str(p.scheduler).lower() not in ("automatic", ""):
        notes.append(f"调度器「{p.scheduler}」WebUI 里没有对应的，保留 WebUI 当前的调度器")

    size = p.size or (f"{meta.width}x{meta.height}" if meta.width and meta.height else None)
    base = _parse_size(size)
    np_ = SamplerParameters(
        steps=p.steps, sampler_name=ws, scheduler=wsc, cfg_scale=p.cfg_scale, seed=p.seed,
        size=size, model_name=lora_stem(p.model_name) if p.model_name else None,
        clip_skip=p.clip_skip if (p.clip_skip or 0) > 1 else None, loras=[], other_params={},
    )
    hires = _hires_from_comfy(meta, base) if src == "ComfyUI" else None
    if hires:
        hd = {"upscale": _num(hires["upscale"])}
        if hires.get("upscaler"):
            hd["upscaler"] = hires["upscaler"]
        if hires.get("steps"):
            hd["steps"] = int(hires["steps"])
        np_.other_params["hires"] = hd
        if hires.get("denoise") is not None:
            np_.denoising_strength = _num(hires["denoise"])
        notes.append(f"工作流里的第二次采样当成了高清修复（放大 {hd['upscale']} 倍"
                     + (f"、重绘幅度 {np_.denoising_strength}" if np_.denoising_strength is not None else "") + "）")
    elif not _is_webui(meta) and p.denoising_strength is not None and p.denoising_strength < 0.999:
        np_.denoising_strength = p.denoising_strength

    skipped = [g for g in ("facedetailer", "adetailer_nodes", "controlnet_nodes", "ipadapter", "inpaint")
               if (p.other_params or {}).get(g)]
    if skipped:
        names = {"facedetailer": "FaceDetailer 修脸", "adetailer_nodes": "ADetailer 节点",
                 "controlnet_nodes": "ControlNet", "ipadapter": "IPAdapter", "inpaint": "局部重绘"}
        notes.append("这些 ComfyUI 节点的设置 WebUI 套不上，需要自己在插件里重新开："
                     + "、".join(names[g] for g in skipped))
    text = WebUIParser.build_text(prompt, neg, np_)
    return {"text": text, "notes": notes, "mode": "built", "source": src}


# ============================================================
# 任意来源 → ComfyUI
# ============================================================

def _norm(s):
    return str(s or "").replace("\\", "/").strip().lower()


def match_file(name, options):
    """按 ComfyUI 下拉框里的写法找文件：完整相对路径 → 文件名 → 去后缀的文件名。找不到返回 None"""
    if not name or not options:
        return None
    n = _norm(name)
    by_full = {_norm(o): o for o in options}
    if n in by_full:
        return by_full[n]
    base = n.rsplit("/", 1)[-1]
    stem = _MODEL_EXT.sub("", base)
    for o in options:
        ob = _norm(o).rsplit("/", 1)[-1]
        if ob == base:
            return o
    for o in options:
        ob = _MODEL_EXT.sub("", _norm(o).rsplit("/", 1)[-1])
        if ob == stem:
            return o
    return None


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# WebUI 的放大器名字 → ComfyUI upscale_models 里常见的文件名（去掉符号后比）
_UPSCALER_ALIASES = {
    "resrgan4xanime6b": ["realesrganx4plusanime6b", "realesrgan4xplusanime6b"],
    "resrgan4x": ["realesrganx4plus", "realesrgan4xplus"],
    "4xultrasharp": ["4xultrasharp"],
    "4xanimesharp": ["4xanimesharp"],
    "4xnmkdsiaxcx200k": ["4xnmkdsiax200k", "4xnmkdsiaxcx200k"],
}


def match_upscaler(name, options):
    if not name or not options:
        return None
    q = _squash(name)
    wants = _UPSCALER_ALIASES.get(q, []) + [q]
    for w in wants:
        for o in options:
            if _squash(_MODEL_EXT.sub("", lora_stem(o))) == w:
                return o
    for w in wants:
        for o in options:
            so = _squash(lora_stem(o))
            if w and (w in so or so in w) and len(so) > 4:
                return o
    return None


def _upscaler_factor(name):
    m = re.search(r"(?:^|[^0-9])([248])x|x([248])(?:[^0-9]|$)", str(name or "").lower())
    return int(m.group(1) or m.group(2)) if m else 4


# 分开加载（UNETLoader + CLIPLoader + VAELoader）的模型：CLIPLoader 的 type 填什么
# 对照官方 / 社区工作流；没把握的不写，用 stable_diffusion 兜底并在说明里提醒
_FAMILY_CLIP = {
    "anima": "stable_diffusion", "netayume": "lumina2", "zimage": "lumina2",
    "qwen_image": "qwen_image", "qwen_image_edit": "qwen_image", "chroma": "chroma",
    "flux2_klein": "flux2", "flux2_dev": "flux2", "flux1": "flux", "flux1_kontext": "flux",
}
_FAMILY_DUAL = {"flux1", "flux1_kontext"}
_FAMILY_HINTS = [("anima", "anima"), ("netayume", "netayume"), ("lumina", "netayume"),
                 ("z_image", "zimage"), ("zimage", "zimage"), ("chroma", "chroma"),
                 ("qwen_image_edit", "qwen_image_edit"), ("qwen-image-edit", "qwen_image_edit"),
                 ("qwen_image", "qwen_image"), ("qwen-image", "qwen_image"),
                 ("klein", "flux2_klein"), ("flux2", "flux2_dev"), ("flux-2", "flux2_dev"),
                 ("kontext", "flux1_kontext"), ("flux", "flux1")]


def _catalog_family(model_name, catalog):
    """主模型文件名 → 模型目录里的哪一家，以及那一家默认的文本编码器 / VAE 文件名"""
    stem = _MODEL_EXT.sub("", lora_stem(model_name)).lower()
    fam = None
    for m in (catalog or {}).get("models") or []:
        for v in m.get("versions") or []:
            for c in v.get("components") or []:
                if c.get("role") != "DiffusionModel":
                    continue
                for o in c.get("options") or []:
                    if _MODEL_EXT.sub("", str(o.get("file") or "")).lower() == stem:
                        fam = m
                        break
    fid = fam.get("id") if fam else None
    if not fid:
        for key, f in _FAMILY_HINTS:
            if key in stem:
                fid = f
                break
        if fid:
            fam = next((m for m in (catalog or {}).get("models") or [] if m.get("id") == fid), None)
    te, vae = [], []
    if fam:
        for v in fam.get("versions") or []:
            for c in v.get("components") or []:
                files = [o.get("file") for o in c.get("options") or [] if o.get("file")]
                if c.get("role") == "TextEncoder" and c.get("required", True):
                    te.append(files)
                elif c.get("role") == "VAE" and c.get("required", True) and not vae:
                    vae.append(files)
            break                       # 只看默认（第一个）版本的组件
    return fid, te, (vae[0] if vae else [])


def _pick(cands, options):
    """候选文件名里挑一个 ComfyUI 能看到的；都没有就给第一个（网页会标成缺模型，诊断里能一键下）"""
    for c in cands or []:
        hit = match_file(c, options)
        if hit:
            return hit, True
    return ((cands or [None])[0]), False


class _Graph:
    """拼 ComfyUI 网页格式（UI 格式，带位置和连线）的工作流"""

    SPEC = {
        # type: (输入 [(名字, 类型)], 输出 [(名字, 类型)], 大小)
        "CheckpointLoaderSimple": ([], [("MODEL", "MODEL"), ("CLIP", "CLIP"), ("VAE", "VAE")], (315, 98)),
        "UNETLoader": ([], [("MODEL", "MODEL")], (315, 82)),
        "CLIPLoader": ([], [("CLIP", "CLIP")], (315, 106)),
        "DualCLIPLoader": ([], [("CLIP", "CLIP")], (315, 130)),
        "VAELoader": ([], [("VAE", "VAE")], (315, 58)),
        "LoraLoader": ([("model", "MODEL"), ("clip", "CLIP")], [("MODEL", "MODEL"), ("CLIP", "CLIP")], (315, 126)),
        "CLIPSetLastLayer": ([("clip", "CLIP")], [("CLIP", "CLIP")], (315, 58)),
        "CLIPTextEncode": ([("clip", "CLIP")], [("CONDITIONING", "CONDITIONING")], (420, 180)),
        "FluxGuidance": ([("conditioning", "CONDITIONING")], [("CONDITIONING", "CONDITIONING")], (317, 58)),
        "EmptyLatentImage": ([], [("LATENT", "LATENT")], (315, 106)),
        "KSampler": ([("model", "MODEL"), ("positive", "CONDITIONING"), ("negative", "CONDITIONING"),
                      ("latent_image", "LATENT")], [("LATENT", "LATENT")], (315, 262)),
        "VAEDecode": ([("samples", "LATENT"), ("vae", "VAE")], [("IMAGE", "IMAGE")], (210, 46)),
        "VAEEncode": ([("pixels", "IMAGE"), ("vae", "VAE")], [("LATENT", "LATENT")], (210, 46)),
        "LatentUpscaleBy": ([("samples", "LATENT")], [("LATENT", "LATENT")], (315, 82)),
        "UpscaleModelLoader": ([], [("UPSCALE_MODEL", "UPSCALE_MODEL")], (315, 58)),
        "ImageUpscaleWithModel": ([("upscale_model", "UPSCALE_MODEL"), ("image", "IMAGE")], [("IMAGE", "IMAGE")], (241, 46)),
        "ImageScaleBy": ([("image", "IMAGE")], [("IMAGE", "IMAGE")], (315, 82)),
        "SaveImage": ([("images", "IMAGE")], [], (420, 470)),
        "Note": ([], [], (400, 160)),
    }

    def __init__(self):
        self.nodes, self.links = [], []

    def add(self, type_, pos, widgets=(), title=None):
        ins, outs, size = self.SPEC[type_]
        node = {
            "id": len(self.nodes) + 1, "type": type_, "pos": [int(pos[0]), int(pos[1])], "size": list(size),
            "flags": {}, "order": len(self.nodes), "mode": 0,
            "inputs": [{"name": n, "type": t, "link": None} for n, t in ins],
            "outputs": [{"name": n, "type": t, "links": [], "slot_index": i} for i, (n, t) in enumerate(outs)],
            "properties": {"Node name for S&R": type_}, "widgets_values": list(widgets),
        }
        if title:
            node["title"] = title
        if type_ == "Note":
            node["properties"] = {"text": ""}
            node["color"], node["bgcolor"] = "#432", "#653"
        self.nodes.append(node)
        return node["id"]

    def link(self, src, slot, dst, input_name):
        s, d = self.nodes[src - 1], self.nodes[dst - 1]
        di = next(i for i, x in enumerate(d["inputs"]) if x["name"] == input_name)
        lid = len(self.links) + 1
        typ = s["outputs"][slot]["type"]
        self.links.append([lid, src, slot, dst, di, typ])
        s["outputs"][slot]["links"].append(lid)
        d["inputs"][di]["link"] = lid

    def dump(self):
        return {"last_node_id": len(self.nodes), "last_link_id": len(self.links), "nodes": self.nodes,
                "links": self.links, "groups": [], "config": {},
                "extra": {"ds": {"scale": 0.75, "offset": [40, 40]}, "wwy_launcher": True}, "version": 0.4}


def _embeddingize(text, names):
    """WebUI 提示词里直接写的 embedding 名 → ComfyUI 的 embedding:名字"""
    if not text or not names:
        return text
    for n in sorted({lora_stem(x) for x in names if x}, key=len, reverse=True):
        if len(n) < 3:
            continue
        text = re.sub(r"(?<![\w:./\\-])" + re.escape(n) + r"(?![\w.-])", "embedding:" + n, text)
    return text


def build_workflow(meta, lists=None, catalog=None):
    """
    按参数生成一个默认的 ComfyUI 工作流（UI 格式）。返回 {"workflow", "notes", "missing"}。
    lists：comfy_lists() 拿到的 ComfyUI 当前能看到的模型清单（拿不到给 None，文件名就原样填）
    """
    lists = lists or {}
    notes, missing = [], []
    p = meta.params
    other = _other(p)

    raw_pos = meta.positive_prompt or ""
    raw_neg = meta.negative_prompt or ""
    loras = []
    seen = set()
    for l in prompt_lora_tags(raw_pos) + prompt_lora_tags(raw_neg) + list(p.loras or []):
        if not isinstance(l, dict) or not l.get("name"):
            continue
        st = lora_stem(l["name"]).lower()
        if st in seen:
            continue
        seen.add(st)
        w = lora_weight(l)
        cw = l.get("clip") if l.get("clip") is not None else l.get("strength_clip")
        loras.append({"name": l["name"], "w": w, "cw": _num(cw) if cw is not None else w})
    pos, neg = strip_lora_tags(raw_pos), strip_lora_tags(raw_neg)
    chars = [c for c in (getattr(meta, "character_prompts", None) or []) if (c.get("prompt") or "").strip()]
    if chars:
        pos = ", ".join([x for x in [pos] + [c["prompt"].strip() for c in chars] if x])
        notes.append(f"NovelAI 的 {len(chars)} 个角色提示词接在了正向提示词后面")
    if re.search(r"\bBREAK\b", pos + neg):
        pos, neg = re.sub(r"\s*\bBREAK\b\s*", ", ", pos), re.sub(r"\s*\bBREAK\b\s*", ", ", neg)
        notes.append("提示词里的 BREAK 换成了逗号（ComfyUI 原生不认 BREAK）")
    if re.search(r"\[[^\]]*:[^\]]*\]", pos + neg) or re.search(r"(?<!\\)\[", pos + neg):
        notes.append("提示词里有 [ ] 写法（WebUI 的减权 / 提示词切换），ComfyUI 原生不认，会被当成普通文字")
    if lists.get("embeddings"):
        pos, neg = _embeddingize(pos, lists["embeddings"]), _embeddingize(neg, lists["embeddings"])

    steps = int(p.steps or 20)
    cfg = float(p.cfg_scale) if p.cfg_scale is not None else 7.0
    random_seed = p.seed is None or int(p.seed) < 0
    seed = 0 if random_seed else int(p.seed)
    seed_mode = "randomize" if random_seed else "fixed"
    sampler, scheduler = comfy_sampler(p.sampler_name, p.scheduler)
    if p.sampler_name and comfy_sampler(p.sampler_name, p.scheduler, strict=True)[0] is None:
        notes.append(f"采样器「{p.sampler_name}」没有对应的，用了 euler")
    size = _parse_size(p.size) or ((meta.width, meta.height) if meta.width and meta.height else (1024, 1024))

    # ---- 主模型：ComfyUI 下拉框里找；checkpoints 找不到再去 diffusion_models 找 ----
    model = p.model_name or ""
    fam, te_cands, vae_cands = _catalog_family(model, catalog)
    ckpt = match_file(model, lists.get("checkpoints"))
    unet = None if ckpt else match_file(model, lists.get("unet"))
    if not ckpt and not unet:
        if model:
            missing.append(model)
        if fam and (fam in _FAMILY_CLIP or te_cands):
            unet = (lora_stem(model) + ".safetensors") if model else "model.safetensors"
        else:
            ckpt = (lora_stem(model) + ".safetensors") if model else (
                (lists.get("checkpoints") or ["model.safetensors"])[0])
        if model:
            notes.append(f"ComfyUI 里没找到模型「{model}」，节点里先填了这个名字：下好模型后按 R 刷新，或在节点里换一个")

    # WebUI 里额外选的 VAE / 文本编码器（Forge 的 Module 1/2/…、老的 VAE 键）
    modules = [v for k, v in sorted(other.items()) if re.fullmatch(r"Module \d+", str(k)) and v]
    want_vae = other.get("VAE") or ""
    mod_vae = [m for m in modules if match_file(m, lists.get("vae"))]
    mod_te = [m for m in modules if match_file(m, lists.get("clip"))]

    g = _Graph()
    X0, X1, X2, X3, X4 = 0, 380, 760, 1220, 1600
    if unet:
        mdl = g.add("UNETLoader", (X0, 0), [unet, "default"])
        model_out, clip_src, vae_src = (mdl, 0), None, None
        te_pick = []
        for i, cands in enumerate(te_cands or [[]]):
            pref = [mod_te[i]] if i < len(mod_te) else []
            got, ok = _pick(pref + list(cands), lists.get("clip"))
            if got:
                te_pick.append(got)
                if not ok:
                    missing.append(got)
        if not te_pick and mod_te:
            te_pick = mod_te[:1]
        ctype = _FAMILY_CLIP.get(fam or "")
        if lists.get("clip_types") and ctype and ctype not in lists["clip_types"]:
            notes.append(f"这个 ComfyUI 的 CLIPLoader 没有「{ctype}」类型，可能需要更新 ComfyUI")
        if fam in _FAMILY_DUAL:
            te_pick = (te_pick + ["clip_l.safetensors", "t5xxl_fp16.safetensors"])[:2]
            clip_n = g.add("DualCLIPLoader", (X0, 130), [te_pick[0], te_pick[1], "flux", "default"])
        else:
            if not ctype:
                notes.append("没认出这个模型属于哪一家，文本编码器的类型先填了 stable_diffusion，跑不起来请在 CLIPLoader 里换")
            clip_n = g.add("CLIPLoader", (X0, 130), [(te_pick or ["text_encoder.safetensors"])[0],
                                                     ctype or "stable_diffusion", "default"])
            if not te_pick:
                notes.append("不知道该用哪个文本编码器，CLIPLoader 里请自己选一个")
        clip_src = (clip_n, 0)
        vae_name, ok = _pick(([want_vae] if want_vae else []) + mod_vae + list(vae_cands), lists.get("vae"))
        if vae_name and not ok:
            missing.append(vae_name)
        vae_n = g.add("VAELoader", (X0, 290), [vae_name or "ae.safetensors"])
        vae_src = (vae_n, 0)
    else:
        mdl = g.add("CheckpointLoaderSimple", (X0, 0), [ckpt])
        model_out, clip_src, vae_src = (mdl, 0), (mdl, 1), (mdl, 2)
        vae_hit = match_file(want_vae, lists.get("vae")) if want_vae and want_vae.lower() not in ("automatic", "none") else None
        if vae_hit:
            vae_n = g.add("VAELoader", (X0, 160), [vae_hit])
            vae_src = (vae_n, 0)

    # ---- LoRA 串 ----
    y = 0
    lost = []
    for l in loras:
        hit = match_file(l["name"], lists.get("loras"))
        if not hit:
            missing.append(l["name"])
            lost.append(lora_stem(l["name"]))
            hit = l["name"] if re.search(r"\.\w+$", l["name"]) else l["name"] + ".safetensors"
        n = g.add("LoraLoader", (X1, y), [hit, l["w"], l["cw"]])
        g.link(model_out[0], model_out[1], n, "model")
        g.link(clip_src[0], clip_src[1], n, "clip")
        model_out, clip_src = (n, 0), (n, 1)
        y += 160
    if lost:
        notes.append("ComfyUI 里没找到这些 LoRA（节点里先填了原名，下好后按 R 刷新）：" + "、".join(lost))
    if (p.clip_skip or 0) > 1 and not unet:
        cs = g.add("CLIPSetLastLayer", (X1, y), [-int(p.clip_skip)])
        g.link(clip_src[0], clip_src[1], cs, "clip")
        clip_src = (cs, 0)

    pos_n = g.add("CLIPTextEncode", (X2, 0), [pos], title="正向提示词")
    neg_n = g.add("CLIPTextEncode", (X2, 230), [neg], title="负向提示词")
    for n in (pos_n, neg_n):
        g.link(clip_src[0], clip_src[1], n, "clip")
    pos_out = (pos_n, 0)
    if fam in ("flux1", "flux1_kontext"):
        dcfg = other.get("Distilled CFG Scale")
        fg = g.add("FluxGuidance", (X2, 460), [_num(dcfg, 3.5) if dcfg else 3.5])
        g.link(pos_n, 0, fg, "conditioning")
        pos_out = (fg, 0)
        if cfg > 1.5 and not dcfg:
            notes.append("FLUX 一般 CFG 用 1、靠 FluxGuidance 控制，跑出来不对就把 KSampler 的 cfg 改成 1")

    lat = g.add("EmptyLatentImage", (X2, 560), [size[0], size[1], 1])
    ks = g.add("KSampler", (X3, 0), [seed, seed_mode, steps, cfg, sampler, scheduler, 1.0])
    g.link(model_out[0], model_out[1], ks, "model")
    g.link(pos_out[0], pos_out[1], ks, "positive")
    g.link(neg_n, 0, ks, "negative")
    g.link(lat, 0, ks, "latent_image")
    last = (ks, 0)

    # ---- 高清修复 ----
    hires = ((p.other_params or {}).get("hires") or {})
    try:
        hscale = float(hires.get("upscale") or 0)
    except (TypeError, ValueError):
        hscale = 0
    if hscale > 1.0:
        upn = str(hires.get("upscaler") or "")
        hsteps = int(hires.get("steps") or 0) or steps
        hden = p.denoising_strength if p.denoising_strength is not None else hires.get("denoise")
        hden = float(hden) if hden is not None else 0.5
        hcfg = other.get("Hires CFG Scale") or hires.get("Hires CFG Scale")
        hcfg = float(hcfg) if hcfg else cfg
        model_up = None if (not upn or "latent" in upn.lower()) else match_upscaler(upn, lists.get("upscale"))
        if model_up:
            dec1 = g.add("VAEDecode", (X3, 300))
            g.link(ks, 0, dec1, "samples")
            g.link(vae_src[0], vae_src[1], dec1, "vae")
            uml = g.add("UpscaleModelLoader", (X3, 380), [model_up])
            up = g.add("ImageUpscaleWithModel", (X3, 470))
            g.link(uml, 0, up, "upscale_model")
            g.link(dec1, 0, up, "image")
            sb = g.add("ImageScaleBy", (X3, 550), ["lanczos", round(hscale / _upscaler_factor(model_up), 4)])
            g.link(up, 0, sb, "image")
            enc = g.add("VAEEncode", (X3, 660))
            g.link(sb, 0, enc, "pixels")
            g.link(vae_src[0], vae_src[1], enc, "vae")
            lat2 = (enc, 0)
        else:
            method = "nearest-exact"
            for key, m in (("bicubic", "bicubic"), ("bilinear", "bilinear"), ("area", "area"),
                           ("antialiased", "bilinear")):
                if key in upn.lower():
                    method = m
            lu = g.add("LatentUpscaleBy", (X3, 300), [method, hscale])
            g.link(ks, 0, lu, "samples")
            lat2 = (lu, 0)
            if upn and "latent" not in upn.lower():
                notes.append(f"ComfyUI 的 upscale_models 里没找到放大器「{upn}」，高清修复改用了 Latent 放大"
                             "（重绘幅度低于 0.5 时可能出噪点，可以调高一点）")
        ks2 = g.add("KSampler", (X4, 0), [seed, "fixed" if not random_seed else "randomize", hsteps, hcfg, sampler, scheduler, round(hden, 3)],
                    title="高清修复")
        g.link(model_out[0], model_out[1], ks2, "model")
        g.link(pos_out[0], pos_out[1], ks2, "positive")
        g.link(neg_n, 0, ks2, "negative")
        g.link(lat2[0], lat2[1], ks2, "latent_image")
        last = (ks2, 0)
        xd = X4 + 360
    else:
        xd = X4

    dec = g.add("VAEDecode", (xd, 0))
    g.link(last[0], last[1], dec, "samples")
    g.link(vae_src[0], vae_src[1], dec, "vae")
    save = g.add("SaveImage", (xd, 90), ["WWY"])
    g.link(dec, 0, save, "images")

    op = p.other_params or {}
    plug = []
    if op.get("adetailer"):
        plug.append("ADetailer 修脸（ComfyUI 里对应 Impact Pack 的 FaceDetailer）")
    if op.get("controlnet"):
        plug.append("ControlNet")
    if op.get("multidiffusion") or op.get("spectrum") or op.get("dynamic_threshold") or op.get("freeu"):
        plug.append("其他 WebUI 插件参数（MultiDiffusion / 动态阈值 / FreeU 等）")
    if plug:
        notes.append("WebUI 插件的设置没有转换：" + "、".join(plug) + "，需要的话自己加节点")

    if notes:
        g.add("Note", (X0, 420), ["由 WWY 启动器按图片参数生成\n\n" + "\n".join("· " + n for n in notes)],
              title="转换说明")
    return {"workflow": g.dump(), "notes": notes, "missing": list(dict.fromkeys(missing))}


def comfy_payload(meta, lists=None, catalog=None):
    """
    交给 ComfyUI 网页的东西：{"workflow": UI 格式} 或 {"api": API 格式}，外加 notes / mode。
    ComfyUI 出的图：用图里存的原工作流（一模一样）；只存了 API 格式的用 API 格式；别的按参数生成
    """
    wf = getattr(meta, "raw_workflow_json", None)
    if isinstance(wf, dict) and (wf.get("nodes") or wf.get("definitions")):
        return {"workflow": wf, "notes": [], "mode": "original", "missing": []}
    api = getattr(meta, "raw_prompt_json", None)
    if isinstance(api, dict) and any(isinstance(v, dict) and v.get("class_type") for v in api.values()):
        return {"api": api, "notes": ["图里只存了 API 格式的工作流，节点会按连线自动排版"], "mode": "api", "missing": []}
    has = (meta.positive_prompt or meta.negative_prompt or meta.params.model_name
           or meta.params.steps is not None)
    if not has:
        raise ValueError("这张图里没有生成参数，没法生成工作流")
    r = build_workflow(meta, lists, catalog)
    r["mode"] = "built"
    return r


# ============================================================
# 送达：本机实例的桥接插件
# ============================================================

def _session():
    import requests
    s = requests.Session()
    s.trust_env = False          # 本机地址，别走系统代理
    return s


def _combo(spec, key):
    """/object_info 里某个输入的下拉选项（新老两种写法）"""
    for sect in ("required", "optional"):
        item = ((spec or {}).get("input") or {}).get(sect, {}).get(key)
        if not item:
            continue
        if isinstance(item[0], list):
            return [str(x) for x in item[0]]
        if item[0] == "COMBO" and len(item) > 1 and isinstance(item[1], dict):
            return [str(x) for x in item[1].get("options") or []]
    return []


_LIST_QUERIES = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"), "unet": ("UNETLoader", "unet_name"),
    "loras": ("LoraLoader", "lora_name"), "vae": ("VAELoader", "vae_name"),
    "clip": ("CLIPLoader", "clip_name"), "clip_types": ("CLIPLoader", "type"),
    "upscale": ("UpscaleModelLoader", "model_name"),
}


def comfy_lists(base, session=None, timeout=8):
    """问 ComfyUI 它下拉框里有哪些模型（含共享模型库 / extra_model_paths 里的）"""
    s = session or _session()
    out, cache = {}, {}
    for key, (cls, inp) in _LIST_QUERIES.items():
        if cls not in cache:
            try:
                r = s.get(f"{base}/object_info/{cls}", timeout=timeout)
                cache[cls] = (r.json() or {}).get(cls) if r.status_code == 200 else None
            except Exception:
                cache[cls] = None
        out[key] = _combo(cache[cls], inp) if cache[cls] else []
    try:
        r = s.get(f"{base}/embeddings", timeout=timeout)
        out["embeddings"] = [str(x) for x in (r.json() or [])] if r.status_code == 200 else []
    except Exception:
        out["embeddings"] = []
    return out


class BridgeError(Exception):
    def __init__(self, msg, code=""):
        super().__init__(msg)
        self.code = code


def _post(base, path, body, session, timeout=8):
    import requests
    try:
        r = session.post(base + path, json=body, timeout=timeout)
    except requests.RequestException:
        raise BridgeError("连不上", "unreachable")
    if r.status_code == 404:
        raise BridgeError("桥接插件没装或是旧版", "old_plugin")
    try:
        return r.json()
    except ValueError:
        raise BridgeError(f"返回的不是 JSON（HTTP {r.status_code}）", "bad_reply")


def post_comfy(base, item, session=None):
    """item: {"id", "name", "workflow"|"api"}。返回插件的回复 {"ok","clients","queued"}"""
    return _post(base, "/wwy_bridge/load", item, session or _session(), timeout=15)


def post_webui(base, item, session=None):
    """item: {"id", "infotext", "tab"}。返回 {"ok","clients","queued"}"""
    return _post(base, "/wwy_bridge/send", item, session or _session())


def wait_ack(base, cid, wait=8.0, session=None, poll=0.4):
    """等网页回执：{"ok","text"}；超时返回 None"""
    s = session or _session()
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(poll)
        try:
            ack = s.get(f"{base}/wwy_bridge/ack/{cid}", timeout=5).json().get("ack")
        except Exception:
            ack = None
        if ack:
            return ack
    return None


def new_id():
    import uuid
    return uuid.uuid4().hex[:12]


def item_name(meta, fallback="WWY"):
    """ComfyUI 新标签页的名字：图片文件名"""
    n = os.path.splitext(os.path.basename(getattr(meta, "file_path", "") or ""))[0]
    return ("WWY-" + n) if n else fallback


__all__ = ["to_infotext", "comfy_payload", "build_workflow", "lora_stem", "lora_tag", "lora_tags",
           "webui_sampler", "comfy_sampler", "comfy_lists", "post_comfy", "post_webui", "wait_ack",
           "BridgeError", "match_file"]


def needs_lists(meta):
    """要不要先问 ComfyUI 的模型清单：图里自带工作流的直接用，不用问"""
    wf = getattr(meta, "raw_workflow_json", None)
    if isinstance(wf, dict) and (wf.get("nodes") or wf.get("definitions")):
        return False
    api = getattr(meta, "raw_prompt_json", None)
    return not (isinstance(api, dict) and any(isinstance(v, dict) and v.get("class_type") for v in api.values()))
