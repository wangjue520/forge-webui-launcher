"""
metadata_types.py - 数据结构定义
定义统一的图像元数据与LoRA模型元数据结构 (包含 Civitai / Kohya 详尽训练参数)
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

@dataclass
class SamplerParameters:
    steps: Optional[int] = None
    sampler_name: Optional[str] = None
    scheduler: Optional[str] = None
    cfg_scale: Optional[float] = None
    seed: Optional[int] = None
    size: Optional[str] = None
    model_name: Optional[str] = None
    model_hash: Optional[str] = None
    denoising_strength: Optional[float] = None
    clip_skip: Optional[int] = None
    loras: List[Dict[str, Any]] = field(default_factory=list)
    other_params: Dict[str, Any] = field(default_factory=dict)
    refiners: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class ImageMetadata:
    source_type: str = "Unknown"
    file_path: str = ""
    file_format: str = ""
    width: int = 0
    height: int = 0
    file_size_kb: float = 0.0
    positive_prompt: str = ""
    negative_prompt: str = ""
    params: SamplerParameters = field(default_factory=SamplerParameters)
    raw_prompt_json: Optional[Dict[str, Any]] = None
    raw_workflow_json: Optional[Dict[str, Any]] = None
    raw_text: str = ""
    post_processing: List[Dict[str, Any]] = field(default_factory=list)
    # ── 扩展读取字段 (带默认值, 现有构造/签名零改动) ──
    # NovelAI V4/V4.5/V5 角色提示词: [{"index": 0, "prompt": str, "negative_prompt": str,
    #   "center": {"x": float, "y": float} (可选)}]
    character_prompts: List[Dict[str, Any]] = field(default_factory=list)
    # Civitai AIR 资源 (来自 "Civitai resources:" 标记 / urn:air:...:civitai:<id>@<ver>):
    # [{"model_name": str, "version_name": Optional[str], "weight": Optional[float],
    #   "air": str, "model_id": int, "version_id": int, "model_type": str, "civitai_url": str}]
    civitai_resources: List[Dict[str, Any]] = field(default_factory=list)
    # 读取来源为同名 sidecar 文本 (三期读路径): 嵌入解析全部落空后由 SidecarReader
    # 命中并分类; 嵌入读取 (PNG chunk/EXIF/隐写/多生成器探测器/供应商标注) 恒为 False
    from_sidecar: bool = False
    # 读取时的采样参数快照 (写回精准度锚点): ComfyUI 保存时 update_comfy_prompt_text
    # 只写"与快照不同"的参数字段, 多阶段流水线图 (二采/Refiner) 未被编辑的字段
    # 不会被汇总值污染到别的采样器节点。UI 编辑流程由 _prepare_image_metadata 前置拷贝,
    # 故用户改动过的字段自然不同于快照。仅 ComfyUI 读路径填充, 其他来源 None
    params_snapshot: Optional[SamplerParameters] = None
    # NovelAI 原始 Comment JSON (写回锚点): NAI 图就地保存时以 NAI 形态写回 —
    # 只更新 prompt/uc (及 v4 结构中的 base_caption), 其余 50+ 专有键原样保留。
    # 仅 NAI 读路径填充, 其他来源 None
    raw_comment_json: Optional[Dict[str, Any]] = None
    # 源图 PNG 文本块的原始快照 (写回锚点): NAI 就地保存时要把 Source / Title /
    # Generation time 这些身份块原样写回去。块级手术本来会自动保留没碰过的块,
    # 但这几个键属于"编辑器自有字段"(存成非 NAI 形态时必须丢弃, 否则下次读取
    # NAI 分支会抢先命中、旧提示词复活), 所以得显式带一份原值过来
    source_chunks: Dict[str, str] = field(default_factory=dict)

@dataclass
class LoraTagFrequency:
    tag: str
    count: int

@dataclass
class LoraMetadata:
    file_path: str = ""
    file_size_mb: float = 0.0
    header_size: int = 0
    
    # 基本与架构信息
    output_name: str = ""
    base_model: str = ""
    base_model_name: str = ""
    network_dim: Optional[int] = None      # Rank
    network_alpha: Optional[float] = None  # Alpha
    network_module: str = ""
    network_args: str = ""
    clip_skip: Optional[int] = None
    
    # 训练与优化超参数 (Civitai / Kohya 详细参数)
    learning_rate: Optional[float] = None
    unet_lr: Optional[float] = None
    text_encoder_lr: Optional[float] = None
    optimizer: str = ""
    optimizer_args: str = ""
    lr_scheduler: str = ""
    lr_warmup_steps: Optional[int] = None
    mixed_precision: str = ""
    seed: Optional[int] = None
    epochs: Optional[int] = None
    steps: Optional[int] = None
    batch_size: Optional[int] = None
    resolution: str = ""
    noise_offset: Optional[float] = None
    min_snr_gamma: Optional[float] = None
    num_train_images: Optional[int] = None
    num_reg_images: Optional[int] = None
    dataset_dirs: str = ""
    
    # 描述与作者 (ModelSpec / Comment)
    title: str = ""
    author: str = ""
    description: str = ""
    training_comment: str = ""
    
    # 词频与全部原始键值对
    tag_frequencies: List[LoraTagFrequency] = field(default_factory=list)
    raw_metadata: Dict[str, str] = field(default_factory=dict)
