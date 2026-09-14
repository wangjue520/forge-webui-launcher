"""
comfyui_parser.py - ComfyUI 节点图深度遍历与提示词/采样参数提取器 (全节点与自定义插件增强版)
"""
import json
import re
from collections import deque
from typing import Dict, Any, Tuple, List, Optional
from .metadata_types import SamplerParameters, ImageMetadata
from .common import safe_int, safe_float
from .webui_parser import _LORA_TAG_RE
from .civitai_air import scan_json as _scan_civitai_air, text_has_marker as _air_marker_in_text

# 通用回溯排除: 文件名类键名 / 文件名类值后缀 / 非文本链接键
_NON_PROMPT_KEY_RE = re.compile(r"(_name|_path|_file|_prefix|_dir|^file|^path|^lora|^vae|^ckpt|^unet|^temp_str|^random_template)", re.I)
_NON_PROMPT_VAL_RE = re.compile(r"\.(safetensors|ckpt|pt|pth|bin|gguf|onnx|pkl|sft|png|jpe?g|webp|gif|bmp|tiff?|mp4|json|ya?ml|txt)$", re.I)
_NON_TEXT_LINK_KEYS = frozenset({"mask", "image", "images", "pixels", "latent", "latent_image",
                                 "samples", "vae", "upscale_model", "control_net", "clip_vision"})

class ComfyUIParser:
    """ComfyUI 执行节点图与工作流解析器"""

    _SAMPLER_CLASSES = [
        "KSampler", "KSamplerAdvanced", "SamplerCustomAdvanced", "KSamplerWithRefiner",
        "FluxSampler", "EffortlessKSampler", "BNK_TiledKSampler"
    ]

    _NODE_PATTERNS = {
        "facedetailer": ["FaceDetailer", "DetailerForEach", "DetailerForEachPipe", "FaceDetailerPipe", "FaceDetailerSAM3"],
        "adetailer": ["ADetailer"],
        "upscale": ["UpscaleModelLoader", "ImageUpscaleWithModel", "UltimateSDUpscale", "ImageScaleBy", "ImageScale"],
        "controlnet": ["ControlNetLoader", "ControlNetApply", "ControlNetApplyAdvanced"],
        "ipadapter": ["IPAdapterModelLoader", "IPAdapterApply", "IPAdapter"],
        "vae": ["VAELoader"],
        "text_encoder": ["CLIPLoader", "DualCLIPLoader", "TripleCLIPLoader", "QuadrupleCLIPLoader"],
        "inpaint": ["VAEEncodeForInpaint", "SetLatentNoiseMask", "DifferentialDiffusion", "InpaintModelConditioning"],
        "multidiffusion": ["TiledDiffusion", "TiledDiffusionTTP", "TTP_TiledDiffusion", "TiledKSampler", "VAEDecodeTiled", "VAEEncodeTiled"],
        "freeu": ["FreeU", "FreeU_V2", "FreeU_Advanced"],
    }

    # class_type → 分类 反向查找表 (避免逐节点遍历所有分类)
    _CLASS_TO_CATEGORY = {cls: cat for cat, pats in _NODE_PATTERNS.items() for cls in pats}

    # 第三方提示词文本字段名 (来自 61 包 / 450 节点扫描): 按优先级排序, populated_text 等已解析结果优先
    _PROMPT_TEXT_FIELDS = [
        "populated_text", "positive_populated_text", "negative_populated_text",
        "text", "positive", "negative", "prompt",
        "positive_prompt", "negative_prompt", "text_positive", "text_negative",
        "wildcard_text", "wildcard", "wildcard_prompt",
        "positive_wildcard_text", "negative_wildcard_text",
        "string", "value", "caption", "tags", "text_input", "global_prompt",
        "seed_prompt", "common_text", "instruction_prompt", "high_level_description",
        "文本", "提示词", "正面", "负面", "正向", "负向",
    ]

    # 变长/多路合并节点的槽位族 (数字或 a-g 字母后缀), 用于"取合并后提示词"
    _SLOT_FAMILY_RE = re.compile(
        r"^(conditioning|cond|text|string|prompt|positive|negative|part)[_\-]?(\d+|[a-g])$",
        re.IGNORECASE,
    )
    # 合并时各槽位族的拼接优先级 (conditioning 类优先, 因其为合并节点主输入)
    _SLOT_FAMILY_PRIORITY = ("conditioning", "cond", "positive", "text", "string", "prompt", "part", "negative")
    # 具名条件合并字段 (ConditioningConcat/Average 等非编号槽位)
    _NAMED_COND_FIELDS = ("conditioning_to", "conditioning_from", "conditioning", "conditioning_1", "conditioning_2")
    # 超分模型名前缀原生倍率 (4x-UltraSharp → 4.0): 预编译避免每次 _merge_upscale_chain 重编译
    _NATIVE_UPSCALE_RE = re.compile(r"^(\d+(?:\.\d+)?)x", re.IGNORECASE)

    # 底模文件名字段: 标准 ckpt/unet + 第三方/自定义加载器常见变体 (GGUF/Diffusers/自研包/Eff. Loader)
    _MODEL_NAME_FIELDS = (
        "ckpt_name", "unet_name", "base_ckpt_name", "refiner_ckpt_name", "base_model_name",
        "checkpoint_name", "ckpt", "unet", "checkpoint",
        "model_path", "unet_path", "ckpt_path", "diffusion_model",
    )
    # 模型文件扩展名 (workflow 载入 / 底模识别共用)
    _MODEL_FILE_EXTS = (".safetensors", ".ckpt", ".pt", ".bin", ".gguf", ".sft")
    # 非底模文件名特征 (VAE/超分/检测模型不应被当底模)
    _NON_BASE_MODEL_HINTS = ("vae", "upscale", "detector", "florence")
    
    @staticmethod
    def _loads(obj: Any) -> Optional[Dict[str, Any]]:
        """宽容解析: dict 直返, 字符串走 json.loads, 空/畸形/非 dict 一律 None"""
        if isinstance(obj, dict): return obj
        if isinstance(obj, (bytes, bytearray)): obj = obj.decode("utf-8", "ignore")
        if not isinstance(obj, str) or not obj.strip(): return None
        try:
            data = json.loads(obj)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _workflow_to_nodes(workflow: Dict[str, Any]) -> Dict[str, Any]:
        """将 ComfyUI 前端 workflow 格式 (nodes + links) 转换为标准后端 nodes 节点图"""
        if not isinstance(workflow, dict):
            return {}
        nodes_list = workflow.get("nodes", [])
        links_list = workflow.get("links", [])
        if not isinstance(nodes_list, list):
            return {}

        links_map: Dict[Any, List[Any]] = {}
        for link in links_list:
            if isinstance(link, (list, tuple)) and len(link) >= 3:
                links_map[link[0]] = [str(link[1]), link[2]]

        nodes: Dict[str, Any] = {}
        for n in nodes_list:
            if not isinstance(n, dict) or n.get("id") is None:
                continue
            nid = str(n.get("id"))
            ctype = n.get("type", "")
            inputs: Dict[str, Any] = {}

            # 1. 具名部件值 (widgets_values_named 优先)
            if isinstance(n.get("widgets_values_named"), dict):
                inputs.update(n["widgets_values_named"])
            elif isinstance(n.get("widgets_values"), list):
                wv = n["widgets_values"]
                inputs["_widgets_values"] = wv
                clow = ctype.lower()
                if "ksampler" in clow:
                    if "advanced" in clow or (len(wv) >= 7 and str(wv[0]).lower() in ("enable", "disable")):
                        inputs.setdefault("add_noise", wv[0])
                        # 新版 ComfyUI 在 noise_seed 后附带 control_after_generate 控件 ("fixed"/"randomize"/...),
                        # 旧版无此项: 以 wv[2] 是否为字符串区分两代布局, 各按正确槽位取值
                        # (旧 9 元素: [add_noise, seed, steps, cfg, sampler, scheduler, start, end, return])
                        off = 1 if len(wv) > 2 and isinstance(wv[2], str) else 0
                        if len(wv) > 1 and isinstance(wv[1], (int, float)): inputs.setdefault("seed", wv[1])
                        if len(wv) > 2 + off and isinstance(wv[2 + off], (int, float)): inputs.setdefault("steps", wv[2 + off])
                        if len(wv) > 3 + off and isinstance(wv[3 + off], (int, float)): inputs.setdefault("cfg", wv[3 + off])
                        if len(wv) > 4 + off and isinstance(wv[4 + off], str): inputs.setdefault("sampler_name", wv[4 + off])
                        if len(wv) > 5 + off and isinstance(wv[5 + off], str): inputs.setdefault("scheduler", wv[5 + off])
                        if len(wv) > 6 + off and isinstance(wv[6 + off], (int, float)): inputs.setdefault("start_at_step", wv[6 + off])
                        if len(wv) > 7 + off and isinstance(wv[7 + off], (int, float)): inputs.setdefault("end_at_step", wv[7 + off])
                        if len(wv) > 8 + off and isinstance(wv[8 + off], str): inputs.setdefault("return_with_leftover_noise", wv[8 + off])
                    else:
                        str_idx = [i for i, v in enumerate(wv) if isinstance(v, str) and v.lower() not in ("fixed", "increment", "decrement", "randomize", "enable", "disable")]
                        if wv and isinstance(wv[0], (int, float)):
                            inputs.setdefault("seed", wv[0])
                        if len(str_idx) >= 2:
                            inputs.setdefault("sampler_name", wv[str_idx[0]])
                            inputs.setdefault("scheduler", wv[str_idx[1]])
                        elif len(str_idx) == 1:
                            inputs.setdefault("sampler_name", wv[str_idx[0]])
                        if len(wv) >= 7 and isinstance(wv[2], (int, float)) and isinstance(wv[3], (int, float)):
                            inputs.setdefault("steps", wv[2])
                            inputs.setdefault("cfg", wv[3])
                            inputs.setdefault("sampler_name", wv[4])
                            inputs.setdefault("scheduler", wv[5])
                            inputs.setdefault("denoise", wv[6])
                        elif len(wv) >= 6 and isinstance(wv[1], (int, float)) and isinstance(wv[2], (int, float)):
                            inputs.setdefault("steps", wv[1])
                            inputs.setdefault("cfg", wv[2])
                            inputs.setdefault("sampler_name", wv[3])
                            inputs.setdefault("scheduler", wv[4])
                            inputs.setdefault("denoise", wv[5])
                elif "scheduler" in clow:
                    for item in wv:
                        if isinstance(item, str) and item.strip() and "scheduler" not in inputs:
                            inputs["scheduler"] = item.strip()
                        elif isinstance(item, int) and not isinstance(item, bool) and "steps" not in inputs:
                            inputs["steps"] = item
                        elif isinstance(item, float) and "denoise" not in inputs:
                            inputs["denoise"] = item
                elif "guider" in clow:
                    for item in wv:
                        if isinstance(item, (int, float)) and not isinstance(item, bool):
                            inputs.setdefault("cfg", item)
                            break
                elif "samplerselect" in clow:
                    for item in wv:
                        if isinstance(item, str) and item.strip():
                            inputs.setdefault("sampler_name", item.strip())
                            break
                elif "seed" in clow or "noise" in clow:
                    for item in wv:
                        if isinstance(item, (int, float)) and not isinstance(item, bool):
                            inputs.setdefault("seed", item)
                            inputs.setdefault("noise_seed", item)
                            break
                elif "lora" in clow:
                    for item in wv:
                        if isinstance(item, str) and item.lower().endswith(ComfyUIParser._MODEL_FILE_EXTS):
                            inputs.setdefault("lora_name", item)
                            break
                    nums = [x for x in wv if isinstance(x, (int, float)) and not isinstance(x, bool)]
                    if len(nums) >= 1:
                        inputs.setdefault("strength_model", nums[0])
                        inputs.setdefault("strength_clip", nums[1] if len(nums) >= 2 else nums[0])
                elif any(k in clow for k in ("checkpoint", "ckpt", "unet", "diffusion", "loader", "precision")):
                    for item in wv:
                        if isinstance(item, str) and item.lower().endswith(ComfyUIParser._MODEL_FILE_EXTS):
                            if "unet" in clow or "diffusion" in clow:
                                inputs.setdefault("unet_name", item)
                            elif "vae" in clow:
                                inputs.setdefault("vae_name", item)
                            elif "clip" in clow or "textencoder" in clow:
                                inputs.setdefault("clip_name", item)
                            else:
                                inputs.setdefault("ckpt_name", item)
                            break
                elif "emptylatent" in clow or "latentimage" in clow:
                    nums = [v for v in wv if isinstance(v, (int, float))]
                    if len(nums) >= 2:
                        inputs.setdefault("width", nums[0])
                        inputs.setdefault("height", nums[1])
                elif "vae" in clow:
                    for item in wv:
                        if isinstance(item, str) and item.strip():
                            inputs.setdefault("vae_name", item)
                            break
                elif "freeu" in clow:
                    nums = [v for v in wv if isinstance(v, (int, float))]
                    if len(nums) >= 4:
                        inputs.setdefault("b1", nums[0])
                        inputs.setdefault("b2", nums[1])
                        inputs.setdefault("s1", nums[2])
                        inputs.setdefault("s2", nums[3])
                elif not any(nk in clow for nk in ("note", "markdown")) and any(k in clow for k in ("cliptextencode", "prompt", "text", "string", "caption", "textarea")):
                    for item in wv:
                        if isinstance(item, str) and item.strip() and not _NON_PROMPT_VAL_RE.search(item.strip()):
                            inputs.setdefault("text", item)
                            break

            # 2. 槽位连线输入
            for inp_slot in n.get("inputs", []) or []:
                if isinstance(inp_slot, dict):
                    name = inp_slot.get("name")
                    label = str(inp_slot.get("label") or "").lower()
                    link_id = inp_slot.get("link")
                    if link_id in links_map:
                        if name: inputs[name] = links_map[link_id]
                        if label in ("positive", "negative", "model", "clip", "vae", "latent"):
                            inputs.setdefault(label, links_map[link_id])

            mode = n.get("mode", 0)
            nodes[nid] = {
                "class_type": ctype,
                "inputs": inputs,
                "mode": mode,
                "_meta": {"title": n.get("title") or ""}
            }
        return nodes

    @staticmethod
    def parse_comfy_data(prompt_json_obj: Any, workflow_json_obj: Optional[Any] = None,
                         scan_air: Optional[bool] = None) -> ImageMetadata:
        meta = ImageMetadata(source_type="ComfyUI")
        meta.raw_prompt_json = ComfyUIParser._loads(prompt_json_obj) or {}
        meta.raw_workflow_json = ComfyUIParser._loads(workflow_json_obj)
        # 核心容灾: 若 prompt 为空但 workflow 存在，自动将前端 workflow 转译为标准 nodes 图
        if not meta.raw_prompt_json and meta.raw_workflow_json:
            meta.raw_prompt_json = ComfyUIParser._workflow_to_nodes(meta.raw_workflow_json)

        if not meta.raw_prompt_json: return meta
                
        # 入口净化: 过滤非 dict 节点值, 并规范化 inputs 为 dict (畸形图如 {"1": None} / inputs=null)
        # 下游所有 nodes.values()/nodes[sid].get("inputs", {}) 即可安全 (浅拷贝, 不变异原图)
        nodes = {str(k): (v if isinstance(v.get("inputs"), dict) else {**v, "inputs": {}})
                 for k, v in meta.raw_prompt_json.items() if isinstance(v, dict)}
    
        # 查找所有采样器并按拓扑排序
        all_samplers = ComfyUIParser._find_all_samplers(nodes)
        params, pos_prompt, neg_prompt = SamplerParameters(), "", ""
    
        if all_samplers:
            # 第一个采样器 -> 主参数 (展平 guider/sampler/sigmas 子节点以兼容 SamplerCustomAdvanced)
            first_id, first_node = all_samplers[0]
            inp = ComfyUIParser._flatten_sampler_inputs(nodes, first_node.get("inputs") or {})
            params.steps = safe_int(ComfyUIParser._resolve_num(nodes, inp.get("steps")))
            params.cfg_scale = safe_float(ComfyUIParser._resolve_num(nodes, inp.get("cfg")))
            params.sampler_name = ComfyUIParser._resolve_str(nodes, inp.get("sampler_name"))
            params.scheduler = ComfyUIParser._resolve_str(nodes, inp.get("scheduler"))
            params.seed = safe_int(ComfyUIParser._resolve_seed(nodes, inp))
            params.denoising_strength = safe_float(ComfyUIParser._resolve_num(nodes, inp.get("denoise")))
    
            if inp.get("positive"): pos_prompt = ComfyUIParser._trace_text(nodes, inp["positive"], polarity="positive")
            if inp.get("negative"): neg_prompt = ComfyUIParser._trace_text(nodes, inp["negative"], polarity="negative")
            if inp.get("model"): params.model_name, params.loras = ComfyUIParser._trace_model_and_loras(nodes, inp["model"])
            if inp.get("latent_image"): params.size = ComfyUIParser._trace_latent_size(nodes, inp["latent_image"])
            params.clip_skip = ComfyUIParser._extract_clip_skip(nodes, first_node)

            # 后续采样器 -> refiners
            for ref_id, ref_node in all_samplers[1:]:
                ref_inp = ComfyUIParser._flatten_sampler_inputs(nodes, ref_node.get("inputs") or {})
                refiner: Dict[str, Any] = {
                    "steps": safe_int(ComfyUIParser._resolve_num(nodes, ref_inp.get("steps"))),
                    "cfg": safe_float(ComfyUIParser._resolve_num(nodes, ref_inp.get("cfg"))),
                    "sampler_name": ComfyUIParser._resolve_str(nodes, ref_inp.get("sampler_name")),
                    "scheduler": ComfyUIParser._resolve_str(nodes, ref_inp.get("scheduler")),
                    "seed": safe_int(ComfyUIParser._resolve_seed(nodes, ref_inp)),
                    "denoise": safe_float(ComfyUIParser._resolve_num(nodes, ref_inp.get("denoise"))),
                }
                # 二采/Refiner 使用的底模 (ckpt) 名称: 回溯 model 链接; 回溯失败则回退主采样器底模
                ref_model = None
                if ref_inp.get("model"):
                    ref_model, ref_loras = ComfyUIParser._trace_model_and_loras(nodes, ref_inp["model"])
                    if ref_loras: refiner["loras"] = ref_loras
                if not ref_model:
                    # model 键缺失或回溯失败 (pipe/wrapper 节点可能改名): 扫描其余链接输入
                    for v in ref_inp.values():
                        if isinstance(v, list) and v:
                            m2, _l2 = ComfyUIParser._trace_model_and_loras(nodes, v)
                            if m2:
                                ref_model = m2
                                break
                ref_model = ref_model or params.model_name
                if ref_model: refiner["model_name"] = ref_model
                # 独立提示词
                if ref_inp.get("positive"):
                    refiner["positive_prompt"] = ComfyUIParser._trace_text(nodes, ref_inp["positive"], polarity="positive")
                if ref_inp.get("negative"):
                    refiner["negative_prompt"] = ComfyUIParser._trace_text(nodes, ref_inp["negative"], polarity="negative")
                params.refiners.append(refiner)
            
        # 兜底查找: 正/负各自补齐
        if not pos_prompt or not neg_prompt:
            fb_pos, fb_neg = ComfyUIParser._fallback_scan_prompts(nodes)
            pos_prompt, neg_prompt = pos_prompt or fb_pos, neg_prompt or fb_neg
                
        if not params.size: params.size = ComfyUIParser._find_any_latent_size(nodes)
        if not params.model_name:
            m_name, loras = ComfyUIParser._find_any_model_and_loras(nodes)
            params.model_name, params.loras = m_name, loras or params.loras
        if params.clip_skip is None:
            params.clip_skip = ComfyUIParser._extract_clip_skip(nodes)
    
        meta.positive_prompt = pos_prompt.strip()
        meta.negative_prompt = neg_prompt.strip()
        meta.params = params
        # 内嵌 LoRA 标签: 正/负提示词文本中的 <lora:name:weight> 与图节点提取结果去重合并
        ComfyUIParser._merge_prompt_lora_tags(params, meta.positive_prompt, meta.negative_prompt)

        # 提取关键节点/插件信息
        meta.post_processing = ComfyUIParser._extract_key_nodes(nodes)

        # Civitai AIR 资源提取 (廉价预检: 字符串入参先子串扫描; scan_air 可由调用方用
        # 原始 JSON 文本预检结果显式指定, dict 入参默认交给 walk 内部按字符串过滤)
        try:
            if scan_air is None:
                do_scan_air = _air_marker_in_text(prompt_json_obj) or _air_marker_in_text(workflow_json_obj) \
                    or isinstance(prompt_json_obj, dict) or isinstance(workflow_json_obj, dict)
            else:
                do_scan_air = bool(scan_air)
            if do_scan_air:
                meta.civitai_resources = _scan_civitai_air(meta.raw_prompt_json, meta.raw_workflow_json)
        except Exception:
            pass
        return meta

    @staticmethod
    def _merge_prompt_lora_tags(params: SamplerParameters, pos_prompt: str, neg_prompt: str) -> None:
        """从正/负提示词文本提取 <lora:name:weight> 标签合并进 params.loras (原地修改)。

        与图节点提取的 LoRA 按名称去重: 已有条目权重保留 (节点值为准), 缺权重时以标签补齐;
        新标签按其在文本中的出现顺序追加, 权重保留 (默认 1.0)。解析失败静默跳过。
        """
        try:
            if not pos_prompt and not neg_prompt:
                return
            existing: Dict[str, Dict[str, Any]] = {}
            loras = params.loras if isinstance(params.loras, list) else []
            params.loras = loras
            for entry in loras:
                if isinstance(entry, dict) and entry.get("name"):
                    existing.setdefault(str(entry["name"]), entry)
            for text in (pos_prompt, neg_prompt):
                if not text:
                    continue
                for lora_name, lora_weight in _LORA_TAG_RE.findall(text):
                    name = lora_name.strip()
                    if not name:
                        continue
                    weight = safe_float(lora_weight) if lora_weight else 1.0
                    if weight is None:
                        weight = 1.0
                    if name in existing:
                        entry = existing[name]
                        if entry.get("weight") is None:
                            entry["weight"] = weight
                    else:
                        entry = {"name": name, "weight": weight}
                        loras.append(entry)
                        existing[name] = entry
        except Exception:
            pass

    @staticmethod
    def _is_sampler_node(n: Any) -> bool:
        """判定采样器节点; 畸形节点安全返回 False"""
        if not isinstance(n, dict): return False
        ctype = str(n.get("class_type", ""))
        if ctype in ComfyUIParser._SAMPLER_CLASSES: return True
        inp = n.get("inputs") or {}
        return "sampler" in ctype.lower() and any(k in inp for k in ("steps", "cfg", "positive"))

    # SamplerCustomAdvanced 系把参数拆到子节点 (guider/sampler/sigmas) 以及 SDXL Tuple/Pipe
    _ALIAS_INPUT_KEYS = ("guider", "sampler", "sigmas", "model", "sdxl_tuple", "pipe", "basic_pipe", "tuple", "noise")

    @staticmethod
    def _flatten_sampler_inputs(nodes: Dict[str, Any], inp: Dict[str, Any]) -> Dict[str, Any]:
        """把 guider/sampler/sigmas/tuple 子节点的输入展平进同一命名空间; 已存在的键优先不覆盖"""
        flat, stack, seen = dict(inp), [inp], set()
        while stack:
            cur = stack.pop()
            if "base_positive" in cur: flat.setdefault("positive", cur["base_positive"])
            if "base_negative" in cur: flat.setdefault("negative", cur["base_negative"])
            if "base_model" in cur: flat.setdefault("model", cur["base_model"])
            if "base_clip" in cur: flat.setdefault("clip", cur["base_clip"])
            if "noise_seed" in cur: flat.setdefault("seed", cur["noise_seed"])
            if "optional_vae" in cur: flat.setdefault("vae", cur["optional_vae"])

            for k in ComfyUIParser._ALIAS_INPUT_KEYS:
                v = cur.get(k)
                if not isinstance(v, (list, tuple)) or not v: continue
                sid = str(v[0])
                if sid in seen or not isinstance(nodes.get(sid), dict): continue
                seen.add(sid)
                sub = nodes[sid].get("inputs") or {}
                if "base_positive" in sub: flat.setdefault("positive", sub["base_positive"])
                if "base_negative" in sub: flat.setdefault("negative", sub["base_negative"])
                if "base_model" in sub: flat.setdefault("model", sub["base_model"])
                if "base_clip" in sub: flat.setdefault("clip", sub["base_clip"])
                if "noise_seed" in sub: flat.setdefault("seed", sub["noise_seed"])

                for sk, sv in sub.items(): flat.setdefault(sk, sv)
                stack.append(sub)
        return flat

    @staticmethod
    def _resolve_alias_input(nodes: Dict[str, Any], inp: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
        """写回侧对称于读取的 _flatten_sampler_inputs: 沿 guider/sampler/sigmas/model 别名链定位真正持有
        key 的节点 inputs。SamplerCustomAdvanced 的 positive/negative 不在采样器自身, 而在 guider(CFGGuider)
        子节点上; 写回若只看采样器 inputs 会静默丢编辑 (真实数据 类型A)。返回持有该键的 inputs dict (就地
        可写), 未找到返回 None。已存在的键优先 (与读取 setdefault 语义一致)。"""
        if key in inp: return inp
        stack, seen = [inp], set()
        while stack:
            cur = stack.pop()
            for ak in ComfyUIParser._ALIAS_INPUT_KEYS:
                v = cur.get(ak)
                if not isinstance(v, (list, tuple)) or not v: continue
                sid = str(v[0])
                if sid in seen or not isinstance(nodes.get(sid), dict): continue
                seen.add(sid)
                sub = nodes[sid].get("inputs") or {}
                if key in sub: return sub
                stack.append(sub)
        return None

    @staticmethod
    def _find_primary_sampler(nodes: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        all_samplers = ComfyUIParser._find_all_samplers(nodes)
        # 多采样器图 (二采/Refiner/旁路调试残留): 静音(2)/旁路(4)采样器不参与出图,
        # 优先取活采样器; 全部被旁路 (整图存档形态) 时保持原顺序兜底, 不返回空
        for nid, n in all_samplers:
            if n.get("mode") not in (2, 4):
                return (nid, n)
        return all_samplers[0] if all_samplers else (None, None)

    @staticmethod
    def _find_all_samplers(nodes: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """返回所有 KSampler 类型节点，按链接拓扑排序（latent_image 链接到前一个采样器的输出）"""
        candidates = [(nid, n) for nid, n in nodes.items() if ComfyUIParser._is_sampler_node(n)]
        if len(candidates) <= 1:
            return candidates

        # 拓扑排序：若采样器 B 的 latent_image (直接或多跳) 源于采样器 A，则 A 先执行
        sampler_ids = {nid for nid, _ in candidates}
        output_of: Dict[str, str] = {}  # output_of[consumer] = producer
        for nid, n in candidates:
            latent_link = (n.get("inputs") or {}).get("latent_image")
            if not (isinstance(latent_link, (list, tuple)) and latent_link):
                continue
            # BFS 回溯上游，定位首个采样器生产者 (visited 防环)
            visited = set()
            queue = deque([str(latent_link[0])])
            while queue:
                curr = queue.popleft()
                if curr in visited:
                    continue
                visited.add(curr)
                if curr in sampler_ids and curr != nid:
                    output_of[nid] = curr
                    break
                if curr in nodes:
                    queue.extend(str(v[0]) for v in (nodes[curr].get("inputs") or {}).values()
                                 if isinstance(v, (list, tuple)) and v)

        # Kahn 式拓扑排序 (max_iter 防死循环，循环依赖时追加剩余)
        ordered: List[Tuple[str, Dict[str, Any]]] = []
        remaining = list(candidates)
        placed = set()
        max_iter = len(remaining) * len(remaining) + 1
        while remaining and max_iter > 0:
            max_iter -= 1
            for item in list(remaining):
                nid, _ = item
                dep = output_of.get(nid)
                if dep is None or dep in placed:
                    ordered.append(item)
                    placed.add(nid)
                    remaining.remove(item)
        ordered.extend(remaining)
        return ordered

    @staticmethod
    def _extract_key_nodes(nodes: Dict[str, Any]) -> List[Dict[str, Any]]:
        """遍历 prompt_data 的所有节点，按 class_type 分类提取关键信息"""
        result: Dict[str, List[Dict[str, Any]]] = {cat: [] for cat in ComfyUIParser._NODE_PATTERNS}

        upscale_nodes: List[Tuple[str, Dict[str, Any]]] = []  # 收集后合并为一张卡

        # 被引用节点集 (出现在任意节点 inputs 的链接源中): 用于识别完全未连线的空闲节点
        referenced = set()
        for n in nodes.values():
            for v in (n.get("inputs") or {}).values():
                if isinstance(v, (list, tuple)) and v:
                    referenced.add(str(v[0]))

        for nid, n in nodes.items():
            ctype = n.get("class_type", "")
            # 检测范围: 绕过/静音节点 (编辑器格式 mode 2=静音 4=绕过) 不参与出图, 不成卡
            if n.get("mode") in (2, 4):
                continue
            inp = n.get("inputs", {}) or {}
            # 检测范围: 完全未连线的空闲节点 (无链接输入且输出未被任何节点引用) 不成卡
            if not any(isinstance(v, (list, tuple)) for v in inp.values()) and nid not in referenced:
                continue
            category = ComfyUIParser._CLASS_TO_CATEGORY.get(ctype)
            # 兜底: 自定义 TE 加载器 (如 AnimaCLIPLoader) 类名含 cliploader/textencoderloader
            if category is None and ("cliploader" in ctype.lower() or "textencoderloader" in ctype.lower()):
                category = "text_encoder"
            # 兜底: 硬编码表未收录的第三方/自定义变体类 (FaceDetailerSAM3 / DetailerForEachDebug / 自定义 ControlNet 加载器等)
            if category is None:
                category = ComfyUIParser._guess_category_by_name(ctype, inp)
            if category is None:
                continue

            if category in ("facedetailer", "adetailer"):
                info: Dict[str, Any] = {"node_id": nid, "class_type": ctype}
                for k in ["model_name", "detection_model", "sam_model"]:
                    if isinstance(inp.get(k), str) and inp[k].strip(): info["model"] = str(inp[k])
                # 检测模型链接回溯: bbox/segm/sam 检测器子节点 (UltralyticsDetectorProvider 等)
                detectors: List[str] = []
                for k in ("bbox_detector", "segm_detector", "sam_model_opt", "bbox_detector_opt", "segm_detector_opt", "detector"):
                    if isinstance(inp.get(k), list):
                        dn = ComfyUIParser._trace_detector_model(nodes, inp[k])
                        if dn and dn not in detectors: detectors.append(dn)
                if detectors: info["detector"] = ", ".join(detectors)
                for k in (
                    "sampler_name", "scheduler", "steps", "cfg", "denoise",
                    "guide_size", "guide_size_for", "max_size", "feather",
                    "crop_factor", "drop_size", "bbox_threshold", "bbox_dilation",
                    "bbox_crop_factor", "sam_detection_hint", "sam_dilation",
                    "sam_threshold", "sam_prompt", "cycle", "inpaint_model", "noise_mask"
                ):
                    if k in inp and (val := ComfyUIParser._resolve_widget(nodes, inp[k])) is not None: info[k] = val
                # 独立提示词回溯
                if inp.get("positive"): info["positive_prompt"] = ComfyUIParser._trace_text(nodes, inp["positive"], polarity="positive")
                if inp.get("negative"): info["negative_prompt"] = ComfyUIParser._trace_text(nodes, inp["negative"], polarity="negative")
                # Detailer 专属通配提示词 (空串不入卡)
                wp = inp.get("wildcard_prompt") or inp.get("wildcard")
                if isinstance(wp, str) and wp.strip(): info["wildcard_prompt"] = wp.strip()
                result[category].append(info)

            elif category == "upscale":
                upscale_nodes.append((ctype, inp))  # 延迟合并, 避免 ImageUpscaleWithModel 的链接引用成为垃圾卡

            elif category == "controlnet":
                info = {"node_id": nid, "class_type": ctype}
                for k in ("control_net_name", "controlnet_name", "model_name", "control_net"):
                    if isinstance(inp.get(k), str) and inp[k].strip():
                        info["model"] = inp[k].strip()
                        break
                for k in ("strength", "start_percent", "end_percent"):
                    if k in inp and (val := ComfyUIParser._resolve_widget(nodes, inp[k])) is not None: info[k] = val
                result[category].append(info)

            elif category == "ipadapter":
                info = {"node_id": nid, "class_type": ctype}
                for k in ("ipadapter_file", "model_name", "ipadapter"):
                    if isinstance(inp.get(k), str) and inp[k].strip(): info["model"] = inp[k].strip(); break
                for k in ("weight", "weight_type", "start_at", "end_at"):
                    if k in inp and (w := ComfyUIParser._resolve_widget(nodes, inp[k])) is not None: info[k] = w
                result[category].append(info)

            elif category == "vae":
                info = {"node_id": nid, "class_type": ctype}
                if "vae_name" in inp: info["vae_name"] = str(inp["vae_name"])
                result[category].append(info)

            elif category == "text_encoder":
                # 仅当图中真实存在 CLIP 加载器节点时才提取 (打包 ckpt 无此节点, 自然不显示, 无需额外开关)
                names = [str(inp[k]) for k in ("clip_name", "clip_name1", "clip_name2", "clip_name3")
                         if isinstance(inp.get(k), str) and inp[k].strip()]
                if not names:
                    # 自定义 TE 加载器字段兜底: 键名含 clip/te 且值为模型文件
                    names = [str(v) for k, v in inp.items()
                             if isinstance(v, str) and v.lower().endswith((".safetensors", ".gguf", ".bin", ".pt", ".ckpt"))
                             and ("clip" in k.lower() or k.lower() in ("te", "te_name", "text_encoder"))]
                if names:
                    result[category].append({"node_id": nid, "class_type": ctype, "text_encoder": ", ".join(names)})

            elif category == "inpaint":
                info = {"node_id": nid, "class_type": ctype}
                for k in ("strength", "denoise", "grow_mask_by", "feather"):
                    if k in inp and (val := ComfyUIParser._resolve_widget(nodes, inp[k])) is not None:
                        info[k] = val
                result[category].append(info)

            elif category == "multidiffusion":
                info = {"node_id": nid, "class_type": ctype}
                for k in ("method", "tile_width", "tile_height", "tile_overlap", "wd14_model", "tile_size", "overlap", "temporal_size", "temporal_overlap"):
                    if k in inp and (val := ComfyUIParser._resolve_widget(nodes, inp[k])) is not None:
                        info[k] = val
                result[category].append(info)

            elif category == "freeu":
                info = {"node_id": nid, "class_type": ctype}
                for k in ("b1", "b2", "s1", "s2"):
                    if k in inp and (val := ComfyUIParser._resolve_widget(nodes, inp[k])) is not None:
                        info[k] = val
                result[category].append(info)

        # 超分链合并为一张卡 (真实模型名 + 放大倍率 + 目标尺寸), 剔除 ['75',0] 这类链接引用垃圾
        if upscale_nodes:
            merged = ComfyUIParser._merge_upscale_chain(upscale_nodes)
            if merged: result["upscale"].append(merged)

        # 转换为列表格式，过滤空分类
        return [{"type": cat, "nodes": items} for cat, items in result.items() if items]

    @staticmethod
    def _guess_category_by_name(ctype: str, inp: Dict[str, Any]) -> Optional[str]:
        """硬编码表未收录的自定义/第三方变体类: 按类名子串 + 特征字段兜底分类"""
        low = ctype.lower()
        if "freeu" in low:
            return "freeu"
        if "detailer" in low:
            if low.startswith("adetailer"): return "adetailer"
            # 真 detailer 节点必带 model + positive/wildcard 输入 (排除 DetailerHook 等辅助节点)
            if "model" in inp and ("positive" in inp or "wildcard_prompt" in inp or "wildcard" in inp):
                return "facedetailer"
            return None
        if "controlnet" in low and ("control_net_name" in inp or "control_net" in inp):
            return "controlnet"
        if ("ipadapter" in low or "ip-adapter" in low) and ("ipadapter_file" in inp or "model_name" in inp or "weight" in inp):
            return "ipadapter"
        if "vaeloader" in low and "vae_name" in inp:
            return "vae"
        if ("upscale" in low or "esrgan" in low) and any(k in inp for k in ("model_name", "upscaler_name", "scale_by", "upscale_by", "width")):
            return "upscale"
        return None

    @staticmethod
    def _trace_detector_model(nodes: Dict[str, Any], link: Any, depth: int = 0) -> Optional[str]:
        """回溯检测器链接 (bbox_detector/sam_model_opt 等) 到源节点取模型文件名"""
        if depth > 6 or not isinstance(link, (list, tuple)) or not link:
            return None
        n = nodes.get(str(link[0]))
        if not isinstance(n, dict):
            return None
        inp = n.get("inputs", {}) or {}
        for k in ("model_name", "model", "ckpt_name", "sam_model", "detection_model"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip(): return v.strip()
        # 透明节点穿透: 继续找链接输入
        for v in inp.values():
            if isinstance(v, list):
                r = ComfyUIParser._trace_detector_model(nodes, v, depth + 1)
                if r: return r
        return None

    @staticmethod
    def _merge_upscale_chain(upscale_nodes: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
        """将超分链合并为单卡; 有效放大倍率 = 模型原生倍率(名前缀 4x) × 缩放节点(scale_by)"""
        info: Dict[str, Any] = {}
        model_names: List[str] = []
        scale: Optional[float] = None
        for ctype, inp in upscale_nodes:
            # 真实超分模型名: 仅取字符串型 (排除 ['75',0] 这类节点链接引用)
            for k in ("model_name", "upscaler_name", "upscale_model"):
                v = inp.get(k)
                if isinstance(v, str) and v.strip() and v not in model_names:
                    model_names.append(v)
            # 相对缩放倍率仅来自 ImageScaleBy (如 0.5), 它本身不代表最终放大
            if ctype == "ImageScaleBy":
                for k in ("scale_by", "upscale_by"):
                    if isinstance(inp.get(k), (int, float)): scale = float(inp[k])
            # 目标尺寸 (ImageScale 绝对尺寸)
            if isinstance(inp.get("width"), (int, float)) and isinstance(inp.get("height"), (int, float)):
                info["size"] = f"{int(inp['width'])}x{int(inp['height'])}"
            # UltimateSDUpscale 附加采样参数
            for k in ("steps", "denoise"):
                if isinstance(inp.get(k), (int, float)): info.setdefault(k, inp[k])
        # 模型原生倍率: 解析名前缀 "4x" / "2x"
        native = None
        if model_names:
            m = ComfyUIParser._NATIVE_UPSCALE_RE.match(model_names[0])
            if m: native = float(m.group(1))
        # 有效放大 = 原生 × 缩放 (4x 模型 + 0.5 缩放 = 2x)
        if native is not None and scale is not None: info["upscale_by"] = native * scale
        elif native is not None: info["upscale_by"] = native
        elif scale is not None: info["upscale_by"] = scale
        if model_names:
            info = {"model": ", ".join(model_names), **info}
        # 过滤无效超分卡: 无模型/尺寸且倍率为 1.0 (等于没放大, 显示出来是干扰信息)
        if not model_names and "size" not in info and info.get("upscale_by") == 1.0:
            return {}
        return info

    @staticmethod
    def _trace_text(nodes: Dict[str, Any], link: Any, visited: Optional[set] = None, depth: int = 0, polarity: Optional[str] = None) -> str:
        """递归回溯文本/条件节点, 兼容内置与第三方提示词输入/合并节点"""
        if depth > 200: return ""
        if visited is None: visited = set()
        if not isinstance(link, (list, tuple)) or not link: return ""
        sid = str(link[0])
        slot = int(link[1]) if len(link) > 1 and str(link[1]).isdigit() else 0
        state_key = (sid, slot, polarity)
        if state_key in visited or sid not in nodes: return ""
        visited.add(state_key)

        node = nodes[sid]
        ctype = str(node.get("class_type", ""))
        clow = ctype.lower()
        inp = node.get("inputs", {})

        # 0a. ConditioningZeroOut (Flux/SD3 负向零化条件, 对应空负向提示词)
        if "conditioningzeroout" in clow:
            return ""

        # 0b. InpaintModelConditioning (Slot 0=positive, Slot 1=negative)
        if "inpaintmodelconditioning" in clow:
            if slot == 1 or polarity == "negative":
                if "negative" in inp and isinstance(inp["negative"], (list, tuple)):
                    return ComfyUIParser._trace_text(nodes, inp["negative"], visited, depth + 1, polarity="negative")
                return ""
            else:
                if "positive" in inp and isinstance(inp["positive"], (list, tuple)):
                    return ComfyUIParser._trace_text(nodes, inp["positive"], visited, depth + 1, polarity="positive")
                return ""

        # 0c. Unpack SDXL Tuple (Slot 2/6 = Positive, Slot 3/7 = Negative)
        if "unpack sdxl tuple" in clow:
            if "sdxl_tuple" in inp and isinstance(inp["sdxl_tuple"], (list, tuple)):
                eff_pol = "negative" if (slot in (3, 7) or polarity == "negative") else ("positive" if (slot in (2, 6) or polarity == "positive") else polarity)
                return ComfyUIParser._trace_text(nodes, inp["sdxl_tuple"], visited, depth + 1, polarity=eff_pol)

        # 0d. ApplyLLMToSDXLAdapter / LLM 隐藏状态穿透
        if "applyllmtosdxladapter" in clow or "llm_hidden_states" in inp:
            if isinstance(inp.get("llm_hidden_states"), (list, tuple)):
                return ComfyUIParser._trace_text(nodes, inp["llm_hidden_states"], visited, depth + 1, polarity=polarity)

        # 0e. 显式空字符串控件守卫: 若文本编码节点控件显式填写为空串, 直接返回空, 防止回退到图像/蒙版链接
        if any(k in clow for k in ("textencode", "cliptext", "prompt", "text")) and not any(nk in clow for nk in ("note", "markdown")):
            wv = inp.get("_widgets_values")
            if isinstance(wv, list) and len(wv) > 0 and wv[0] == "":
                return ""

        # 0f. 节点同时持有 positive 与 negative 输入链接时按极性严格分流 (如 Inpaint/FaceDetailer/自定义条件路由节点)
        if "positive" in inp and "negative" in inp:
            eff_k = "negative" if (slot == 1 or polarity == "negative") else "positive"
            if isinstance(inp[eff_k], (list, tuple)):
                return ComfyUIParser._trace_text(nodes, inp[eff_k], visited, depth + 1, polarity=eff_k)
            elif isinstance(inp[eff_k], str) and inp[eff_k].strip():
                return inp[eff_k].strip()

        # 1. ConditioningSetMaskAndCombine* (Slot 0=positive, Slot 1=negative)
        if "maskandcombine" in clow:
            eff_polarity = "negative" if (slot == 1 or polarity == "negative") else "positive"
            prefix = f"{eff_polarity}_"
            parts: List[str] = []
            for k in sorted(inp.keys()):
                if k.startswith(prefix):
                    v = inp[k]
                    if isinstance(v, str) and v.strip(): parts.append(v.strip())
                    elif isinstance(v, (list, tuple)):
                        if sub := ComfyUIParser._trace_text(nodes, v, visited, depth + 1, polarity=eff_polarity):
                            parts.append(sub)
            return ", ".join(dict.fromkeys(parts))

        # 2. Efficient Loader / EffortlessLoader / Eff. Loader
        if "efficient loader" in clow or "effortlessloader" in clow or "eff. loader" in clow:
            eff_pol = "negative" if (slot in (2, 4) or polarity == "negative") else ("positive" if (slot in (1, 3) or polarity == "positive") else None)
            if eff_pol == "negative" and "negative" in inp:
                v = inp["negative"]
                if isinstance(v, str) and v.strip(): return v.strip()
                if isinstance(v, (list, tuple)): return ComfyUIParser._trace_text(nodes, v, visited, depth + 1, polarity="negative")
            elif eff_pol == "positive" and "positive" in inp:
                v = inp["positive"]
                if isinstance(v, str) and v.strip(): return v.strip()
                if isinstance(v, (list, tuple)): return ComfyUIParser._trace_text(nodes, v, visited, depth + 1, polarity="positive")

        # 3. SDXL/双 CLIP (text_g+text_l 等) 需合并 g+l, 优先于单字段
        if dual := ComfyUIParser._trace_dual_clip(inp, nodes, visited, depth + 1, polarity=polarity): return dual

        # 4. 直接文本字段 (用户填写的提示词控件, 含第三方与中文字段名)
        if polarity == "positive":
            target_fields = [k for k in ComfyUIParser._PROMPT_TEXT_FIELDS if not any(k.lower().startswith(nk) for nk in ("negative", "neg", "负"))]
        elif polarity == "negative":
            neg_fields = [k for k in ComfyUIParser._PROMPT_TEXT_FIELDS if any(k.lower().startswith(nk) for nk in ("negative", "neg", "负"))]
            other_fields = [k for k in ComfyUIParser._PROMPT_TEXT_FIELDS if not any(k.lower().startswith(pk) for pk in ("positive", "pos", "正")) and k not in neg_fields]
            target_fields = neg_fields + other_fields
        else:
            target_fields = ComfyUIParser._PROMPT_TEXT_FIELDS

        for k in target_fields:
            if k not in inp: continue
            val = inp[k]
            if isinstance(val, str) and val.strip(): return val.strip()
            if isinstance(val, (list, tuple)):
                sub_pol = "negative" if any(k.lower().startswith(nk) for nk in ("negative", "neg", "负")) else ("positive" if any(k.lower().startswith(pk) for pk in ("positive", "pos", "正")) else polarity)
                if res := ComfyUIParser._trace_text(nodes, val, visited, depth + 1, polarity=sub_pol): return res

        # 5. 变长/多路合并节点 (conditioning1..N / text_a..d / prompt_1..5): 取合并后提示词
        if merged := ComfyUIParser._trace_merge_slots(inp, nodes, visited, depth + 1, polarity=polarity): return merged

        # 5b. 具名条件合并 (ConditioningConcat/Average: conditioning_to + conditioning_from)
        named: List[str] = []
        for k in ComfyUIParser._NAMED_COND_FIELDS:
            v = inp.get(k)
            if isinstance(v, (list, tuple)):
                if sub := ComfyUIParser._trace_text(nodes, v, visited, depth + 1, polarity=polarity): named.append(sub)
        if named: return ", ".join(dict.fromkeys(named))

        # 6. 通用回溯: 递归进入上游链接 (穿透 condition_carry 类节点: 采样器/Pipe/Bus/Loader)
        for k, v in inp.items():
            k_low = k.lower()
            if polarity == "positive":
                if k_low in ("negative", "negative_prompt", "text_negative") or k_low.startswith("negative_"):
                    continue
            elif polarity == "negative":
                if k_low in ("positive", "positive_prompt", "text_positive") or k_low.startswith("positive_"):
                    continue
            if isinstance(v, (list, tuple)):
                if k in _NON_TEXT_LINK_KEYS: continue  # 蒙版/图像链不产提示词
                if res := ComfyUIParser._trace_text(nodes, v, visited, depth + 1, polarity=polarity): return res
            elif (isinstance(v, str) and len(v) > 20 and ("," in v or " " in v)
                  and not _NON_PROMPT_KEY_RE.search(k)
                  and not _NON_PROMPT_VAL_RE.search(v.strip())):
                if polarity == "positive" and any(k_low.startswith(nk) for nk in ("neg", "负")):
                    continue
                if polarity == "negative" and any(k_low.startswith(pk) for pk in ("pos", "正")):
                    continue
                return v.strip()

        # 7. 兜底取 _widgets_values: 若文本编码器连线输入未产出文本, 取该节点自身填写的提示词控件文本 (排除 loader 与非提示词配置)
        if any(k in clow for k in ("textencode", "cliptext", "prompt", "text")) and "loader" not in clow and not any(nk in clow for nk in ("note", "markdown")):
            wv = inp.get("_widgets_values")
            if isinstance(wv, list):
                for item in wv:
                    if isinstance(item, str) and len(item.strip()) >= 5 and not _NON_PROMPT_VAL_RE.search(item.strip()) and not _NON_PROMPT_KEY_RE.search(item.strip()):
                        if item.lower() not in ("fixed", "randomize", "increment", "decrement", "enable", "disable", "cpu", "cuda", "fp16", "fp32", "bf16"):
                            return item.strip()

        return ""

    @staticmethod
    def _trace_dual_clip(inp: Dict[str, Any], nodes: Dict[str, Any], visited: set, depth: int = 0, polarity: Optional[str] = None) -> str:
        """SDXL/双 CLIP 提示词合并 (text_g+text_l / prompt_g+prompt_l / pos_g+pos_l)"""
        if depth > 200: return ""
        if polarity == "negative":
            field_pairs = (("neg_g", "neg_l"), ("negative_g", "negative_l"))
        else:
            field_pairs = (("text_g", "text_l"), ("prompt_g", "prompt_l"),
                           ("pos_g", "pos_l"), ("positive_g", "positive_l"))
        for gk, lk in field_pairs:
            if gk not in inp and lk not in inp: continue
            def _res(key: str) -> str:
                v = inp.get(key)
                if isinstance(v, str): return v.strip()
                if isinstance(v, (list, tuple)): return ComfyUIParser._trace_text(nodes, v, visited, depth + 1, polarity=polarity)
                return ""
            tg, tl = _res(gk), _res(lk)
            if tg or tl:
                return tg if (not tl or tg == tl) else f"{tg}\n{tl}".strip()
        return ""

    @staticmethod
    def _trace_merge_slots(inp: Dict[str, Any], nodes: Dict[str, Any], visited: set, depth: int = 0, polarity: Optional[str] = None) -> str:
        """合并节点 (变长/多路槽位) 取合并后提示词: 按族分组, 数字升序/字母序拼接去重"""
        if depth > 200: return ""
        families: Dict[str, List[Tuple[int, Any]]] = {}
        for k, v in inp.items():
            m = ComfyUIParser._SLOT_FAMILY_RE.match(k)
            if not m: continue
            fam, idx = m.group(1).lower(), m.group(2)
            order = int(idx) if idx.isdigit() else (1000 + ord(idx.lower()) - ord('a'))
            families.setdefault(fam, []).append((order, v))
        if not families: return ""

        if polarity == "positive":
            priority = ("positive", "conditioning", "cond", "text", "string", "prompt", "part")
        elif polarity == "negative":
            priority = ("negative", "conditioning", "cond", "text", "string", "prompt", "part")
        else:
            priority = ComfyUIParser._SLOT_FAMILY_PRIORITY

        for fam in priority:
            if fam not in families: continue
            parts: List[str] = []
            sub_pol = "negative" if fam == "negative" else ("positive" if fam == "positive" else polarity)
            for _, v in sorted(families[fam], key=lambda x: x[0]):
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
                elif isinstance(v, (list, tuple)):
                    if sub := ComfyUIParser._trace_text(nodes, v, visited, depth + 1, polarity=sub_pol): parts.append(sub)
            uniq = list(dict.fromkeys(parts))  # 去重保序 (合并节点常有重复分支)
            if uniq:
                sep = inp.get("delimiter", ", ") if isinstance(inp.get("delimiter"), str) else (", " if fam in ("conditioning", "cond", "positive", "negative") else "\n")
                return sep.join(uniq)
        return ""

    @staticmethod
    def _trace_model_and_loras(nodes: Dict[str, Any], link: Any, visited: Optional[set] = None, depth: int = 0) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        if depth > 200: return None, []
        if visited is None: visited = set()
        if not isinstance(link, (list, tuple)) or not link: return None, []
        sid = str(link[0])
        if sid in visited or sid not in nodes: return None, []
        visited.add(sid)
        inp = nodes[sid].get("inputs", {})
        model_name, lora = ComfyUIParser._extract_model_info(inp)
        loras = [lora] if lora else []
        loras.extend(ComfyUIParser._extract_lora_stack(inp))
        # 收集下一步候选链接: model 键 > 开关节点生效分支 > 透明节点穿透键 (后两组复用统一 helper, 与 _trace_num/_trace_str 同语义)
        candidates: List[Any] = []
        if isinstance(inp.get("model"), (list, tuple)):
            candidates.append(inp["model"])
        for lk in ("lora_stack", "loras", "lora_list", "stack"):
            if isinstance(inp.get(lk), (list, tuple)):
                candidates.append(inp[lk])
        candidates.extend(ComfyUIParser._pass_through_candidates(inp))
        for cand in candidates:
            # 每个分支用独立 visited 副本, 避免分支汇合点被误判为环
            pm, pl = ComfyUIParser._trace_model_and_loras(nodes, cand, set(visited), depth + 1)
            if pl:
                loras.extend(pl)
            if pm:
                model_name = pm
                break
        return model_name, loras

    # 数值/字符串部件源节点的取值字段 (随机种节点 / primitive / int=float 节点等)
    _NUM_VALUE_FIELDS = ("seed", "noise_seed", "seed_num", "rand_seed", "value", "int", "number", "float")
    _STR_VALUE_FIELDS = ("value", "string", "text", "sampler_name", "scheduler")

    @staticmethod
    def _resolve_num(nodes: Dict[str, Any], v: Any) -> Any:
        """数值部件 (steps/cfg/denoise 等) 被转成链接时, 回溯源节点取值"""
        return ComfyUIParser._trace_num(nodes, v) if isinstance(v, (list, tuple)) else v

    @staticmethod
    def _resolve_str(nodes: Dict[str, Any], v: Any) -> Optional[str]:
        """下拉部件 (sampler_name/scheduler 等) 被转成链接时, 回溯源节点取值"""
        if isinstance(v, str) and v.strip(): return v.strip()
        if isinstance(v, (list, tuple)):
            r = ComfyUIParser._trace_str(nodes, v)
            if r: return r
        return None

    @staticmethod
    def _resolve_seed(nodes: Dict[str, Any], inp: Dict[str, Any]) -> Any:
        """种子: 直取值 > 回溯链接到随机种节点 (easy seed/CR Seed/RandomNoise 等) > SamplerCustomAdvanced 的 noise 链接"""
        v = inp.get("seed")
        if v is None: v = inp.get("noise_seed")
        if isinstance(v, (list, tuple)): v = ComfyUIParser._trace_num(nodes, v)
        if v is None and isinstance(inp.get("noise"), (list, tuple)):
            v = ComfyUIParser._trace_num(nodes, inp["noise"])
        return v

    @staticmethod
    def _pass_through_candidates(inp: Dict[str, Any]) -> List[Any]:
        """开关/透明节点的穿透候选链接 (与 model 链回溯同一套语义)"""
        cands: List[Any] = []
        if "on_false" in inp or "on_true" in inp:
            _sw = inp.get("switch")
            order = ("on_true", "on_false") if (_sw is True or str(_sw).lower() == "true") else ("on_false", "on_true")
            cands += [inp[k] for k in order if isinstance(inp.get(k), (list, tuple))]
        cands += [inp[k] for k in ("anything", "model_in", "input") if isinstance(inp.get(k), (list, tuple))]
        return cands

    @staticmethod
    def _trace_num(nodes: Dict[str, Any], link: Any, visited: Optional[set] = None, depth: int = 0) -> Optional[float]:
        if depth > 200: return None
        if visited is None: visited = set()
        if not isinstance(link, (list, tuple)) or not link: return None
        sid = str(link[0])
        if sid in visited or sid not in nodes: return None
        visited.add(sid)
        inp = nodes[sid].get("inputs", {})
        for k in ComfyUIParser._NUM_VALUE_FIELDS:
            v = inp.get(k)
            if isinstance(v, bool): continue
            if isinstance(v, (int, float)): return v
            if isinstance(v, str) and v.strip():
                try: return float(v.strip()) if ("." in v or "e" in v.lower()) else int(v.strip())
                except ValueError: continue
            if isinstance(v, (list, tuple)):
                r = ComfyUIParser._trace_num(nodes, v, visited, depth + 1)
                if r is not None: return r
        for cand in ComfyUIParser._pass_through_candidates(inp):
            r = ComfyUIParser._trace_num(nodes, cand, set(visited), depth + 1)
            if r is not None: return r
        return None

    @staticmethod
    def _trace_str(nodes: Dict[str, Any], link: Any, visited: Optional[set] = None, depth: int = 0) -> Optional[str]:
        if depth > 200: return None
        if visited is None: visited = set()
        if not isinstance(link, (list, tuple)) or not link: return None
        sid = str(link[0])
        if sid in visited or sid not in nodes: return None
        visited.add(sid)
        inp = nodes[sid].get("inputs", {})
        for k in ComfyUIParser._STR_VALUE_FIELDS:
            v = inp.get(k)
            if isinstance(v, str) and v.strip(): return v.strip()
            if isinstance(v, (list, tuple)):
                r = ComfyUIParser._trace_str(nodes, v, visited, depth + 1)
                if r: return r
        for cand in ComfyUIParser._pass_through_candidates(inp):
            r = ComfyUIParser._trace_str(nodes, cand, set(visited), depth + 1)
            if r: return r
        return None

    @staticmethod
    def _trace_latent_size(nodes: Dict[str, Any], link: Any, visited: Optional[set] = None, depth: int = 0) -> Optional[str]:
        if depth > 200: return None
        if visited is None: visited = set()
        if not isinstance(link, (list, tuple)) or not link: return None
        sid = str(link[0])
        if sid in visited or sid not in nodes: return None
        visited.add(sid)
        inp = nodes[sid].get("inputs", {})
        if size := ComfyUIParser._extract_size(inp): return size
        for k in ["samples", "latent_image", "latent", "pixels", "image"]:
            if k in inp and isinstance(inp[k], (list, tuple)) and (res := ComfyUIParser._trace_latent_size(nodes, inp[k], visited, depth + 1)): return res
        for cand in ComfyUIParser._pass_through_candidates(inp):
            if res := ComfyUIParser._trace_latent_size(nodes, cand, set(visited), depth + 1):
                return res
        return None

    @staticmethod
    def _extract_size(inp: Dict[str, Any]) -> Optional[str]:
        w = safe_int(inp.get("width", inp.get("empty_latent_width")))
        h = safe_int(inp.get("height", inp.get("empty_latent_height")))
        if w is not None and h is not None and w > 0 and h > 0:
            return f"{w}x{h}"
        return None

    @staticmethod
    def _is_base_model_filename(v: str) -> bool:
        """字符串是否为底模文件名形态: 模型扩展名且不含 VAE/超分/检测特征"""
        low = v.lower()
        return low.endswith(ComfyUIParser._MODEL_FILE_EXTS) and not any(
            k in low for k in ComfyUIParser._NON_BASE_MODEL_HINTS)

    @staticmethod
    def _extract_model_info(inp: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        # 仅接受字符串值: 字段被转成链接输入时 (API 格式为 [node, slot]) 不得当作文件名
        model_name = None
        for k in ComfyUIParser._MODEL_NAME_FIELDS:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                model_name = v.strip()
                break
        # 自定义加载器以 "model" 为部件键 (SeedVR2LoadDiTModel 等): 仅接受底模文件名形态的字符串
        if not model_name and isinstance(inp.get("model"), str) \
                and ComfyUIParser._is_base_model_filename(inp["model"].strip()):
            model_name = inp["model"].strip()
        if not model_name and isinstance(inp.get("_widgets_values"), list):
            for w in inp["_widgets_values"]:
                if isinstance(w, str) and ComfyUIParser._is_base_model_filename(w):
                    model_name = w.strip()
                    break
        lora = None
        for lk in ("lora_name", "lora"):
            if isinstance(inp.get(lk), str) and inp[lk].strip():
                name = inp[lk].strip()
                if name.lower() not in ("none", ""):
                    lora = {
                        "name": name,
                        "strength_model": safe_float(inp.get("strength_model", inp.get("strength", 1.0))),
                        "strength_clip": safe_float(inp.get("strength_clip", inp.get("strength", 1.0)))
                    }
                break
        return model_name, lora

    @staticmethod
    def _extract_lora_stack(inp: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从 LoRA Stack / Power Lora Loader 节点提取多个 LoRA"""
        # 快路径: 无任何 lora_ 前缀键的节点 (绝大多数) 跳过下方 150 次编号键探测
        # (500 节点图实测 _find_any_model_and_loras 兜底扫描的热点即在此, 11ms -> 4ms)
        if not any(isinstance(k, str) and k.startswith(("lora_name_", "lora_")) for k in inp):
            return []
        stack_loras = []
        for i in range(1, 51):
            for pattern in (f"lora_name_{i}", f"lora_{i}_name", f"lora_{i}"):
                if isinstance(inp.get(pattern), str) and inp[pattern].strip():
                    name = inp[pattern].strip()
                    if name.lower() not in ("none", ""):
                        wt = safe_float(inp.get(f"lora_wt_{i}"))
                        m_str = safe_float(inp.get(f"model_str_{i}", inp.get(f"lora_{i}_model_strength", inp.get(f"lora_strength_{i}", 1.0))))
                        c_str = safe_float(inp.get(f"clip_str_{i}", inp.get(f"lora_{i}_clip_strength", m_str)))
                        if wt is not None:
                            eff_m = round(wt * (m_str if m_str is not None else 1.0), 4)
                            eff_c = round(wt * (c_str if c_str is not None else 1.0), 4)
                        else:
                            eff_m = m_str
                            eff_c = c_str
                        stack_loras.append({
                            "name": name,
                            "strength_model": eff_m,
                            "strength_clip": eff_c,
                        })
                    break
        return stack_loras

    @staticmethod
    def _find_any_latent_size(nodes: Dict[str, Any]) -> Optional[str]:
        for n in nodes.values():
            if not isinstance(n, dict): continue
            ctype = str(n.get("class_type", "")).lower()
            if "cliptext" in ctype or "textencode" in ctype: continue
            size = ComfyUIParser._extract_size(n.get("inputs", {}))
            if size: return size
        return None

    @staticmethod
    def _find_any_model_and_loras(nodes: Dict[str, Any]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        model_name, loras = None, []
        for n in nodes.values():
            if not isinstance(n, dict): continue
            inp = n.get("inputs", {})
            m, lora = ComfyUIParser._extract_model_info(inp)
            model_name = model_name or m
            if lora: loras.append(lora)
            st_loras = ComfyUIParser._extract_lora_stack(inp)
            if st_loras: loras.extend(st_loras)
        return model_name, loras

    @staticmethod
    def _extract_clip_skip(nodes: Dict[str, Any], first_node: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """提取 Clip Skip: 首个采样器输入 > 扫描加载器/CLIPSetLastLayer 节点"""
        clip_keys = ("clip_skip", "stop_at_clip_layer", "base_clip_skip", "refiner_clip_skip")
        if first_node:
            inp = first_node.get("inputs", {})
            for k in clip_keys:
                if (cs := ComfyUIParser._resolve_num(nodes, inp.get(k))) is not None:
                    v = safe_int(cs)
                    if v is not None and v != 0: return abs(v)
        for n in nodes.values():
            if not isinstance(n, dict): continue
            inp = n.get("inputs", {})
            for k in clip_keys:
                if k in inp:
                    cs = ComfyUIParser._resolve_num(nodes, inp.get(k))
                    if cs is not None:
                        v = safe_int(cs)
                        if v is not None and v != 0: return abs(v)
        return None

    @staticmethod
    def _resolve_widget(nodes: Dict[str, Any], v: Any) -> Any:
        """部件值: 非链接直返; 链接引用时回溯数值/字符串源节点, 避免 ['47',0] 泄漏进卡片"""
        if not isinstance(v, (list, tuple)): return v
        num = ComfyUIParser._trace_num(nodes, v)
        return num if num is not None else ComfyUIParser._trace_str(nodes, v)

    _NEG_KEYWORDS = re.compile(r"\b(worst quality|low quality|bad anatomy|bad hands|lowres|watermark|censor|mutated|deformed|blurry|ugly)\b", re.I)
    _POS_KEYWORDS = re.compile(r"\b(masterpiece|best quality|highres|absurdres|1girl|1boy|extremely detailed)\b", re.I)

    @staticmethod
    def _fallback_scan_prompts(nodes: Dict[str, Any]) -> Tuple[str, str]:
        pos_cands: List[str] = []
        neg_cands: List[str] = []
        other_cands: List[str] = []

        for nid, n in nodes.items():
            if not isinstance(n, dict): continue
            clow = str(n.get("class_type", "")).lower()
            if any(nk in clow for nk in ("note", "markdown")):
                continue
            inp = n.get("inputs") or {}
            title = ((n.get("_meta") or {}).get("title") or n.get("title") or "").lower()

            for k in ComfyUIParser._PROMPT_TEXT_FIELDS:
                v = inp.get(k)
                if not (isinstance(v, str) and v.strip()): continue
                text = v.strip()
                klow = k.lower()

                if any(pk in title or pk in clow or pk in klow for pk in ("positive", "pos", "正向", "正面")):
                    if text not in pos_cands: pos_cands.append(text)
                elif any(nk in title or nk in clow or nk in klow for nk in ("negative", "neg", "负向", "负面")):
                    if text not in neg_cands: neg_cands.append(text)
                else:
                    if text not in other_cands: other_cands.append(text)

        pos = pos_cands[0] if pos_cands else ""
        neg = neg_cands[0] if neg_cands else ""

        if not pos and other_cands:
            for c in list(other_cands):
                if not ComfyUIParser._NEG_KEYWORDS.search(c) or ComfyUIParser._POS_KEYWORDS.search(c):
                    pos = c
                    other_cands.remove(c)
                    break
        if not neg and other_cands:
            for c in list(other_cands):
                if ComfyUIParser._NEG_KEYWORDS.search(c) and not ComfyUIParser._POS_KEYWORDS.search(c):
                    neg = c
                    other_cands.remove(c)
                    break

        if not pos and other_cands:
            pos = other_cands.pop(0)
        if not neg and other_cands:
            neg = other_cands.pop(0)

        # 极性校验证: 防止正负颠倒 (如因字典迭代顺序或未标记节点导致负向被误认作正向)
        if pos and neg:
            pos_neg_score = len(ComfyUIParser._NEG_KEYWORDS.findall(pos))
            pos_pos_score = len(ComfyUIParser._POS_KEYWORDS.findall(pos))
            neg_neg_score = len(ComfyUIParser._NEG_KEYWORDS.findall(neg))
            neg_pos_score = len(ComfyUIParser._POS_KEYWORDS.findall(neg))
            if pos_neg_score > pos_pos_score and neg_pos_score > neg_neg_score:
                pos, neg = neg, pos

        return pos, neg

    # 参数写回映射: SamplerParameters 属性 → prompt 图字段名
    _WIDGET_FIELDS = (("steps", "steps", int), ("cfg_scale", "cfg", float), ("seed", "seed", int),
                      ("sampler_name", "sampler_name", str), ("scheduler", "scheduler", str),
                      ("denoising_strength", "denoise", float))
    _TEXT_WRITE_FIELDS = ("text", "positive", "prompt", "value", "string", "populated_text", "wildcard_text")
    _SIZE_RE = re.compile(r"\s*(\d+)\s*[xX\u00d7*]\s*(\d+)\s*")

    @staticmethod
    def update_comfy_prompt_text(prompt_graph: Dict[str, Any], positive_text: str, negative_text: str,
                                  params: Optional[SamplerParameters] = None,
                                  params_snapshot: Optional[SamplerParameters] = None) -> Dict[str, Any]:
        """把编辑后的提示词与采样参数写回 prompt 图 (返回新图, 不修改入参)"""
        new_graph = json.loads(json.dumps(prompt_graph))
        _, sampler_node = ComfyUIParser._find_primary_sampler(new_graph)
        if not sampler_node:
            # 无标准采样器 (BananaForgeText2Img/纯 TiledDiffusion 等): 对称于读取 _fallback_scan_prompts,
            # 就地写回持有文本的节点, 否则编辑静默丢失 (真实数据 类型A-1, 59/1135 张)
            ComfyUIParser._fallback_write_prompts(new_graph, positive_text, negative_text)
            return new_graph
        inp = sampler_node.get("inputs")
        if not isinstance(inp, dict): inp = sampler_node["inputs"] = {}

        # 1) 参数写回: 仅写"用户真正改动"的字段 — 与 params_snapshot (读取时冻结) 比较逐字段判定,
        #    未被编辑的字段一律不写, 多阶段流水线图 (二采/Refiner) 的其他采样器/latent 节点不被
        #    汇总值污染 (anima 类图: 主生成 denoise=1.0 不得冲掉活二采节点的 0.2)。
        #    无快照 (旧调用方) 时退回旧行为, 但保留"数值等值不写"的表示漂移防护。
        if params:
            snap = params_snapshot
            for attr, key, cast in ComfyUIParser._WIDGET_FIELDS:
                val = getattr(params, attr, None)
                if val is None:
                    continue
                # 对称于读取的 _flatten_sampler_inputs: 参数可能不在采样器自身, 而在 guider/
                # sampler/sigmas/noise 子节点 (SamplerCustomAdvanced 系)。owner 定位真正持有
                # 该键的 inputs, 否则子节点参数的用户编辑静默丢失 (正负提示词写回同款处理)。
                owner = ComfyUIParser._resolve_alias_input(new_graph, inp, key)
                wkey = key
                if owner is None and key == "seed":
                    # 噪声源节点 (RandomNoise 等) 的种子键是 noise_seed (读取端 flatten 同款映射)
                    owner = ComfyUIParser._resolve_alias_input(new_graph, inp, "noise_seed")
                    wkey = "noise_seed"
                if owner is None or isinstance(owner.get(wkey), (list, tuple)):
                    continue  # 采样器与别名链均不持有该键, 或该部件已被转成链接输入, 不冒写
                new_val = cast(val) if cast is not str else str(val)
                if snap is not None:
                    # 有快照: 只写用户改动的字段 (数值等值比较, int/float 视为同值)
                    old_val = getattr(snap, attr, None)
                    if old_val is None:
                        continue  # 读取时汇总不到的字段 (可能来自别的节点), 不冒写
                    try:
                        if cast is not str and float(old_val) == float(new_val):
                            continue
                        if cast is str and str(old_val).strip() == str(new_val).strip():
                            continue
                    except (TypeError, ValueError):
                        if old_val == new_val:
                            continue
                elif isinstance(owner[wkey], (int, float)) and not isinstance(owner[wkey], bool) \
                        and isinstance(new_val, (int, float)) and owner[wkey] == new_val:
                    continue  # 无快照兜底: 数值等值不写 (表示漂移防护)
                owner[wkey] = new_val
            # 尺寸写回 latent 节点: 同样仅当用户真的改了尺寸
            if params.size and isinstance(inp.get("latent_image"), (list, tuple)):
                size_changed = True
                if snap is not None:
                    size_changed = (snap.size or "") != (params.size or "")
                if size_changed:
                    li = (new_graph.get(str(inp["latent_image"][0])) or {}).get("inputs") or {}
                    if (m := ComfyUIParser._SIZE_RE.fullmatch(params.size)) and "width" in li:
                        li["width"], li["height"] = int(m.group(1)), int(m.group(2))

        # 2) 提示词写回: 正负各自方向感知定位文本节点, 防越界串扰 + 防共用节点覆盖。
        #    先 positive 后 negative, positive 写过的槽位记入 written; negative 遇同槽位跳过 ->
        #    Flux Kontext 等“正负共用同一 CLIPTextEncode”时保证 positive 编辑不被 negative 覆盖 (类型A-2a)。
        written: set = set()
        wrote_any = False
        for key, txt in (("positive", positive_text), ("negative", negative_text)):
            if txt is None: continue
            # 对称于读取的 _flatten_sampler_inputs: positive/negative 可能不在采样器自身, 而在 guider
            # 子节点 (SamplerCustomAdvanced+CFGGuider)。owner 定位真正持有该键的 inputs, 否则静默丢编辑。
            owner = ComfyUIParser._resolve_alias_input(new_graph, inp, key)
            link = owner.get(key) if owner else None
            # BasicGuider (cfg=1 单条件): positive 经 guider.conditioning 传递, 无独立 positive 键 (类型A-3)
            if link is None and key == "positive":
                cond_owner = ComfyUIParser._resolve_alias_input(new_graph, inp, "conditioning")
                if cond_owner is not None:
                    owner, link = cond_owner, cond_owner.get("conditioning")
            if isinstance(link, (list, tuple)) and link:
                # polarity 方向感知: 遍历时跳过相反极性键, 防 ControlNetApplyAdvanced 越界写到另一极性节点 (类型A-2b)
                loc = ComfyUIParser._write_text_at(new_graph, str(link[0]), txt, polarity=key, forbidden=written)
                if loc: written.add(loc); wrote_any = True
            elif isinstance(link, str) and owner is not None:
                # 采样器/guider 直接内联字符串提示词 (部分第三方 all-in-one 节点): 就地写回持有该字符串的节点
                owner[key] = txt; wrote_any = True
        # 采样器存在但无任何链接/内联槽位可写 (畸形图如 KSampler 未连 conditioning: 读取端靠
        # _fallback_scan_prompts 取到提示词): 对称地就地写回, 否则编辑静默丢失
        if not wrote_any and (positive_text is not None or negative_text is not None):
            ComfyUIParser._fallback_write_prompts(new_graph, positive_text, negative_text)
        return new_graph

    @staticmethod
    def sync_workflow_prompt_widgets(workflow: Any, old_prompt_graph: Optional[Dict[str, Any]],
                                     updated_prompt_graph: Optional[Dict[str, Any]]) -> None:
        """R25: 把 API prompt 图的文本编辑按节点 id 对齐同步进 UI workflow 的对应 widget (原地)。

        以 update_comfy_prompt_text 的输出为唯一事实源做差分, 只同步变化的字符串字段;
        不在 UI 侧重跑节点定位启发式 — UI 图含显示节点 (如 easy showAnything 的命名 widget
        镜像), 启发式会写偏到镜像节点而漏掉真正的提示词输入节点 (真实 ComfyUI_temp 图实测)。
        widgets_values 中与旧文本相同的字符串被新文本替换; widgets_values_named 按字段名同步;
        数值参数 widget 不同步 (API prompt 块已承载)。非 UI 格式 (无 nodes 列表) 自然跳过。"""
        if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), list):
            return
        ui_by_id: Dict[str, Dict[str, Any]] = {}
        for n in workflow["nodes"]:
            if isinstance(n, dict) and n.get("id") is not None:
                ui_by_id[str(n["id"])] = n
        for nid, new_node in (updated_prompt_graph or {}).items():
            old_node = (old_prompt_graph or {}).get(nid)
            ui_node = ui_by_id.get(str(nid))
            if not isinstance(ui_node, dict) or not isinstance(new_node, dict):
                continue
            new_inputs = new_node.get("inputs") or {}
            old_inputs = (old_node or {}).get("inputs") or {}
            for field, new_val in new_inputs.items():
                old_val = old_inputs.get(field)
                if not isinstance(new_val, str) or not isinstance(old_val, str) or new_val == old_val:
                    continue
                named = ui_node.get("widgets_values_named")
                if isinstance(named, dict) and isinstance(named.get(field), str):
                    named[field] = new_val
                wv = ui_node.get("widgets_values")
                if isinstance(wv, list):
                    for i, w in enumerate(wv):
                        if isinstance(w, str) and w == old_val:
                            wv[i] = new_val
                            break

    @staticmethod
    def _write_text_at(nodes: Dict[str, Any], nid: str, txt: str, polarity: Optional[str] = None,
                       forbidden: Optional[set] = None, depth: int = 0) -> Optional[Tuple[str, str]]:
        """写入文本: 沿 conditioning 传递链找到第一个持有文本字段的节点即写入, 返回写入的 (节点id, 字段名)。
        用显式栈 + visited 迭代取代旧的 depth>6 递归上限 (读取 _trace_text 可回溯任意深度)。
        polarity: 'positive'/'negative' — 方向感知, 遍历时跳过相反极性的链接键, 防止在 ControlNetApplyAdvanced
                  等同时持有正/负两条输入链的节点上越界写到另一极性的文本节点。
        forbidden: 已被另一极性写入的 (节点id, 字段名) 集合 — 跳过之, 防止 Flux Kontext 等“正负共用同一
                  CLIPTextEncode”的工作流里 negative 写回覆盖掉 positive 的编辑。
        visited 防环, budget 限制总访问节点数防止病态图失控 (保留 depth 形参仅为签名兼容)。"""
        forbidden = forbidden or set()
        opposite = {"positive": "negative", "negative": "positive"}.get(polarity or "")
        stack: List[str] = [str(nid)]
        visited: set = set()
        budget = 10000
        while stack and budget > 0:
            cur = stack.pop()
            if cur in visited: continue
            visited.add(cur)
            budget -= 1
            node = nodes.get(cur)
            if not isinstance(node, dict): continue
            # 对称于读取端: 静音(2)/旁路(4)节点不参与出图, 不写入也不穿透
            # (真实缺陷: flux_kontext 的旁路 ADetailer 正向节点曾被负向写回清空)
            if node.get("mode") in (2, 4): continue
            inp = node.get("inputs") or {}
            # 1a) 优先：若节点自身有匹配当前极性的字符串字段 (如 negative, positive)
            if polarity and polarity in inp and isinstance(inp[polarity], str):
                if (cur, polarity) not in forbidden:
                    if str(inp[polarity]).strip() != txt.strip():  # 内容未变化(空白归一)则保留原字节 (尾换行等)
                        inp[polarity] = txt
                        if isinstance(inp.get("_widgets_values"), list) and inp["_widgets_values"] and isinstance(inp["_widgets_values"][0], str):
                            inp["_widgets_values"][0] = txt
                    return (cur, polarity)
            # 1b) 文本字段为字符串 -> 直接写入 (对称于读取 _trace_text 步骤2 的 str 分支)
            for k in ComfyUIParser._TEXT_WRITE_FIELDS:
                if opposite and (opposite in k.lower() or any(k.lower().startswith(p) for p in ("neg" if opposite == "negative" else "pos", "负" if opposite == "negative" else "正"))):
                    continue
                if isinstance(inp.get(k), str):
                    if (cur, k) in forbidden: continue  # 已被另一极性占用, 换下一个字段/节点, 不覆盖
                    if str(inp[k]).strip() != txt.strip():  # 内容未变化(空白归一)则保留原字节 (尾换行等)
                        inp[k] = txt
                        if isinstance(inp.get("_widgets_values"), list) and inp["_widgets_values"] and isinstance(inp["_widgets_values"][0], str):
                            inp["_widgets_values"][0] = txt
                    return (cur, k)
            # 2) 追溯所有可能承载提示词的链接 (与读取 _trace_text 步骤3/5 对称): 穿透 text1/conditioning1
            #    等编号槽位、CR Text Concatenate/PromptBuilder/通配符/PowerLora 等第三方传递字段。
            #    排除图像/蒙版/latent 等非文本链; 方向感知时额外跳过相反极性键 (防止正负越界串扰)。
            #    逆序压栈: 让 DFS 优先探索靠前字段, 贴合读取的字段优先级。
            for k in reversed(list(inp.keys())):
                k_low = k.lower()
                if opposite and (opposite in k_low or any(k_low.startswith(p) for p in ("neg" if opposite == "negative" else "pos", "负" if opposite == "negative" else "正"))):
                    continue
                v = inp.get(k)
                if isinstance(v, (list, tuple)) and v and k not in _NON_TEXT_LINK_KEYS:
                    stack.append(str(v[0]))
        return None

    @staticmethod
    def _fallback_write_prompts(nodes: Dict[str, Any], positive_text: Optional[str], negative_text: Optional[str]) -> None:
        """无采样器时对称于读取 _fallback_scan_prompts 的写回: 按相同的节点遍历顺序 + 字段顺序 +
        极性判定收集文本槽位, 极性匹配优先 (positive→正向标记槽/中性槽, negative→负向标记槽),
        无匹配标记时回退旧的槽位顺序分配 (全中性槽位时与旧行为逐字节一致), 不静默丢编辑。
        覆盖 BananaForgeText2Img(.prompt)、纯 TiledDiffusion 等无 KSampler 的工作流 (读取端靠
        _fallback_scan_prompts 取到提示词, 写回端必须对称, 否则编辑静默丢失)。
        Note/Markdown 节点对称跳过 (读取端不读它们, 写入会毁掉用户笔记)。
        静音(2)/旁路(4)节点不参与出图: 仅当活节点里凑不满槽位时才允许旁路节点兜底 (防止 flux_kontext 类
        旁路 ADetailer 残件被误写/清空, 同时不破坏"全图皆旁路"时编辑仍有处的极端情况)。"""
        active_slots: List[Tuple[Dict[str, Any], str, Optional[str]]] = []
        bypass_slots: List[Tuple[Dict[str, Any], str, Optional[str]]] = []
        seen: set = set()
        for n in nodes.values():
            if not isinstance(n, dict): continue
            clow = str(n.get("class_type", "")).lower()
            if any(nk in clow for nk in ("note", "markdown")):
                continue
            slot_list = bypass_slots if n.get("mode") in (2, 4) else active_slots
            inp = n.get("inputs") or {}
            title = ((n.get("_meta") or {}).get("title") or n.get("title") or "").lower()
            # 与读取 _fallback_scan_prompts 用同一套 _PROMPT_TEXT_FIELDS 键 + 同序遍历 + 同款极性
            # 判定 (正向标记优先于负向), 保证读写槽位对称
            for k in ComfyUIParser._PROMPT_TEXT_FIELDS:
                v = inp.get(k)
                if not (isinstance(v, str) and v.strip() and v.strip() not in seen):
                    continue
                seen.add(v.strip())
                klow = k.lower()
                if any(pk in title or pk in clow or pk in klow for pk in ("positive", "pos", "正向", "正面")):
                    polarity = "positive"
                elif any(nk in title or nk in clow or nk in klow for nk in ("negative", "neg", "负向", "负面")):
                    polarity = "negative"
                else:
                    polarity = None
                slot_list.append((inp, k, polarity))
        slots = active_slots if len(active_slots) >= 2 or not bypass_slots else active_slots + bypass_slots
        # 极性匹配优先: 全中性槽位时 pos=slots[0], neg=第一个未占槽, 与旧 slots[0]/slots[1] 一致;
        # slots[0] 为负向标记槽时不再把 positive 写进去 (读取端把它判为负向, 写入即正负对调损毁)
        pos_slot = next((s for s in slots if s[2] == "positive"), None) \
            or next((s for s in slots if s[2] is None), None) \
            or (slots[0] if slots else None)
        neg_slot = next((s for s in slots if s[2] == "negative" and s is not pos_slot), None)
        if neg_slot is None and len(slots) >= 2:
            neg_slot = next((s for s in slots if s is not pos_slot), None)
        if positive_text is not None and pos_slot is not None:
            pos_slot[0][pos_slot[1]] = positive_text
        if negative_text is not None and neg_slot is not None:
            neg_slot[0][neg_slot[1]] = negative_text
