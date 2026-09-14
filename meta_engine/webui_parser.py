"""
webui_parser.py - SD WebUI / Forge 格式解析与生成
"""
import json
import re
from typing import Dict, Any, List
from .metadata_types import SamplerParameters, ImageMetadata
from .common import safe_int, safe_float

# ── 模块级预编译正则 (编译一次全局复用) ──
# 参数行定位: 末尾包含 "Steps: N ... Sampler/CFG scale/Seed/Model hash:" 的行 (兼容中英文冒号)
_PARAM_LINE_RE = re.compile(r"Steps[:：]\s*\d+.*(?:Sampler|CFG scale|Seed|Model hash)[:：]")
# 正负提示词切分与 <lora:name:weight> 标签: 高频调用, 预编译避免每次 parse_text 重编译
_NEG_PROMPT_RE = re.compile(r"(?:^|\n)Negative prompt[:：]\s*")
_LORA_TAG_RE = re.compile(r"<lora:([^:>]+)(?::([^:>]+))?>")
# 键名规范: 字母开头, 允许数字/下划线/连字符/空格 (如 "Model hash", "Hires resize-up", "ControlNet 0 Guidance Start")
_KEY_RE = re.compile(r"[A-Za-z][\w \-]*")

# ── 来源细化 (Forge / reForge 判别, 正则与参考实现 sd-webui-reForge 识别策略对齐) ──
# 身份键: 参数行中可能标注生成器身份的键 (大小写不敏感)
_FAMILY_IDENTITY_KEYS = frozenset({"software", "source", "generator", "version"})
# 显式版本键 (键名即身份)
_REFORGE_PARAM_KEYS = frozenset({"reforge_version", "sd_webui_reforge_version"})
_FORGE_PARAM_KEYS = frozenset({"forge_version", "sd_webui_forge_version"})
# reForge 签名: 容忍连字符/下划线/空格/驼峰变体
_REFORGE_SIG_RE = re.compile(
    r"\bre[-_\s]?forge\b|\bsd[-_\s]?webui[-_\s]?re[-_\s]?forge\b"
    r"|\bstable[-_\s]?diffusion[-_\s]?(?:webui[-_\s]?)?re[-_\s]?forge\b", re.IGNORECASE)
# Forge 签名: 严格区分于 reForge (先查 reForge 再查 Forge)
_FORGE_SIG_RE = re.compile(
    r"\bsd[-_\s]?webui[-_\s]?forge\b|\bstable[-_\s]?diffusion[-_\s]?(?:webui[-_\s]?)?forge\b"
    r"|\bwebui[-_\s]+forge\b|\bforge[-_\s]+webui\b", re.IGNORECASE)
# Forge 版本号签名: "f2.0.1v1.10.1-previous-635-gf9dc4ffe" 形态 (仅用于身份键的值)
_FORGE_VERSION_VALUE_RE = re.compile(
    r"\bf\d+(?:\.\d+)*v\d+(?:\.\d+)*(?:[-+][a-z0-9_.\-]+)?\b", re.IGNORECASE)


def _split_segments(text: str) -> List[str]:
    """按引号外的逗号分段 (尊重 JSON 引号内的逗号与转义)"""
    if '"' not in text: return text.split(",")  # 无引号快速路径
    segs: List[str] = []
    buf: List[str] = []
    in_quote = False
    escaped = False
    for ch in text:
        if in_quote:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quote = False
            continue
        if ch == '"':
            in_quote = True
            buf.append(ch)
        elif ch == ",":
            segs.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    segs.append("".join(buf))
    return segs

# 不参与插件分组的顶层核心键
_CORE_KEYS = frozenset({
    "Skip Early CFG", "NGMS", "NGMS all steps", "Refiner",
    "Refiner switch at", "Noise Schedule", "Emphasis", "TI",
    "MaHiRo", "Rescale CFG", "RNG", "Forge", "Version",
    "Diffusion in Low Bits",
})
# parse_text 中已提升为 SamplerParameters 一级字段的键, 不再进入 other_params
_HANDLED_KEYS = {
    "Steps", "Sampler", "Schedule type", "CFG scale", "CFG", "Seed", "Size", "Model",
    "Model hash", "Denoising strength", "Clip skip", "Lora hashes",
}
# build_text 中由 param_defs 负责写出的键, other 分组里出现时跳过以防重复
_PRIMARY_KEYS = {"Steps", "Sampler", "Schedule type", "CFG scale", "Seed", "Size",
                 "Model hash", "Model", "Denoising strength", "Clip skip"}
# other_params 中非文本回写的分组 (ComfyUI 关键节点等, 仅用于 UI 展示)
# novelai: NAI 专有参数 (cfg_rescale/sm/sm_dyn 等) 仅检视展示; WebUI infotext 无法表达, 回写时跳过
_NON_TEXT_GROUPS = {"facedetailer", "adetailer_nodes", "upscale", "controlnet_nodes", "vae", "inpaint", "ipadapter", "novelai"}
# hires 分组内已结构化映射的短键 (build_text 用显式 emit 回写); 其余以原始 "Hires ..." 全名
# 存入的键视为无损兜底 extras, build_text 原样回写, UI 直接按原始键名显示 (杜绝"读取了但不显示")
_HIRES_STRUCT_KEYS = frozenset({"upscale", "upscaler", "steps", "denoise", "resize_up", "latent_sampler"})
# 已被上方结构化逻辑消费的原始 Hires* 键 (不再重复进 extras)
_HIRES_CONSUMED_KEYS = frozenset({"Hires upscale", "Hires upscaler", "Hires steps",
                                  "Hires denoising strength", "Hires resize-up"})


def _quote_value(text: str) -> str:
    """与 Forge infotext_utils.quote 一致: 值含 , : 换行时用 JSON 引号包裹"""
    if not any(c in text for c in ',:\n"'):
        return text
    try:
        return json.dumps(text, ensure_ascii=False)
    except Exception:
        return text


def _unquote_value(text: str) -> str:
    """与 Forge infotext_utils.unquote 一致: 剥离 JSON 引号并还原转义"""
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        if "\\" in text:
            try:
                return json.loads(text)
            except Exception:
                pass
        return text[1:-1]
    return text


def _fmt_value(v: Any) -> str:
    """整数值的 float 去掉 .0 (7.0 → 7), 保证 infotext 往返字节一致"""
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


class WebUIParser:
    """SD WebUI / Forge 生成信息解析与生成器"""

    @staticmethod
    def _structure_plugin_params(params: dict) -> dict:
        """将 WebUI/Forge 扁平参数结构化为插件分组，支持全部 Forge 内置扩展"""
        result: Dict[str, Any] = {}

        consumed = set()

        def _collect_group(prefix: str) -> dict:
            grp = {k[len(prefix):].strip(): v for k, v in params.items() if k.startswith(prefix)}
            consumed.update(k for k in params if k.startswith(prefix))
            return grp

        # ── ADetailer 多阶段 (先处理编号阶段 2-10 再处理第一阶段, 避免前缀串扰) ──
        # Forge 有两种多阶段命名: 编号式 "ADetailer 2 model" 与后缀式 "ADetailer model 2nd";
        # 后缀式及 "ADetailer version" 等杂项键必须原样并入首单元 (build_text 按 "ADetailer {键}" 逐字节回写),
        # 绝不能因缺首阶段主键 "ADetailer model" 而整体丢弃 (真实数据: 仅配 2nd 阶段的图 12 键全丢)
        later_prefixes = tuple(f"ADetailer {i} " for i in range(2, 11))
        ad_units = []
        first_stage = {k[len("ADetailer "):].strip(): v for k, v in params.items()
                       if k.startswith("ADetailer ") and not k.startswith(later_prefixes)}
        if first_stage:
            first_stage["_idx"] = 1  # 保留原始阶段号(第1阶段); 跳过中间阶段时防止 build 重编号错位
            ad_units.append(first_stage)
            consumed.update(k for k in params if k.startswith("ADetailer ") and not k.startswith(later_prefixes))
        for i in range(2, 11):
            prefix = f"ADetailer {i} "
            if f"{prefix}model" in params:
                stage = _collect_group(prefix)
                stage["_idx"] = i
                ad_units.append(stage)
        if ad_units: result["adetailer"] = ad_units

        # ── ControlNet (支持 Forge 序列化格式 "ControlNet 0" 和展开格式 "ControlNet 0 Model") ──
        cn_units = []
        for i in range(10):
            prefix = f"ControlNet {i} "
            if f"{prefix}Model" in params:
                unit = _collect_group(prefix)
                unit["_idx"] = i  # 保留原始单元号; 禁用中间单元时防止 build 用 enumerate 重编号错位
                cn_units.append(unit)
            elif isinstance(params.get(f"ControlNet {i}"), str):
                unit = WebUIParser._parse_cn_serialized(params[f"ControlNet {i}"])
                if unit:
                    unit["_idx"] = i
                    cn_units.append(unit)
                    consumed.add(f"ControlNet {i}")
        if cn_units: result["controlnet"] = cn_units

        # ── Hires Fix ──
        if "Hires upscaler" in params or "Hires upscale" in params:
            hires: Dict[str, Any] = {}
            if params.get("Hires upscale") is not None: hires["upscale"] = params["Hires upscale"]  # 放大倍率 (最常被漏读)
            if "Hires upscaler" in params: hires["upscaler"] = params["Hires upscaler"]
            hs = safe_int(params.get("Hires steps"))
            if hs is not None: hires["steps"] = hs
            # 重绘幅度: 依次尝试真实键, 仅当元数据确实写入时才显示, 绝不填默认值 (Forge txt2img+Hires 常无此键)
            for dk in ("Hires denoising strength", "Denoising strength"):
                if params.get(dk) is not None and str(params[dk]).strip():
                    hires["denoise"] = safe_float(params[dk])
                    break
            if "Hires resize-up" in params: hires["resize_up"] = params["Hires resize-up"]
            if "Latent sampler" in params: hires["latent_sampler"] = params["Latent sampler"]
            # 无损兜底: 捕获所有未被上方结构化映射的 Hires* 原始键 (如 "Hires CFG Scale" / "Hires Module 1"),
            # 以原始全名存入 — build_text 原样回写, UI 逐行显示, 杜绝硬编码枚举导致的字段泄漏
            for k, v in params.items():
                if k.startswith("Hires") and k not in _HIRES_CONSUMED_KEYS:
                    hires[k] = v
            result["hires"] = hires
            consumed.update(k for k in params if k.startswith("Hires") or k == "Latent sampler")

        # ── Forge 前缀类扩展 (MultiDiffusion / Spectrum / Soft Inpainting) ──
        for prefix, group in (("multidiffusion_", "multidiffusion"), ("spec_", "spectrum")):
            if grp := _collect_group(prefix): result[group] = grp
        if grp := {k[16:]: v for k, v in params.items() if k.lower().startswith("soft inpainting ")}:
            result["soft_inpainting"] = grp
            consumed.update(k for k in params if k.lower().startswith("soft inpainting "))

        # ── 核心参数 (Skip Early CFG, NGMS, Refiner, etc.) ──
        core = {k: params[k] for k in params if k in _CORE_KEYS}
        if core:
            result["core_extras"] = core
            consumed.update(core.keys())

        # ── Dynamic Thresholding ──
        dt = {k: v for k, v in params.items()
              if any(t in k.lower() for t in ("mimic cfg", "cfg mode", "dynamic threshold"))}
        if dt:
            result["dynamic_threshold"] = dt
            consumed.update(dt.keys())

        # ── FreeU ──
        freeu = {k: v for k, v in params.items() if k.lower().startswith("freeu")}
        if freeu:
            result["freeu"] = freeu
            consumed.update(freeu.keys())

        # ── 剩余未分类的全部放入 other (杜绝任何参数因未预料前缀而被静默吞掉) ──
        other = {k: v for k, v in params.items()
                 if k not in _CORE_KEYS and k not in _HANDLED_KEYS and k not in consumed}
        if other: result["other"] = other

        return result

    @staticmethod
    def _parse_cn_serialized(serialized: str) -> dict:
        """解析 Forge ControlNet 序列化字符串: 'Module: x, Model: y, Weight: z, ...'
        (Forge serialize_unit 保证值内不含 , 和 : , 按 ', ' + ': ' 切分安全)"""
        unit = {}
        for part in (serialized or "").split(", "):
            k, sep, v = part.partition(": ")
            if sep and k.strip():
                unit[k.strip()] = v.strip()
        if unit:
            # 保留原始序列化串供 build_text 逐字节回写 (下划线开头 = 私有标记, UI 不展示);
            # 仅靠键大小写启发式无法区分序列/展开格式 (Forge 序列键实为 Module/Model 首字母大写)
            unit["_raw"] = serialized
        return unit

    @staticmethod
    def _detect_webui_family(p_dict: Dict[str, str]) -> str:
        """按参数行键值判别 WebUI 家族: reForge → "WebUI (ReForge)", Forge → "WebUI (Forge)",
        纯 A1111/WebUI 严格保持 "WebUI" (ui 层与既有取值比较依赖该值不变)。
        结果恒以 "WebUI" 前缀开头, 保证任何 startswith("WebUI") 判断安全。
        判别策略与参考实现 _detect_webui_family_generator 对齐: 先 reForge 后 Forge
        (reForge 文本身含 forge 字样), 仅查身份键 (Software/Source/Generator/Version) 的
        键名/值 与显式版本键; 裸 "forge"/版本号 "1.10.0" 均不误判。"""
        reforge_hit = forge_hit = False
        for key, value in p_dict.items():
            key_normalized = str(key or "").strip().lower().replace(" ", "_")
            value_text = str(value or "").strip().lower()
            if key_normalized in _REFORGE_PARAM_KEYS \
                    or _REFORGE_SIG_RE.search(key) or _REFORGE_SIG_RE.search(value_text):
                reforge_hit = True
                break
            if key_normalized in _FORGE_PARAM_KEYS \
                    or _FORGE_SIG_RE.search(key) or _FORGE_SIG_RE.search(value_text) \
                    or (key_normalized in _FAMILY_IDENTITY_KEYS
                        and _FORGE_VERSION_VALUE_RE.search(value_text)):
                forge_hit = True
        if reforge_hit:
            return "WebUI (ReForge)"
        if forge_hit:
            return "WebUI (Forge)"
        return "WebUI"

    @staticmethod
    def parse_text(raw_text: str) -> ImageMetadata:
        meta = ImageMetadata(source_type="WebUI", raw_text=raw_text)
        if not raw_text or not raw_text.strip(): return meta
        text = raw_text.strip()
        
        # 1. 拆分参数行与正负提示词 (参数行位于最后一个匹配行到末尾)
        lines = text.split("\n")
        param_line_idx = next((i for i in range(len(lines) - 1, -1, -1) if _PARAM_LINE_RE.search(lines[i])), -1)
        param_line = "\n".join(lines[param_line_idx:]).strip() if param_line_idx != -1 else ""
        prompt_and_neg = "\n".join(lines[:param_line_idx]).strip() if param_line_idx != -1 else text
            
        neg_match = _NEG_PROMPT_RE.search(prompt_and_neg)
        meta.positive_prompt = prompt_and_neg[:neg_match.start()].strip() if neg_match else prompt_and_neg
        meta.negative_prompt = prompt_and_neg[neg_match.end():].strip() if neg_match else ""
        
        # 2. 解析参数键值对
        if param_line:
            p_dict = WebUIParser._parse_param_line(param_line)
            # 来源细化: 依已捕获键判别 Forge / reForge, 纯 A1111 严格保持 "WebUI"
            meta.source_type = WebUIParser._detect_webui_family(p_dict)
            loras = []
            seen_loras: set = set()
            if "Lora hashes" in p_dict:
                for item in p_dict["Lora hashes"].split(","):
                    n, sep, h = item.partition(":")
                    if not sep: n, sep, h = item.partition("：")
                    if sep:
                        name = n.strip().strip('"')
                        loras.append({"name": name, "hash": h.strip().strip('"')})
                        seen_loras.add(name)
            
            # A1111/Forge 负面提示词中的 <lora:> 标签同样会作用于模型, 与 ComfyUI 解析器
            # (_merge_prompt_lora_tags) 保持一致: 正负两处都收集, 同名去重 (hash 行优先)
            for prompt_text in (meta.positive_prompt, meta.negative_prompt):
                for lora_name, lora_weight in _LORA_TAG_RE.findall(prompt_text):
                    w = safe_float(lora_weight) if lora_weight else 1.0
                    if lora_name not in seen_loras:
                        seen_loras.add(lora_name)
                        loras.append({"name": lora_name, "weight": w})
                    else:
                        for l in loras:
                            if l.get("name") == lora_name and "weight" not in l:
                                l["weight"] = w
                                break
                    
            # 传入完整 p_dict: hires 需读取真实 Denoising strength; other 组内部已按 _HANDLED_KEYS 过滤, 不会泄漏核心键
            meta.params = SamplerParameters(
                steps=safe_int(p_dict.get("Steps")),
                sampler_name=p_dict.get("Sampler"),
                scheduler=p_dict.get("Schedule type"),
                cfg_scale=safe_float(p_dict.get("CFG scale") or p_dict.get("CFG")),
                seed=safe_int(p_dict.get("Seed")),
                size=p_dict.get("Size"),
                model_name=p_dict.get("Model"),
                model_hash=p_dict.get("Model hash"),
                denoising_strength=safe_float(p_dict.get("Denoising strength")),
                clip_skip=safe_int(p_dict.get("Clip skip")),
                loras=loras,
                other_params=WebUIParser._structure_plugin_params(p_dict)
            )
            # 键序锚: 记录参数行完整键序 (往返保真, "改哪动哪"不重排)。私有下划线键,
            # _flatten_other_params 消费后移除; _shown_key 保证 UI 不显示。
            # 与 _parse_param_line 同款 replace: 多行参数段的续行键不能并入前一段 (否则丢锚错位)
            order = [k for k in _split_segments(param_line.replace("\n", ", ")) if ":" in k or "：" in k]
            key_order = [k.split(":")[0].split("：")[0].strip() for k in order]
            if key_order:
                meta.params.other_params["_key_order"] = key_order
        return meta

    @staticmethod
    def _parse_param_line(param_line: str) -> Dict[str, str]:
        """解析 WebUI/Forge 参数行 (按引号外逗号分段, 避免值内冒号/逗号误匹配, 兼容中英文冒号)"""
        result: Dict[str, str] = {}
        for seg in _split_segments(param_line.replace("\n", ", ")):
            seg = seg.strip()
            k, sep, v = seg.partition(":")
            if not sep: k, sep, v = seg.partition("：")
            if not sep: continue
            k = k.strip()
            if not k or not _KEY_RE.fullmatch(k): continue
            result[k] = _unquote_value(v.strip())
        return result

    @staticmethod
    def build_text(pos_prompt: str, neg_prompt: str, params: SamplerParameters) -> str:
        sections = [s for s in [pos_prompt.strip(), f"Negative prompt: {neg_prompt.strip()}" if neg_prompt.strip() else ""] if s]
        param_defs = [
            ("Steps", params.steps), ("Sampler", params.sampler_name), ("Schedule type", params.scheduler),
            ("CFG scale", params.cfg_scale), ("Seed", params.seed), ("Size", params.size),
            ("Model hash", params.model_hash), ("Model", params.model_name),
            ("Denoising strength", params.denoising_strength), ("Clip skip", params.clip_skip),
        ]
        p = [f"{k}: {_quote_value(_fmt_value(v))}" for k, v in param_defs if v is not None and str(v).strip()]
        if isinstance(params.other_params, dict):
            p.extend(WebUIParser._flatten_other_params(params.other_params))
        if inner := ", ".join(f'{l["name"]}: {l["hash"]}' for l in params.loras if isinstance(l, dict) and l.get("name") and l.get("hash")):
            p.append(f"Lora hashes: {_quote_value(inner)}")
        # 键序锚重排 (设置行整体): 原始键序里一级参数与插件键交错时 (如 Variation seed 在
        # Denoising strength 之前), 按锚还原原序; 锚中没有的键保持相对顺序追加在后
        key_order = (params.other_params or {}).get("_key_order") if isinstance(params.other_params, dict) else None
        if isinstance(key_order, list) and key_order:
            by_key: Dict[str, str] = {}
            extras: List[str] = []
            for line in p:
                k = line.split(":")[0].strip()
                if k in by_key:
                    extras.append(line)
                else:
                    by_key[k] = line
            ordered = [by_key[k] for k in key_order if k in by_key]
            ordered.extend(l for l in p if l not in ordered)
            p = ordered
        if p: sections.append(", ".join(p))
        return "\n".join(sections)

    @staticmethod
    def _flatten_other_params(other_params: dict) -> List[str]:
        """将结构化插件分组还原为 Forge infotext 扁平键值对 (parse → build 往返一致)。
        存在 _key_order 键序锚时按原始键序输出 (改哪动哪, 不重排), 锚中无的键按组序追加在后。"""
        key_order = other_params.get("_key_order") if isinstance(other_params.get("_key_order"), list) else None
        grouped: List[str] = []

        def emit(k: str, v: Any):
            if v is None: return
            s = str(v).strip()
            if s: grouped.append(f"{k}: {_quote_value(s)}")

        for group, val in other_params.items():
            if group in _NON_TEXT_GROUPS or group.startswith("_") or not isinstance(val, (dict, list)):
                continue
            if group == "adetailer":
                for i, unit in enumerate(val):
                    idx = unit.get("_idx", i + 1)  # 原始阶段号(跳过中间阶段也逐字节还原)
                    prefix = "ADetailer " if idx == 1 else f"ADetailer {idx} "
                    for k, v in unit.items():
                        if not str(k).startswith("_"): emit(f"{prefix}{k}", v)
            elif group == "controlnet":
                for i, unit in enumerate(val):
                    idx = unit.get("_idx", i)  # 原始单元号(禁用中间单元也逐字节还原)
                    raw = unit.get("_raw")
                    if raw is not None:
                        # 序列化格式: 逐字节回写原始串 (最忠实, 不因键大小写启发式误判而改成展开格式)
                        if str(raw).strip():
                            grouped.append(f"ControlNet {idx}: {_quote_value(str(raw))}")
                    else:
                        # 展开格式 (ControlNet 0 Model / Weight ...): 跳过私有标记键, 按展开格式回写
                        for k, v in unit.items():
                            if not str(k).startswith("_"):
                                emit(f"ControlNet {idx} {k}", v)
            elif group == "hires":
                emit("Hires upscale", val.get("upscale"))
                emit("Hires upscaler", val.get("upscaler"))
                emit("Hires steps", val.get("steps"))
                # Denoising strength 已由顶层 param_defs 回写 (hires.denoise 仅为其派生副本), 避免重复
                emit("Hires resize-up", val.get("resize_up"))
                emit("Latent sampler", val.get("latent_sampler"))
                # 无损回写所有未被结构化映射的原始 Hires* 键 (原始全名, 如 Hires CFG Scale / Hires Module 1)
                for k, v in val.items():
                    if k not in _HIRES_STRUCT_KEYS:
                        emit(k, v)
            elif group == "multidiffusion":
                for k, v in val.items(): emit(f"multidiffusion_{k}", v)
            elif group == "spectrum":
                for k, v in val.items(): emit(f"spec_{k}", v)
            elif group == "soft_inpainting":
                for k, v in val.items(): emit(f"Soft inpainting {k}", v)
            else:
                # core_extras / dynamic_threshold / other: 键名已是原始 infotext 键
                for k, v in val.items():
                    if k not in _PRIMARY_KEYS: emit(k, v)

        if not key_order:
            return grouped
        # ── 键序锚重排: 键名 → 行 映射 (同名键取首次出现), 按原始顺序重排; 新增键追加在后 ──
        by_key: Dict[str, str] = {}
        others: List[str] = []
        for line in grouped:
            k = line.split(":")[0].strip()
            if k in by_key:
                others.append(line)  # 同名重复键 (极少): 保留, 追加在后
            else:
                by_key[k] = line
        ordered: List[str] = []
        used = set()
        for k in key_order:
            if k in by_key and k not in used:
                ordered.append(by_key[k])
                used.add(k)
        for line in grouped:
            if line not in ordered:
                ordered.append(line)
        return ordered
