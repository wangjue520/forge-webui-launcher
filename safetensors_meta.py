# -*- coding: utf-8 -*-
"""
safetensors 文件头读取 + 架构判断（纯标准库实现）。

safetensors 格式：文件开头 8 字节是小端 uint64，表示紧跟其后的 JSON 头长度。
JSON 头包含所有张量的名字/形状/偏移，外加可选的 "__metadata__" 字符串字典
（kohya 系训练器会把 ss_base_model_version、ss_tag_frequency 等训练信息写在
这里）。只读头部，不碰权重数据，几 GB 的大模型也是毫秒级完成。

架构判断思路：不同架构的张量键名差异非常大——
  - SDXL 系（Illustrious / NoobAI / Pony / Animagine 都是 SDXL 架构）：
    checkpoint 有 conditioner.embedders.1（双文本编码器）/ label_emb；
    LoRA 有 lora_te2_（第二个文本编码器的 LoRA）
  - SD1.5 系：cond_stage_model（单文本编码器）/ lora_unet_down_blocks
  - Flux 系：double_blocks / single_blocks
  - DiT/Transformer 类（Anima、Wan 等新架构）：blocks.N.attn / cross_attn
    这类键名，完全没有 UNet 的 input_blocks/down_blocks 结构

注意：同架构内部（例如 Illustrious vs NoobAI）从权重上是分不出来的，
需要靠 __metadata__ 里的训练底模字段，或者按文件哈希去 Civitai 查询。
"""
import json
import os
import struct

MAX_HEADER_BYTES = 100 * 1024 * 1024  # 头部超过这个大小肯定不是正常文件


def read_safetensors_header(path):
    """
    返回 (metadata_dict, tensor_key_list)；不是有效 safetensors 时返回 None。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(8)
            if len(raw) < 8:
                return None
            n = struct.unpack("<Q", raw)[0]
            if n <= 0 or n > MAX_HEADER_BYTES:
                return None
            header = json.loads(f.read(n).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(header, dict):
        return None
    meta = header.get("__metadata__") or {}
    if not isinstance(meta, dict):
        meta = {}
    keys = [k for k in header.keys() if k != "__metadata__"]
    return meta, keys


def guess_architecture(meta, keys):
    """
    返回 (kind, arch, note)：
      kind: "LoRA" / "完整模型/Checkpoint" / "未知"
      arch: 架构判断结果文本
      note: 补充说明（可能为空字符串）
    优先信任 __metadata__ 里的 modelspec.architecture（跨训练器的标准字段），
    没有再按键名启发式判断。
    """
    def has(sub):
        return any(sub in k for k in keys)

    is_lora = (
        has("lora_") or has(".lora_A.") or has(".lora_B.")
        or has(".lora_down.") or has(".lora_up.")
        or has(".hada_w1_")  # LyCORIS
    )
    kind = "LoRA" if is_lora else "完整模型/Checkpoint"
    note = ""

    # 1) 标准字段直接给出答案
    ms_arch = meta.get("modelspec.architecture", "")
    if ms_arch:
        return kind, f"{ms_arch}（来自 modelspec 标准元数据）", note

    # 2) 键名启发式
    if has("double_blocks") or has("single_blocks"):
        arch = "Flux 系"
    elif has("conditioner.embedders.1") or has("label_emb") or has("add_embedding") \
            or has("lora_te2_") or has("text_encoder_2"):
        arch = "SDXL 系"
        note = ("Illustrious / NoobAI / Pony / Animagine 都属于 SDXL 架构，"
                "权重键名上无法进一步区分是哪一家——请看训练元数据的底模字段，"
                "或用“按哈希查 Civitai”按钮查询")
    elif has("cond_stage_model"):
        arch = "SD1.5 系"
    elif has("lora_unet_input_blocks"):
        arch = "SDXL 系 UNet LoRA"
        note = "（未检测到文本编码器 LoRA 键，按 UNet 键名结构判断为 SDXL 系）"
    elif has("lora_unet_down_blocks") or has("down_blocks"):
        arch = "SD1.5 / diffusers UNet 系"
    elif has("cross_attn") or has("self_attn") or has("transformer_blocks") or has("blocks."):
        arch = "DiT/Transformer 类架构（非 SDXL）"
        note = ("Anima、Wan 等新架构模型属于这一类——跟 Illustrious(SDXL) 的"
                "键名结构完全不同，所以两者从文件本身就能区分开")
    else:
        arch = "未识别"
        note = "键名示例: " + ", ".join(keys[:3]) if keys else "（没有张量键）"

    return kind, arch, note


# kohya / sd-scripts 系训练元数据里最值得展示的字段
_SS_FIELDS = [
    ("ss_sd_model_name", "训练底模文件"),
    ("ss_base_model_version", "底模版本"),
    ("ss_output_name", "训练输出名"),
    ("ss_network_dim", "Network Dim"),
    ("ss_network_alpha", "Network Alpha"),
    ("ss_resolution", "训练分辨率"),
    ("ss_num_train_images", "训练图片数"),
    ("ss_num_epochs", "Epoch 数"),
    ("ss_training_comment", "训练备注/触发词"),
]


def summarize_training_meta(meta):
    """把 __metadata__ 里有价值的字段整理成 [(标签, 值), ...]"""
    rows = []
    for key, label in _SS_FIELDS:
        v = meta.get(key)
        if v not in (None, "", "None"):
            rows.append((label, str(v)))
    return rows


def top_trained_tags(meta, topn=12):
    """
    从 ss_tag_frequency（各数据集里每个标签出现次数的 JSON）汇总出
    训练时出现最多的标签——对判断一个来路不明的 LoRA 是练什么的非常直观。
    """
    tf = meta.get("ss_tag_frequency")
    if not tf:
        return []
    try:
        data = json.loads(tf)
    except (ValueError, TypeError):
        return []
    agg = {}
    if isinstance(data, dict):
        for dataset in data.values():
            if isinstance(dataset, dict):
                for tag, cnt in dataset.items():
                    try:
                        agg[tag.strip()] = agg.get(tag.strip(), 0) + int(cnt)
                    except (ValueError, TypeError):
                        continue
    return sorted(agg.items(), key=lambda x: -x[1])[:topn]


# ============================================================
# GGUF 文件头（H3 / Flux / Wan 的量化主模型、量化文本编码器都是这个格式）
# ============================================================
# 格式：'GGUF' + uint32 版本 + uint64 张量数 + uint64 键值对数，接着是键值对
# （键 = uint64 长度 + utf8；值 = uint32 类型 + 数据），再往后是张量信息
# （名字 + 维数 + 各维大小 + 类型 + 偏移）。只读到前几十个张量名就停，不碰权重。
# 只支持 v2/v3（v1 的计数是 uint32，早已没人用）。

_GGUF_SCALAR = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_GGUF_STRING, _GGUF_ARRAY = 8, 9
_GGUF_MAX_STR = 16 * 1024 * 1024


class _GGUFError(Exception):
    pass


def _gguf_read(f, n):
    b = f.read(n)
    if len(b) != n:
        raise _GGUFError("文件提前结束")
    return b


def _gguf_str(f):
    n = struct.unpack("<Q", _gguf_read(f, 8))[0]
    if n > _GGUF_MAX_STR:
        raise _GGUFError("字符串长度异常")
    return _gguf_read(f, n).decode("utf-8", "replace")


def _gguf_value(f, vtype, want):
    """读一个值；want=False 时只跳过（大数组用 seek，不进内存）"""
    if vtype in _GGUF_SCALAR:
        b = _gguf_read(f, _GGUF_SCALAR[vtype])
        if not want:
            return None
        fmt = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
               7: "<?", 10: "<Q", 11: "<q", 12: "<d"}[vtype]
        return struct.unpack(fmt, b)[0]
    if vtype == _GGUF_STRING:
        if want:
            return _gguf_str(f)
        n = struct.unpack("<Q", _gguf_read(f, 8))[0]
        if n > _GGUF_MAX_STR:
            raise _GGUFError("字符串长度异常")
        f.seek(n, 1)
        return None
    if vtype == _GGUF_ARRAY:
        itype = struct.unpack("<I", _gguf_read(f, 4))[0]
        count = struct.unpack("<Q", _gguf_read(f, 8))[0]
        if itype in _GGUF_SCALAR:
            f.seek(_GGUF_SCALAR[itype] * count, 1)
        else:
            if count > 10_000_000:
                raise _GGUFError("数组长度异常")
            for _ in range(count):     # 字符串数组（分词表）只能逐个跳
                _gguf_value(f, itype, False)
        return None
    raise _GGUFError(f"未知的值类型 {vtype}")


def read_gguf_header(path, max_tensors=64):
    """
    返回 (metadata, tensor_names)：metadata 只含字符串/数值型的键值（数组跳过），
    tensor_names 最多 max_tensors 个。不是有效 GGUF 时返回 None。
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            version = struct.unpack("<I", _gguf_read(f, 4))[0]
            if version < 2:
                return None
            n_tensors, n_kv = struct.unpack("<QQ", _gguf_read(f, 16))
            if n_kv > 1_000_000 or n_tensors > 10_000_000:
                return None
            meta = {}
            for _ in range(n_kv):
                key = _gguf_str(f)
                vtype = struct.unpack("<I", _gguf_read(f, 4))[0]
                val = _gguf_value(f, vtype, vtype != _GGUF_ARRAY)
                if val is not None:
                    meta[key] = val
            names = []
            for _ in range(min(n_tensors, max_tensors)):
                names.append(_gguf_str(f))
                n_dims = struct.unpack("<I", _gguf_read(f, 4))[0]
                if n_dims > 8:
                    raise _GGUFError("维数异常")
                f.seek(8 * n_dims + 4 + 8, 1)    # 各维大小 + 类型 + 偏移
            return meta, names
    except (OSError, _GGUFError, struct.error, ValueError):
        return None


# 文本编码器（语言模型）的 GGUF：llama.cpp 转出来的张量名是 token_embd / blk.N.*，
# general.architecture 是语言模型的名字。扩散模型的 GGUF（city96 / unsloth 给 ComfyUI-GGUF
# 用的那种）保留原始张量名（double_blocks.* / blocks.* …），架构名是 flux / wan / sdxl 之类。
_GGUF_TE_ARCH_PREFIX = ("llama", "qwen2", "qwen3", "gemma", "t5", "umt5", "mistral", "phi",
                        "clip", "bert", "mllama", "glm", "internlm", "deepseek")
_GGUF_TE_ARCH_NOT = ("qwen_image", "qwenimage")
_TE_FILE_HINTS = ("text_encoder", "text-encoder", "textencoder", "t5xxl", "umt5", "clip_l", "clip_g",
                  "qwen3", "qwen_3", "qwen2.5", "qwen2_5", "gemma", "llama")


def _classify_gguf(path):
    head = read_gguf_header(path)
    if not head:
        return None
    meta, names = head
    arch = str(meta.get("general.architecture") or "").lower()
    if any(n == "token_embd.weight" or n.startswith("blk.") for n in names):
        return "TextEncoder"
    if any(".lora_" in n or n.startswith("lora_") for n in names):
        return "LoRA"
    if arch and arch.startswith(_GGUF_TE_ARCH_PREFIX) and not arch.startswith(_GGUF_TE_ARCH_NOT):
        return "TextEncoder"
    low = os.path.basename(path).lower()
    if any(h in low for h in _TE_FILE_HINTS) and "image" not in low:
        return "TextEncoder"
    # 剩下的就是扩散模型本体：ComfyUI 的 GGUF 加载器只认 diffusion_models / unet
    return "DiffusionModel"


def classify_model_role(path):
    """
    按文件内容判断模型的真实类型（下载后纠正网站标错、合并/上传时判断该放哪个分类）：
      LoRA / Checkpoint（整合了 VAE 或文本编码器的完整大模型）/
      DiffusionModel（只有 UNet / DiT：Anima、Flux、Wan、MiniMax-H3 这类）/ VAE / TextEncoder
    认不出时返回 None（调用方据此「不动」）。支持 .safetensors / .sft / .gguf。
    """
    low = str(path).lower()
    if low.endswith(".gguf"):
        return _classify_gguf(path)
    if not low.endswith((".safetensors", ".sft")):
        return None
    r = read_safetensors_header(path)
    if not r:
        return None
    _meta, keys = r
    if not keys:
        return None

    def has(sub):
        return any(sub in k for k in keys)

    def starts(*pre):
        return any(k.startswith(pre) for k in keys)

    if has("lora_") or has(".lora_A.") or has(".lora_down.") or has(".lora_up.") or has(".hada_w1_"):
        return "LoRA"
    if starts("model.diffusion_model."):
        bundled = starts("first_stage_model.", "conditioner.", "cond_stage_model.", "text_encoders.", "vae.")
        return "Checkpoint" if bundled else "DiffusionModel"
    # T5 的键也是 encoder.* 开头，必须先于 VAE 判断
    if has("embed_tokens") or starts("text_model.", "token_embedding", "transformer.resblocks") \
            or "shared.weight" in keys:
        return "TextEncoder"
    enc_dec = sum(1 for k in keys if k.startswith(("encoder.", "decoder.", "quant_conv", "post_quant_conv")))
    if enc_dec >= 0.85 * len(keys):
        return "VAE"
    if starts("net.blocks.", "double_blocks.", "single_blocks.", "blocks.", "transformer_blocks.", "joint_blocks.",
              "input_blocks.", "layers."):
        return "DiffusionModel"
    return None
