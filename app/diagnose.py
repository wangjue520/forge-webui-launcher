# -*- coding: utf-8 -*-
"""
报错诊断（本地规则层）：从运行日志里认出常见报错，给出说明和一键修复。不联网就能用。

日志来源：LauncherApi._emit 把每条 log 事件顺手抄一份进 LOGS（按来源分开的环形缓冲），
诊断从这里取，不用改各处推日志的代码。实例日志的来源键是 "inst:<iid>"，其余是事件的
scope（deploy / ext / ver / civitai ...），启动器自己的内部错误记在 "app"。

一条规则 = 正则 + 构造函数。构造函数拿到匹配和上下文（实例类型、配置、目录），返回一个
「发现」：标题、说明、证据行、修复动作列表。动作只能是 ACTIONS 白名单里的，按风险分级：
  ui      前端自己做（打开页面 / 网址）
  low     改启动参数、切镜像、重新检测网络这类可撤销的；规则认定没有歧义时标 auto，
          诊断时自动执行（启动器设置里能关），并在日志里写明、给「撤销」
  confirm 装依赖 / 装节点 / 停用扩展 / 重启实例，用户点了才做
  danger  删文件（移到回收站），点了还要再确认一次
AI 层（diagnose_llm）推荐的动作也走这张表校验，且一律要用户点。
"""
import collections
import hashlib
import json
import os
import re
import struct
import threading
import time

import config_manager as cm

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER_LOG = os.path.join(APP_DIR, "launcher_data", "launcher.log")
NODE_MAP_CACHE = os.path.join(APP_DIR, "launcher_data", "comfy_node_map.json")
NODE_MAP_URL = "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main/extension-node-map.json"
MODEL_LIST_CACHE = os.path.join(APP_DIR, "launcher_data", "comfy_model_list.json")
MODEL_LIST_URL = "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main/model-list.json"

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# 实例日志里每轮启动的开头（process_manager 打的），诊断只看最近一轮
_RUN_MARKERS = ("[启动器] 正在启动 ComfyUI", "[启动器] 正在启动 webui.bat")


# ============================================================
# 日志缓冲
# ============================================================

class LogBook:
    """按来源分开的日志环形缓冲。进度条用 \\r 反复刷同一行，只留最后一次的内容。"""

    def __init__(self, max_lines=3000):
        self.max_lines = max_lines
        self._lock = threading.Lock()
        self._lines = {}
        self._partial = {}

    def add(self, key, text):
        if not text:
            return
        text = _ANSI_RE.sub("", text)
        with self._lock:
            buf = self._lines.get(key)
            if buf is None:
                buf = self._lines[key] = collections.deque(maxlen=self.max_lines)
            parts = (self._partial.pop(key, "") + text).split("\n")
            for line in parts[:-1]:
                buf.append(line.rsplit("\r", 1)[-1] if "\r" in line.rstrip("\r") else line.rstrip("\r"))
            if parts[-1]:
                self._partial[key] = parts[-1]

    def tail(self, key, n=None):
        with self._lock:
            lines = list(self._lines.get(key) or ())
            part = self._partial.get(key, "")
        if part:
            lines.append(part.rsplit("\r", 1)[-1])
        if n:
            lines = lines[-n:]
        return "\n".join(lines)

    def keys(self):
        with self._lock:
            return [k for k, v in self._lines.items() if v] + [k for k in self._partial if k not in self._lines]

    def clear(self, key):
        with self._lock:
            self._lines.pop(key, None)
            self._partial.pop(key, None)


LOGS = LogBook()


def inst_key(iid):
    return f"inst:{iid}"


def current_run(text):
    """实例日志只看最近一轮启动：之前那轮的报错可能已经修好了"""
    pos = max(text.rfind(m) for m in _RUN_MARKERS)
    return text[pos:] if pos > 0 else text


def read_launcher_log(max_bytes=200_000):
    try:
        with open(LAUNCHER_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# 有这些词的日志块才值得跑一遍规则（流式检测用，省得每块日志都跑全部正则）
QUICK_TRIGGER = re.compile(
    r"error|exception|traceback|nan|import failed|cannot import|does not exist|not found|out of memory|"
    r"页面文件|paging file|proxy|timed out|timeout|max retries|no module named|numpy|refused|"
    r"ssl|unauthorized|forbidden|no space|空间不足|deserializ|pytorchstreamreader|invalid value encountered|"
    r"\[WWY诊断\]",
    re.IGNORECASE)


# ============================================================
# 小工具（规则和修复动作共用，单测直接测这些）
# ============================================================

_MODEL_PATH_RE = re.compile(
    r"((?:[A-Za-z]:|\\\\[^\\/\s]+)[\\/][^\r\n\"'<>|*?]*?\.(?:safetensors|ckpt|pth|pt|bin|gguf|sft))\b",
    re.IGNORECASE)


def find_model_path(text):
    """日志里最后出现的模型文件路径（报错前通常会打一行「Loading weights ... from xxx」）。
    repr 打出来的字典里反斜杠是双写的，先还原。"""
    text = (text or "").replace("\\\\", "\\")
    hits = _MODEL_PATH_RE.findall(text)
    for p in reversed(hits):
        p = p.strip().strip("'\"")
        # 「from D:\a b\x.safetensors」前面可能粘着别的词：从盘符起算
        m = re.search(r"[A-Za-z]:[\\/]", p)
        if m:
            p = p[m.start():]
        return p
    return ""


def check_model_file(path):
    """核对模型文件完整性，返回 (状态, 说明)。状态：ok / corrupt / missing / unknown。
    只读文件头和文件尾，几 GB 的文件也是毫秒级。"""
    if not path or not os.path.isfile(path):
        return "missing", "文件不存在"
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return "unknown", f"读不到文件大小：{e}"
    if size == 0:
        return "corrupt", "文件大小是 0（下载失败留下的空文件）"
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            if ext in (".safetensors", ".sft"):
                if len(head) < 8:
                    return "corrupt", "文件太小，连文件头都不完整"
                n = struct.unpack("<Q", head)[0]
                if n <= 1 or n > min(size - 8, 100 * 1024 * 1024):
                    return "corrupt", "文件头声明的长度不对（下载不完整，或者根本不是 safetensors 文件）"
                try:
                    header = json.loads(f.read(n).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return "corrupt", "文件头不是有效的 JSON（文件损坏）"
                end = 0
                for k, v in header.items():
                    if k != "__metadata__" and isinstance(v, dict):
                        offs = v.get("data_offsets") or [0, 0]
                        end = max(end, int(offs[1]))
                need = 8 + n + end
                if size < need:
                    return "corrupt", (f"文件不完整：应有 {_mb(need)}，实际只有 {_mb(size)}"
                                       f"（缺 {_mb(need - size)}，多半是下载中断）")
                return "ok", f"文件头完整，大小 {_mb(size)}"
            if ext == ".gguf":
                if head[:4] != b"GGUF":
                    return "corrupt", "文件开头不是 GGUF 标识（文件损坏或下载的是网页）"
                return "ok", f"GGUF 文件头正常，大小 {_mb(size)}"
            if head[:2] == b"PK":
                import zipfile
                try:
                    with zipfile.ZipFile(path) as z:
                        z.namelist()
                except (zipfile.BadZipFile, OSError):
                    return "corrupt", "PyTorch 模型的 zip 结构损坏（找不到目录区，多半是下载不完整）"
                return "ok", f"zip 结构完整，大小 {_mb(size)}"
            if head[:1] == b"\x80":
                return "unknown", "老式 pickle 格式，没法只看文件头判断是否完整"
            if head.lstrip()[:1] in (b"<", b"{"):
                return "corrupt", "文件内容是网页 / 文本，不是模型（下载链接需要登录或已失效）"
    except OSError as e:
        return "unknown", f"读不了文件：{e}"
    return "unknown", "不认识的文件格式"


def _mb(n):
    return f"{n / 1024 / 1024:.1f} MB" if n < 1024 ** 3 else f"{n / 1024 ** 3:.2f} GB"


def civitai_page(model_path):
    """模型旁边的 .civitai.info 里记着来源 → Civitai 页面地址（重新下载用）"""
    base = os.path.splitext(model_path)[0]
    try:
        with open(base + ".civitai.info", "r", encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return ""
    mid, vid = info.get("modelId"), info.get("versionId")
    if not mid:
        return ""
    return f"https://civitai.com/models/{mid}" + (f"?modelVersionId={vid}" if vid else "")


# 模块名 → pip 包名（不一样的那些；其余默认同名）
_MODULE_PKG = {
    "cv2": "opencv-python", "PIL": "pillow", "skimage": "scikit-image", "sklearn": "scikit-learn",
    "yaml": "pyyaml", "google.protobuf": "protobuf", "dateutil": "python-dateutil", "bs4": "beautifulsoup4",
    "Crypto": "pycryptodome", "fvcore": "fvcore", "insightface": "insightface", "mediapipe": "mediapipe",
    "onnxruntime": "onnxruntime-gpu", "segment_anything": "segment-anything", "git": "gitpython",
    "attr": "attrs", "dotenv": "python-dotenv", "jwt": "pyjwt", "magic": "python-magic",
    "sentencepiece": "sentencepiece", "lark": "lark", "piexif": "piexif", "ultralytics": "ultralytics",
}
_NEVER_PIP = {"torch", "torchvision", "torchaudio", "xformers", "triton"}


def module_to_package(mod):
    mod = (mod or "").strip().strip("'\"")
    if mod in _MODULE_PKG:
        return _MODULE_PKG[mod]
    top = mod.split(".")[0]
    if top in _NEVER_PIP:
        return ""
    return _MODULE_PKG.get(top, top.replace("_", "-"))


def comfy_declared_args(comfy_dir):
    """ComfyUI comfy/cli_args.py 里声明的参数；读不到返回 None（不做过滤）"""
    try:
        with open(os.path.join(comfy_dir, "comfy", "cli_args.py"), "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    return set(cm._ARG_LITERAL_RE.findall(text)) or None


def declared_flags(ctx):
    if ctx.get("kind") == "comfy":
        return comfy_declared_args(ctx.get("comfy_dir") or "")
    return cm.declared_args(ctx.get("root") or "")


# ============================================================
# 修复动作白名单
# ============================================================

# 能被诊断改的实例配置键（其余一概不让改，AI 推荐了也不认）
_INST_KEYS = {
    "vram_mode": {k for _l, k, _a in cm.VRAM_MODE_OPTIONS_LEGACY + cm.VRAM_MODE_OPTIONS_NEO2},
    "precision_mode": {k for _l, k, _a in cm.PRECISION_MODE_OPTIONS_LEGACY + cm.PRECISION_MODE_OPTIONS_NEO2},
    "unet_precision": {k for _l, k, _a in cm.UNET_PRECISION_OPTIONS_NEO2},
    "vae_precision": {k for _l, k, _a in cm.VAE_PRECISION_OPTIONS_NEO2},
    "text_enc_precision": {k for _l, k, _a in cm.TEXT_ENC_PRECISION_OPTIONS_NEO2},
    "attention_impl": {k for _l, k, _a in cm.ATTENTION_OPTIONS_NEO2},
    "cuda_malloc": {True, False}, "fast_fp16": {True, False}, "pin_shared_memory": {True, False},
    "install_xformers": {True, False}, "install_sage": {True, False}, "comfy_sage": {True, False},
    "comfy_flash": {True, False}, "comfy_fast": {True, False},
}
_LAUNCHER_KEYS = {"mirror_mode": {"auto", "always", "never"}, "proxy_enabled": {True, False}}
_ARG_RE = re.compile(r"^--[A-Za-z0-9][\w-]*(?:\s+[\w.:/\\-]+)?$")
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[\w,.-]+\])?(?:[<>=!~]=?[\w.*+-]+(?:,[<>=!~]=?[\w.*+-]+)*)?$")
_PAGES = {"launch", "instances", "settings", "launcher", "deploy", "versions", "civitai", "models", "outputs",
          "extensions", "wd14", "meta"}

ACTIONS = {
    "open_page": {"risk": "ui", "params": "page: launch（一键启动）/ settings（高级选项）/ launcher（启动器设置）/ deploy（环境部署）/ "
                                    "versions（版本管理）/ civitai（模型下载）/ models（模型管理）/ extensions（常用插件）/ instances（实例管理）",
                  "desc": "打开启动器的某个页面（让用户去那里操作）"},
    "open_url": {"risk": "ui", "params": "url: http(s) 网址", "desc": "在浏览器里打开网址（驱动下载页、文档等）"},
    "civitai_redownload": {"risk": "ui", "params": "url: civitai.com 模型页地址",
                           "desc": "打开「模型下载」页并填好 Civitai 地址，重新下载损坏的模型"},
    "reveal_path": {"risk": "low", "params": "path: 文件或文件夹", "desc": "在资源管理器里定位文件"},
    "set_instance_cfg": {"risk": "low", "params": "iid, changes: {键: 值}，键只能是 " + "/".join(sorted(_INST_KEYS)),
                         "desc": "改实例的高级选项（显存模式、精度等），下次启动生效，可撤销"},
    "add_launch_arg": {"risk": "low", "params": "iid, arg: 例如 --no-half-vae（必须是这个版本认识的参数）",
                       "desc": "往实例的附加参数里加一个启动参数，下次启动生效，可撤销"},
    "remove_launch_arg": {"risk": "low", "params": "iid, arg", "desc": "从附加参数里去掉一个启动参数，可撤销"},
    "set_launcher_cfg": {"risk": "low", "params": "changes: mirror_mode=auto/always/never，proxy_enabled=true/false",
                         "desc": "改启动器网络设置（国内镜像、启动器代理开关），可撤销"},
    "redetect_network": {"risk": "low", "params": "无", "desc": "重新检测网络环境（决定走不走国内镜像）"},
    "restart_instance": {"risk": "confirm", "params": "iid", "desc": "停止并重新启动实例（会中断正在生成的任务）"},
    "comfy_install_deps": {"risk": "confirm", "params": "iid, node_dir: custom_nodes 下的节点文件夹",
                           "desc": "给 ComfyUI 节点重装依赖（requirements.txt + install.py），实例需先停止"},
    "comfy_install_node": {"risk": "confirm", "params": "iid, url: https://github.com/作者/仓库, name",
                           "desc": "从 GitHub 安装一个 ComfyUI 节点并装依赖，实例需先停止"},
    "pip_install": {"risk": "confirm", "params": "iid, packages: [包名或版本约束，如 numpy<2]",
                    "desc": "往 ComfyUI 实例的 Python 里装 / 换 pip 包（不允许动 torch 系），实例需先停止"},
    "ext_disable": {"risk": "confirm", "params": "iid, rel: 扩展/节点文件夹名", "desc": "停用一个出错的扩展或节点（可再启用）"},
    "download_model": {"risk": "confirm",
                       "params": "iid, url: 直接下载地址（huggingface.co/…/resolve/…、civitai.com/api/download/models/版本号、"
                                 "modelscope.cn/…、github.com/…/releases/download/…）, dir: 模型要放的文件夹（缺模型发现里给出的目录）, "
                                 "name: 文件名（和工作流里写的一致）",
                       "desc": "在后台下载缺的模型到 ComfyUI 的模型目录（可续传，HuggingFace 按网络情况走 hf-mirror）"},
    "comfy_set_mode": {"risk": "low",
                       "params": "iid, node_ids: [节点 id，见 current_workflow.json 里 nodes[].id], mode: bypass（绕过，输入直通到输出）"
                                 " / mute（静音，不执行）/ enable（恢复）",
                       "desc": "改 ComfyUI 网页里当前打开的工作流：绕过 / 静音 / 恢复节点（ComfyUI 要开着、网页要打开着；"
                               "改完网页里标成未保存，用户自己 Ctrl+S），可撤销"},
    "comfy_set_widget": {"risk": "low",
                         "params": "iid, node_id, widget: 参数名（current_workflow.json 里节点的 widgets_values 对应的名字，"
                                   "不确定就先读文件确认）, value: 新值（数字 / 文字 / true false；下拉框必须是可选项之一）",
                         "desc": "改 ComfyUI 网页里当前工作流某个节点的参数（同上，要网页开着，可撤销）"},
    "ai_ask": {"risk": "ui", "params": "note: 交给 AI 的问题说明", "desc": "让 AI 接着查（规则查不到下载地址时用）"},
    "trash_file": {"risk": "danger", "params": "path: 模型文件", "desc": "把损坏的模型文件移到回收站"},
}
_DL_URL_RE = re.compile(
    r"^https://(?:(?:huggingface\.co|hf-mirror\.com)/\S+/resolve/\S+|civitai\.(?:com|green)/api/download/models/\d+\S*|"
    r"(?:www\.)?modelscope\.cn/\S+|github\.com/\S+/releases/download/\S+)$")


def action(aid, label, auto=False, **params):
    return {"aid": aid, "label": label, "params": params, "risk": ACTIONS[aid]["risk"], "auto": bool(auto)}


def actions_doc():
    """给 AI 看的动作说明"""
    return "\n".join(f"- {aid}（{a['risk']}）：{a['desc']}。参数：{a['params']}" for aid, a in ACTIONS.items())


def validate_action(api, aid, params, allowed_roots=()):
    """校验一个动作（主要防 AI 乱推荐）。返回 (规整后的 params, 错误)"""
    a = ACTIONS.get(aid)
    if not a:
        return None, f"未知动作 {aid}"
    p = dict(params or {})
    if "iid" in p or aid in ("set_instance_cfg", "add_launch_arg", "remove_launch_arg", "restart_instance",
                             "comfy_install_deps", "comfy_install_node", "pip_install", "ext_disable",
                             "download_model", "comfy_set_mode", "comfy_set_widget"):
        iid = p.get("iid") or api.cfg.get("active_instance")
        if not cm.find_instance(api.cfg, iid):
            return None, "实例不存在"
        p["iid"] = iid
    if aid == "open_page":
        if p.get("page") not in _PAGES:
            return None, "未知页面"
    elif aid in ("open_url", "civitai_redownload"):
        url = str(p.get("url") or "")
        if not re.match(r"^https?://[^\s]+$", url):
            return None, "网址不对"
        if aid == "civitai_redownload" and not re.match(r"^https://civitai\.(com|green)/models/\d+", url):
            return None, "不是 Civitai 模型页"
    elif aid == "set_instance_cfg":
        ch = p.get("changes")
        if not isinstance(ch, dict) or not ch:
            return None, "缺少 changes"
        for k, v in ch.items():
            if k not in _INST_KEYS or v not in _INST_KEYS[k]:
                return None, f"不允许把 {k} 改成 {v}"
    elif aid in ("add_launch_arg", "remove_launch_arg"):
        arg = str(p.get("arg") or "").strip()
        if not _ARG_RE.match(arg):
            return None, "参数格式不对"
        if aid == "add_launch_arg":
            ctx = instance_ctx(api, p["iid"])
            declared = declared_flags(ctx)
            if declared is not None and arg.split()[0] not in declared:
                return None, f"这个版本不认识 {arg.split()[0]}"
        p["arg"] = arg
    elif aid == "set_launcher_cfg":
        ch = p.get("changes")
        if not isinstance(ch, dict) or not ch:
            return None, "缺少 changes"
        for k, v in ch.items():
            if k not in _LAUNCHER_KEYS or v not in _LAUNCHER_KEYS[k]:
                return None, f"不允许把 {k} 改成 {v}"
    elif aid == "comfy_install_node":
        url = str(p.get("url") or "").strip().rstrip("/")
        if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+$", url):
            return None, "只支持 GitHub 仓库地址"
        p["url"] = url
    elif aid == "pip_install":
        pk = [str(x).strip() for x in (p.get("packages") or []) if str(x).strip()]
        if not pk or any(not _PKG_RE.match(x) for x in pk):
            return None, "包名格式不对"
        if any(re.split(r"[<>=!~\[]", x)[0].lower() in _NEVER_PIP for x in pk):
            return None, "不允许改 torch 系的包（会把 CUDA 版换成 CPU 版）"
        p["packages"] = pk
    elif aid in ("reveal_path", "trash_file"):
        path = os.path.abspath(str(p.get("path") or ""))
        if not os.path.exists(path):
            return None, "文件不存在"
        if aid == "trash_file":
            if not path.lower().endswith(MODEL_EXTS) or not os.path.isfile(path):
                return None, "只能删模型文件"
            if allowed_roots and not any(_under(path, r) for r in allowed_roots):
                return None, "文件不在实例或模型库目录里"
        p["path"] = path
    elif aid == "comfy_install_deps":
        nd = os.path.abspath(str(p.get("node_dir") or ""))
        ctx = instance_ctx(api, p["iid"])
        if not ctx.get("comfy_dir") or not _under(nd, os.path.join(ctx["comfy_dir"], "custom_nodes")) \
                or not os.path.isdir(nd):
            return None, "不是这个实例的节点文件夹"
        p["node_dir"] = nd
    elif aid == "download_model":
        url = str(p.get("url") or "").strip()
        if not _DL_URL_RE.match(url):
            return None, "只能从 HuggingFace / hf-mirror / Civitai / ModelScope / GitHub Releases 的直接下载地址下"
        name = str(p.get("name") or "").strip()
        if not name or os.path.basename(name) != name or name in (".", "..") or \
                not name.lower().endswith(MODEL_EXTS + (".onnx",)):
            return None, "文件名不对（只能是模型文件名，不能带路径）"
        d = os.path.abspath(str(p.get("dir") or ""))
        if not p.get("dir") or not allowed_roots or not any(_under(d, r) for r in allowed_roots):
            return None, "目标文件夹不在实例或模型库目录里"
        p.update(url=url, name=name, dir=d)
    elif aid == "ai_ask":
        p["note"] = str(p.get("note") or "")[:2000]
    elif aid in ("comfy_set_mode", "comfy_set_widget"):
        if not cm.is_comfy(cm.instance_cfg(api.cfg, p["iid"]) or {}):
            return None, "只有 ComfyUI 实例能改工作流"
        if aid == "comfy_set_mode":
            ids = p.get("node_ids")
            ids = ids if isinstance(ids, list) else [ids]
            try:
                ids = [int(x) for x in ids][:20]
            except (TypeError, ValueError):
                return None, "节点 id 要是数字"
            if not ids or p.get("mode") not in ("bypass", "mute", "enable"):
                return None, "缺少节点 id，或 mode 不是 bypass / mute / enable"
            p["node_ids"] = ids
        else:
            try:
                p["node_id"] = int(p.get("node_id"))
            except (TypeError, ValueError):
                return None, "节点 id 要是数字"
            w, v = p.get("widget"), p.get("value")
            if not isinstance(w, str) or not w.strip() or len(w) > 80:
                return None, "缺少参数名"
            if not isinstance(v, (str, int, float, bool)) or (isinstance(v, str) and len(v) > 1000):
                return None, "参数值只能是数字、文字或 true / false"
    return p, None


def _under(path, root):
    if not root:
        return False
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


# ============================================================
# 上下文
# ============================================================

def instance_ctx(api, iid):
    cfg = cm.instance_cfg(api.cfg, iid) or {}
    root = (cfg.get("webui_root") or "").strip()
    comfy = cm.is_comfy(cfg)
    ctx = {"kind": "comfy" if comfy else "forge", "iid": iid, "cfg": cfg, "root": root,
           "branch": cfg.get("webui_branch", ""), "name": cfg.get("_name") or cm.kind_label(
               cfg.get("webui_branch"), cfg.get("webui_variant")),
           "comfy_dir": cm.comfy_layout(root)[0] if comfy and root else "",
           "a1111": (not comfy) and cm.is_a1111(root)}
    return ctx


def launcher_ctx(api):
    return {"kind": "launcher", "iid": None, "cfg": api.cfg, "root": APP_DIR, "branch": "", "name": "启动器",
            "comfy_dir": "", "a1111": False}


# ============================================================
# 规则
# ============================================================

class Rule:
    def __init__(self, rid, pattern, build, kinds=None):
        self.id = rid
        self.re = re.compile(pattern, re.IGNORECASE | re.MULTILINE) if isinstance(pattern, str) else pattern
        self.build = build
        self.kinds = kinds


def _line_at(text, pos):
    s = text.rfind("\n", 0, pos) + 1
    e = text.find("\n", pos)
    return text[s:e if e >= 0 else len(text)].strip()[:400]


def _finding(rule, title, text, evidence="", actions=(), level="error", key=None, **facts):
    return {"rule": rule, "key": key or rule, "level": level, "title": title, "text": text,
            "evidence": evidence, "actions": list(actions), "facts": facts}


# ---- ComfyUI 节点 ----

def _b_comfy_import(m, text, ctx):
    node_dir, reason = m.group(1).strip(), m.group(2).strip()
    name = os.path.basename(node_dir.rstrip("\\/"))
    acts = []
    mm = re.search(r"No module named '([^']+)'", reason)
    if ctx.get("iid"):
        if os.path.isfile(os.path.join(node_dir, "requirements.txt")) or os.path.isfile(
                os.path.join(node_dir, "install.py")):
            acts.append(action("comfy_install_deps", f"重装「{name}」的依赖", iid=ctx["iid"], node_dir=node_dir))
        if mm and module_to_package(mm.group(1)):
            pkg = module_to_package(mm.group(1))
            acts.append(action("pip_install", f"安装缺的包 {pkg}", iid=ctx["iid"], packages=[pkg]))
        acts.append(action("ext_disable", f"先停用「{name}」", iid=ctx["iid"], rel=name))
    why = f"缺少 Python 包 {mm.group(1)}" if mm else reason[:200]
    return _finding("comfy_import_failed", f"节点「{name}」加载失败",
                    f"ComfyUI 启动时没能导入节点「{name}」：{why}。\n"
                    "这个节点提供的功能都用不了，用到它的工作流会报「缺少节点」。"
                    "多半是节点的依赖没装上（手动 git clone / 解压的节点不会自动装依赖），"
                    "或者节点版本太新 / 太旧，跟当前 ComfyUI 对不上。",
                    m.group(0), acts, key=f"comfy_import_failed:{name}", node_dir=node_dir)


def _b_comfy_missing(m, text, ctx):
    names = sorted(set(re.findall(r"node (\S+?) does not exist|Node '([^']+)' not found|"
                                  r"'class_type':\s*'([^']+)'", text)))
    flat = sorted({x for tup in names for x in tup if x and not x.startswith("ID")})
    return _finding("comfy_missing_node", "工作流里有没装的节点",
                    "工作流用到了这个 ComfyUI 里没有的节点：" + ("、".join(flat[:12]) or m.group(0)) + "。\n"
                    "装上对应的节点包再重启 ComfyUI 就行。诊断会去 ComfyUI-Manager 的节点表里查是哪个包。",
                    m.group(0), [], key="comfy_missing_node", node_types=flat)


# 启动器桥接插件（assets/comfyui/wwy_launcher_bridge）打的日志：网页加载工作流时查出来的缺节点 / 缺模型。
# 每行都是当前工作流的完整状态，只看最后一行（缺的都补上了会打一行「检查通过」，发现随之消失）
BRIDGE_RE = re.compile(r"\[WWY诊断\] (工作流「[^\n]*?) (\{[^\n]*\})[ \t\r]*$", re.MULTILINE)


def _bridge(m):
    try:
        r = json.loads(m.group(2))
    except ValueError:
        return None
    return r if isinstance(r, dict) else None


def _bridge_sig(items):
    return hashlib.md5(json.dumps(sorted(items), ensure_ascii=False).encode("utf-8")).hexdigest()[:8]


def _b_bridge_nodes(m, text, ctx):
    r = _bridge(m)
    types = [str(t) for t in (r or {}).get("nodes") or []]
    if not types:
        return None
    return _finding("comfy_missing_node", "工作流里有没装的节点",
                    f"工作流「{r.get('wf') or '当前工作流'}」用到了这个 ComfyUI 里没有的节点：{'、'.join(types[:12])}。\n"
                    "ComfyUI 网页会直接拦着不让运行，所以运行日志里没有报错，是启动器桥接插件在网页里查到的。\n"
                    "装上对应的节点包再重启 ComfyUI 就行。诊断会去 ComfyUI-Manager 的节点表里查是哪个包。",
                    m.group(1), [], key="comfy_missing_node:" + _bridge_sig(types), node_types=types,
                    cnr=r.get("cnr") or {})


def _b_bridge_models(m, text, ctx):
    r = _bridge(m)
    models = [x for x in (r or {}).get("models") or [] if isinstance(x, dict) and x.get("name")]
    if not models:
        return None
    rows = []
    for x in models[:12]:
        where = f"，放 models\\{x['folder']}" if x.get("folder") else ""
        rows.append(f"· {x['name']}（{x.get('node_title') or x.get('node_type') or '?'} 的 {x.get('input') or '?'}{where}）")
    return _finding("comfy_missing_model", "工作流里的模型本机没有",
                    f"工作流「{r.get('wf') or '当前工作流'}」选的这些模型在本机的模型目录里找不到：\n" + "\n".join(rows) +
                    "\n运行时会被网页拦下，或者跑到用它的节点时报错。下好放进对应目录后，在网页里按 R 刷新再运行。",
                    m.group(1), [], key="comfy_missing_model:" + _bridge_sig(x["name"] for x in models),
                    models=models[:12])


# ---- 网络 ----

_HOSTS = {
    "huggingface.co": "HuggingFace", "hf.co": "HuggingFace", "cdn-lfs.huggingface.co": "HuggingFace",
    "cas-bridge.xethub.hf.co": "HuggingFace", "github.com": "GitHub", "raw.githubusercontent.com": "GitHub",
    "objects.githubusercontent.com": "GitHub", "codeload.github.com": "GitHub", "api.github.com": "GitHub",
    "civitai.com": "Civitai", "civitai.green": "Civitai", "download.pytorch.org": "PyTorch 官方源",
    "pypi.org": "PyPI", "files.pythonhosted.org": "PyPI",
}
_HOST_RE = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(h) for h in sorted(_HOSTS, key=len, reverse=True)) + r")\b",
                      re.IGNORECASE)


def _hosts_near(text, pos, span=1500):
    win = text[max(0, pos - span):pos + span]
    return sorted({_HOSTS[h.lower()] for h in _HOST_RE.findall(win)})


def _b_proxy(m, text, ctx):
    port = re.search(r"(?:127\.0\.0\.1|localhost)[: ]+(?:port )?(\d{2,5})", text[max(0, m.start() - 300):m.end() + 300])
    where = f"（本机端口 {port.group(1)}）" if port else ""
    cfg = ctx.get("cfg") or {}
    acts = [action("open_page", "去设置网络代理", page="launcher")]
    if cfg.get("proxy_enabled"):
        acts.insert(0, action("set_launcher_cfg", "关闭启动器代理", changes={"proxy_enabled": False}))
    return _finding("proxy_bad", "代理连不上" + where,
                    f"联网请求走的代理{where}没有响应。常见原因：梯子没开、换了梯子端口变了、"
                    "梯子退出时没清掉系统代理。\n"
                    "· 用梯子的：先把梯子打开，再到「启动器设置 → 网络代理」点「测试」\n"
                    "· 不用梯子的：关掉启动器代理，并到 Windows 设置 → 网络 → 代理 里把手动代理关掉\n"
                    "启动器自己联网时会自动换路（见日志里的 [网络] 行），但 WebUI / ComfyUI 子进程不会，要重启它们才用上新设置。",
                    m.group(0), acts, key="proxy_bad", port=port.group(1) if port else "")


def _b_net(m, text, ctx):
    sites = _hosts_near(text, m.start())
    if not sites:
        return None
    cfg = ctx.get("cfg") or {}
    acts = []
    mirrorable = [s for s in sites if s in ("HuggingFace", "GitHub", "PyTorch 官方源", "PyPI")]
    if mirrorable and cfg.get("mirror_mode") != "always":
        acts.append(action("set_launcher_cfg", "改成总是用国内镜像", auto=True, changes={"mirror_mode": "always"}))
    acts.append(action("redetect_network", "重新检测网络"))
    acts.append(action("open_page", "去设置网络代理", page="launcher"))
    tip = ""
    if "Civitai" in sites:
        tip = "\n· Civitai 没有国内镜像：国内基本要开梯子，并在「启动器设置 → 网络代理」填上梯子端口。"
    if mirrorable:
        tip += f"\n· {'、'.join(mirrorable)} 有国内镜像：已改成总是走镜像（下次下载 / 启动生效），能直连了可以撤销。"
    return _finding("net_unreachable", f"连不上 {'、'.join(sites)}",
                    f"下载时连 {'、'.join(sites)} 超时或被断开，国内直连这些站很常见。" + tip,
                    m.group(0), acts, key="net_unreachable:" + ",".join(sites), sites=sites)


def _b_pip_nomatch(m, text, ctx):
    pkg = m.group(1)
    return _finding("pip_no_match", f"pip 找不到 {pkg}",
                    f"pip 在软件源里找不到能装的 {pkg}。两种可能：\n"
                    "· 网络：镜像没同步到 / 连不上源，换个时间或开梯子再试\n"
                    "· 版本：这个包没有当前 Python 版本能用的安装包（比如 Python 3.13 装老包），"
                    "要换 Python 版本或换包的版本",
                    m.group(0), [action("redetect_network", "重新检测网络")], level="warn", key=f"pip_no_match:{pkg}")


# ---- 模型 ----

def _b_model_corrupt(m, text, ctx):
    path = find_model_path(text[max(0, m.start() - 8000):m.end()])
    acts, state, why = [], "unknown", ""
    if path:
        state, why = check_model_file(path)
        if state == "ok":
            return _finding("model_load_error", "模型加载出错，但文件本身是完整的",
                            f"{path}\n文件检查：{why}。\n报错不是文件损坏造成的，可能是模型类型和 WebUI / 节点不匹配"
                            "（比如把 LoRA 当大模型加载、Flux 模型用了 SD 的加载器）。",
                            m.group(0), [action("reveal_path", "定位文件", path=path)], level="warn",
                            key=f"model_load_error:{path}", path=path)
        acts.append(action("reveal_path", "定位文件", path=path))
        page = civitai_page(path)
        if page:
            acts.append(action("civitai_redownload", "从 Civitai 重新下载", url=page))
        acts.append(action("trash_file", "把损坏的文件移到回收站", path=path))
    detail = f"\n文件：{path}\n文件检查：{why}" if path else "\n日志里没找到是哪个文件，往上翻找最近一行 Loading ... 的路径。"
    return _finding("model_corrupt", "模型文件损坏或下载不完整",
                    "读取模型文件时出错，典型原因是下载中断、文件没下完就被拿来用，或下载到的其实是网页。" + detail +
                    "\n删掉重新下载即可；从 Civitai 下的模型旁边有 .civitai.info，可以一键重新下载。",
                    m.group(0), acts, key=f"model_corrupt:{path}", path=path, state=state)


# ---- 生成过程 ----

def vae_fix_actions(ctx):
    """VAE 出 NaN / 黑图：按实例类型给出该用的参数"""
    cfg, iid = ctx.get("cfg") or {}, ctx.get("iid")
    if not iid:
        return []
    if ctx.get("kind") == "comfy" or cm.uses_new_neo_args(cfg):
        if cfg.get("vae_precision") in ("fp32", "cpu"):
            if cfg.get("vae_precision") == "fp32":
                return [action("set_instance_cfg", "改成 VAE 放 CPU（--cpu-vae）", iid=iid,
                               changes={"vae_precision": "cpu"})]
            return []
        return [action("set_instance_cfg", "VAE 改用 FP32（--fp32-vae）", auto=True, iid=iid,
                       changes={"vae_precision": "fp32"})]
    declared = declared_flags(ctx)
    cands = ["--no-half-vae"] if ctx.get("a1111") else ["--vae-in-fp32", "--no-half-vae"]
    extra = (cfg.get("extra_args") or "").split()
    for flag in cands:
        if flag in extra:
            return []
        if declared is None or flag in declared:
            return [action("add_launch_arg", f"加启动参数 {flag}", auto=True, iid=iid, arg=flag)]
    return []


def _b_vae_nan(m, text, ctx):
    acts = vae_fix_actions(ctx)
    if acts and ctx.get("iid"):
        acts.append(action("restart_instance", "重启生效", iid=ctx["iid"]))
    return _finding("vae_nan", "VAE 溢出（出图全黑 / NaN）",
                    "解码图片时 VAE 用半精度算溢出了，结果是 NaN，出来就是黑图或报错。"
                    "SDXL 原版 VAE 和不少二次元模型自带的 VAE 都有这个毛病。\n"
                    "解决：让 VAE 用 FP32 算（略慢一点、多占一点显存），"
                    "或者换一个修过的 VAE（如 sdxl-vae-fp16-fix）。",
                    m.group(0), acts, key="vae_nan")


def _b_unet_nan(m, text, ctx):
    acts = []
    if ctx.get("iid"):
        cfg = ctx.get("cfg") or {}
        if ctx.get("kind") == "comfy" or cm.uses_new_neo_args(cfg):
            acts.append(action("set_instance_cfg", "UNet 改用 BF16", iid=ctx["iid"], changes={"unet_precision": "bf16"}))
        acts.append(action("open_page", "去高级选项", page="settings"))
    return _finding("unet_nan", "UNet 算出了 NaN",
                    "生成过程中模型本体的计算溢出。常见原因：老显卡（16 系）用 FP16 跑、模型本身有问题、"
                    "或者 LoRA 权重太高。可以先降低 LoRA 权重 / 换模型试试，再考虑改精度（更慢）。",
                    m.group(0), acts, level="warn", key="unet_nan")


def _b_oom(m, text, ctx):
    acts = []
    cfg = ctx.get("cfg") or {}
    if ctx.get("iid") and cfg.get("vram_mode", "auto") not in ("low", "no_vram", "cpu"):
        acts.append(action("set_instance_cfg", "改成低显存模式", iid=ctx["iid"], changes={"vram_mode": "low"}))
        acts.append(action("restart_instance", "重启生效", iid=ctx["iid"]))
    return _finding("cuda_oom", "显存不够（CUDA out of memory）",
                    "这一步需要的显存超过了显卡剩余显存。先试：降低分辨率 / 批量、关掉同时开着的其它实例或游戏；"
                    "大模型（Flux、Qwen、Wan 视频）可以换 FP8 / GGUF 量化版。还不够再切低显存模式（会慢一些）。",
                    m.group(0), acts, level="warn", key="cuda_oom")


def _b_pagefile(m, text, ctx):
    return _finding("pagefile", "虚拟内存（页面文件）太小",
                    "加载模型时 Windows 的虚拟内存不够用了（错误 1455）。模型越来越大，16 GB 内存的电脑尤其常见。\n"
                    "解决（需要你自己改系统设置）：开始菜单搜「查看高级系统设置」→ 性能「设置」→ 高级 → 虚拟内存「更改」→ "
                    "取消「自动管理」，给 WebUI 所在的盘设「自定义大小」，初始 16384、最大 65536（MB）→ 设置 → 重启电脑。",
                    m.group(0), [], key="pagefile")


def _b_numpy2(m, text, ctx):
    acts = [action("pip_install", "把 numpy 降到 1.x", iid=ctx["iid"], packages=["numpy<2"])] \
        if ctx.get("kind") == "comfy" and ctx.get("iid") else []
    return _finding("numpy2", "NumPy 2 跟老插件不兼容",
                    "某个包是按 NumPy 1.x 编译的，环境里却装了 NumPy 2，导入就崩。"
                    "通常是装某个节点 / 扩展时把 numpy 升上去了。把 numpy 降回 1.x 即可"
                    "（WebUI 会在启动时按自己的依赖装回去；ComfyUI 可以用下面的按钮）。",
                    m.group(0), acts, key="numpy2")


def _b_disk(m, text, ctx):
    return _finding("disk_full", "磁盘空间不足",
                    "写文件时磁盘满了。清理一下目标盘（下载的模型、outputs 出图、pip 缓存 %LOCALAPPDATA%\\pip\\cache），"
                    "或者把模型库放到别的盘（「模型管理」页可以设共享模型库）。",
                    m.group(0), [], key="disk_full")


def _b_forge_ext(m, text, ctx):
    rel = m.group(1)
    acts = [action("ext_disable", f"停用扩展「{rel}」", iid=ctx["iid"], rel=rel)] if ctx.get("iid") else []
    acts.append(action("open_page", "去插件页更新 / 换版本", page="extensions"))
    return _finding("forge_ext_error", f"扩展「{rel}」报错",
                    f"扩展「{rel}」加载或运行时出错（见证据行和上面的堆栈）。扩展版本和 WebUI 对不上最常见："
                    "到「常用插件」页更新它，或者先停用。",
                    m.group(0), acts, level="warn", key=f"forge_ext_error:{rel}")


def _b_civitai_auth(m, text, ctx):
    return _finding("civitai_auth", "Civitai 需要登录 / API Key",
                    "这个模型要登录才能下载（Civitai 对部分模型强制要求）。到 civitai.com 账号设置里生成 API Key，"
                    "填进「模型下载」页的 API Key 框。",
                    m.group(0), [action("open_page", "去填 API Key", page="civitai")], key="civitai_auth")


def _b_port(m, text, ctx):
    return _finding("port_in_use", "端口被占用",
                    "要用的端口已经被别的程序（多半是上次没退干净的 WebUI / ComfyUI）占了。"
                    "Forge 会自己换端口继续；想彻底解决就在启动前的「检测到残留进程」弹窗里选「结束并启动」。",
                    m.group(0), [], level="warn", key="port_in_use")


def _b_fatal(m, text, ctx):
    """已有的启动致命错误说明（process_manager._fatal_hint_for）：文案复用，这里只补动作"""
    import process_manager as pm
    hint = pm._fatal_hint_for(text, (ctx.get("cfg") or {}).get("gpu_backend") == "rocm")
    if not hint:
        return None
    acts = []
    if "显卡驱动太旧" in hint:
        import cuda_compat as cc
        acts.append(action("open_url", "去下载显卡驱动", url=cc.DRIVER_URL))
    elif "torch 下载/安装没成功" in hint:
        if (ctx.get("cfg") or {}).get("mirror_mode") != "always":
            acts.append(action("set_launcher_cfg", "改成总是用国内镜像", auto=True, changes={"mirror_mode": "always"}))
    elif "环境部署" in hint:
        acts.append(action("open_page", "去环境部署页", page="deploy"))
    title = hint.split("\n", 1)[0].replace("[启动器] ", "").replace("启动失败：", "")[:40]
    return _finding("fatal_hint", title, hint.replace("[启动器] ", ""), m.group(0), acts, key="fatal_hint")


def _b_bad_git(m, text, ctx):
    return _finding("bad_git", "Git 用不了",
                    "WebUI 调用的 git.exe 运行失败（便携版 Git 解压不完整或被杀毒软件拦截）。重新解压整合包，"
                    "或者把根目录下的 git 文件夹改名，让启动器改用系统装的 Git。",
                    m.group(0), [action("open_page", "去环境部署页", page="deploy")], key="bad_git")


def _b_launcher_internal(m, text, ctx):
    return _finding("launcher_internal", "启动器内部错误",
                    "启动器自己的代码出错了（不是 WebUI / ComfyUI 的问题）。可以让 AI 结合启动器源码分析，"
                    "或把诊断报告复制给作者。",
                    m.group(0), [], key="launcher_internal:" + _line_at(text, m.end())[:80])


_CONN_ERR = (r"(?:ConnectTimeout|ReadTimeout|Max retries exceeded|Connection timed out|timed out|ConnectionResetError|"
             r"SSLError|SSLEOFError|SSL_ERROR_SYSCALL|Connection aborted|Failed to establish a new connection|"
             r"getaddrinfo failed|Could not resolve host|Failed to connect to \S+ port 443|"
             r"LocalEntryNotFoundError|couldn't connect to|远程主机强迫关闭|连接尝试失败|RemoteDisconnected|"
             r"Connection was reset|unable to access 'https://)")

RULES = [
    Rule("fatal_hint", r"please update your gpu driver|driver on your system is too old|cuda driver version is insufficient|"
                       r"couldn't install (?:py)?torch|no kernel image is available|torch is not able to use gpu|"
                       r"pytorch is not able to access any compute device|torchvision::nms does not exist", _b_fatal,
         {"forge", "comfy"}),
    Rule("comfy_import_failed", r"Cannot import (.+?) module for custom nodes:\s*([^\r\n]+)", _b_comfy_import, {"comfy"}),
    Rule("comfy_missing_node", r"node \S+ does not exist|Node '[^']+' not found|missing_node_type", _b_comfy_missing,
         {"comfy"}),
    Rule("bridge_nodes", BRIDGE_RE, _b_bridge_nodes, {"comfy"}),
    Rule("bridge_models", BRIDGE_RE, _b_bridge_models, {"comfy"}),
    Rule("model_corrupt", r"SafetensorError|Error while deserializing header|MetadataIncompleteBuffer|HeaderTooLarge|"
                          r"InvalidHeaderDeserialization|incomplete metadata|PytorchStreamReader failed|"
                          r"failed finding central directory|invalid load key|_pickle\.UnpicklingError|"
                          r"file not fully covered|unexpected EOF, expected", _b_model_corrupt),
    Rule("vae_nan", r"NaNs was produced in VAE|NansException[^\n]*VAE|invalid value encountered in cast", _b_vae_nan,
         {"forge", "comfy"}),
    Rule("unet_nan", r"NaNs was produced in Unet", _b_unet_nan, {"forge", "comfy"}),
    Rule("cuda_oom", r"CUDA out of memory|torch\.(?:cuda\.)?OutOfMemoryError|Allocation on device", _b_oom,
         {"forge", "comfy"}),
    Rule("pagefile", r"页面文件太小|paging file is too small|os error 1455|WinError 1455", _b_pagefile),
    Rule("numpy2", r"compiled using NumPy 1\.x cannot be run in\s+NumPy 2", _b_numpy2, {"forge", "comfy"}),
    Rule("disk_full", r"No space left on device|There is not enough space on the disk|磁盘空间不足|\[Errno 28\]", _b_disk),
    Rule("proxy_bad", r"ProxyError|Unable to connect to proxy|Cannot connect to proxy|Tunnel connection failed|"
                      r"proxyconnect tcp|Failed to connect to (?:127\.0\.0\.1|localhost) port \d+", _b_proxy),
    Rule("net_unreachable", _CONN_ERR, _b_net),
    Rule("pip_no_match", r"No matching distribution found for (\S+)", _b_pip_nomatch),
    Rule("forge_ext_error", r"Error (?:loading script|running \w+|calling):?[^\n]*?extensions[\\/]([^\\/\s:]+)",
         _b_forge_ext, {"forge"}),
    Rule("civitai_auth", r"civitai[^\n]*(?:401|403|Unauthorized|requires? (?:login|auth))|用户未登录", _b_civitai_auth),
    Rule("port_in_use", r"error while attempting to bind on address|\[Errno 10048\]|address already in use", _b_port,
         {"forge", "comfy"}),
    Rule("bad_git", r"Bad git executable", _b_bad_git, {"forge"}),
    Rule("launcher_internal", r"^内部错误:", _b_launcher_internal, {"launcher"}),
]


def scan(text, ctx):
    """跑一遍全部规则，返回发现列表（同一个 key 只留最后一次出现的；按出现位置从新到旧）"""
    text = text or ""
    kind = ctx.get("kind")
    found = {}
    for rule in RULES:
        if rule.kinds and kind not in rule.kinds:
            continue
        last = None
        for m in rule.re.finditer(text):
            last = m
        if not last:
            continue
        try:
            f = rule.build(last, text, ctx)
        except Exception as e:      # 单条规则出错不影响别的
            f = None
            print(f"[诊断] 规则 {rule.id} 出错: {e}")
        if not f:
            continue
        f["pos"] = last.start()
        if not f.get("evidence"):
            f["evidence"] = _line_at(text, last.start())
        elif "\n" not in f["evidence"] and len(f["evidence"]) < 40:
            f["evidence"] = _line_at(text, last.start())
        found[f["key"]] = f
    # 代理坏了时，同一处的「连不上 xxx」是它的后果，不单独列
    if any(f["rule"] == "proxy_bad" for f in found.values()):
        found = {k: f for k, f in found.items() if f["rule"] != "net_unreachable"}
    # 缺节点有两个来源（运行时后端报的、桥接插件在网页里查的），只留最近的那条
    miss = sorted((f for f in found.values() if f["rule"] == "comfy_missing_node"), key=lambda f: -f["pos"])
    for f in miss[1:]:
        found.pop(f["key"], None)
    if kind in ("forge", "comfy"):
        g = _generic_error(text, ctx)
        if g:
            found[g["key"]] = g
    return sorted(found.values(), key=lambda f: -f["pos"])


# ---- 兜底：规则不认识的运行时报错 ----
# 只认真正的「出错」：ComfyUI 执行节点出异常 / 提交时校验没过、Python 的 Traceback 堆栈。
# 同一处已经有专门规则认出来的不重复报；用户自己点了中断的不算。

_TB_RE = re.compile(r"^.*Traceback \(most recent call last\):[ \t]*\r?$", re.MULTILINE)
_COMFY_EXC_RE = re.compile(r"!!! Exception during processing !!!\s*([^\r\n]*)")
_COMFY_INVALID_RE = re.compile(r"(?:Failed to validate prompt for output [^\r\n]*|Prompt outputs failed validation[^\r\n]*)"
                               r"((?:\r?\n[ \t]*[*-][^\r\n]*){0,6})")
_EXC_LINE_RE = re.compile(r"^((?:[A-Za-z_][\w]*\.)*[A-Z]\w*(?:Error|Exception|Exit|Fault))(?::\s*(.*))?$")
_FRAME_RE = re.compile(r'File "([^"]+)"')
_BENIGN_EXC = ("KeyboardInterrupt", "InterruptProcessingException", "CancelledError", "SystemExit")


def _traceback_at(text, start):
    """从 Traceback 那一行往下找异常类型那一行。返回 (结束位置, 异常类型, 信息, 出错的节点包 / 扩展)"""
    pack = ""
    off = text.find("\n", start)
    off = len(text) if off < 0 else off + 1
    for ln in text[off:off + 30000].split("\n")[:400]:
        s = ln.rstrip("\r")
        fm = _FRAME_RE.search(s)
        if fm:
            m = re.search(r"\\(?:custom_nodes|extensions(?:-builtin)?)\\([^\\]+)\\", fm.group(1).replace("/", "\\"))
            if m:
                pack = m.group(1)       # 最深的那一层节点包 / 扩展
        if s and not s[0].isspace():
            em = _EXC_LINE_RE.match(re.sub(r"^\[[\d\-: .]+\]\s*", "", s).strip())   # 去掉 ComfyUI 行首时间戳
            if em:
                return off + len(ln), em.group(1).rsplit(".", 1)[-1], (em.group(2) or "").strip(), pack
        off += len(ln) + 1
    return off, "", "", pack


def _explained(text, s, e, ctx):
    """这一段报错是不是已经被专门规则认出来了"""
    win = text[max(0, s - 400):e + 600]
    kind = ctx.get("kind")
    for rule in RULES:
        if rule.kinds and kind not in rule.kinds:
            continue
        if rule.id in ("bridge_nodes", "bridge_models"):
            continue
        if rule.re.search(win):
            return True
    return False


def _generic_error(text, ctx):
    events = []
    for m in _COMFY_EXC_RE.finditer(text):
        tb = _TB_RE.search(text, m.end(), m.end() + 400)
        end, etype, msg, pack = _traceback_at(text, tb.start()) if tb else (m.end(), "", "", "")
        events.append((m.start(), end, etype, m.group(1).strip() or msg, pack))
    for m in _TB_RE.finditer(text):
        if any(s <= m.start() <= e for s, e, *_r in events):
            continue
        end, etype, msg, pack = _traceback_at(text, m.start())
        if etype:
            events.append((m.start(), end, etype, msg, pack))
    for m in _COMFY_INVALID_RE.finditer(text):
        detail = " ".join(x.strip(" *-\t\r") for x in m.group(1).split("\n") if x.strip(" *-\t\r"))
        events.append((m.start(), m.end(), "", detail[:300] or m.group(0).strip(), ""))
    for s, e, etype, msg, pack in sorted(events, key=lambda x: -x[0]):
        if etype in _BENIGN_EXC or _explained(text, s, e, ctx):
            continue
        comfy = ctx.get("kind") == "comfy"
        what = ("ComfyUI" if comfy else "WebUI") + " 运行时报错"
        head = f"{etype}: {msg}" if etype else msg
        where = (f"\n出错的位置在{'节点包' if comfy else '扩展'}「{pack}」里。" if pack else "")
        f = _finding(
            "runtime_error", f"{what}：{(msg or etype)[:60]}",
            f"报错信息：{head}{where}\n"
            "本地规则不认识这个报错。可以让 AI 结合日志和本地文件分析原因；如果是节点 / 扩展自己给的提示"
            "（比如某两个节点不能一起用、参数超出范围），照提示改工作流或设置就行。",
            head[:400] if etype else _line_at(text, s),
            [action("ai_ask", "让 AI 分析这个报错", note=f"运行时报错：{head[:400]}" +
                    (f"（出在「{pack}」）" if pack else "") + "。帮我看看是什么原因、该怎么改。")],
            key="runtime_error:" + hashlib.md5(head.encode("utf-8", "replace")).hexdigest()[:8],
            exc_type=etype, message=msg, pack=pack)
        f["pos"] = s
        return f
    return None


# ============================================================
# ComfyUI 缺失节点 → 节点包（ComfyUI-Manager 的 extension-node-map.json）
# ============================================================

def _is_node_map(data):
    if not isinstance(data, dict) or not data:
        return False
    k, v = next(iter(data.items()))
    return isinstance(k, str) and k.startswith("http") and isinstance(v, list) and v and isinstance(v[0], list)


def _manager_files(comfy_dir, basename, cache):
    """本机已有的 ComfyUI-Manager 数据文件：custom_nodes 里的旧版 Manager（停用了的也算）、
    新版内置 Manager 的缓存目录（文件名带时间戳前缀，内容靠调用方校验），最后是启动器自己的缓存"""
    cands = []
    cn = os.path.join(comfy_dir or "", "custom_nodes")
    for d in ("comfyui-manager", "ComfyUI-Manager", "comfyui-manager.disabled", "ComfyUI-Manager.disabled"):
        cands.append(os.path.join(cn, d, basename))
    for base in (os.path.join(comfy_dir or "", "user", "default", "ComfyUI-Manager", "cache"),
                 os.path.join(comfy_dir or "", "user", "__manager", "cache"),
                 os.path.join(comfy_dir or "", "custom_nodes", "comfyui-manager", ".cache")):
        try:
            cands += [os.path.join(base, n) for n in sorted(os.listdir(base), reverse=True) if n.endswith(".json")]
        except OSError:
            pass
    cands.append(cache)
    return [c for c in cands if os.path.isfile(c)]


def local_node_maps(comfy_dir):
    return _manager_files(comfy_dir, "extension-node-map.json", NODE_MAP_CACHE)


def _load_manager_json(paths, valid, url, cache, fetch, max_age):
    """优先本机，其次启动器缓存（max_age 内），都没有再用 fetch() 下载并缓存。返回数据或 None"""
    for path in paths:
        if path == cache and time.time() - os.path.getmtime(path) > max_age and fetch:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if valid(data):
            return data
    if not fetch:
        return None
    try:
        data = fetch(url)
    except Exception:
        return None
    if not valid(data):
        return None
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass
    return data


def load_node_map(comfy_dir, fetch=None, max_age=7 * 86400):
    """节点表 dict（{仓库地址: [[节点类型...], {元信息}]}），拿不到返回 {}"""
    return _load_manager_json(local_node_maps(comfy_dir), _is_node_map, NODE_MAP_URL, NODE_MAP_CACHE,
                              fetch, max_age) or {}


def _clean_type(name):
    """ComfyUI 网页加载缺失节点时会去掉类型名里的 & < > 引号等（sanitizeNodeName），存回去的工作流里就是去掉后的名字"""
    return " ".join(re.sub(r"[&<>\"'`=]", "", name).split())


def resolve_node_types(node_types, node_map):
    """节点类型名 → [(仓库地址, 包名, [命中的类型])]，没查到的单独返回"""
    by_repo, missing = {}, []
    for t in node_types:
        exact, by_pat, by_clean = [], [], []
        for url, entry in node_map.items():
            names = entry[0] if entry else []
            meta = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
            if t in names:
                exact.append((url, meta, names))
                continue
            if not exact and _clean_type(t) in {_clean_type(n) for n in names if isinstance(n, str)}:
                by_clean.append((url, meta, names))
                continue
            pat = meta.get("nodename_pattern")
            if pat:
                try:
                    if re.search(pat, t):
                        by_pat.append((url, meta, names))
                except re.error:
                    pass
        # 同名节点常有好几个仓库提供（单节点小仓库、整合包）：Manager 标了 preemptions 的优先，
        # 其次是这次已经选中的仓库，再其次挑节点多的大包（ImageResize+ 该装 cubiq 的 essentials）
        hit = max(exact or by_clean or by_pat, default=None,
                  key=lambda c: (t in (c[1].get("preemptions") or ()), c[0] in by_repo, len(c[2])))
        if hit:
            url, meta, _ = hit
            title = meta.get("title_aux") or url.rstrip("/").rsplit("/", 1)[-1]
            by_repo.setdefault(url, [url, title, []])[2].append(t)
        else:
            missing.append(t)
    return [tuple(v) for v in by_repo.values()], missing


# ============================================================
# ComfyUI 缺失模型 → 下载地址（ComfyUI-Manager 的 model-list.json）
# ============================================================

def _is_model_list(data):
    return isinstance(data, dict) and isinstance(data.get("models"), list) and \
        any(isinstance(m, dict) and m.get("filename") and m.get("url") for m in data["models"][:20])


def load_model_list(comfy_dir, fetch=None, max_age=7 * 86400):
    """模型表里的条目列表 [{name, filename, url, save_path, size, ...}]，拿不到返回 []"""
    data = _load_manager_json(_manager_files(comfy_dir, "model-list.json", MODEL_LIST_CACHE), _is_model_list,
                              MODEL_LIST_URL, MODEL_LIST_CACHE, fetch, max_age)
    return (data or {}).get("models") or []


def model_list_lookup(entries, filename):
    """按文件名（不分大小写）找模型表条目"""
    low = os.path.basename(str(filename).replace("\\", "/")).lower()
    for m in entries:
        if isinstance(m, dict) and str(m.get("filename") or "").lower() == low and m.get("url"):
            return m
    return None


# ComfyUI 的模型目录 → Civitai 搜索用的角色（civitai_downloader._ROLE_SEARCH_TYPES）
FOLDER_ROLES = {"checkpoints": "Checkpoint", "loras": "LoRA", "vae": "VAE", "embeddings": "Embedding",
                "controlnet": "ControlNet", "upscale_models": "Upscaler", "text_encoders": "TextEncoder",
                "clip": "TextEncoder", "diffusion_models": "Checkpoint", "unet": "Checkpoint"}


def split_model_name(name):
    """下拉框里的值 "Anima\\x.safetensors" → ("Anima", "x.safetensors")"""
    parts = [p for p in str(name).replace("\\", "/").split("/") if p and p not in (".", "..")]
    return (os.path.join(*parts[:-1]) if len(parts) > 1 else ""), (parts[-1] if parts else "")


# ============================================================
# 脱敏（发给 AI 之前）
# ============================================================

_SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"), re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|authorization|password|usertoken)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]{6,})"),
    re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._-]{12,})"),
]


def redact(text):
    if not text:
        return text
    home = os.path.expanduser("~")
    if home and len(home) > 3:
        text = re.sub(re.escape(home), "%USERPROFILE%", text, flags=re.IGNORECASE)
        text = re.sub(re.escape(home.replace("\\", "\\\\")), "%USERPROFILE%", text, flags=re.IGNORECASE)
    for r in _SECRET_RES:
        text = r.sub(lambda m: (m.group(1) + "***") if m.lastindex and m.lastindex >= 2 else "***", text)
    return text


def redact_config(cfg):
    out = {}
    for k, v in (cfg or {}).items():
        if any(s in k.lower() for s in ("key", "token", "password", "secret")) and v:
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = redact_config(v)
        elif isinstance(v, list):
            out[k] = [redact_config(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def snippet(text, findings=(), max_chars=12000):
    """给 AI 的日志片段：报错附近的行优先，其余从尾部补，总长有上限"""
    lines = (text or "").splitlines()
    lines = [l for l in lines if l.strip()]
    if sum(len(l) + 1 for l in lines) <= max_chars:
        return "\n".join(lines)
    keep = set()
    err_re = re.compile(r"Traceback|Error|Exception|FAILED|失败|错误", re.IGNORECASE)
    idx = [i for i, l in enumerate(lines) if err_re.search(l)]
    for i in idx[-6:]:
        keep.update(range(max(0, i - 25), min(len(lines), i + 8)))
    keep.update(range(max(0, len(lines) - 60), len(lines)))
    out, total = [], 0
    for i in sorted(keep, reverse=True):
        total += len(lines[i]) + 1
        if total > max_chars:
            break
        out.append((i, lines[i]))
    out.sort()
    res, prev = [], -1
    for i, l in out:
        if prev >= 0 and i != prev + 1:
            res.append("……（省略）……")
        res.append(l[:600])
        prev = i
    return "\n".join(res)
