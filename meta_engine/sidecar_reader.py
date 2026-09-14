"""
sidecar_reader.py - 同名 sidecar 文本读取 (三期读路径, 只读兜底)

移植自参考实现 backend/metadata_parser/_runtime.py 的 _load_sidecar_metadata /
_load_one_sidecar 与 constants.py 的 SIDECAR_EXTENSIONS / _MAX_SIDECAR_BYTES。

调度约定: 仅在嵌入解析全部落空 (read_metadata 即将返回 Plain) 时调用一次, 命中后按
内容三分类并置 ImageMetadata.from_sidecar=True (与"图片自带元数据"严格区分, status
展示由上层接线):
  ① WebUI 签名 (Steps:/Negative prompt:)      → WebUIParser.parse_text
  ② ComfyUI 图 JSON (prompt/workflow 载体)    → ComfyUIParser.parse_comfy_data
  ③ 其余整体作为正向提示词 (空参数, source_type="Plain")

与参考实现的差异:
- 本项目为单文件编辑器: 不做每目录列表缓存, 仅 2-3 次 os.path.isfile 存在性检查;
- 参考实现把 JSON 的 caption/text 类键标记为独立 caption 字段不提升为 prompt;
  本项目 ImageMetadata 无 caption 字段, 未带 prompt/positive 等明确提示词键的 JSON
  按任务规格走 ③ (整体原文作为正向提示词);
- .xmp sidecar 取 exif:UserComment 文本后走同一三分类 (复用 alt_generators 的提取器),
  不移植参考实现的 Adobe SD XMP (sd:DerivedFrom/stEvt) 结构化解析。
所有读取失败静默返回 None (降级为 Plain)。
"""
import json
import os
import re
from typing import Any, List, Optional, Set, Tuple

from .metadata_types import ImageMetadata
from .webui_parser import WebUIParser
from .comfyui_parser import ComfyUIParser
from .alt_generators import AltGenerators

# sidecar 扩展名清单 (与参考实现 constants.py 一致); 候选: image.png.txt 与 image.txt 双形态
SIDECAR_EXTENSIONS = (".txt", ".json", ".xmp")
# 单个 sidecar 大小上限 (参考实现 _MAX_SIDECAR_BYTES): 超限视为异常文件不读
_MAX_SIDECAR_BYTES = 256 * 1024
# WebUI infotext 签名 (与 image_io 同款, 中英文冒号兼容)
_WEBUI_SIGNATURE_RE = re.compile(r"Steps[:：]\s*\d+|Negative prompt[:：]", re.IGNORECASE)
# JSON 中视为生成提示词的键 (小写化比对): c/uc 为 Draw Things 载体键
_POSITIVE_JSON_KEYS = frozenset({"c", "prompt", "positive", "positive_prompt"})
_NEGATIVE_JSON_KEYS = frozenset({"uc", "negative", "negativeprompt", "negative prompt", "negative_prompt"})


class SidecarReader:
    """同名 sidecar 文本读取 (最终兜底, 全部只读静态方法)"""

    @staticmethod
    def read_sidecar_metadata(file_path: str) -> Optional[ImageMetadata]:
        """查找同目录同名不同扩展 sidecar (image.png.txt / image.txt 双候选, 参考实现
        同构), 读到内容即按三分类构建 ImageMetadata (from_sidecar=True)。
        任何失败静默返回 None。"""
        if not file_path:
            return None
        try:
            directory = os.path.dirname(file_path) or "."
            base_name = os.path.basename(file_path)
            stem = os.path.splitext(base_name)[0]
            candidates: List[str] = []
            seen: Set[str] = set()
            for ext in SIDECAR_EXTENSIONS:
                for name in (base_name + ext, stem + ext):
                    candidate = os.path.join(directory, name)
                    key = os.path.normcase(os.path.abspath(candidate))
                    if key not in seen:
                        seen.add(key)
                        candidates.append(candidate)
            for candidate in candidates:
                if not os.path.isfile(candidate):
                    continue
                meta = SidecarReader._load_one_sidecar(candidate)
                if meta is not None:
                    return meta
        except Exception:
            return None
        return None

    @staticmethod
    def _load_one_sidecar(sidecar_path: str) -> Optional[ImageMetadata]:
        """读取单个 sidecar: 拒绝符号链接与超大文件 (参考实现同款防护);
        .xmp 先提取 exif:UserComment 文本再分类, 其余按扩展名读文本分类。"""
        try:
            if os.path.islink(sidecar_path):
                return None
            if os.path.getsize(sidecar_path) > _MAX_SIDECAR_BYTES:
                return None
            with open(sidecar_path, "r", encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
        text = text.strip()
        if not text:
            return None
        suffix = os.path.splitext(sidecar_path)[1].lower()
        if suffix == ".xmp":
            text = AltGenerators._extract_xmp_usercomment_text(text)
            if not text:
                return None
        return SidecarReader._classify_and_parse(text)

    @staticmethod
    def _classify_and_parse(text: str) -> Optional[ImageMetadata]:
        """sidecar 内容三分类 (见模块 docstring)。分类结果写入 source_type 并置
        from_sidecar=True; ①的门槛: 文本以 "{" 开头时跳过 (JSON 内含
        "Negative prompt:" 字样的文本值不应被误判为 WebUI infotext)。"""
        stripped = (text or "").strip()
        if not stripped:
            return None
        # ① WebUI infotext
        if not stripped.startswith("{") and _WEBUI_SIGNATURE_RE.search(stripped):
            meta = WebUIParser.parse_text(stripped)
            if meta is not None and (meta.positive_prompt or meta.negative_prompt
                                     or meta.params.steps is not None):
                meta.from_sidecar = True
                return meta
        # ② ComfyUI 图 JSON / 提示词键 JSON
        data: Any = None
        if stripped[0] in "{[":
            try:
                data = json.loads(stripped)
            except (json.JSONDecodeError, ValueError, TypeError):
                data = None
        if isinstance(data, dict):
            prompt_obj, wf_obj = SidecarReader._comfy_carriers(data)
            if prompt_obj is not None or wf_obj is not None:
                meta = ComfyUIParser.parse_comfy_data(prompt_obj, wf_obj)
                if meta is not None and (meta.positive_prompt or meta.raw_prompt_json):
                    meta.from_sidecar = True
                    return meta
            positive = SidecarReader._prompt_key_text(data, _POSITIVE_JSON_KEYS)
            if positive:
                # 普通 JSON 的 prompt/positive 等明确提示词键: 直接取文本 (ComfyUI
                # 解析器只认节点图, 纯文本键会被静默丢弃, 故不路由给它)
                meta = ImageMetadata(
                    source_type="Plain", positive_prompt=positive,
                    negative_prompt=SidecarReader._prompt_key_text(data, _NEGATIVE_JSON_KEYS))
                meta.from_sidecar = True
                return meta
        elif isinstance(data, list):
            flat = SidecarReader._text_of(data)
            if flat:
                meta = ImageMetadata(source_type="Plain", positive_prompt=flat)
                meta.from_sidecar = True
                return meta
        # ③ 整体作为正向提示词 (空参数)
        meta = ImageMetadata(source_type="Plain", positive_prompt=stripped)
        meta.from_sidecar = True
        return meta

    @staticmethod
    def _comfy_carriers(data: dict) -> Tuple[Any, Any]:
        """ComfyUI 载体识别 (与 image_io .json 分支同构): {"prompt": 图, "workflow": ...} /
        {"workflow": 图} / nodes+links 前端工作流本体 / class_type 节点图。
        "prompt" 为纯文本 (非 JSON 图) 时不作 ComfyUI 载体 (交给提示词键分支)。"""
        if "prompt" in data or "workflow" in data:
            prompt = data.get("prompt")
            if isinstance(prompt, dict) or (isinstance(prompt, str) and prompt.lstrip().startswith("{")):
                return prompt, data.get("workflow")
            workflow = data.get("workflow")
            if workflow is not None:
                return None, workflow
            return None, None
        if "nodes" in data and "links" in data:
            return None, data
        if any(isinstance(v, dict) and "class_type" in v for v in data.values()):
            return data, None
        return None, None

    @staticmethod
    def _prompt_key_text(data: dict, keys: frozenset) -> str:
        """取 JSON 中指定提示词键的首个非空文本值 (键名小写化比对)。"""
        for key, value in data.items():
            if str(key).strip().lower() in keys:
                text = SidecarReader._text_of(value)
                if text:
                    return text
        return ""

    @staticmethod
    def _text_of(value: Any) -> str:
        """提示词值归一为文本: 字符串剥离; 列表/元组换行拼接; 其余空串。"""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple)):
            parts: List[str] = []
            for item in value:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, (int, float)) and not isinstance(item, bool):
                    parts.append(str(item))
            return "\n".join(parts)
        return ""
