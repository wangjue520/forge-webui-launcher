"""
alt_generators.py - 多生成器识别 (三期读路径, 只读新增)

移植自参考实现 backend/metadata_parser/alt_generators.py (mixin 结构), 按本项目
静态方法风格改写。覆盖五类小众生成器探测器 + 闭源 AI 供应商标注:

  Fooocus / Easy Diffusion / InvokeAI / SwarmUI / Draw Things  → 硬解析提示词与参数
  Gemini / GPT Image (C2PA 字节签名扫描兜底)                    → 仅标注 source_type

每个探测器都带参考实现原样的严格门禁 (专属特征键齐全才认领), 防止误吞其他软件的图;
所有探测器只读, 任何异常静默返回 None (由调用方降级到下一级调度)。

与参考实现的差异 (均见对应函数 docstring):
- 参考实现返回 dict + model_assets / sidecar_field_source_keys 机制; 本项目直接构建
  ImageMetadata: checkpoint -> params.model_name, loras -> params.loras ({"name": ..}),
  生成参数整体入 params.other_params[<generator>] (与 NAI other_params["novelai"] 同型);
- _normalize_lora_names 简化为去重保序 (省去文件名优先级启发);
- 不移植 SynthID/像素水印检测 (参考实现自身标注为研究性 TODO);
- gemini 正则去掉裸 "google" 备选: EXIF Make="Google" 的 Pixel 相机照片会被误标为
  Gemini, 保留 "google ai / google deepmind" 等带上下文的备选。
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from .metadata_types import ImageMetadata, SamplerParameters
from .common import safe_int, safe_float
from .novelai_parser import _loads_braced

logger = logging.getLogger(__name__)

# ── Fooocus 自有 JSON 键组 (Title-Case, Fooocus 专有; 任意一键在场即视为 Fooocus 形态) ──
_FOOOCUS_KEYS = (
    "Prompt", "Negative Prompt", "negative_prompt", "Sampler", "Performance",
    "Resolution", "ADM Guidance", "Base Model", "Refiner Model", "Refiner Switch",
    "Sharpness", "Guidance Scale", "Metadata Scheme", "Style Selections",
)
# Fooocus 真实输出形态 (小写 prompt+negative_prompt) 的同现特征键
_FOOOCUS_SIBLING_KEYS = (
    "base_model", "performance", "sampler", "steps", "seed",
    "metadata_scheme", "sharpness", "guidance_scale",
)
# Fooocus 提取进生成参数的键 (键名小写 + 下划线化后存 other_params; 附小写别名,
# 覆盖真实输出形态的 steps/seed/sampler 等小写键)
_FOOOCUS_PARAM_KEYS = (
    "Steps", "Sampler", "Scheduler", "CFG Scale", "Guidance Scale",
    "Seed", "Resolution", "Sharpness", "Performance", "ADM Guidance",
    "Refiner Model", "Refiner Switch", "Style Selections",
    "Metadata Scheme", "Version",
    "steps", "seed", "sampler", "scheduler", "cfg_scale", "guidance_scale",
    "resolution", "performance", "sharpness", "metadata_scheme",
)
# Fooocus LoRA 键 (含旧版单复数与 lora_combined_N 写法)
_FOOOCUS_LORA_KEYS = (
    "LoRAs", "loras", "LoRA", "lora", "Lora", "Loras",
    "lora_combined_1", "lora_combined_2", "lora_combined_3",
    "lora_combined_4", "lora_combined_5",
)

# ── Easy Diffusion (cmdr2/stable-diffusion-ui) 专属键组: 至少一键在场才认领 ──
_EASY_DIFFUSION_KEYS = (
    "use_stable_diffusion_model", "use_vae_model", "use_lora_model",
    "use_hypernetwork_model", "use_face_correction", "use_upscale",
    "sampler_name", "num_inference_steps", "guidance_scale", "negative_prompt_scale",
)
_ED_PARAM_KEYS = (
    "use_stable_diffusion_model", "use_vae_model", "use_lora_model",
    "use_hypernetwork_model", "sampler_name", "num_inference_steps",
    "guidance_scale", "seed", "width", "height", "use_face_correction", "use_upscale",
)

# ── 闭源 AI 供应商标识 (Software/EXIF 字段正则, 仅标注不硬解提示词) ──
_AI_PROVIDER_PATTERNS = (
    ("Gemini", re.compile(
        r"\b(?:gemini|imagen(?:[-_\s]?\d+)?|google\s*(?:ai|deepmind)|nano[-_\s]?banana"
        r"|made\s*with\s*google\s*ai)\b", re.IGNORECASE)),
    ("GPT Image", re.compile(
        r"\b(?:gpt[-_\s]?image(?:[-_\s]?\d+)?|chatgpt(?:[-_\s]?image)?|openai(?:\s*image)?"
        r"|dall[-_\s]?e(?:[-_\s]?\d+)?)\b", re.IGNORECASE)),
)
_AI_PROVIDER_FIELDS = (
    "Software", "software", "Source", "source", "Generator", "generator",
    "Make", "make", "Model", "model", "Author", "author", "Creator", "creator",
    "Description", "ImageDescription", "Title", "title",
    "claim_generator", "claimGenerator",
    "XML:com.adobe.xmp",
)
# C2PA / Content Credentials 字节签名扫描 (仅标注, 不验签): 供应者签名常在清单锚点
# (c2pa/jumbf/claim_generator) 附近; 有界扫描文件头部, 文件过小不可能携带清单则跳过。
# 边界取 64KiB (参考实现为 512KiB): C2PA 清单按规范位于文件头部 (PNG caBX 应在 IDAT
# 之前, JPEG APP11 紧随文件头), 头部 64KiB 已可覆盖; 更小的边界把无元数据大图的
# 兜底开销从 ~1ms 压到 ~0.1ms (实测)
_C2PA_ANCHORS = (b"c2pa", b"jumbf", b"claim_generator", b"contentcredentials", b"content credentials")
_C2PA_SIGNATURES = (
    ("GPT Image", (b"gpt-image", b"chatgpt", b"openai")),
    ("Gemini", (b"gemini", b"imagen", b"google ai", b"nano-banana", b"deepmind")),
)
_C2PA_SCAN_BYTES = 64 * 1024
_C2PA_MIN_FILE_BYTES = 32 * 1024

# source_type → other_params 展示分组键 (与 NAI other_params["novelai"] 同型)
_SOURCE_TYPE_GROUP = {
    "Fooocus": "fooocus", "Easy Diffusion": "easy_diffusion",
    "InvokeAI": "invokeai", "SwarmUI": "swarmui", "Draw Things": "drawthings",
    "Gemini": "gemini", "GPT Image": "gpt_image",
}


def _coerce_json_block(block: Any) -> Any:
    """对不透明元数据串做尽力 JSON 解析 (容忍 UNICODE/ASCII EXIF 头与首 '{' 前的杂散文本)。"""
    if isinstance(block, dict):
        return block
    if isinstance(block, (bytes, bytearray)):
        try:
            block = bytes(block).decode("utf-8", errors="replace")
        except Exception:
            return None
    if not isinstance(block, str):
        return None
    text = block.strip()
    if text.startswith("UNICODE") or text.startswith("ASCII"):
        text = text[7:].strip("\0 ")
    json_start = text.find("{")
    if json_start < 0:
        return None
    try:
        return json.loads(text[json_start:])
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _flatten_text_value(value: Any) -> Optional[str]:
    """展平嵌套元数据值为可读文本 (str 剥离 / list 递归换行拼接 / dict 按常见文本键优先)。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            part = _flatten_text_value(item)
            if part:
                parts.append(part)
        return "\n".join(parts) if parts else None
    if isinstance(value, dict):
        for key in ("base_caption", "caption", "text", "prompt", "value", "content", "description"):
            nested = _flatten_text_value(value.get(key))
            if nested:
                return nested
        for nested in value.values():
            flattened = _flatten_text_value(nested)
            if flattened:
                return flattened
    return None


def _normalize_lora_names(names: List[str]) -> List[str]:
    """LoRA 名去重保序 (参考实现含文件名优先级启发, 本项目简化为去重)。"""
    result: List[str] = []
    seen = set()
    for name in names:
        text = str(name).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _extract_model_identifier(metadata: dict) -> Optional[str]:
    """从 Source/Model 等键提取模型标识 (参考实现 _extract_metadata_model_identifier 简化:
    取首个非空字符串值, 不做文件名形态判定; 仅用于 Fooocus scheme-only 兜底分支)。"""
    for key in ("Source", "source", "Model", "model"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().strip("\0 ")
    return None


def _raw_block_text(block: Any) -> str:
    """探测器命中的原始载体文本 (bytes 解码 / dict 序列化), 供 raw_text 检视。"""
    if isinstance(block, str):
        return block
    if isinstance(block, (bytes, bytearray)):
        try:
            return bytes(block).decode("utf-8", errors="replace")
        except Exception:
            return ""
    try:
        return json.dumps(block, ensure_ascii=False)
    except Exception:
        return ""


def _build_alt_meta(source_type: str, prompt: Optional[str], negative: Optional[str],
                    checkpoint: Optional[str], loras: List[str],
                    gen_params: Optional[Dict[str, Any]], raw_text: str = "") -> ImageMetadata:
    """探测结果 → ImageMetadata: 常见字段提升进 SamplerParameters, 其余生成参数整体
    存入 other_params[<generator>] (展示分组, 与 NAI other_params["novelai"] 同型)。"""
    meta = ImageMetadata(source_type=source_type, raw_text=raw_text or "")
    meta.positive_prompt = prompt or ""
    meta.negative_prompt = negative or ""
    params = SamplerParameters()
    if checkpoint:
        params.model_name = str(checkpoint)
    if loras:
        params.loras = [{"name": name} for name in _normalize_lora_names(loras)]
    if isinstance(gen_params, dict) and gen_params:
        params.steps = safe_int(gen_params.get("steps") or gen_params.get("num_inference_steps"))
        params.seed = safe_int(gen_params.get("seed"))
        params.cfg_scale = safe_float(
            gen_params.get("cfg_scale") or gen_params.get("guidance_scale") or gen_params.get("cfgscale"))
        for sk in ("sampler", "sampler_name"):
            sv = gen_params.get(sk)
            if isinstance(sv, str) and sv.strip():
                params.sampler_name = sv.strip()
                break
        size = gen_params.get("resolution") or gen_params.get("size")
        if not (isinstance(size, str) and re.fullmatch(r"\d+\s*[xX×]\s*\d+", size.strip())):
            size = None
            w, h = gen_params.get("width"), gen_params.get("height")
            if isinstance(w, int) and not isinstance(w, bool) and isinstance(h, int) and not isinstance(h, bool) \
                    and w > 0 and h > 0:
                size = f"{w}x{h}"
        if size:
            params.size = str(size).strip()
        params.other_params[_SOURCE_TYPE_GROUP.get(source_type, source_type.lower())] = dict(gen_params)
    meta.params = params
    return meta


class AltGenerators:
    """多生成器探测器与闭源 AI 供应商标注 (三期读路径, 全部只读静态方法)"""

    # ============================================================
    # 五类小众生成器探测器。调度顺序与参考实现 _detect_and_parse 的
    # alt 分发一致: Fooocus → SwarmUI → InvokeAI → Draw Things →
    # Easy Diffusion (见 detect_alt_generator)。
    # ============================================================

    @staticmethod
    def maybe_parse_fooocus(metadata: dict) -> Optional[ImageMetadata]:
        """Fooocus 探测器。

        载体: PNG tEXt `Comment` 或 JPEG/WEBP `comment` (小写 COM) 内的 JSON 参数块,
        另有 Fooocus 专有 `fooocus_scheme` 文本块确认身份。JSON 键为 Title-Case
        (`Prompt`/`Negative Prompt`/`Performance`/...) 或真实输出形态的小写
        `prompt`+`negative_prompt`+特征键, 与 NAI 的小写 `uc` 形态 distinct。
        门禁 (三选一, 全不满足不认领): fooocus_scheme 在场 / Software 含 fooocus /
        专有键组或真实输出形态命中。
        """
        fooocus_scheme = metadata.get("fooocus_scheme") or metadata.get("Fooocus_Scheme")
        candidate_blocks = [(key, metadata[key]) for key in ("Comment", "comment") if key in metadata]
        # a1111 scheme 的 Fooocus 写常规 parameters 文本 (WebUI 路径已接走), 此处跳过
        if not candidate_blocks and fooocus_scheme is None:
            return None
        software = str(metadata.get("Software", metadata.get("software", "")) or "").lower()
        software_is_fooocus = "fooocus" in software

        for _source_key, block in candidate_blocks:
            data = _coerce_json_block(block)
            if not isinstance(data, dict):
                continue
            # NovelAI 专属 Comment 形态让路 (uc/v4_* 且无 negative_prompt 才是 NAI)
            if ("uc" in data or "v4_prompt" in data or "v4_negative_prompt" in data) \
                    and "negative_prompt" not in data:
                continue
            data_software = str(data.get("Software", data.get("software", "")) or "").lower()
            looks_like_fooocus = (
                fooocus_scheme is not None
                or software_is_fooocus
                or "fooocus" in data_software
                or any(key in data for key in _FOOOCUS_KEYS)
                or ("prompt" in data and "negative_prompt" in data
                    and any(k in data for k in _FOOOCUS_SIBLING_KEYS))
            )
            if not looks_like_fooocus:
                continue

            prompt = _flatten_text_value(
                data.get("Prompt") or data.get("prompt") or data.get("Positive Prompt"))
            negative = _flatten_text_value(
                data.get("Negative Prompt") or data.get("negative_prompt") or data.get("Negative"))
            checkpoint = _flatten_text_value(
                data.get("Base Model") or data.get("base_model") or data.get("Model"))
            loras: List[str] = []
            for lora_key in _FOOOCUS_LORA_KEYS:
                value = data.get(lora_key)
                if value is None:
                    continue
                if isinstance(value, (list, tuple, set)):
                    loras.extend(str(v).strip() for v in value if str(v).strip())
                else:
                    loras.append(str(value).strip())
            gen_params: Dict[str, Any] = {}
            for k in _FOOOCUS_PARAM_KEYS:
                if k in data and data[k] not in (None, ""):
                    gen_params[k.lower().replace(" ", "_")] = data[k]
            if checkpoint and "model" not in gen_params:
                gen_params["model"] = checkpoint
            return _build_alt_meta("Fooocus", prompt, negative, checkpoint, loras,
                                   gen_params, raw_text=_raw_block_text(block))

        if fooocus_scheme is not None or software_is_fooocus:
            # 仅有 fooocus_scheme/Software 标识而无可用 Comment JSON 的罕见文件
            # (a1111 scheme 且只写了参数块等): 仍标注身份, 免得落进 "Plain"。
            return _build_alt_meta("Fooocus", None, None,
                                   _extract_model_identifier(metadata), [], None)
        return None

    @staticmethod
    def maybe_parse_swarmui(metadata: dict) -> Optional[ImageMetadata]:
        """SwarmUI / StableSwarmUI 探测器。

        载体: `parameters`/`Parameters`/`UserComment` 或 EXIF `Make`(0x0110) 内含
        `sui_image_params` 的 JSON。门禁: 字符串载体必须含 sui_image_params 标记。
        """
        candidates = [(key, metadata[key]) for key in
                      ("parameters", "Parameters", "UserComment", "Make", "make", "0x0110")
                      if key in metadata]
        for _source_key, block in candidates:
            text = block
            if isinstance(text, (bytes, bytearray)):
                text = bytes(text).decode("utf-8", errors="replace")
            if isinstance(text, str):
                if "sui_image_params" not in text:
                    continue
                data = _coerce_json_block(text)
                if not isinstance(data, dict):
                    continue
            elif isinstance(text, dict):
                data = text
            else:
                continue

            params = data.get("sui_image_params") or data
            if not isinstance(params, dict):
                continue

            prompt = _flatten_text_value(params.get("prompt"))
            negative = _flatten_text_value(params.get("negativeprompt") or params.get("negative_prompt"))
            checkpoint = _flatten_text_value(params.get("model"))
            gen_params = {k: v for k, v in params.items()
                          if k not in ("prompt", "negativeprompt", "negative_prompt")
                          and v not in (None, "")}
            loras: List[str] = []
            lora_value = params.get("loras") or params.get("lora")
            if isinstance(lora_value, list):
                loras = [str(v).strip() for v in lora_value if str(v).strip()]
            elif lora_value:
                loras = [s.strip() for s in re.split(r"[,\n]", str(lora_value)) if s.strip()]
            return _build_alt_meta("SwarmUI", prompt, negative, checkpoint, loras,
                                   gen_params, raw_text=_raw_block_text(block))
        return None

    @staticmethod
    def maybe_parse_invokeai(metadata: dict) -> Optional[ImageMetadata]:
        """InvokeAI (v3+) 探测器。

        载体与门禁 (四类载体键至少一个在场才继续):
          - v3 `invokeai_metadata` JSON (positive_prompt/negative_prompt/model dict/steps/...)
          - v3 graph `invokeai_graph` 内嵌 `core_metadata` 节点
          - v2 `sd-metadata`
          - legacy `Dream` 字符串 ("<prompt> -s 50 -S 12345 ...")
        """
        v3_block = metadata.get("invokeai_metadata")
        v3_graph = metadata.get("invokeai_graph")
        v2_block = metadata.get("sd-metadata")
        legacy = metadata.get("Dream")
        if not v3_block and not v3_graph and not v2_block and not legacy:
            return None

        prompt = negative = checkpoint = None
        loras: List[str] = []
        gen_params: Dict[str, Any] = {}

        if v3_block:
            data = _coerce_json_block(v3_block)
            if isinstance(data, dict):
                prompt = _flatten_text_value(data.get("positive_prompt"))
                negative = _flatten_text_value(data.get("negative_prompt"))
                model = data.get("model")
                if isinstance(model, dict):
                    checkpoint = _flatten_text_value(model.get("model_name") or model.get("name"))
                elif isinstance(model, str):
                    checkpoint = model
                for k in ("steps", "cfg_scale", "scheduler", "seed", "width", "height",
                          "rand_device", "controlnets"):
                    if k in data and data[k] not in (None, ""):
                        gen_params[k] = data[k]
                lora_value = data.get("loras") or data.get("lora")
                if isinstance(lora_value, list):
                    for entry in lora_value:
                        if isinstance(entry, dict):
                            lora_ref = entry.get("lora")
                            name = entry.get("model_name") \
                                or (lora_ref.get("model_name") if isinstance(lora_ref, dict) else None) \
                                or entry.get("name")
                            if name:
                                loras.append(str(name))
                        elif entry:
                            loras.append(str(entry))

        if not prompt and v3_graph:
            # `invokeai_graph` JSON 含 nodes dict; `core_metadata` 节点携带与 v3 块
            # 相同的字段 (镜像参考实现/IIB 的查找方式)
            graph = _coerce_json_block(v3_graph)
            if isinstance(graph, dict):
                nodes = graph.get("nodes") or {}
                core_meta = None
                if isinstance(nodes, dict):
                    for key, node in nodes.items():
                        if isinstance(key, str) and key.startswith("core_metadata") \
                                and isinstance(node, dict):
                            core_meta = node
                            break
                if isinstance(core_meta, dict):
                    prompt = _flatten_text_value(core_meta.get("positive_prompt"))
                    negative = _flatten_text_value(core_meta.get("negative_prompt"))
                    model = core_meta.get("model")
                    if isinstance(model, dict):
                        checkpoint = _flatten_text_value(model.get("model_name") or model.get("name"))
                    elif isinstance(model, str):
                        checkpoint = model
                    for k in ("steps", "cfg_scale", "scheduler", "seed", "width", "height"):
                        if k in core_meta and core_meta[k] not in (None, ""):
                            gen_params[k] = core_meta[k]

        if not prompt and v2_block:
            data = _coerce_json_block(v2_block)
            if isinstance(data, dict):
                image = data.get("image", {}) if isinstance(data.get("image"), dict) else {}
                prompt_field = image.get("prompt") or data.get("prompt")
                if isinstance(prompt_field, list) and prompt_field:
                    first = prompt_field[0]
                    if isinstance(first, dict):
                        prompt = _flatten_text_value(first.get("prompt") or first.get("text"))
                else:
                    prompt = _flatten_text_value(prompt_field)
                if not checkpoint:
                    checkpoint = _flatten_text_value(data.get("model_weights"))

        if not prompt and legacy:
            # legacy Dream 串: "<prompt> -s 50 -S 12345 -W 512 -H 512 -C 7.0"
            text = str(legacy)
            match = re.match(r'^"?([^"]*?)"?\s+(?:-[A-Za-z]\s+\S+(?:\s+|$))*$', text.strip())
            prompt = (match.group(1).strip() if match
                      else text.strip().split(" -", 1)[0].strip()) or None

        if not prompt and not negative and not checkpoint and not gen_params:
            return None
        return _build_alt_meta("InvokeAI", prompt, negative, checkpoint, loras, gen_params,
                               raw_text=_raw_block_text(v3_block or v3_graph or v2_block or legacy))

    @staticmethod
    def maybe_parse_drawthings(metadata: dict) -> Optional[ImageMetadata]:
        """Draw Things (iOS/macOS) 探测器。

        载体: XMP (PNG `XML:com.adobe.xmp`/`xmp` 文本块) 内 exif:UserComment/rdf:li
        的 JSON 参数 (键 c/uc/model/sampler/steps/seed)。门禁: XMP 在场且 UserComment
        JSON 至少含一个特征键, 其余 XMP UserComment 一律不认领 (不影响无 XMP 图)。
        """
        data = AltGenerators._extract_drawthings_usercomment(
            metadata.get("XML:com.adobe.xmp") or metadata.get("xmp"))
        if not isinstance(data, dict):
            return None
        prompt = _flatten_text_value(data.get("c") or data.get("prompt"))
        negative = _flatten_text_value(data.get("uc") or data.get("negative_prompt"))
        checkpoint = _flatten_text_value(data.get("model"))
        gen_params = {k: v for k, v in data.items()
                      if k not in ("c", "uc", "prompt", "negative_prompt") and v not in (None, "")}
        return _build_alt_meta("Draw Things", prompt, negative, checkpoint, [], gen_params)

    @staticmethod
    def _extract_drawthings_usercomment(xmp: Any) -> Optional[Dict[str, Any]]:
        """提取 exif:UserComment/rdf:li 内的 JSON 对象并做 Draw Things 特征键门禁。"""
        text = AltGenerators._extract_xmp_usercomment_text(xmp)
        if not text:
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if not any(k in data for k in ("c", "uc", "model", "sampler", "steps", "seed")):
            # 真实 Draw Things blob 至少含一个特征键; 其余 XMP UserComment 不碰
            return None
        return data

    @staticmethod
    def _extract_xmp_usercomment_text(xmp: Any) -> Optional[str]:
        """从 XMP 提取 exif:UserComment/rdf:li 文本 (无门禁, 供本探测器与
        sidecar_reader 的 .xmp 分支复用; 门禁由调用方施加)。"""
        if xmp is None:
            return None
        try:
            from xml.dom import minidom  # 延迟导入: xml.dom 拉入较重, 仅命中 XMP 时需要
            if isinstance(xmp, (bytes, bytearray)):
                xmp = bytes(xmp).decode("utf-8", errors="replace")
            if not isinstance(xmp, str):
                return None
            doc = minidom.parseString(xmp)
            user_comment_nodes = doc.getElementsByTagName("exif:UserComment")
            if not user_comment_nodes:
                return None
            # XPath 等价: rdf:Alt > rdf:li 文本
            li_nodes = user_comment_nodes[0].getElementsByTagName("rdf:li")
            if not li_nodes or not li_nodes[0].firstChild:
                return None
            text = str(li_nodes[0].firstChild.nodeValue or "").strip()
            return text or None
        except Exception:
            return None

    @staticmethod
    def maybe_parse_easy_diffusion(metadata: dict) -> Optional[ImageMetadata]:
        """Easy Diffusion (cmdr2/stable-diffusion-ui) 探测器。

        载体: PNG/JPEG 直接文本键组 (prompt/negative_prompt + 专属键)。
        门禁 (与参考实现一致, 防止吞掉泛用 {prompt, negative_prompt} JSON):
        negative_prompt 在场 + prompt 非 ComfyUI 图 JSON + 专属键组至少一键。
        """
        prompt = metadata.get("prompt")
        negative = metadata.get("negative_prompt") or metadata.get("Negative Prompt")
        if not negative:
            return None
        # ComfyUI 的 `prompt` 恒为节点图 JSON dict, 不许吞
        if isinstance(prompt, str) and prompt.strip().startswith("{"):
            try:
                if isinstance(json.loads(prompt), dict):
                    return None
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        if not any(key in metadata for key in _EASY_DIFFUSION_KEYS):
            return None

        prompt_text = _flatten_text_value(prompt)
        negative_text = _flatten_text_value(negative)
        if not prompt_text and not negative_text:
            return None

        gen_params = {k: metadata[k] for k in _ED_PARAM_KEYS
                      if k in metadata and metadata[k] not in (None, "")}
        checkpoint = _flatten_text_value(
            metadata.get("use_stable_diffusion_model")
            or metadata.get("Model") or metadata.get("model"))
        loras: List[str] = []
        lora_value = metadata.get("use_lora_model") or metadata.get("LoRA")
        if isinstance(lora_value, (list, tuple, set)):
            loras = [str(v).strip() for v in lora_value if str(v).strip()]
        elif lora_value:
            loras = [s.strip() for s in re.split(r"[,\n]", str(lora_value)) if s.strip()]
        return _build_alt_meta("Easy Diffusion", prompt_text, negative_text, checkpoint,
                               loras, gen_params, raw_text=_raw_block_text(prompt))

    # ============================================================
    # 闭源 AI 供应商标注 (Gemini / gpt-image / DALL-E)。
    # 这些图通常不内嵌 A1111 式参数, 仅通过 Software/EXIF 字段正则 +
    # C2PA 字节签名扫描 (有界, 仅标注不验签) 识别供应者, 并把
    # Description/UserComment 等可见字段带出为提示词。
    # 不做 SynthID/像素水印检测 (参考实现自身标注为研究性 TODO)。
    # ============================================================

    @staticmethod
    def maybe_detect_ai_provider(metadata: dict, image_path: Optional[str] = None,
                                 file_size: int = 0) -> Optional[ImageMetadata]:
        """闭源 AI 供应商标注: 字段正则优先, 未命中再做 C2PA 字节签名扫描。
        门禁: 元数据字段 / C2PA 锚点+供应者签名 双重命中, 普通图零误报。"""
        haystacks: List[str] = []
        for field in _AI_PROVIDER_FIELDS:
            value = metadata.get(field)
            if value is None:
                continue
            if isinstance(value, (bytes, bytearray)):
                try:
                    value = bytes(value).decode("utf-8", errors="replace")
                except Exception:
                    continue
            haystacks.append(str(value))

        joined = "\n".join(haystacks)
        matched: Optional[str] = None
        if joined:
            for provider, pattern in _AI_PROVIDER_PATTERNS:
                if pattern.search(joined):
                    matched = provider
                    break

        if matched is None and image_path:
            # 元数据字段未命中时的兜底: 很多供应者会剥掉明文 Software 标签但保留
            # 密码学 C2PA 清单 (PNG caBX / JPEG APP11), 有界扫描文件头部即可廉价命中
            matched = AltGenerators._scan_c2pa_byte_signatures(image_path, file_size)

        if matched is None:
            return None

        prompt = _flatten_text_value(
            metadata.get("Description") or metadata.get("ImageDescription")
            or metadata.get("Title") or metadata.get("UserComment"))
        checkpoint = _flatten_text_value(metadata.get("Model") or metadata.get("model"))
        return _build_alt_meta(matched, prompt, None, checkpoint, [], None)

    @staticmethod
    def _scan_c2pa_byte_signatures(image_path: str, file_size: int) -> Optional[str]:
        """C2PA / Content Credentials 字节签名扫描 (仅标注): 先找清单锚点再匹配供应者
        签名, 双重命中才认领 (普通 SD 图 prompt 里偶尔出现的 openai 等词不会误报)。
        有界: 仅读文件头 _C2PA_SCAN_BYTES; <32KiB 的文件不可能携带清单, 直接跳过。"""
        if not image_path or file_size < _C2PA_MIN_FILE_BYTES:
            return None
        try:
            with open(image_path, "rb") as fh:
                blob = fh.read(_C2PA_SCAN_BYTES)
        except OSError as exc:
            logger.debug("C2PA byte scan failed for %s: %s", image_path, exc)
            return None
        if not blob:
            return None
        haystack = blob.lower()
        if not any(anchor in haystack for anchor in _C2PA_ANCHORS):
            return None
        for provider, markers in _C2PA_SIGNATURES:
            if any(marker in haystack for marker in markers):
                return provider
        return None

    # ============================================================
    # 尾部调度入口与协调判定 (供 image_io.read_metadata 使用)。
    # ============================================================

    @staticmethod
    def build_metadata_view(info: Dict[str, Any], exif_raw: Optional[bytes] = None,
                            usercomment: Optional[str] = None) -> Dict[str, Any]:
        """合并探测器所需的元数据视图: PNG/WebP info 文本键 + EXIF 常用字符串字段
        (Software/Make/Model/ImageDescription/Artist/Copyright) + 解码后的 UserComment
        文本; UserComment 内的 PNG 包装对象 ({Software/Description/Comment/Source, ...})
        将其字符串键提升到顶层, 使 Fooocus JPEG 等以 UserComment 为载体的图也能被探测。
        info 自有键优先于 EXIF 同名键。"""
        metadata: Dict[str, Any] = {}
        for key, value in (info or {}).items():
            if isinstance(key, str):
                metadata[key] = value
        if exif_raw and isinstance(exif_raw, bytes):
            try:
                import piexif  # 延迟导入: read_metadata 已 _ensure_pil, 此处命中缓存零开销
                exif_dict = piexif.load(exif_raw)
                ifd0 = exif_dict.get("0th") or {}
                for tag_id, name in ((piexif.ImageIFD.ImageDescription, "ImageDescription"),
                                     (piexif.ImageIFD.Make, "Make"),
                                     (piexif.ImageIFD.Model, "Model"),
                                     (piexif.ImageIFD.Software, "Software"),
                                     (piexif.ImageIFD.Artist, "Artist"),
                                     (piexif.ImageIFD.Copyright, "Copyright")):
                    value = ifd0.get(tag_id)
                    if name in metadata:
                        continue
                    if isinstance(value, str) and value.strip():
                        metadata[name] = value
                    elif isinstance(value, (bytes, bytearray)):
                        try:
                            text = bytes(value).decode("utf-8", errors="replace").strip("\x00 ")
                            if text:
                                metadata[name] = text
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug("EXIF metadata view extraction failed: %s", exc)
        if usercomment and isinstance(usercomment, str):
            metadata["UserComment"] = usercomment
            wrapped = _loads_braced(usercomment)
            if isinstance(wrapped, dict):
                for key, value in wrapped.items():
                    if isinstance(key, str) and isinstance(value, str) and key not in metadata:
                        metadata[key] = value
        return metadata

    @staticmethod
    def detect_alt_generator(metadata: Dict[str, Any]) -> Optional[ImageMetadata]:
        """尾部多生成器调度: 顺序与参考实现 _detect_and_parse 的 alt 探测分发一致
        (Fooocus → SwarmUI → InvokeAI → Draw Things → Easy Diffusion)。
        全部门禁失败返回 None; 单个探测器异常静默跳过。"""
        for detector in (AltGenerators.maybe_parse_fooocus,
                         AltGenerators.maybe_parse_swarmui,
                         AltGenerators.maybe_parse_invokeai,
                         AltGenerators.maybe_parse_drawthings,
                         AltGenerators.maybe_parse_easy_diffusion):
            try:
                result = detector(metadata)
            except Exception as exc:  # 防御: 探测器任何异常都不得中断读路径
                logger.debug("alt-generator detector %s failed: %s",
                             getattr(detector, "__name__", detector), exc)
                continue
            if result is not None:
                return result
        return None

    @staticmethod
    def detect_ai_provider(metadata: Dict[str, Any], image_path: Optional[str] = None,
                           file_size: int = 0) -> Optional[ImageMetadata]:
        """尾部 AI 供应商标注 (多生成器探测器全部落空后的最后兜底)。"""
        try:
            return AltGenerators.maybe_detect_ai_provider(
                metadata, image_path=image_path, file_size=file_size)
        except Exception as exc:  # 防御: 标注失败静默降级
            logger.debug("ai-provider detection failed: %s", exc)
            return None

    # ── 与既有分支的协调判定 (让路条件与对应探测器门禁同构) ──

    @staticmethod
    def nai_should_yield_to_fooocus(metadata: Any) -> bool:
        """read_metadata 第 3 步 (PNG Description/Comment → NAI) 的 Fooocus 让路判定。

        Fooocus 与 NAI 共用 Description/Comment 键位; 本项目 NAI 解析器的弱门禁
        (Description+Comment/Source 同现即尝试解析) 会把 Fooocus 图误标为 NovelAI。
        让路须同时满足: Software/Source 不含 novelai (真 NAI 优先, 既有 NAI 行为不变)
        + Comment JSON 不携带 NAI 专属键 (uc/v4_*/characterPrompts, Fooocus 恒不写)
        + Fooocus 探测器能给出实质结果 (提示词/负面/底模至少一项非空)。
        让路后由尾部探测器以 Fooocus 身份认领。"""
        if not isinstance(metadata, dict):
            return False
        software = str(metadata.get("Software", "") or metadata.get("Source", "") or "").lower()
        if "novelai" in software:
            return False
        if AltGenerators._comment_is_nai_shaped(metadata):
            return False
        result = AltGenerators.maybe_parse_fooocus(metadata)
        return bool(result and (result.positive_prompt or result.negative_prompt
                                or result.params.model_name))

    @staticmethod
    def _comment_is_nai_shaped(metadata: dict) -> bool:
        """Comment JSON 含 NAI 专属键 (uc / v4_prompt / v4_negative_prompt /
        characterPrompts) 即为 NAI 领地, Fooocus 不让路 (Fooocus 从不使用这些键)。"""
        for key in ("Comment", "comment"):
            block = metadata.get(key)
            if block is None:
                continue
            data = _coerce_json_block(block)
            if isinstance(data, dict) and any(
                    k in data for k in ("uc", "v4_prompt", "v4_negative_prompt",
                                        "characterPrompts", "character_prompts")):
                return True
        return False

    @staticmethod
    def nai_should_yield_to_fooocus_text(uc_text: Optional[str]) -> bool:
        """read_metadata 第 4 步 (EXIF UserComment → NAI) 的 Fooocus 让路判定:
        UserComment 内为 PNG 包装对象 JSON ({Software/Description/Comment/...}) 时
        按 dict 判定 (Fooocus JPEG/WEBP 载体); 其余文本不参与。"""
        if not uc_text or "{" not in uc_text:
            return False
        wrapped = _loads_braced(uc_text)
        if not isinstance(wrapped, dict):
            return False
        return AltGenerators.nai_should_yield_to_fooocus(wrapped)

    @staticmethod
    def easy_diffusion_should_yield(metadata: Any) -> bool:
        """read_metadata 第 1 步 (prompt/workflow → ComfyUI) 的 Easy Diffusion 让路判定。

        Easy Diffusion 的 PNG 用同名 "prompt" tEXt 存纯文本提示词 (非 JSON 图),
        若被 ComfyUI 分支吞掉会解析成空图。仅当 prompt 为非 JSON 纯文本 +
        negative_prompt 在场 + Easy Diffusion 专属键在场时让路 (与探测器门禁同构;
        workflow 在场恒不让, 损坏 ComfyUI prompt 块无 negative_prompt/专属键也不让)。"""
        if not isinstance(metadata, dict) or "workflow" in metadata:
            return False
        prompt = metadata.get("prompt")
        if not isinstance(prompt, str) or prompt.lstrip().startswith("{"):
            return False
        negative = metadata.get("negative_prompt") or metadata.get("Negative Prompt")
        if not negative:
            return False
        return any(key in metadata for key in _EASY_DIFFUSION_KEYS)

    @staticmethod
    def looks_like_comfy_usercomment_payload(cj: Any) -> bool:
        """read_metadata 第 4 步 ComfyUI 分支的图载体判定 (既有 '"prompt"' 子串判定的
        收紧): prompt 为图 dict / JSON 图串, 或 workflow 在场, 或 nodes+links 工作流
        本体 (ComfyUI-JpegExport 写法兼容)。Fooocus 等把纯文本提示词放 "prompt" 键的
        JSON 不再误入空 ComfyUI 分支。"""
        if not isinstance(cj, dict):
            return False
        prompt = cj.get("prompt")
        if isinstance(prompt, dict):
            return True
        if isinstance(prompt, str) and prompt.lstrip().startswith("{"):
            return True
        if cj.get("workflow") is not None:
            return True
        return "nodes" in cj and "links" in cj
