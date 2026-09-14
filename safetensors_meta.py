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
