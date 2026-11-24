# -*- coding: utf-8 -*-
"""
图片信息页的「AI 分析」：把图片里的 ComfyUI 工作流交给用户自己配置的大模型，
让它理出正向 / 负向 / LoRA / 关键参数。

为什么在规则解析之外再加这一层：
  规则解析（meta_engine）是沿连线硬追的，认得出的结构很准，但工作流的写法无穷无尽——
  自定义节点把提示词存在奇怪的字段里、开关节点选了哪一路、提示词经过好几层字符串
  处理——规则只能如实列出「有哪些文本框」，没法像人一样读懂「这个工作流到底想干什么」。
  大模型正好擅长这个：把节点图给它，它能直接说出最终的正向 / 负向是什么、用了哪些
  LoRA、各自多大强度。两条路互补：规则解析不花钱、不联网、结果可复现，默认就有；
  AI 分析要用户点一下才跑，结果单独放一张卡片，不覆盖规则解析的结果。

接口复用报错诊断那一套（diagnose_llm：用户在「启动器设置 → 报错诊断」填的 Key，
DeepSeek / 通义千问 / Claude / 自定义 OpenAI 兼容接口），不另外要用户再配一遍。

发出去的内容：节点图（提示词、模型文件名、节点参数）+ 规则解析的结果作参考。
先过 diagnose.redact 脱敏（用户目录、各种 Key）；内嵌的图片数据之类的超长串会被截掉。
"""
import hashlib
import json
import re

import diagnose as dg
import diagnose_llm as dl

# 发给模型的材料上限（字符）。超过就先把长字符串截短再试，还超就放弃——
# 几百 KB 的节点图塞进去既慢又贵，多数模型也读不完
MAX_CHARS = 150_000
_STR_LIMITS = (6000, 1500, 400)

_S = {"type": "string"}
_N = {"type": "number"}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_PART = _obj({
    "text": {"type": "string", "description": "这个文本框里的提示词，原样照抄"},
    "node": {"type": "string", "description": "来自哪个节点：标题或类型 + #节点id"},
    "stage": {"type": "string", "description": "用在哪一步：主采样 / 二次采样 / 高清修复 / FaceDetailer / 放大 …"},
    "note": {"type": "string", "description": "可选的补充说明"},
}, ["text"])

REPORT_TOOL = ("report", "提交工作流分析结果。", _obj({
    "summary": {"type": "string", "description": "一两句中文：这个工作流在做什么"},
    "positive": {"type": "string", "description": "主采样器最终拿到的完整正向提示词"},
    "negative": {"type": "string", "description": "主采样器最终拿到的完整负向提示词，没有就留空"},
    "positive_parts": {"type": "array", "items": _PART, "description": "正向提示词按来源拆开，每个文本框一项"},
    "negative_parts": {"type": "array", "items": _PART, "description": "负向提示词按来源拆开"},
    "loras": {"type": "array", "description": "用到的 LoRA", "items": _obj({
        "name": {"type": "string", "description": "LoRA 文件名，原样照抄"},
        "weight": {"type": "number", "description": "模型强度"},
        "clip_weight": {"type": "number", "description": "CLIP 强度，和模型强度不同时才填"},
        "enabled": {"type": "boolean", "description": "被关掉 / 强度为 0 / 节点被旁路时填 false"},
        "node": _S,
    }, ["name"])},
    "model": {"type": "string", "description": "底模文件名"},
    "params": {"type": "array", "description": "关键设置，一项一条",
               "items": _obj({"name": _S, "value": _S}, ["name", "value"])},
    "notes": {"type": "array", "items": _S, "description": "需要提醒用户的地方"},
}, ["summary", "positive", "negative", "loras"]))

SYSTEM = (
    "你是 ComfyUI 工作流分析助手。用户给你一张图片里保存的工作流节点图，请理清它实际是怎么出图的，"
    "然后调用 report 工具提交结果。\n\n"
    "规则：\n"
    "1. 只依据节点图里的内容，不要编造；看不出来的留空，或者写进 notes。\n"
    "2. 提示词原样照抄（保留权重括号、<lora:…>、通配符写法和换行），不要翻译、润色、补词。\n"
    "3. positive / negative：主采样器（第一次采样）最终拿到的完整提示词，按连线的实际拼接顺序合并。"
    "负向被 ConditioningZeroOut 清零、或者根本没有负向时留空。\n"
    "4. positive_parts / negative_parts：把提示词按来源拆开，每个文本框一项；node 写「节点标题或类型 #节点id」，"
    "stage 写它用在哪一步（主采样 / 二次采样 / 高清修复 / FaceDetailer / 放大 …）。二次采样、Detailer "
    "自己的提示词也列在这里。没有接到任何采样器、或者被静音 / 旁路的文本框不要放进来，在 notes 里说一声。\n"
    "5. loras：所有 LoRA（LoraLoader、Power Lora Loader、LoRA Stack、加载器自带的 lora 槽、提示词里的"
    " <lora:名:权重>）。name 用文件名原文；weight 是模型强度；clip_weight 和模型强度不同时才填；"
    "被关掉的（on=false、强度 0、节点被旁路）enabled 填 false。\n"
    "6. model：底模（checkpoint / unet / diffusion model）文件名。params：采样器、调度器、步数、CFG、种子、"
    "尺寸、重绘幅度、VAE、文本编码器、放大 / 高清修复、ControlNet、IPAdapter 等关键设置，name 用中文，一项一条。\n"
    "7. summary：一两句中文，说明这个工作流在做什么（文生图 / 图生图 / 局部重绘 / 视频 …，用了什么模型、几次采样）。\n"
    "8. notes：提示词是反推 / LLM / 通配符在运行时生成的（图里没有保存结果）、有没生效的文本框、"
    "你拿不准的地方，都写在这里。\n"
    "说明性的字段用中文写。"
)

# 模型不支持（或不肯）调用工具时的退路：直接要一个 JSON
JSON_ASK = (
    "\n\n请只输出一个 JSON 对象，不要任何别的文字，字段：summary、positive、negative、"
    "positive_parts（数组，每项 {text, node, stage, note}）、negative_parts（同上）、"
    "loras（数组，每项 {name, weight, clip_weight, enabled, node}）、model、"
    "params（数组，每项 {name, value}）、notes（字符串数组）。"
)


class TooLarge(Exception):
    pass


def has_workflow(m):
    """这张图里有没有可分析的 ComfyUI 节点图。"""
    return bool(getattr(m, "raw_prompt_json", None) or getattr(m, "raw_workflow_json", None))


# ============================================================
# 整理要发给模型的材料
# ============================================================

def _is_link(v):
    return (isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], (str, int))
            and isinstance(v[1], int) and not isinstance(v[1], bool))


def _clip(s, limit):
    s = str(s)
    return s if len(s) <= limit else s[:limit] + f"…（已截断，共 {len(s)} 字符）"


def _compact_value(v, limit):
    if isinstance(v, str):
        return _clip(v, limit)
    if isinstance(v, (int, float, bool)) or v is None or _is_link(v):
        return v
    # 嵌套结构（rgthree Power Lora 的 {"on","lora","strength"}、各种部件配置）：
    # 原样保留但限制长度，里面要是塞了图片数据就截掉
    try:
        text = json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(v)
    return v if len(text) <= limit else _clip(text, limit)


def compact_graph(m, str_limit=_STR_LIMITS[0]):
    """节点图 → {节点id: {"class_type", "title", "inputs"}}，去掉对理解工作流没用的东西。"""
    graph = getattr(m, "raw_prompt_json", None)
    if not isinstance(graph, dict) or not graph:
        from meta_engine.comfyui_parser import ComfyUIParser
        graph = ComfyUIParser._workflow_to_nodes(getattr(m, "raw_workflow_json", None) or {})
    out = {}
    for nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        named = {str(k): _compact_value(v, str_limit) for k, v in inputs.items() if k != "_widgets_values"}
        # workflow 转译图里，认不出键名的部件值都堆在 _widgets_values：没有别的可看时才带上
        wv = inputs.get("_widgets_values")
        if isinstance(wv, list) and not any(not _is_link(v) for v in named.values()):
            named["widgets_values"] = [_compact_value(v, str_limit) for v in wv[:40]]
        item = {"class_type": str(node.get("class_type") or "")}
        meta = node.get("_meta")
        title = (meta.get("title") if isinstance(meta, dict) else "") or ""
        if title and title != item["class_type"]:
            item["title"] = str(title)
        if node.get("mode") in (2, 4):
            item["disabled"] = "静音" if node.get("mode") == 2 else "旁路"
        item["inputs"] = named
        out[str(nid)] = item
    return out


def _hints(shown):
    """规则解析已经得到的结果，给模型当参考（也让它知道哪些节点被停用了——
    被静音的节点不在 prompt 块里，模型光看节点图是看不到的）。"""
    shown = shown or {}
    lines = []
    if shown.get("prompt"):
        lines.append("正向（主采样）：" + _clip(shown["prompt"], 3000))
    if shown.get("negative"):
        lines.append("负向（主采样）：" + _clip(shown["negative"], 2000))
    for k, v in (shown.get("rows") or [])[:40]:
        lines.append(f"{k}：{_clip(v, 300)}")
    boxes = shown.get("text_boxes") or []
    if boxes:
        lines.append("文本框清单（极性 / 状态 / 所属阶段 / 节点 / 内容）：")
        names = {"positive": "正向", "negative": "负向", "both": "正负都接", "unknown": "未知"}
        states = {"used": "参与出图", "floating": "没接到采样器", "disabled": "节点被停用", "unknown": "无法判断"}
        for b in boxes[:60]:
            lines.append("- {pol} / {st} / {stages} / {cls} #{nid} / {text}".format(
                pol=names.get(b.get("polarity"), "未知"), st=states.get(b.get("status"), "无法判断"),
                stages="、".join(b.get("stages") or []) or "—", cls=b.get("class_type") or "节点",
                nid=b.get("node_id"), text=_clip(b.get("text") or "（运行时生成）", 400)))
    return "\n".join(lines)


def digest(m, shown=None):
    """拼出发给模型的那段文字。太大时逐级截短长字符串；实在放不下抛 TooLarge。"""
    hints = _hints(shown)
    for limit in _STR_LIMITS:
        graph = json.dumps(compact_graph(m, limit), ensure_ascii=False, separators=(",", ":"))
        text = (
            "下面是一张图片里保存的 ComfyUI 工作流。\n\n"
            "===== 节点图 =====\n"
            "格式：{节点id: {class_type, title, inputs}}；inputs 里形如 [\"节点id\", 输出槽序号] 的值表示"
            "这个输入接在那个节点的那个输出上，其余是直接填的值。\n"
            f"{graph}\n\n"
            "===== 启动器按规则解析出来的结果（仅供参考，可能有错漏，以节点图为准）=====\n"
            f"{hints or '（没有解析出内容）'}\n\n"
            "请分析这个工作流，调用 report 提交结果。"
        )
        if len(text) <= MAX_CHARS:
            return dg.redact(text)
    raise TooLarge(f"这个工作流太大了（节点图超过 {MAX_CHARS // 1000} 千字符），没法交给 AI 分析")


def cache_key(text, model):
    return hashlib.sha1((model + "\n" + text).encode("utf-8", "replace")).hexdigest()


# ============================================================
# 调模型、整理结果
# ============================================================

def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _text(v, limit=20000):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = "\n".join(str(x) for x in v)
    return str(v).strip()[:limit]


def _parts(v):
    out = []
    for p in v if isinstance(v, list) else []:
        if isinstance(p, str):
            p = {"text": p}
        if not isinstance(p, dict):
            continue
        text = _text(p.get("text"))
        if not text:
            continue
        out.append({"text": text, "node": _text(p.get("node"), 200), "stage": _text(p.get("stage"), 60),
                    "note": _text(p.get("note"), 500)})
    return out[:80]


def normalize(raw, model=""):
    """模型交回来的东西 → 前端直接能用的固定结构。字段缺了、类型不对都在这里抹平，
    界面那边不用再到处判空。"""
    raw = raw if isinstance(raw, dict) else {}
    pos_parts, neg_parts = _parts(raw.get("positive_parts")), _parts(raw.get("negative_parts"))
    positive, negative = _text(raw.get("positive")), _text(raw.get("negative"))

    def is_main(p):
        s = p["stage"]
        return not s or "主" in s or "main" in s.lower() or "base" in s.lower() or "一次" in s

    # 两种视图互相兜底：只给了整条的，拆分视图里就是一项；只给了分段的，汇总就把主采样的几段拼起来
    if not positive and pos_parts:
        positive = ", ".join(p["text"] for p in pos_parts if is_main(p)) or pos_parts[0]["text"]
    if not negative and neg_parts:
        negative = ", ".join(p["text"] for p in neg_parts if is_main(p)) or neg_parts[0]["text"]
    if positive and not pos_parts:
        pos_parts = [{"text": positive, "node": "", "stage": "", "note": ""}]
    if negative and not neg_parts:
        neg_parts = [{"text": negative, "node": "", "stage": "", "note": ""}]

    loras, seen = [], set()
    for l in raw.get("loras") if isinstance(raw.get("loras"), list) else []:
        if isinstance(l, str):
            l = {"name": l}
        if not isinstance(l, dict):
            continue
        name = _text(l.get("name") or l.get("lora"), 300)
        if not name or name.lower() == "none" or name.lower() in seen:
            continue
        seen.add(name.lower())
        w = _num(l.get("weight", l.get("strength")))
        cw = _num(l.get("clip_weight"))
        loras.append({"name": name, "weight": w, "clip_weight": cw if (cw is not None and cw != w) else None,
                      "enabled": l.get("enabled") is not False and w != 0, "node": _text(l.get("node"), 200)})

    params = []
    src = raw.get("params")
    if isinstance(src, dict):       # 有的模型不按数组给，直接给了个字典
        src = [{"name": k, "value": v} for k, v in src.items()]
    for p in src if isinstance(src, list) else []:
        if isinstance(p, dict):
            k, v = _text(p.get("name"), 60), p.get("value")
        elif isinstance(p, (list, tuple)) and len(p) == 2:
            k, v = _text(p[0], 60), p[1]
        else:
            continue
        v = _text(v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False), 600)
        if k and v:
            params.append([k, v])

    notes = raw.get("notes")
    if isinstance(notes, str):
        notes = [notes]
    return {
        "summary": _text(raw.get("summary"), 1000),
        "positive": positive, "negative": negative,
        "positive_parts": pos_parts, "negative_parts": neg_parts,
        "loras": loras[:60],
        "model": _text(raw.get("model"), 300),
        "params": params[:40],
        "notes": [_text(n, 600) for n in (notes if isinstance(notes, list) else []) if _text(n)][:12],
        "by": model,
    }


def _json_from_text(text):
    """从模型的文字回复里抠出 JSON 对象（可能包在 ```json 代码块里，前后可能有废话）。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# 这几类错误换个问法也没用，直接报给用户（重试只会白等、白花钱）
_NO_RETRY = ("API Key", "余额", "太频繁", "连不上", "超时", "接口地址")


def analyze(cfg, text, post=None):
    """
    把 digest() 的结果发给模型，返回 normalize() 之后的结构；失败抛 diagnose_llm.LlmError。

    先要求模型调用 report 工具（结构化输出最稳）；个别服务商或「思考」模式不支持
    强制调用工具，那就退一步让它直接回一个 JSON。
    """
    conf = dl.resolve(cfg)
    if not conf:
        raise dl.LlmError("还没有设置 AI：到「启动器设置 → 报错诊断」填 API Key")
    fmt, base, model, key = conf
    chat = dl.Chat(fmt, base, model, key, SYSTEM, [REPORT_TOOL], timeout=180, post=post)
    chat.user(text)
    reply = ""
    try:
        reply, calls = chat.send(force="report")
        for _cid, name, args in calls:
            if name == "report" and isinstance(args, dict) and "_raw" not in args:
                return normalize(args, model)
    except dl.LlmError as e:
        if any(s in str(e) for s in _NO_RETRY):
            raise
    data = _json_from_text(reply)
    if data is None:
        plain = dl.Chat(fmt, base, model, key, SYSTEM, [], timeout=180, post=post)
        plain.user(text + JSON_ASK)
        reply, _calls = plain.send()
        data = _json_from_text(reply)
        if data is None:
            if not reply.strip() and str(plain.finish).lower() in ("length", "max_tokens"):
                raise dl.LlmError(dl.TRUNCATED_TEXT)
            raise dl.LlmError("AI 的回复不是预期的格式，没法整理成结果。可以再试一次，或者换一个模型")
    return normalize(data, model)
