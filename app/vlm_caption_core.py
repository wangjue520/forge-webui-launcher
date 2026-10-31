# -*- coding: utf-8 -*-
"""
自然语言反推（给图片写一段描述）：本地视觉模型 + 云端 API。

跟 WWY LoRA 训练器（app/captionutil.py、app/tagging.py）是同一套：同样的四个本地模型、同样的提示词模板、
同样的 API 预设和拒答判断，两边打出来的标是一个样子。

本地模型两边共用一份，不重复下载：
  训练器：<训练器目录>/models/taggers/<作者__仓库名>
  启动器：<启动器目录>/vlm_models/<作者__仓库名>（没装训练器时才下到这里；训练器也会来这里找）
装了训练器时新下载的模型直接放进训练器的目录。下完写 .lora_complete（和训练器的格式一样：
记每个文件校验过的 sha256），训练器看到它就直接用，不再联网检查。

这个模块在启动器主进程里用，只依赖标准库 + requests；真正跑模型的是 vlm_caption_worker.py（独立环境里的子进程）。
"""
import base64
import fnmatch
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import requests

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 启动器根目录（代码在 app/ 里）
LAUNCHER_MODELS = os.path.join(APP_DIR, "vlm_models")
MARK = ".lora_complete"     # 训练器 downloader.py 的下载完成标记，格式要一致
MARK_VERSION = 2

# ============================================================
# 模型和提示词（和训练器 captionutil.py 保持一致）
# ============================================================
VLM_MODELS = {
    "toriigate": {"label": "ToriiGate 0.5（二次元专精，可参考 WD14 标签）", "repo": "Minthy/ToriiGate-0.5", "size_gb": 10},
    "joycaption": {"label": "JoyCaption Beta One（通用，描述细致）", "repo": "fancyfeast/llama-joycaption-beta-one-hf-llava", "size_gb": 17},
    "qwen3vl8b": {"label": "Qwen3-VL-8B abliterated（无审查，写实友好）", "repo": "huihui-ai/Huihui-Qwen3-VL-8B-Instruct-abliterated", "size_gb": 17.5},
    "qwen3vl4b": {"label": "Qwen3-VL-4B abliterated（显存小）", "repo": "huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated", "size_gb": 9},
}
DEFAULT_MODEL = "toriigate"

_MOSS_TAIL = ("Include only information visible in the image. Preserve any visible text exactly as written (do not translate, "
              "paraphrase or romanize it) and describe its layout: line breaks, ordering, relative position, alignment, size, "
              "font style and color. Do not describe watermarks. Use plain natural sentences without markdown or bullet points. "
              "Do not start with phrases like 'This image shows'.")

PROMPT_PRESETS = {
    "detailed": "Write a detailed description of this image for training an image generation model. Describe the subject, "
                "appearance, clothing, pose, expression, composition, background, lighting, colors and art style. "
                "Use plain natural English sentences. Do not use bullet points, markdown or line breaks. "
                "Do not start with phrases like 'This image shows'.",
    "short": "Write a short one or two sentence description of this image for an image generation model. "
             "Mention the main subject, key details and style. Plain English, no line breaks.",
    "style": "Describe this image focusing on the content only (subject, pose, clothing, background). Do NOT describe "
             "the art style, medium, rendering technique or overall color palette (colors of hair, eyes and clothing are fine). "
             "Plain natural English sentences, no line breaks.",
    "moss": "Write a detailed description of this image for training a text-to-image model. Describe the composition, key "
            "subjects, actions, setting, colors, lighting and artistic style in detail. " + _MOSS_TAIL,
    "moss_style": "Write a detailed description of this image for training a text-to-image style LoRA. Describe the composition, "
                  "key subjects, appearance, clothing, pose, expression, actions, setting and lighting in detail. Do NOT describe "
                  "the art style, medium, rendering technique, line work, shading or overall color palette, and do not name any "
                  "artist or style; the colors of individual things such as hair, eyes and clothing are fine. " + _MOSS_TAIL,
    "character": "Describe this image for training a character LoRA. Describe pose, expression, clothing, accessories, "
                 "background and composition, but do NOT describe the character's permanent features (face, hair "
                 "color, eye color, body type). Plain natural English sentences, no line breaks.",
}
# 界面上的名字和顺序（和训练器反推页一样）
PROMPT_LABELS = [
    ("detailed", "详细描述（通用）"), ("short", "简短描述"),
    ("moss", "MOSS 官方风格（通用 / 练人物，保留图中文字）"), ("moss_style", "MOSS · 练画风（不描述画风）"),
    ("style", "练画风（不描述画风）"), ("character", "练人物（不描述人物固有特征）"), ("custom", "自定义"),
]

TORII_FORMATS = {
    "long": "Make a caption for given image with natural text. Use 2 to 5 paragraphs. Make your description long and vivid, mentioning all the details.\n",
    "short": "The caption for image should be quite short without long purple prose and slop. Cover main objects and details.\n",
}
TORII_SYSTEM = "You are image captioning expert. Describe user's picture according to requested format and instructions."
DEFAULT_SYSTEM = "You are a helpful image captioner."

API_PRESETS = [
    {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
    {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash"},
    {"name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-VL-32B-Instruct"},
    {"name": "阿里云百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3-vl-plus"},
    {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "google/gemini-2.5-flash"},
    {"name": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3-vl:8b"},
    {"name": "本地 LM Studio", "base_url": "http://127.0.0.1:1234/v1", "model": ""},
]


class CaptionError(Exception):
    pass


def torii_query(fmt, tags=None, use_names=True):
    q = "# Captioning format:\n" + TORII_FORMATS.get(fmt, TORII_FORMATS["long"]) + "\n"
    if tags:
        q += "# Booru tags for the image\n[" + ", ".join(tags) + "]\n\n"
    if use_names:
        q += "# Characters on picture:\nTry to recognize the characters in the picture and use their names.\n\n"
    else:
        q += "# Characters on picture:\nAvoid to guess names for characters.\n"
    return q


def split_tags(s):
    return [t.strip() for t in re.split(r",|\n", s or "") if t.strip()]


def user_prompt(opts):
    """给模型的指令（API 和非 ToriiGate 的本地模型用）：选了预设就用当前版本的预设文字，自定义用用户写的。"""
    preset = (opts or {}).get("preset") or "detailed"
    if preset == "custom":
        return ((opts or {}).get("prompt") or "").strip() or PROMPT_PRESETS["detailed"]
    return PROMPT_PRESETS.get(preset) or PROMPT_PRESETS["detailed"]


def build_prompt(key, opts, tags=None):
    """本地模型的 (system, user)。tags：WD14 标签列表（「标签 + 自然语言」且勾了「交给模型参考」时才有）。"""
    opts = opts or {}
    if key == "toriigate":
        return TORII_SYSTEM, torii_query(opts.get("torii_format") or "long", tags or None, opts.get("use_names", True))
    user = user_prompt(opts)
    if tags:
        user += "\nReference tags (may contain mistakes): " + ", ".join(tags)
    return DEFAULT_SYSTEM, user


def clean_text(s):
    """模型输出整理成一行：去掉「Caption:」前缀、markdown 符号、换行。"""
    s = (s or "").strip()
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()
    s = re.sub(r"^(\*\*)?(caption|description)(\*\*)?\s*[:：]\s*", "", s, flags=re.I)
    s = re.sub(r"[#*`]+", "", s)
    s = re.sub(r"\s*\n+\s*", " ", s)
    return s.strip().strip('"').strip()


def compose(tags, text):
    """最终写进 .txt 的内容：只有句子就是句子；「标签 + 自然语言」是「标签, 标签. 句子」（和训练器一样用句号隔开）。"""
    t = clean_text(text)
    tags = (tags or "").strip().strip(",").strip()
    if tags and t:
        return tags + ". " + t
    return tags or t


# ============================================================
# 本地模型放在哪
# ============================================================
def model_dirname(repo):
    return repo.replace("/", "__")


def trainer_models_dir(trainer_root):
    """训练器的反推模型目录；trainer_root 不是装好的训练器时返回 ""。"""
    root = (trainer_root or "").strip()
    if not root or not os.path.isfile(os.path.join(root, "app", "main.py")):
        return ""
    return os.path.join(root, "models", "taggers")


def _incomplete_left(d):
    """目录里有没下完的东西（我们的 .part、huggingface_hub 的 .incomplete）"""
    for dp, dirs, fs in os.walk(d):
        for f in fs:
            if f.endswith((".part", ".incomplete")):
                return True
    return False


def is_complete(d):
    """模型目录是不是已经下完、能直接加载。

    有 .lora_complete 标记（启动器 / 训练器下载器下完写的）就算；训练器也会用 huggingface_hub 直接下，那样没有标记，
    这时看 config.json 在、权重分片齐全、没有下了一半的文件。"""
    if not d or not os.path.isfile(os.path.join(d, "config.json")):
        return False
    if os.path.isfile(os.path.join(d, MARK)):
        return not _incomplete_left(d)
    idx = os.path.join(d, "model.safetensors.index.json")
    try:
        if os.path.isfile(idx):
            with open(idx, encoding="utf-8") as f:
                shards = set((json.load(f).get("weight_map") or {}).values())
            if not shards or not all(os.path.isfile(os.path.join(d, s)) for s in shards):
                return False
        elif not any(f.endswith(".safetensors") for f in os.listdir(d)):
            return False
    except (OSError, ValueError, AttributeError):
        return False
    return not _incomplete_left(d)


def model_dirs(key, trainer_root):
    """这个模型可能在的位置 [(目录, 哪边)]，训练器的排前面（训练器下载的最常见）。"""
    info = VLM_MODELS.get(key)
    if not info:
        return []
    name = model_dirname(info["repo"])
    out = []
    t = trainer_models_dir(trainer_root)
    if t:
        out.append((os.path.join(t, name), "trainer"))
    out.append((os.path.join(LAUNCHER_MODELS, name), "launcher"))
    return out


def find_model(key, trainer_root):
    """已经下好的模型 (目录, "trainer" / "launcher")，两边都没有返回 ("", "")。"""
    for d, where in model_dirs(key, trainer_root):
        if is_complete(d):
            return d, where
    return "", ""


def download_dir(key, trainer_root):
    """新下载放哪：哪边已经下了一部分就接着下在那边；都没有时装了训练器放训练器（训练环境就在那边），否则放启动器。"""
    dirs = model_dirs(key, trainer_root)
    if not dirs:
        raise CaptionError(f"未知的模型：{key}")
    for d, _ in dirs:
        try:
            if os.path.isdir(d) and os.listdir(d):
                return d
        except OSError:
            pass
    return dirs[0][0]


def model_list(trainer_root):
    res = []
    for key, info in VLM_MODELS.items():
        d, where = find_model(key, trainer_root)
        res.append({"key": key, "label": info["label"], "repo": info["repo"], "size_gb": info["size_gb"],
                    "downloaded": bool(d), "where": where, "dir": d,
                    "urls": [f"https://hf-mirror.com/{info['repo']}/tree/main", f"https://huggingface.co/{info['repo']}/tree/main"]})
    return res


# ============================================================
# 下载（整个仓库：hf-mirror / 魔搭 / HF 官方轮换，断点续传，sha256 校验）
# ============================================================
_SOURCES = {
    "hf-mirror": ("HF 镜像 hf-mirror", "https://hf-mirror.com/api/models/{repo}/tree/main?recursive=true",
                  "https://hf-mirror.com/{repo}/resolve/main/{file}"),
    "modelscope": ("魔搭 ModelScope", "https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Recursive=true",
                   "https://www.modelscope.cn/models/{repo}/resolve/master/{file}"),
    "hf": ("HF 官方", "https://huggingface.co/api/models/{repo}/tree/main?recursive=true",
           "https://huggingface.co/{repo}/resolve/main/{file}"),
}
_SKIP_OLD_WEIGHTS = (".bin", ".pt", ".pth", ".msgpack", ".h5", ".ckpt")


def source_order(cfg, log_cb=None):
    """国内网络（或探测不出来）镜像和魔搭排前面，否则官方优先。"""
    use_mirror = True
    if cfg is not None:
        try:
            import mirror_manager as mm
            use_mirror = mm.resolve_mode(cfg, log_cb)
        except Exception:
            pass
    return ["hf-mirror", "modelscope", "hf"] if use_mirror else ["hf", "hf-mirror", "modelscope"]


def _parse_listing(kind, data):
    if kind == "modelscope":
        return [(x["Path"], int(x.get("Size") or 0), (x.get("Sha256") or "").lower())
                for x in ((data.get("Data") or {}).get("Files") or []) if x.get("Type") == "blob"]
    return [(x["path"], int(x.get("size") or 0), ((x.get("lfs") or {}).get("oid") or "").lower())
            for x in data if x.get("type") == "file"]


def filter_files(files, patterns=None):
    """和训练器一样：不下说明文档、隐藏文件；有 safetensors 时不下重复的旧格式权重。"""
    files = [f for f in files if not patterns or any(fnmatch.fnmatch(f[0], p) for p in patterns)]
    has_st = any(p.endswith(".safetensors") for p, _, _ in files)
    return [(p, s, h) for p, s, h in files
            if not os.path.basename(p).startswith(".") and not p.endswith((".md", ".gitattributes"))
            and not (has_st and p.lower().endswith(_SKIP_OLD_WEIGHTS))]


def list_repo_files(repo, order, log_cb=None, get=None):
    """仓库文件列表 [(路径, 大小, sha256)]；小文件在 HF 上不是 LFS、没有 sha256，下完只比大小。"""
    get = get or (lambda url: requests.get(url, timeout=15, headers={"User-Agent": "forge-launcher-vlm/1.0"}))
    last = None
    for kind in order:
        name, api, _ = _SOURCES[kind]
        try:
            r = get(api.format(repo=repo))
            r.raise_for_status()
            files = _parse_listing(kind, r.json())
            if files:
                return files
        except Exception as e:
            last = f"{name}: {type(e).__name__}: {str(e)[:120]}"
            if log_cb:
                log_cb(f"  读文件列表失败 —— {last}")
    raise CaptionError(f"读不到模型仓库 {repo} 的文件列表（hf-mirror / 魔搭 / HF 官方都试过了）：{last}")


def sha256_file(path, cancel_flag=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel_flag and cancel_flag():
                raise CaptionError("下载已取消")
            b = f.read(16 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _read_mark(d):
    try:
        with open(os.path.join(d, MARK), encoding="utf-8") as f:
            return json.load(f).get("sha256") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def download_repo(repo, local, progress_cb=None, log_cb=None, cancel_flag=None, cfg=None, fetch_one=None, get=None):
    """把仓库下到 local。已经下好、大小和 sha256 都对的文件跳过；每个文件按 source_order 换源，断了接着下。
    progress_cb(已下载字节, 总字节, 当前文件名)。都下好后写训练器格式的 .lora_complete。"""
    log = log_cb or (lambda t: None)
    if fetch_one is None:
        import wd14_tagger_core as wt
        fetch_one = wt._download_one
    order = source_order(cfg, log_cb)
    files = filter_files(list_repo_files(repo, order, log_cb, get))
    if not files:
        raise CaptionError(f"模型仓库 {repo} 里没有找到需要的文件")
    os.makedirs(local, exist_ok=True)
    checked = _read_mark(local)
    total = sum(s for _, s, _ in files)
    done_bytes = 0
    todo = []
    for p, s, h in files:
        fp = os.path.join(local, p)
        if os.path.isfile(fp) and (not s or os.path.getsize(fp) == s):
            if h and checked.get(p) != h:
                log(f"校验已下载的 {p} …")
                if sha256_file(fp, cancel_flag) != h:
                    log(f"⚠ {p} 和官方文件不一致（之前下载时损坏），删掉重下")
                    os.remove(fp)
                    todo.append((p, s, h))
                    continue
                checked[p] = h
            done_bytes += s
        else:
            todo.append((p, s, h))
    if todo:
        log(f"下载 {repo}：{len(todo)} 个文件，共 {sum(s for _, s, _ in todo) / 1e9:.2f} GB")
    for k, (p, s, h) in enumerate(sorted(todo, key=lambda x: x[1])):
        dest = os.path.join(local, p)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        log(f"[{k + 1}/{len(todo)}] {p}（{s / 1e6:.1f} MB）")
        fails = []
        ok = False
        for kind in order:
            name, _, tpl = _SOURCES[kind]
            url = tpl.format(repo=repo, file=urllib.parse.quote(p))
            for attempt in range(1, 3):
                if cancel_flag and cancel_flag():
                    raise CaptionError("下载已取消")
                try:
                    fetch_one(url, dest, (lambda _n, d, _t, base=done_bytes: progress_cb(base + d, total, p))
                              if progress_cb else None, log_cb, cancel_flag, p)
                except Exception as e:
                    if "取消" in str(e):
                        raise CaptionError("下载已取消")
                    fails.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
                    log(f"  {name} 失败 —— {fails[-1]}")
                    time.sleep(min(4, 2 * attempt))
                    continue
                if s and os.path.getsize(dest) != s:
                    fails.append(f"{name}: 大小不对")
                    os.remove(dest)
                    continue
                if h and sha256_file(dest, cancel_flag) != h:
                    fails.append(f"{name}: 和官方 sha256 不一致")
                    log(f"  ⚠ {p} 下载后校验不通过，换一个源重下")
                    os.remove(dest)
                    break
                if h:
                    checked[p] = h
                ok = True
                break
            if ok:
                break
        if not ok:
            raise CaptionError(f"{p} 下载失败，所有下载源都试过了：\n" + "\n".join("  · " + f for f in fails[-6:]) +
                               "\n\n已下好的部分保留着，重新点下载会接着下。也可以到「高级选项」改网络模式后再试。")
        done_bytes += s
    with open(os.path.join(local, MARK), "w", encoding="utf-8") as f:
        json.dump({"v": MARK_VERSION, "repo": repo, "sha256": checked}, f)
    return local


# ============================================================
# 云端 API（OpenAI 兼容格式，和训练器 tagging.py 一样）
# ============================================================
class Refused(Exception):
    """API 模型拒答（直接拒绝、被安全审核拦下，或者只给了一段刻意回避的描述）。"""


_BLOCKED = re.compile(r"safety|prohibited|content[_ ]?(policy|filter|management)|moderation|blocked|harm_category|"
                      r"sensitive|inappropriate|violat|敏感|违规|审核|不当内容", re.I)
_REFUSE = [re.compile(x, re.I) for x in (
    r"\bI(?:'m| am)\s+(?:sorry|unable|not able|not comfortable|afraid I can(?:'|no)t)\b",
    r"\bI\s+(?:can(?:'|no)t|cannot|won't|will not|must decline|am not going to|'m not going to|do not feel comfortable)\s+"
    r"(?:help|assist|provide|describe|create|generate|fulfil+|comply|do that|write|caption|process|analy[sz]e|engage|produce|"
    r"go into|detail|depict|discuss|continue|share)",
    r"\b(?:sorry|apolog(?:ize|ies))\b[,.!]?\s+(?:but\s+)?I\b",
    r"\bas an AI\b",
    r"\b(?:violates?|against|breach(?:es)?|not (?:allowed|permitted) (?:by|under))\s+(?:my|the|our|openai'?s?|google'?s?|content|usage|safety)?\s*"
    r"(?:content |usage |safety )?(?:guidelines|polic(?:y|ies)|rules|terms)",
    r"\b(?:not (?:able|allowed|permitted)|unable)\s+to\s+(?:describe|provide|generate|process|assist|help|analy[sz]e|caption)",
    r"\b(?:without|avoid(?:ing)?|won'?t go into|not go(?:ing)? into|omit(?:ting)?|refrain(?:ing)? from|leav(?:e|ing) out|exclud(?:e|ing)|skip(?:ping)?)\s+"
    r"(?:\w+\s+){0,4}(?:explicit|graphic|sexual|nsfw|intimate|lewd|suggestive)",
    r"\b(?:non-?explicit|non-?sexual|tasteful(?:ly)?|family[- ]friendly|safe[- ]for[- ]work|sfw|pg-?13|general audience)\s+"
    r"(?:way|manner|description|terms|version|overview|summary|fashion|level)",
    r"\bhere(?:'s| is)\s+(?:a |an )?(?:general|high[- ]level|non-?explicit|safe|tasteful|brief|limited|toned[- ]down|sanitized)\s+"
    r"(?:description|overview|summary|version)",
    r"[\[(]\s*(?:[\w-]+\s+){0,3}(?:redacted|removed|omitted)\s*[\])]",
    r"(?:抱歉|对不起|很遗憾|不好意思)[，,。！!\s]*(?:我)?(?:无法|不能|没法|不便|不可以)",
    r"(?:无法|不能|不便)(?:为你|为您|帮你|帮您)?(?:提供|描述|处理|协助|完成|生成|分析|识别)",
    r"(?:违反|不符合|违背)(?:了)?(?:相关|平台|内容|使用)?(?:的)?(?:政策|规定|准则|规范|条款)",
    r"(?:避免|不涉及|略过|省略|跳过|不描述|不展开)(?:任何)?(?:过于)?(?:露骨|色情|敏感|成人|性)(?:的)?(?:细节|内容|描写|部分)",
)]
_QUOTED = re.compile(r'"[^"\n]{0,400}"|“[^”\n]{0,400}”|「[^」\n]{0,400}」|『[^』\n]{0,400}』')


def refusal_reason(text, finish_reason=None, refusal=None):
    """API 的回答是不是拒答：是就返回原因，不是返回 ""。只看引号外面的文字（照抄的图中文字可能就是「I'm sorry」）。"""
    if refusal:
        return "模型拒答：" + str(refusal)[:80]
    if str(finish_reason or "").lower() in ("content_filter", "safety", "prohibited_content", "blocklist", "spii", "recitation"):
        return f"被 API 的安全审核拦下（{finish_reason}）"
    t = (text or "").strip()
    if not t:
        return "API 返回了空内容（多半是被安全审核拦下）"
    bare = _QUOTED.sub(" ", t)
    for r in _REFUSE:
        m = r.search(bare)
        if m:
            return "模型拒答：" + bare[max(0, m.start() - 20):m.end() + 40].replace("\n", " ").strip()
    return ""


def _post(api, body, timeout):
    url = api["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {api.get('api_key', '')}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def api_call(api, img_path, prompt, max_tokens, post=None):
    """发一张图，返回描述；拒答抛 Refused，其它错误原样抛出（HTTPError 带 .body）。"""
    post = post or _post
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(img_path)[1].lower().lstrip(".")
    mime = {"jpg": "jpeg"}.get(ext, ext) or "png"
    body = {"model": api["model"], "max_tokens": int(max_tokens or 600), "temperature": 0.4,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}}]}]}
    try:
        d = post(api, body, 180)
    except urllib.error.HTTPError as e:
        # 安全审核直接拦下的（Gemini PROHIBITED_CONTENT、OpenAI content_policy_violation……）算拒答，不算请求出错
        text = ""
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code in (400, 403, 422, 451) and _BLOCKED.search(text):
            raise Refused(f"被 API 的安全审核拦下（HTTP {e.code}）")
        e.body = text
        raise
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    c = msg.get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    c = re.sub(r"<think>.*?</think>", "", c or "", flags=re.S).strip()
    why = refusal_reason(c, ch.get("finish_reason"), msg.get("refusal"))
    if why:
        raise Refused(why)
    return c


def api_error_zh(e):
    s = str(e)
    if hasattr(e, "code"):
        body = getattr(e, "body", None)
        if body is None:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
        zh = {401: "API Key 不对或已失效", 403: "没有权限（Key 无效或地区不支持）", 404: "地址或模型名不对",
              429: "请求太频繁或额度用完", 400: "请求被拒绝（模型可能不支持图片输入）"}.get(e.code, f"HTTP {e.code}")
        return f"{zh}：{(body or '')[:300]}"
    if "timed out" in s:
        return "请求超时"
    if "getaddrinfo" in s or "Name or service" in s:
        return "连不上服务器（地址不对或网络不通）"
    return s


def is_fatal_api_error(e):
    """Key / 地址 / 模型名不对：后面的图也一样会失败，整批停下来。"""
    return getattr(e, "code", None) in (401, 403, 404)


def test_api(api, post=None):
    """发一个纯文本请求，确认地址 / Key / 模型能用。"""
    if not (api or {}).get("base_url") or not api.get("model"):
        return {"ok": False, "message": "请填写接口地址和模型名"}
    post = post or _post
    try:
        post(api, {"model": api["model"], "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}, 30)
        return {"ok": True, "message": "连接成功"}
    except Exception as e:
        return {"ok": False, "message": api_error_zh(e)}
