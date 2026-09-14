"""
lora_parser.py - LoRA (.safetensors) 元数据秒级读取与安全重写模块 (详尽解析版)
"""
import os
import json
import struct
import logging
from collections import Counter
from typing import Dict, Any, List, Optional
from .metadata_types import LoraMetadata, LoraTagFrequency
from .common import safe_int, safe_float, valid_safetensors_header, cleanup_tmp

logger = logging.getLogger(__name__)

# safetensors header 上限守卫 (read/save 必须对称): 声明超过此值视为损坏/恶意文件, 拒绝读入避免撑爆内存
_MAX_HEADER_BYTES = 100 * 1024 * 1024

class LoraParser:
    """Safetensors 模型元数据解析与重写器"""

    # 最近一次写失败的底层原因 (bool 之外的可选通道); 入口清空, UI 侧可读
    last_error: str = ""
    
    @staticmethod
    def read_lora_metadata(file_path: str) -> LoraMetadata:
        meta = LoraMetadata(file_path=file_path)
        if not os.path.exists(file_path): return meta
        try:
            file_size = os.path.getsize(file_path)
            meta.file_size_mb = round(file_size / (1024 * 1024), 2)
            if file_size < 8: return meta
            with open(file_path, "rb") as f:
                header_len_bytes = f.read(8)
                if len(header_len_bytes) < 8: return meta
                header_size = struct.unpack("<Q", header_len_bytes)[0]
                meta.header_size = header_size
                if not valid_safetensors_header(header_size, file_size, _MAX_HEADER_BYTES): return meta
                
                header_dict = json.loads(f.read(header_size).decode("utf-8", errors="ignore"))
                if not isinstance(header_dict, dict): return meta
                raw_meta = header_dict.get("__metadata__", {})
                if not isinstance(raw_meta, dict): raw_meta = {}
                meta.raw_metadata = {str(k): str(v) for k, v in raw_meta.items()}
                
                # 基本与架构
                meta.output_name = raw_meta.get("ss_output_name", "")
                meta.base_model = LoraParser._detect_base_model(raw_meta, file_path=file_path)
                meta.base_model_name = raw_meta.get("ss_sd_model_name", "")
                meta.network_dim = safe_int(raw_meta.get("ss_network_dim"))
                meta.network_alpha = safe_float(raw_meta.get("ss_network_alpha"))
                meta.network_module = raw_meta.get("ss_network_module", "")
                meta.network_args = str(raw_meta.get("ss_network_args", ""))
                meta.clip_skip = safe_int(raw_meta.get("ss_clip_skip"))
                
                # 训练与优化超参数 (Civitai 完整参数)
                meta.learning_rate = safe_float(raw_meta.get("ss_learning_rate"))
                meta.unet_lr = safe_float(raw_meta.get("ss_unet_lr"))
                meta.text_encoder_lr = safe_float(raw_meta.get("ss_text_encoder_lr"))
                meta.optimizer = raw_meta.get("ss_optimizer", "")
                meta.optimizer_args = str(raw_meta.get("ss_optimizer_args", ""))
                meta.lr_scheduler = raw_meta.get("ss_lr_scheduler", "")
                meta.lr_warmup_steps = safe_int(raw_meta.get("ss_lr_warmup_steps"))
                meta.mixed_precision = raw_meta.get("ss_mixed_precision", "")
                meta.seed = safe_int(raw_meta.get("ss_seed"))
                # 有多少读多少: kohya 不同版本存 ss_epoch/ss_num_epochs、ss_max_train_steps/ss_steps,
                # 主键缺失或为 "None" 时回退备用键, 避免结构化字段空白 (原始键均在 raw_metadata 全量保留)
                # 注意不用 `or`: 合法值 0 会被当 falsy 误回退, 必须判 None
                meta.epochs = safe_int(raw_meta.get("ss_epoch"))
                if meta.epochs is None: meta.epochs = safe_int(raw_meta.get("ss_num_epochs"))
                meta.steps = safe_int(raw_meta.get("ss_max_train_steps"))
                if meta.steps is None: meta.steps = safe_int(raw_meta.get("ss_steps"))
                meta.batch_size = safe_int(raw_meta.get("ss_batch_size_per_device"))
                if meta.batch_size is None: meta.batch_size = safe_int(raw_meta.get("ss_total_batch_size"))
                meta.resolution = raw_meta.get("ss_resolution", "")
                meta.noise_offset = safe_float(raw_meta.get("ss_noise_offset"))
                meta.min_snr_gamma = safe_float(raw_meta.get("ss_min_snr_gamma"))
                meta.num_train_images = safe_int(raw_meta.get("ss_num_train_images"))
                meta.num_reg_images = safe_int(raw_meta.get("ss_num_reg_images"))
                meta.dataset_dirs = str(raw_meta.get("ss_dataset_dirs", ""))
                
                # 注释与描述
                meta.title = raw_meta.get("modelspec.title") or meta.output_name or os.path.splitext(os.path.basename(file_path))[0]
                meta.author = raw_meta.get("modelspec.author", "")
                meta.description = raw_meta.get("modelspec.description", "")
                meta.training_comment = raw_meta.get("ss_training_comment", "")
                
                # 词频
                meta.tag_frequencies = LoraParser._parse_tag_frequency(raw_meta.get("ss_tag_frequency"))
        except Exception as e:
            logger.error("Error reading %s: %s", file_path, e)
        return meta

    # 底模识别规则: 顺序即优先级 (衍生底模必须排在 SDXL 之前)
    _BASE_MODEL_RULES = (
        ("flux", "Flux.1"), ("hunyuan", "HunyuanVideo"), ("sd3", "SD 3.x"), ("sdx3", "SD 3.x"),
        ("pony", "Pony Diffusion V6"), ("illustrious", "Illustrious"), ("noobai", "NoobAI-XL"),
        ("illu", "Illustrious"), ("anima", "Anima"), ("qwen", "Qwen-Image"),
        ("sdxl", "SDXL 1.0"), ("sd_xl", "SDXL 1.0"),
        ("v1-5", "SD 1.5"), ("sd15", "SD 1.5"),
    )
    _SDXL_DERIVATIVES = ("pony", "illu", "noobai")

    @staticmethod
    def _detect_base_model(raw: Dict[str, str], file_path: str = "") -> str:
        ver = str(raw.get("ss_base_model_version", "")).lower()
        text = f"{raw.get('ss_sd_model_name', '')} {ver} {raw.get('ss_network_module', '')}".lower()
        if not text.strip() and file_path:
            text = os.path.basename(file_path).lower()
        # sdxl_base_v1-0 且不含任何非 SDXL 底模特征 -> 直接 SDXL (旧实现仅排除 pony/illu/noobai 三元组,
        # flux/anima/qwen 等同样会被误判 SDXL; 此处以后缀规则表为准, 与下方 for 循环优先级一致)
        if ver == "sdxl_base_v1-0" and not any(n in text for n, _ in LoraParser._BASE_MODEL_RULES if n not in ("sdxl", "sd_xl")):
            return "SDXL 1.0"
        for needle, label in LoraParser._BASE_MODEL_RULES:
            if needle in text: return label
        if file_path:
            fn = os.path.basename(file_path).lower()
            for needle, label in LoraParser._BASE_MODEL_RULES:
                if needle in fn: return label
        if str(raw.get("ss_v2", "")).lower() in ("true", "1"): return "SD 2.x"
        if "1.5" in text: return "SD 1.5"
        return raw.get("ss_base_model_version") or raw.get("ss_sd_model_name") or "Custom / Unknown"

    @staticmethod
    def _parse_tag_frequency(tag_freq_raw: Any) -> List[LoraTagFrequency]:
        if not tag_freq_raw: return []
        try:
            data = json.loads(tag_freq_raw) if isinstance(tag_freq_raw, str) else tag_freq_raw
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(data, dict): return []
        
        counter = Counter()
        for tag_map in data.values():
            if isinstance(tag_map, dict):
                for t, c in tag_map.items():
                    tag = str(t).strip()
                    if tag:
                        try: counter[tag] += int(float(c))
                        except (ValueError, TypeError, OverflowError): pass
        return [LoraTagFrequency(tag=k, count=v) for k, v in counter.most_common()]

    @staticmethod
    def save_lora_metadata(file_path: str, new_meta_dict: Dict[str, str], output_path: Optional[str] = None) -> bool:
        LoraParser.last_error = ""
        if not os.path.exists(file_path):
            LoraParser.last_error = "源文件不存在"
            return False
        target_path = output_path or file_path
        
        try:
            file_size = os.path.getsize(file_path)
            if file_size < 8: return False
            with open(file_path, "rb") as f:
                header_len_bytes = f.read(8)
                if len(header_len_bytes) < 8: return False
                old_h_size = struct.unpack("<Q", header_len_bytes)[0]
                if not valid_safetensors_header(old_h_size, file_size, _MAX_HEADER_BYTES): return False  # R8: 与 read 路径对称的 header 上限与物理大小守卫
                header_dict = json.loads(f.read(old_h_size).decode("utf-8", errors="ignore"))
                if not isinstance(header_dict, dict): return False
                
            header_dict["__metadata__"] = {str(k): str(v) for k, v in new_meta_dict.items()}
            # 紧凑分隔符: 与 safetensors 原生 header 格式字节级一致 (默认分隔符会给 2000+ 张量头
            # 平白增加 ~19KB 空格, 使 in-place 快路径 len(new)<=old 永不触发 -> 每次编辑都全量重写整份模型)
            new_h_bytes = json.dumps(header_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            
            # 同文件判定用 abspath+normcase: "./a.safetensors" 与 "a.safetensors" (及 Windows 大小写) 命中同一 in-place 快路径
            same_file = os.path.normcase(os.path.abspath(target_path)) == os.path.normcase(os.path.abspath(file_path))
            if same_file and len(new_h_bytes) <= old_h_size:
                with open(file_path, "r+b") as f:
                    f.seek(8)
                    f.write(new_h_bytes + b" " * (old_h_size - len(new_h_bytes)))
                return True
                
            target_dir = os.path.dirname(os.path.abspath(target_path))
            os.makedirs(target_dir, exist_ok=True)
            temp_target = f"{target_path}.tmp_{os.getpid()}"
            try:
                with open(file_path, "rb") as src, open(temp_target, "wb") as dst:
                    dst.write(struct.pack("<Q", len(new_h_bytes)))
                    dst.write(new_h_bytes)
                    src.seek(8 + old_h_size)
                    while chunk := src.read(64 * 1024 * 1024): dst.write(chunk)
                os.replace(temp_target, target_path)
            finally:
                cleanup_tmp(temp_target)
            return True
        except Exception as e:
            logger.error("Failed to save LoRA metadata to %s: %s", file_path, e)
            LoraParser.last_error = str(e)
            cleanup_tmp(f"{(output_path or file_path)}.tmp_{os.getpid()}")
            return False
