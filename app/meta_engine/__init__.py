# -*- coding: utf-8 -*-
"""
meta_engine —— AI 图片生成元数据解析引擎。

来源：独立项目「AI 元数据编辑器」的 core 包，移植进启动器时做了两处改动：
  1. 去掉 Pillow / piexif 硬依赖 —— 容器读取（PNG 文本块、JPEG/WebP EXIF）
     改由 container_reader 用标准库完成；隐写 PNG 的像素解码在没有 PIL 时
     回退到 png_raw 的纯实现。启动器的依赖仍然只有 pywebview + requests。
  2. 只保留读路径 —— 原项目的写回/另存（image_io.save_*）没有一并搬过来，
     启动器这边的「图片信息」页是只读的。

相比启动器原来那版解析器，多出来的能力：
  · ComfyUI 工作流真正的图遍历（追采样器 → 追提示词节点 → 追 LoRA 链），
    而不是按节点类型名瞎猜
  · NovelAI V3 / V4 / V4.5 / V5，含多角色提示词插槽
  · Forge / reForge / Fooocus / SwarmUI / InvokeAI / Draw Things /
    Easy Diffusion 各自的格式，且都带严格门禁，不会互相误吞
  · 隐写 PNG（stealth_pnginfo / stealth_rgbinfo，含 gzip 变体）
  · Civitai AIR 资源（urn:air:... 能直接还原出模型页链接）
  · 同名 .txt / .caption sidecar（LoRA 训练集图片常见）
  · LoRA safetensors 深度检视（架构、训练超参、触发词词频、ModelSpec）

写回（writer.py + container_writer.py）走的是**容器块手术**而不是重编码：
只增删改元数据块，像素字节原样复制。比原项目的 Pillow 重存路线更无损，
动图也天然安全，代价是不能跨格式另存（那条路见 image_convert.py）。
"""
from .metadata_types import ImageMetadata, SamplerParameters, LoraMetadata
from .reader import read_metadata, parse_workflow_json
from .writer import save_metadata, strip_metadata, collect_save_warnings, build_webui_text
from .lora_parser import LoraParser

__all__ = [
    "ImageMetadata", "SamplerParameters", "LoraMetadata",
    "read_metadata", "parse_workflow_json",
    "save_metadata", "strip_metadata", "collect_save_warnings", "build_webui_text",
    "LoraParser",
]
