"""
h3_models.py — Forge Neo H3（MiniMax-H3 视频生成）模型一键下载

模型清单对应 sd-webui-forge-neo-h3 仓库 README 的「模型」表：
  主模型 FL2VA / Ref2VA ........ unsloth/MiniMax-H3-GGUF（GGUF 量化，可选档位）
  文本编码器 Qwen3-VL-32B ...... unsloth/MiniMax-H3-GGUF
  视频 VAE / 音频 VAE .......... unsloth/MiniMax-H3-GGUF 的 vae/（与 Comfy-Org 同一份文件）
  Turbo LoRA（8 步）............ lightx2v/Minimax-h3-Turbo（必须用 comfyui 格式）
  ControlNet Union 2.0 ......... Comfy-Org/MiniMax-H3 的 model_patches/

文件大小先用下面写死的数值（界面上马上能算总量，不用等联网）；真正下载前再查一次
HF 的 tree 接口拿 sha256（lfs.oid）和最新大小，查不到就只按大小判断、不做哈希校验。
下载本身复用 civitai_downloader.download_file：.part 断点续传、边下边算 sha256、
校验失败改名 .broken。
"""

import os

import requests

HF = "https://huggingface.co"

GGUF_REPO = "unsloth/MiniMax-H3-GGUF"
TURBO_REPO = "lightx2v/Minimax-h3-Turbo"
COMFY_REPO = "Comfy-Org/MiniMax-H3"

# 量化档位 → 文件大小（字节，2026-10 HF 上的实际值）
_FL2VA_SIZES = {
    "Q2_K": 6724190304, "Q3_K": 8759328864, "Q4_K": 11420663904,
    "Q5_0": 13923170400, "Q6_K": 16586784864, "Q8_0": 21437786208,
}
_REF2VA_SIZES = {
    "Q2_K": 6678171744, "Q3_K": 8716105824, "Q4_K": 11381096544,
    "Q5_0": 13889323104, "Q6_K": 16554313824, "Q8_0": 21414002784,
}
_TE_SIZES = {"Q4_K_M": 18218065024, "Q2_K_M": 13102161024}

_QUANT_NOTES = {
    "Q2_K": "最省内存，画质有损", "Q3_K": "", "Q4_K": "推荐",
    "Q5_0": "", "Q6_K": "", "Q8_0": "接近原版，很吃内存",
    "Q4_K_M": "推荐", "Q2_K_M": "省约 5GB 内存",
}

# 每一项：id、显示名、说明、仓库、仓库内路径（{q} 换成量化档位）、放到实例的哪个目录、
# 是否必需、默认勾选、量化档位表（None 表示只有一个文件）
CATALOG = [
    {"id": "fl2va", "label": "主模型 FL2VA", "desc": "文生视频 / 图生视频 / 首尾帧 / ControlNet",
     "repo": GGUF_REPO, "path": "minimax_h3_fl2va_pruned-{q}.gguf",
     "folder": "models/Stable-diffusion", "required": True, "default": True,
     "sizes": _FL2VA_SIZES, "quant_default": "Q4_K"},
    {"id": "te", "label": "文本编码器 Qwen3-VL-32B", "desc": "必需，所有模式都要",
     "repo": GGUF_REPO, "path": "qwen3vl_32b_minimax_h3-{q}.gguf",
     "folder": "models/text_encoder", "required": True, "default": True,
     "sizes": _TE_SIZES, "quant_default": "Q4_K_M"},
    {"id": "vae_video", "label": "视频 VAE", "desc": "必需",
     "repo": GGUF_REPO, "path": "vae/minimax_h3_video_vae_fp16.safetensors",
     "folder": "models/VAE", "required": True, "default": True, "size": 5207808496},
    {"id": "vae_audio", "label": "音频 VAE", "desc": "必需（H3 同时生成立体声音轨）",
     "repo": GGUF_REPO, "path": "vae/minimax_h3_audio_vae_fp32.safetensors",
     "folder": "models/VAE", "required": True, "default": True, "size": 605254808},
    {"id": "turbo_fl2v", "label": "Turbo LoRA · FL2V 8 步", "desc": "可选，强烈推荐：8 步出片，速度快好几倍",
     "repo": TURBO_REPO, "path": "minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors",
     "folder": "models/Lora", "required": False, "default": True, "size": 1956193000},
    {"id": "ref2va", "label": "主模型 Ref2VA", "desc": "可选：参考图 / 参考视频 / 参考音频生视频才需要",
     "repo": GGUF_REPO, "path": "minimax_h3_ref2va_pruned-{q}.gguf",
     "folder": "models/Stable-diffusion", "required": False, "default": False,
     "sizes": _REF2VA_SIZES, "quant_default": "Q4_K"},
    {"id": "turbo_ref2v", "label": "Turbo LoRA · Ref2V 8 步", "desc": "可选：配合 Ref2VA 用",
     "repo": TURBO_REPO, "path": "minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors",
     "folder": "models/Lora", "required": False, "default": False, "size": 1956193000},
    {"id": "controlnet", "label": "ControlNet Union 2.0（int8）", "desc": "可选：上传普通视频做姿态 / 深度 / 线稿控制、视频局部重绘",
     "repo": COMFY_REPO, "path": "model_patches/minimax_h3_fun_controlnet_union_2.0_pruned_int8_convrot.safetensors",
     "folder": "models/model_patches", "required": False, "default": False, "size": 4531220608},
]

CATALOG_BY_ID = {it["id"]: it for it in CATALOG}

# 跑 H3 的经验值（来自仓库 README：GGUF Q4 一套约 33GB 内存；12GB 显卡可运行）
MIN_VRAM_GB = 12
RAM_Q4_GB = 33


class H3ModelError(Exception):
    pass


def quant_options(item):
    sizes = item.get("sizes")
    if not sizes:
        return []
    return [{"q": q, "size": s, "note": _QUANT_NOTES.get(q, "")} for q, s in sizes.items()]


def resolve(item_id, quant=None):
    """(条目, 仓库内路径, 文件名, 预估大小)；量化档位不认识就用默认档"""
    it = CATALOG_BY_ID.get(item_id)
    if not it:
        raise H3ModelError(f"未知的模型项: {item_id}")
    sizes = it.get("sizes")
    if sizes:
        q = quant if quant in sizes else it["quant_default"]
        path = it["path"].format(q=q)
        size = sizes[q]
    else:
        path = it["path"]
        size = it["size"]
    return it, path, path.rsplit("/", 1)[-1], size


def all_filenames(item):
    """某一项所有档位的文件名（判断「已经下过别的档位」用）"""
    sizes = item.get("sizes")
    if not sizes:
        return [item["path"].rsplit("/", 1)[-1]]
    return [item["path"].format(q=q).rsplit("/", 1)[-1] for q in sizes]


def default_quants(ram_gb):
    """按内存给默认档位：内存不到 40GB 时 Q4 一套（约 35GB）基本放不下，默认降到 Q2"""
    if ram_gb and ram_gb < 40:
        return {"fl2va": "Q2_K", "ref2va": "Q2_K", "te": "Q2_K_M"}
    return {"fl2va": "Q4_K", "ref2va": "Q4_K", "te": "Q4_K_M"}


def base_order(use_mirror):
    """下载源顺序：国内网络先走 hf-mirror，失败再回源站；反之亦然"""
    from mirror_manager import HF_MIRROR
    return [HF_MIRROR, HF] if use_mirror else [HF, HF_MIRROR]


def fetch_tree(repo, bases, timeout=20):
    """
    HF tree 接口 → {仓库内路径: (大小, sha256 或 None)}。
    所有源都失败返回 None（调用方退回到写死的大小、跳过哈希校验）。
    """
    for base in bases:
        url = f"{base}/api/models/{repo}/tree/main?recursive=true"
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "ForgeLauncher/1.0"})
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            continue
        out = {}
        for ent in data if isinstance(data, list) else []:
            if ent.get("type") != "file":
                continue
            lfs = ent.get("lfs") or {}
            out[ent.get("path")] = (int(lfs.get("size") or ent.get("size") or 0), lfs.get("oid") or None)
        return out
    return None


def file_url(base, repo, path):
    return f"{base}/{repo}/resolve/main/{path}"


def existing_file(dirs, filename, size=None):
    """
    在若干候选目录里找同名文件 → (路径, 大小是否吻合)；没找到返回 (None, False)。
    size 为 None 时只要存在就算吻合。
    """
    for d in dirs:
        if not d:
            continue
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            try:
                got = os.path.getsize(p)
            except OSError:
                continue
            return p, (size is None or got == size)
    return None, False


def partial_size(dest_dir, filename):
    try:
        return os.path.getsize(os.path.join(dest_dir, filename) + ".part")
    except OSError:
        return 0
