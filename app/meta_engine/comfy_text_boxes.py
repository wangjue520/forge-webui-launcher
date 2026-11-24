# -*- coding: utf-8 -*-
"""
comfy_text_boxes.py —— 把 ComfyUI 节点图里的「每一个提示词文本框」单独列出来。

为什么单独做这一层，而不是继续往 ComfyUIParser._trace_text 里加规则：

  _trace_text 的职责是「给主采样器拼出一条正向、一条负向」，所以它到处是
  「先命中先返回」——一个节点上有两段文本只留一段；合并节点拼成一坨之后
  分不清哪段来自哪个框；第二个采样器 / Detailer 自己的提示词不进主结果；
  没接到采样器的文本框干脆不出现。用户拉了五六个文本框，页面上只看到其中
  两三个，就是这么来的。

  这里换一个问法：不问「主提示词是什么」，而问「图里有哪些文本框、每个框
  流到了哪儿」。做法是从每个消费条件的节点（采样器 / Detailer / 放大）的
  正负输入出发，沿所有可能承载文本的连线一路走到上游尽头，路上遇到的字符串
  输入全部记下来；走不到的文本框再做一次全图扫描，标成「未接入」。

  认节点不靠类名名单：连线往不往上走，只看这个输入键是不是明显不承载文本
  （model / vae / image / mask / latent …）；字符串算不算提示词，只看
  「键名像不像 + 内容像不像」。名单里没有的第三方节点照样能穿过去。

输入是 API 格式的节点图（PNG 的 prompt 块，或者由 workflow 转译出来的同构图）：
    {节点id: {"class_type": str, "inputs": {键: 值 或 [上游节点id, 输出槽]}, "_meta": {"title": str}}}

Reroute / Primitive / SetNode·GetNode / 子图在 API 格式里已经被 ComfyUI 前端
解掉了（子图内部节点的 id 形如 "12:3"），所以这里不需要专门处理；只有
「只有 workflow、没有 prompt」的图（直接拖进来的 .json）才会遇到它们，
那条路由 ComfyUIParser._workflow_to_nodes 先转译成同构图再进来。
"""
import re
from typing import Any, Dict, List, Optional, Tuple

POSITIVE, NEGATIVE, BOTH, UNKNOWN = "positive", "negative", "both", "unknown"

# 文本框的状态
USED = "used"            # 沿连线能走到某个采样器 / Detailer，确实参与了出图
FLOATING = "floating"    # 图里有采样器，但这个框没接上去
DISABLED = "disabled"    # 节点被静音 / 旁路（只在 workflow 里看得到）
UNSURE = "unknown"       # 图里认不出采样器，没法判断接没接上

# ---------------------------------------------------------------- 键名判定

# 键名自带的正负标记。neg 必须先于 pos 判（"negative" 里不含 "pos"，但顺序固定更稳妥）；
# pos 后面要求分隔符，免得把 position / pose / post 认成正向
_NEG_KEY_RE = re.compile(r"negative|(^|[_\-\s.])neg([_\-\s.\d]|$)|(^|_)uncond|负向|负面|反向|负提示|反提示", re.I)
_POS_KEY_RE = re.compile(r"positive|(^|[_\-\s.])pos([_\-\s.\d]|$)|正向|正面|正提示", re.I)


def key_polarity(key: Any) -> Optional[str]:
    """输入键名（或节点标题）自带的正负含义；看不出来返回 None。"""
    k = str(key or "")
    if _NEG_KEY_RE.search(k):
        return NEGATIVE
    if _POS_KEY_RE.search(k):
        return POSITIVE
    return None


# 明显不承载提示词文本的连线：顺着它们往上走只会走进模型加载器、图像处理链，
# 更糟的是会从二次采样器的 latent 走回第一个采样器，把前一阶段的提示词
# 错记到后一阶段头上。clip_l / clip_g 是文本编码器的文本输入，要放行。
_NON_TEXT_LINK_RE = re.compile(
    r"^(model|clip|vae|images?\d*|pixels|masks?\d*|latents?\d*|samples|noise|sigmas|sampler|"
    r"control_?net|upscale_model|ipadapter|insightface|audio|video|frames?|"
    r"seed|noise_seed|steps|cfg|denoise|width|height|batch_size|strength|scheduler|sampler_name|"
    r"clip_vision.*|style_model|bbox.*|segm.*|sam_.*|.*detector.*|.*_stack|"
    r".*_model|model_.*|.*_images?|image_.*|.*_masks?|mask_.*|.*_latents?|latent_.*|"
    r".*_vae|vae_.*|.*_clip|clip_(?!l$|g$).*|.*_seed|.*_strength|t5)$", re.I)

# 采样器 / Detailer 自己身上、键名不带正负标记但可能承载条件的输入
# （guider、各种 pipe、kijai 系的 text_embeds、Impact 的 regional_prompts …）
_ROOT_LINK_RE = re.compile(r"guider|pipe|context|tuple|embed|cond|regional|prompt|text", re.I)

# 值是字符串、但肯定不是提示词的键：文件名 / 路径 / 分隔符 / 查找替换 / 下拉选项 …
# 只收「值可能长得像一句话」的那些，单个词的下拉选项靠内容判定就能挡掉
_STR_KEY_DENY_RE = re.compile(
    r"_names?$|_path|_file|_dir|^file|^path|^folder|^lora|^vae|^ckpt|^unet|^model$|"
    r"^temp_str|^random_template|filename|sampler|scheduler|"
    r"delimiter|separator|^sep$|^find|^replace|^search|regex|pattern|"
    r"^mode$|_mode$|method|^type$|_type$|format|device|dtype|precision|preset|resolution|aspect|"
    r"interpretation|normalization|translate|language|^lang$|^url|_url$|api_?key|^token$|"
    r"^font|colou?r|interpolation|operation|^action$|^select|log_prompt|"
    r"^title$|^name$|^label$|^comment$|^note|extension|codec|^styles?$|"
    r"^variable|^var_?name|^key$|^id$|^preview|^save|^speak|"
    r"json|expression|formula|sigmas|^code$|script|schema", re.I)

# 键名分词后命中这些词，就认定是文本输入（text_g / prompt_2 / positive_prompt / string_a …）
_TEXT_TOKENS = frozenset({
    "text", "texts", "string", "strings", "str", "prompt", "prompts", "caption", "tags", "lyrics",
    "value", "positive", "negative", "pos", "neg", "uncond", "wildcard", "populated",
    "llama", "bert",
    "文本", "提示词", "提示", "正面", "负面", "正向", "负向", "反向",
})
# 多文本编码器 (Flux / SD3 / HiDream / HunyuanDiT) 的字段名带数字，分词会被拆散，整键比对
_TEXT_EXACT_KEYS = frozenset({"clip_l", "clip_g", "t5xxl", "mt5xl", "byt5", "umt5"})
_TEXT_SUBSTR = ("prompt", "caption", "wildcard")
_KEY_TOKEN_RE = re.compile(r"[a-z]+|\d+|[一-鿿]+")


def is_text_key(key: Any) -> bool:
    k = str(key or "").lower()
    if not k or k.startswith("_"):
        return False
    if _STR_KEY_DENY_RE.search(k):
        return False
    if k in _TEXT_EXACT_KEYS:
        return True
    if any(s in k for s in _TEXT_SUBSTR):
        return True
    return any(t in _TEXT_TOKENS for t in _KEY_TOKEN_RE.findall(k))


# ---------------------------------------------------------------- 内容判定

# 文件名 / 路径 / 纯数字 / 尺寸：不带逗号和换行的单行串才按这些形态排除，
# 免得把「…, model.safetensors 风格」这种真提示词也挡掉
_FILE_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{1,10}$")
_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|[\\/]{1,2}[\w.\-]+[\\/]|https?://)")
_NUMBER_RE = re.compile(r"^[-+]?\d+(\.\d+)?$")
_DIMENSION_RE = re.compile(r"^\d+\s*[x×*]\s*\d+$", re.I)
# 纯数字列表（ManualSigmas 的 "1.0, 0.99, 0.5, 0.0"）
_NUMBER_LIST_RE = re.compile(r"^[-+]?\d+(\.\d+)?(\s*[,;\s]\s*[-+]?\d+(\.\d+)?)+$")
# 代码 / 配置：JSON 对象、函数调用式的数学表达式、正则
_CODE_LIKE_RE = re.compile(r"^\s*[\[{]\s*[\"{\[]|\b(max|min|round|floor|ceil|abs|int|float)\(|\(\?[:=!]|\{\{\w+\}\}")
_HAS_WORD_RE = re.compile(r"[0-9A-Za-zÀ-￿]")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_PROMPT_SYNTAX_RE = re.compile(r"<lora:|\(.+:\s*-?\d|__\w+__|\{[^{}]*\|[^{}]*\}", re.I)

# 下拉选项 / 开关值：出现在文本键里也不算提示词（PrimitiveString 接到采样器名下拉框这种）
_ENUM_WORDS = frozenset({
    "none", "true", "false", "enable", "disable", "enabled", "disabled", "fixed", "randomize",
    "increment", "decrement", "auto", "default", "on", "off", "yes", "no", "cpu", "cuda", "gpu",
    "fp16", "fp32", "bf16", "comfy", "comfy++", "a1111", "compel", "down_weight", "mean", "length",
    "length+mean", "populate", "reproduce", "baked vae", "hide", "show", "preview", "save",
    "output", "undefined", "null", "n/a", "text", "prompt", "string", "positive", "negative",
    "select the lora to add to the text", "select the wildcard to add to the text",
    "euler", "euler_ancestral", "euler a", "heun", "dpm_2", "dpm_2_ancestral", "lms", "ddim",
    "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_sde", "dpmpp_3m_sde", "dpmpp_2s_ancestral", "uni_pc",
    "unipc", "lcm", "res_multistep", "normal", "karras", "exponential", "simple", "sgm_uniform",
    "beta", "ddim_uniform", "linear_quadratic", "kl_optimal",
})


def value_ok(value: Any) -> bool:
    """这个字符串有没有可能是提示词（只排除肯定不是的）。"""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not _HAS_WORD_RE.search(s) or (len(s) < 2 and not _CJK_RE.search(s)):
        return False      # 空串、纯标点（合并节点的 ", " 分隔符）、单个字母数字；单个汉字可以是提示词
    if s.lower() in _ENUM_WORDS:
        return False
    if _NUMBER_RE.match(s) or _DIMENSION_RE.match(s) or _NUMBER_LIST_RE.match(s):
        return False
    if "\n" not in s and "," not in s and (_FILE_EXT_RE.search(s) or _PATH_RE.match(s)):
        return False
    return True


def looks_like_prompt(s: str) -> bool:
    """宽松判定：像一句提示词（有逗号 / 换行 / 三个词以上 / 中日韩文字 / 权重语法）。

    两个词不算：文本类节点上的下拉选项经常是两个词（"E minor"、"true (tiled)"）。
    """
    s = s.strip()
    return bool(re.search(r"[,，、\n]", s) or len(s.split()) >= 3
                or _CJK_RE.search(s) or _PROMPT_SYNTAX_RE.search(s))


def strongly_prompt(s: str) -> bool:
    """严格判定：键名和节点类型都提供不了线索时，内容得足够像提示词才收。

    宽松判定会把 "1024 x 1024 (square)"、"style transfer precise"、
    "Roger (male, american)" 这类带空格带逗号的下拉选项也收进来，所以这里要求
    分了三段以上、或者是一整句话；数学表达式 / JSON 配置一律不收。
    """
    s = s.strip()
    cjk = len(_CJK_RE.findall(s))
    # 长度门槛按拉丁字母算；中日韩文字一个字顶好几个字母，单独计
    if (len(s) < 16 and cjk < 6) or _CODE_LIKE_RE.search(s):
        return False
    return bool("\n" in s or len(re.split(r"[,，、]", s)) >= 3 or len(s.split()) >= 6
                or cjk >= 6 or _PROMPT_SYNTAX_RE.search(s))


# ---------------------------------------------------------------- 节点类型判定

# 备注类节点：里面的字是写给人看的，不是提示词
_SKIP_CLASS_RE = re.compile(r"note|markdown|bookmark|sticky|^label|comment", re.I)
# 「文本类」节点：键名认不出来时，它们身上像句子的字符串也算文本框
# （Kontext / Context 里的 "text" 是巧合，不算）
_TEXTISH_CLASS_RE = re.compile(
    r"(?<!kon)(?<!con)text|prompt|string|primitive|wildcard|caption|multiline|strconst|文本|提示|"
    r"easy positive|easy negative", re.I)
# 运行时才产出文本的节点（反推 / LLM 扩写 / 随机通配 / 翻译）：图里只存了它的输入，
# 最终喂给编码器的那段文字没有保存下来，得跟用户说清楚
_RUNTIME_CLASS_RE = re.compile(
    r"florence|joycaption|joy_caption|ollama|blip|tagger|moondream|minicpm|qwen|gemini|"
    r"magicprompt|randomgenerator|combinatorialgenerator|internvl|gpt|llava|onebuttonprompt|"
    r"interrogat|wd14|deepbooru|llm|vlm|deepseek|claude|chatglm|translat|textgenerate|generatetext", re.I)
_RUNTIME_EXCLUDE_RE = re.compile(r"loader|textencode|cliptext", re.I)
# 把文本显示在节点上的节点：workflow 里它的 widgets_values 存着上一次显示的内容
_DISPLAY_CLASS_RE = re.compile(r"showtext|show text|showanything|show any|display any|displaytext|"
                               r"previewany|preview any|preview text|previewtext", re.I)
# 带 steps / denoise 又带正负输入、但自己并不采样的节点（只是把参数往下传）
_NOT_EXECUTOR_RE = re.compile(r"presampling|config|settings|loader|provider|guider|scheduler", re.I)
_EXECUTOR_HINT_RE = re.compile(r"detailer|upscale|sampl|inpaint|refine|hires|img2img|txt2img|generat", re.I)

_NEG_WORDS_RE = re.compile(
    r"\b(worst quality|low quality|bad quality|normal quality|bad anatomy|bad hands|lowres|"
    r"watermark|censor(ed)?|mutated|deformed|disfigured|blurry|ugly|jpeg artifacts|signature|"
    r"poorly drawn|extra (fingers|limbs|digits|arms|legs)|missing (fingers|limbs))\b"
    r"|低质量|最差质量|模糊|水印|畸形|多余的", re.I)
_POS_WORDS_RE = re.compile(
    r"\b(masterpiece|best quality|highres|absurdres|1girl|1boy|extremely detailed|"
    r"ultra[- ]detailed|photorealistic)\b|杰作|最高质量", re.I)

_MAX_DEPTH = 150


def _is_link(v: Any) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) == 2
            and isinstance(v[0], (str, int)) and not isinstance(v[0], bool)
            and isinstance(v[1], int) and not isinstance(v[1], bool))


def _natural_key(nid: str) -> Tuple:
    """节点 id 的自然排序（"9" 排在 "10" 前面；子图的 "12:3" 按段比较）。"""
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in str(nid).split(":"))


class _Collector:
    def __init__(self, nodes: Dict[str, Any], workflow: Any, samplers: Any):
        self.nodes = nodes
        self.workflow = workflow if isinstance(workflow, dict) else None
        self.samplers = samplers
        self.boxes: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.seq = 0
        self.stage_labels: List[str] = []
        self.runtime: Dict[str, List[Tuple[Optional[str], Optional[int]]]] = {}
        # workflow 顶层节点：补标题、找被停用的节点、找显示节点上留下的文本
        self.wf_nodes: Dict[str, Dict[str, Any]] = {}
        if self.workflow and isinstance(self.workflow.get("nodes"), list):
            for n in self.workflow["nodes"]:
                if isinstance(n, dict) and n.get("id") is not None:
                    self.wf_nodes[str(n["id"])] = n

    # ---------------------------------------------------------- 入口

    def run(self) -> List[Dict[str, Any]]:
        roots = self._find_roots()
        for stage, (rid, node) in enumerate(roots):
            self._visit_root(rid, node, stage)
        self._trace_polarity_hints({rid for rid, _ in roots})
        self._scan_floating()
        self._scan_workflow_disabled()
        self._apply_runtime()
        return self._finish()

    # ---------------------------------------------------------- 找「消费条件」的节点

    def _find_roots(self) -> List[Tuple[str, Dict[str, Any]]]:
        samplers = self.samplers
        if samplers is None:
            from .comfyui_parser import ComfyUIParser   # 延迟导入：comfyui_parser 也导入本模块
            samplers = ComfyUIParser._find_all_samplers(self.nodes)
        # 静音(2) / 旁路(4) 的采样器不参与出图，接在它上面的文本框不能算「已使用」
        samplers = [(str(nid), n) for nid, n in samplers
                    if isinstance(n, dict) and n.get("mode") not in (2, 4)]
        roots = list(samplers)
        seen = {nid for nid, _ in roots}
        for i in range(len(samplers)):
            if i == 0:
                self.stage_labels.append("主采样")
            else:
                self.stage_labels.append("二次采样" if len(samplers) == 2 else f"二次采样 {i}")

        # 类名里没有 sampler 但同样自己跑采样的节点：FaceDetailer / UltimateSDUpscale 这类。
        # 它们的提示词可以和主采样完全不同，必须各算一个阶段
        others = [(nid, n) for nid, n in self.nodes.items()
                  if nid not in seen and self._is_executor(n)]
        class_count: Dict[str, int] = {}
        for _nid, n in others:
            c = str(n.get("class_type") or "")
            class_count[c] = class_count.get(c, 0) + 1
        for nid, n in sorted(others, key=lambda x: _natural_key(x[0])):
            c = str(n.get("class_type") or "") or "节点"
            self.stage_labels.append(f"{c} #{nid}" if class_count.get(c, 0) > 1 else c)
            roots.append((nid, n))
        return roots

    @staticmethod
    def _is_executor(n: Any) -> bool:
        if not isinstance(n, dict) or n.get("mode") in (2, 4):
            return False
        inp = n.get("inputs")
        if not isinstance(inp, dict) or not ("steps" in inp or "denoise" in inp):
            return False
        clow = str(n.get("class_type") or "").lower()
        if _NOT_EXECUTOR_RE.search(clow):
            return False
        has_cond = any(_is_link(v) and (key_polarity(k) or "pipe" in str(k).lower())
                       for k, v in inp.items())
        if not has_cond:
            return False
        return bool(_EXECUTOR_HINT_RE.search(clow)) or _is_link(inp.get("model"))

    # ---------------------------------------------------------- 回溯

    def _visit_root(self, rid: str, node: Dict[str, Any], stage: int) -> None:
        inp = node.get("inputs") or {}
        clow = str(node.get("class_type") or "").lower()
        visited: set = set()
        for k, v in inp.items():
            # 根节点自己身上的文本（一体化采样器的内联提示词、Detailer 的 wildcard）。
            # 只认键名明确是文本的，采样器名 / 调度器这些下拉值绝不能混进来
            if isinstance(v, str) and is_text_key(k) and value_ok(v):
                pol = key_polarity(k)
                if pol is None and str(k).lower().startswith("wildcard") and "detailer" in clow:
                    pol = POSITIVE      # Detailer 的 wildcard 填了就是替换正向提示词
                self._record(rid, node, k, v, pol, stage)
        for k, v in inp.items():
            if not _is_link(v) or _NON_TEXT_LINK_RE.match(str(k)):
                continue
            pol = key_polarity(k)
            if pol is None and not _ROOT_LINK_RE.search(str(k)):
                continue
            self._visit(str(v[0]), v[1], pol, stage, visited, 0, str(k))

    def _visit(self, sid: str, slot: int, pol: Optional[str], stage: Optional[int],
               visited: set, depth: int, via: str = "", soft: Optional[str] = None) -> None:
        """从 sid 节点的第 slot 个输出往上游走。

        pol  —— 这条线已经确定的正负（来自路上某个带正负标记的键），None = 还不知道
        via  —— 是经由下游节点的哪个输入键走到这儿的
        soft —— pol 一直没确定时的默认值（引导器上没写正负的那几路条件）
        """
        if depth > _MAX_DEPTH:
            return
        node = self.nodes.get(sid)
        # 槽位也算进「走过」的判定：同一个节点的不同输出槽可能代表不同的正负
        state = (sid, slot, pol or soft)
        if not isinstance(node, dict) or state in visited:
            return
        visited.add(state)
        mode = node.get("mode")
        if mode == 2:
            return      # 静音节点：整条支路不执行
        clow = str(node.get("class_type") or "").lower()
        inp = node.get("inputs")
        if not isinstance(inp, dict):
            return
        if "conditioningzeroout" in clow:
            return      # 条件被清零，上游文本对出图没有作用
        if pol is None and "unpack sdxl tuple" in clow:
            pol = NEGATIVE if slot in (3, 7) else (POSITIVE if slot in (2, 6) else None)
        link_marks = {key_polarity(k) for k, v in inp.items() if _is_link(v)}
        if pol is None and slot in (0, 1) and via.lower().startswith("cond") \
                and POSITIVE in link_marks and NEGATIVE in link_marks:
            # 正负成对进、成对出的条件节点（ControlNetApplyAdvanced / InstructPixToPix /
            # InpaintModelConditioning）：0 号输出是正向、1 号是负向。只在下游是按
            # 「条件」接过来时才这么判——pipe 一个输出里同时装着正负，不能按槽位拆
            pol = POSITIVE if slot == 0 else NEGATIVE

        # KJNodes 的 Get/Set：靠名字配对，连线表里没有这条边（只在 workflow 转译图里出现）
        if clow.startswith("getnode"):
            name = inp.get("name")
            if isinstance(name, str) and name:
                for onid, other in self.nodes.items():
                    if not isinstance(other, dict):
                        continue
                    if not str(other.get("class_type") or "").lower().startswith("setnode"):
                        continue
                    oinp = other.get("inputs") or {}
                    if oinp.get("name") != name:
                        continue
                    for v in oinp.values():
                        if _is_link(v):
                            self._visit(str(v[0]), v[1], pol, stage, visited, depth + 1, via, soft)
            return

        # 旁路(4) 节点只把输入原样透传，自己身上填的文本不生效
        if mode != 4 and not _SKIP_CLASS_RE.search(clow):
            if _RUNTIME_CLASS_RE.search(clow) and not _RUNTIME_EXCLUDE_RE.search(clow):
                self.runtime.setdefault(sid, []).append((pol, stage))
            for k, v in inp.items():
                if not isinstance(v, str):
                    continue
                kp = key_polarity(k)
                if pol and kp and kp != pol:
                    continue    # 沿正向走到这儿，负向那一格不归这条线
                if self._is_prompt_string(k, v, clow):
                    self._record(sid, node, k, v, kp or pol or soft, stage)

        link_keys = [k for k, v in inp.items() if _is_link(v)]
        skip = self._unselected_branches(clow, inp)
        for k in link_keys:
            ks = str(k)
            if k in skip or ks == "_widgets_values":
                continue
            # 这里只按「明显不承载文本」排除。find / replace 这类键上填的字面值不算提示词，
            # 但连进来的线要跟：LLM 模板工作流就是把用户写的提示词接到 StringReplace.replace 上
            if _NON_TEXT_LINK_RE.match(ks):
                continue
            kp = key_polarity(ks)
            if pol and kp and kp != pol:
                continue
            nxt, nsoft = kp or pol, soft
            if nxt is None:
                # 引导器：BasicGuider 只有一个 conditioning，它就是正向。
                # DualCFGGuider 这种「有 negative、另外几路 cond1 / cond2 没写正负」的，
                # 另外几路默认算正向，但只是默认值——上游要是个正负成对的条件节点，
                # 还得按它的输出槽位来分（cond2 接的常常是那个节点的负向输出）
                if "guider" in clow and ks.lower() == "conditioning":
                    nxt = POSITIVE
                elif NEGATIVE in link_marks and POSITIVE not in link_marks and ks.lower().startswith("cond"):
                    nsoft = POSITIVE
            v = inp[k]
            self._visit(str(v[0]), v[1], nxt, stage, visited, depth + 1, ks, nsoft)

    @staticmethod
    def _unselected_branches(clow: str, inp: Dict[str, Any]) -> set:
        """开关节点上没被选中的那几路输入。没选中的支路不参与出图，
        上面的文本框留给后面的全图扫描标成「未接入」。认不出选择方式就全部放行。"""
        skip: set = set()
        if "on_true" in inp or "on_false" in inp:
            sw = inp.get("switch", inp.get("boolean", inp.get("cond")))
            if isinstance(sw, bool) or str(sw).lower() in ("true", "false"):
                chosen = "on_true" if (sw is True or str(sw).lower() == "true") else "on_false"
                skip.update(k for k in ("on_true", "on_false") if k != chosen)
        if "switch" in clow or "selector" in clow:
            if "any switch" in clow or "anyswitch" in clow:
                # rgthree Any Switch：取第一个有值的输入。静音节点不进 API 图，
                # 所以图里排最前的那条连线就是实际生效的
                anys = sorted(k for k, v in inp.items() if _is_link(v) and re.match(r"^any_\d+$", str(k)))
                skip.update(anys[1:])
            sel = None
            for sk in ("select", "Input", "input", "index", "selected", "which"):
                sv = inp.get(sk)
                if isinstance(sv, int) and not isinstance(sv, bool):
                    sel = sv
                    break
            if sel is not None:
                numbered = {}
                for k in inp:
                    m = re.search(r"(\d+)$", str(k))
                    if m and (_is_link(inp[k]) or isinstance(inp[k], str)):
                        numbered[k] = int(m.group(1))
                if sel in numbered.values():
                    skip.update(k for k, n in numbered.items() if n != sel)
        return skip

    @staticmethod
    def _is_prompt_string(key: str, value: str, clow: str) -> bool:
        if not value_ok(value):
            return False
        ks = str(key)
        if ks.startswith("_") or _STR_KEY_DENY_RE.search(ks):
            return False
        if is_text_key(ks):
            return True
        if _TEXTISH_CLASS_RE.search(clow):
            return looks_like_prompt(value)
        return strongly_prompt(value)       # 通用兜底：键名和类型都不认识，内容够像才收

    # ---------------------------------------------------------- 记录

    def _title(self, nid: str, node: Dict[str, Any]) -> str:
        meta = node.get("_meta")
        title = meta.get("title") if isinstance(meta, dict) else None
        if not title:
            title = node.get("title")
        if not title:
            # 老版本 ComfyUI 的 prompt 块不带 _meta，标题只在 workflow 里
            wn = self.wf_nodes.get(str(nid))
            title = wn.get("title") if isinstance(wn, dict) else None
        return title.strip() if isinstance(title, str) else ""

    def _record(self, nid: str, node: Dict[str, Any], field: str, text: str,
                pol: Optional[str], stage: Optional[int], status: Optional[str] = None) -> Dict[str, Any]:
        key = (str(nid), str(field))
        box = self.boxes.get(key)
        if box is None:
            self.seq += 1
            box = self.boxes[key] = {
                "node_id": str(nid),
                "class_type": str(node.get("class_type") or ""),
                "title": self._title(nid, node),
                "field": str(field),
                "text": text.strip(),
                "used_pols": set(), "hint_pols": set(), "stages": [],
                "seq": self.seq, "forced": status, "runtime": False, "stale": False,
            }
        if stage is not None:
            if stage not in box["stages"]:
                box["stages"].append(stage)
            box["used_pols"].add(pol)
        else:
            box["hint_pols"].add(pol)
        return box

    # ---------------------------------------------------------- 没接到采样器的文本框

    def _trace_polarity_hints(self, root_ids: set) -> None:
        """没接到采样器、但接进了某个 positive / negative 输入的支路（比如搭了一半的
        ControlNet 链）：正负还是看得出来的，只是不算「已使用」。"""
        visited: set = set()
        for nid, n in self.nodes.items():
            if nid in root_ids or not isinstance(n, dict) or n.get("mode") in (2, 4):
                continue
            for k, v in (n.get("inputs") or {}).items():
                if _is_link(v) and not _NON_TEXT_LINK_RE.match(str(k)):
                    pol = key_polarity(k)
                    if pol:
                        self._visit(str(v[0]), v[1], pol, None, visited, 0, str(k))

    def _scan_floating(self) -> None:
        for nid in sorted(self.nodes, key=_natural_key):
            n = self.nodes[nid]
            if not isinstance(n, dict):
                continue
            clow = str(n.get("class_type") or "").lower()
            if _SKIP_CLASS_RE.search(clow):
                continue
            inp = n.get("inputs")
            if not isinstance(inp, dict):
                continue
            textish = bool(_TEXTISH_CLASS_RE.search(clow))
            status = DISABLED if n.get("mode") in (2, 4) else None
            for k, v in inp.items():
                if not isinstance(v, str) or (str(nid), str(k)) in self.boxes:
                    continue
                if not self._is_prompt_string(k, v, clow):
                    continue
                # 不在任何连线路径上、节点类型也不是文本类：键名像文本还不够
                # （比如某个加载器的 value: "v2"），内容也得像一句话
                if not textish and not looks_like_prompt(v):
                    continue
                self._record(nid, n, k, v, key_polarity(k), None, status)

    def _scan_workflow_disabled(self) -> None:
        """被静音 / 旁路的节点不进 prompt 块，只有 workflow 里还留着。用户常把备用的
        提示词框静音放在一边，这些也要列出来，并说明它们这次没生效。"""
        sub_names: Dict[str, str] = {}
        defs = (self.workflow or {}).get("definitions")
        for d in (defs.get("subgraphs") if isinstance(defs, dict) and isinstance(defs.get("subgraphs"), list) else []):
            if isinstance(d, dict) and isinstance(d.get("id"), str):
                sub_names[d["id"]] = str(d.get("name") or "")
        for nid, wn in self.wf_nodes.items():
            if nid in self.nodes or wn.get("mode") not in (2, 4):
                continue
            ctype = wn.get("type") if isinstance(wn.get("type"), str) else ""
            clow = ctype.lower()
            if _SKIP_CLASS_RE.search(clow):
                continue
            wv = wn.get("widgets_values")
            if not isinstance(wv, list):
                continue
            textish = bool(_TEXTISH_CLASS_RE.search(clow))
            if ctype in sub_names:
                # 被停用的子图实例。workflow 转译图里它已经展开成 "实例id:内部id"，
                # 里面的文本框各自列过了，这里不重复；只有 prompt 块的图里看不到内部节点，
                # 就列实例上填的文本，类型显示成子图的名字而不是一串 uuid
                if any(str(k).startswith(nid + ":") for k in self.nodes):
                    continue
                ctype = f"子图 {sub_names[ctype]}".strip()
                textish = True
            is_sub = wn.get("type") in sub_names
            idx = 0
            for item in wv:
                if not isinstance(item, str) or not value_ok(item):
                    continue
                if is_sub and not looks_like_prompt(item):
                    continue    # 子图实例上什么部件都可能有（模型名、下拉选项），只收像句子的
                if not (textish or strongly_prompt(item)):
                    continue
                if textish and idx > 0 and not looks_like_prompt(item):
                    continue    # 文本节点后面跟着的下拉选项
                idx += 1
                # workflow 的部件值没有键名，只能按出现顺序编号
                fake = {"class_type": ctype, "title": wn.get("title") or ""}
                self._record(nid, fake, f"#{idx}", item, None, None, DISABLED)

    # ---------------------------------------------------------- 运行时生成的文本

    def _display_texts(self) -> Dict[str, str]:
        """{上游节点id: 显示节点上留着的文本}。只认直接相连的显示节点。"""
        out: Dict[str, str] = {}
        if not self.workflow or not isinstance(self.workflow.get("links"), list):
            return out
        origin_of: Dict[Any, str] = {}
        for link in self.workflow["links"]:
            if isinstance(link, (list, tuple)) and len(link) >= 3:
                origin_of[link[0]] = str(link[1])
            elif isinstance(link, dict) and link.get("id") is not None:
                origin_of[link["id"]] = str(link.get("origin_id"))
        for wn in self.wf_nodes.values():
            ctype = wn.get("type") if isinstance(wn.get("type"), str) else ""
            if not _DISPLAY_CLASS_RE.search(ctype):
                continue
            texts: List[str] = []
            stack = list(reversed(wn.get("widgets_values") or [])) if isinstance(wn.get("widgets_values"), list) else []
            while stack:
                item = stack.pop()
                if isinstance(item, list):
                    stack.extend(reversed(item))
                elif isinstance(item, str) and value_ok(item) and item.strip() not in texts:
                    texts.append(item.strip())
            if not texts:
                continue
            for slot in wn.get("inputs") or []:
                if isinstance(slot, dict) and slot.get("link") in origin_of:
                    out.setdefault(origin_of[slot["link"]], "\n".join(texts))
        return out

    def _apply_runtime(self) -> None:
        if not self.runtime:
            return
        shown = self._display_texts()
        for nid, hits in self.runtime.items():
            own = [b for b in self.boxes.values() if b["node_id"] == nid]
            if own:
                for b in own:
                    b["runtime"] = True
                continue
            # 节点自己没有可列的文本输入（纯反推：只吃图片）。占个位说明来源；
            # 旁边要是接了显示节点，把它留着的文本带上——但那是排队时存下的，
            # 也就是**上一次**运行的输出，不一定是这张图用的
            node = self.nodes.get(nid) or {}
            for pol, stage in hits:
                box = self._record(nid, node, "", shown.get(nid, ""), pol, stage)
                box["runtime"] = True
                box["stale"] = bool(shown.get(nid))

    # ---------------------------------------------------------- 收尾

    @staticmethod
    def _guess_polarity(box: Dict[str, Any]) -> Tuple[str, bool]:
        """连线和键名都没给出正负时的猜测。返回 (极性, 是否靠内容猜的)。"""
        hint = key_polarity(box["title"]) or key_polarity(box["class_type"])
        if hint:
            return hint, False
        text = box["text"]
        neg = len(_NEG_WORDS_RE.findall(text))
        pos = len(_POS_WORDS_RE.findall(text))
        if neg >= 2 and neg > pos:
            return NEGATIVE, True
        if pos >= 1 and neg == 0:
            return POSITIVE, True
        return UNKNOWN, False

    def _finish(self) -> List[Dict[str, Any]]:
        have_roots = bool(self.stage_labels)
        merged: Dict[Tuple, Dict[str, Any]] = {}
        for box in sorted(self.boxes.values(), key=lambda b: b["seq"]):
            used = bool(box["stages"])
            pols = {p for p in (box["used_pols"] if used else box["hint_pols"]) if p}
            guess = False
            if POSITIVE in pols and NEGATIVE in pols:
                pol = BOTH
            elif pols:
                pol = next(iter(pols))
            else:
                pol, guess = self._guess_polarity(box)
            status = box["forced"] or (USED if used else (FLOATING if have_roots else UNSURE))
            if box["forced"] and used:
                status = USED
            stages = sorted(box["stages"])
            # 同一个节点上两格填了一模一样的内容（SDXL 的 text_g / text_l 最常见）：并成一条
            mkey = (box["node_id"], box["text"], pol, status, tuple(stages)) if box["text"] else (box["node_id"], box["field"])
            hit = merged.get(mkey)
            if hit is not None:
                if box["field"] and box["field"] not in hit["field"].split(" / "):
                    hit["field"] = (hit["field"] + " / " + box["field"]) if hit["field"] else box["field"]
                continue
            merged[mkey] = {
                "node_id": box["node_id"], "class_type": box["class_type"], "title": box["title"],
                "field": box["field"], "text": box["text"],
                "polarity": pol, "guess": guess, "status": status,
                "stages": [self.stage_labels[i] for i in stages],
                "runtime": box["runtime"], "stale": box["stale"],
                "_order": (min(stages) if stages else 10 ** 6, box["seq"]),
            }

        pol_rank = {POSITIVE: 0, BOTH: 1, NEGATIVE: 2, UNKNOWN: 3}
        status_rank = {USED: 0, UNSURE: 1, FLOATING: 2, DISABLED: 3}

        def sort_key(b):
            if b["status"] == USED:
                # 同一阶段里先正向后负向；同极性按回溯顺序（也就是拼接顺序）
                return (0, b["_order"][0], pol_rank[b["polarity"]], b["_order"][1])
            return (status_rank[b["status"]],) + _natural_key(b["node_id"]) + ((2, ""),) + (b["_order"][1],)

        out = sorted(merged.values(), key=lambda b: _SortKey(sort_key(b)))
        for b in out:
            b.pop("_order", None)
        return out


class _SortKey:
    """排序键里混着 int 和 (类别, 值) 元组，逐项比较时类型可能对不上；
    统一转成 (类别, 值) 再比，避免 TypeError。"""
    __slots__ = ("items",)

    def __init__(self, items):
        self.items = tuple(x if isinstance(x, tuple) else (0, x) for x in items)

    def __lt__(self, other):
        return self.items < other.items


def collect_text_boxes(nodes: Dict[str, Any], workflow: Any = None,
                       samplers: Any = None) -> List[Dict[str, Any]]:
    """
    列出节点图里所有提示词文本框。每项：

        {"node_id": "6", "class_type": "CLIPTextEncode", "title": "正面提示词",
         "field": "text",                    # 来自哪个输入；内容相同的多格并成 "text_g / text_l"
         "text": "...",
         "polarity": "positive" | "negative" | "both" | "unknown",
         "guess": bool,                      # 正负是按内容里的关键词猜的
         "status": "used" | "floating" | "disabled" | "unknown",
         "stages": ["主采样", "FaceDetailer"],  # 被哪些采样阶段用到
         "runtime": bool,                    # 经过了运行时才出结果的节点（反推 / LLM / 通配 / 翻译）
         "stale": bool}                      # text 取自显示节点，是上一次运行留下的

    排序：参与出图的在前（按阶段、先正后负、同极性按拼接顺序），然后是未接入的、被停用的。

    nodes    —— API 格式节点图（键为节点 id 字符串、inputs 已规范成 dict）
    workflow —— 界面格式的 workflow（可选），用来补标题、找被停用的节点
    samplers —— ComfyUIParser._find_all_samplers 的结果（可选，省一次重复计算）
    """
    if not isinstance(nodes, dict) or not nodes:
        return []
    return _Collector(nodes, workflow, samplers).run()


def join_boxes(boxes: List[Dict[str, Any]], polarity: str, stage: str = "主采样") -> str:
    """把某个阶段某个极性的文本框按顺序拼成一条（主提示词兜底用）。"""
    parts: List[str] = []
    for b in boxes or []:
        if b.get("status") != USED or b.get("polarity") != polarity or b.get("guess"):
            continue
        if stage not in (b.get("stages") or []) or b.get("stale"):
            continue
        t = (b.get("text") or "").strip()
        if t and t not in parts:
            parts.append(t)
    return ", ".join(parts)
