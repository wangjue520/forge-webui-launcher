# -*- coding: utf-8 -*-
"""
pywebview 版启动器的 JS 桥接层。

前端（web/index.html）通过 window.pywebview.api.* 调用这里的方法；
长时间任务（部署/下载/反推/批量查询）都在后台线程里跑，进度和日志通过
_emit() -> window.App.onEvent({scope, type, ...}) 推送给前端。

线程模型：
  - pywebview 会在自己的线程里执行 js_api 方法，短操作（读写配置、扫目录）
    直接同步返回；
  - 长操作一律 spawn 一个 daemon 线程并立即返回 {"ok": True}，后续状态全部
    走事件推送，前端不会卡住；
  - 部署中途需要用户决策（venv 版本不一致）时，用 _ask() 发事件给前端并
    阻塞等待，前端弹窗后调用 answer(ask_id, value) 解除阻塞。

进程管理不再用 QProcess，改用 subprocess.Popen + 读取线程；
杀进程树沿用 taskkill /F /T（QProcess.kill 杀不到孙进程的老坑不变）。
"""
import base64
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime

import config_manager as cm
import portable_env as pe
import civitai_downloader as cd
import liblib_client as lc
import safetensors_meta as sm
import image_meta_core as imc
import meta_engine as me
import wd14_tagger_core as wt
import wd14_venv_manager as venv
import updater

APP_DIR = os.path.dirname(os.path.abspath(__file__))

WEBUI_ENTRY_SCRIPT = "webui.bat"

# subprocess.CREATE_NO_WINDOW，避免拉起 cmd/git/netstat 时闪黑框
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_URL_RE = re.compile(r"Running on local URL:\s*(http://\S+)")
_BIND_ERROR_RE = re.compile(r"error while attempting to bind on address", re.IGNORECASE)

# 第三方整合包（秋叶系等）会在 webui-user.bat 里写死 set PYTHON=/GIT=...
_OVERRIDE_VAR_PATTERN = re.compile(
    r"^\s*set\s+(PYTHON|GIT|VENV_DIR|COMMANDLINE_ARGS)\s*=\s*(.*)$",
    re.IGNORECASE,
)

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx", ".gguf")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

HASH_CACHE_PATH = os.path.join(APP_DIR, "model_hash_cache.json")

BASE_CATEGORIES = [
    ("Checkpoint 大模型", "models/Stable-diffusion", False),
    ("LoRA", "models/Lora", True),
    ("VAE", "models/VAE", False),
    ("Embedding 嵌入", "embeddings", False),
    ("ControlNet", "models/ControlNet", False),
    ("放大模型 (ESRGAN)", "models/ESRGAN", False),
    ("Hypernetwork", "models/hypernetwork", False),
]

_SKIP_AUTO_DIRS = {
    "stable-diffusion", "lora", "vae", "controlnet", "esrgan", "hypernetwork",
    "karlo", "deepbooru", "vae-approx", "configs",
}

# 部署流程自己会产生的内容——目录里只有这些说明是上次部署的残留，可以续跑
RESUMABLE_ENTRIES = {".git", "python", "git", "venv", "tmp"}

CLASSIC_REPO = "https://github.com/lllyasviel/stable-diffusion-webui-forge.git"
NEO_REPO = "https://github.com/Haoming02/sd-webui-forge-classic.git"
NEO_BRANCH = "neo"
_NEO_KEYS = ("neo", "neo2")

DEPLOY_BRANCH_OPTIONS = [
    ("常规版 / Classic（lllyasviel 官方仓库）", "classic"),
    ("Neo 版（Haoming02 社区维护分支）", "neo2"),
]

SETTINGS_BRANCH_OPTIONS = [
    ("常规版 / Classic", "classic"),
    ("Neo 版（旧版参数：--always-xxx-vram / --all-in-fpXX）", "neo"),
    ("Neo 版（新版参数：--normalvram / --force-fpXX）", "neo2"),
]

# 常用扩展目录：每一项 (显示名, 简介, url_by_branch, folder_by_branch)
# url/folder 可以是字符串（两分支通用）或 {"classic":..,"neo":..} 字典；
# 某个分支给 None 表示该分支不可用（列表里会直接隐藏）
EXTENSION_CATALOG = [
    (
        "简体中文汉化 (zh_CN)",
        "把网页界面翻译成中文。装好后还需要去 Settings → User interface → "
        "Localization 选择 zh_CN，点 Apply settings 再 Reload UI 才会生效"
        "（这一步是网页内设置，装完插件后系统不能替你点，需要手动选一次）。",
        "https://github.com/dtlnor/stable-diffusion-webui-localization-zh_CN.git",
        "stable-diffusion-webui-localization-zh_CN",
    ),
    (
        "ADetailer",
        "自动检测并修复脸部/手部等细节，出图质量提升明显，是目前最常用的扩展之一。"
        "Neo 分支用的是社区专门维护的 ADetailer-Neo 分支（原版在 Neo 下会报 "
        "'Namespace' object has no attribute 'use_cpu' 报错，装这个版本能避开）。",
        {
            "classic": "https://github.com/Bing-su/adetailer.git",
            "neo": "https://github.com/Haoming02/ADetailer-Neo.git",
        },
        {"classic": "adetailer", "neo": "ADetailer-Neo"},
    ),
    (
        "WD14 Tagger（图片反推标签）",
        "上传图片自动反推出 Danbooru 风格的标签，训练 LoRA 打标签、以图找提示词很常用。"
        "模型不需要额外下载——装好扩展后首次使用时它会自己从 HuggingFace 自动下载。"
        "Neo 分支用的是修掉了 Deepbooru 依赖问题的分支（原版在 Neo 下会报 "
        "ModuleNotFoundError: No module named 'modules.deepbooru'）。",
        {
            "classic": "https://github.com/picobyte/stable-diffusion-webui-wd14-tagger.git",
            "neo": "https://github.com/hirorohi03/stable-diffusion-webui-wd14-tagger.git",
        },
        "stable-diffusion-webui-wd14-tagger",
    ),
    (
        "Tag Autocomplete",
        "输入提示词时自动补全 Danbooru/E621 标签、LoRA/嵌入名称和通配符，"
        "还能自动填入 LoRA 触发词，打标签效率提升很大。"
        "Neo 分支用的是 TagComplete Neo 分支（Neo 移除了 sd_hijack 导致原版完全失效，"
        "这个分支专为 Neo 维护，还针对 Gradio 4 做了优化）。",
        {
            "classic": "https://github.com/DominikDoom/a1111-sd-webui-tagcomplete.git",
            "neo": "https://github.com/eduardoabreu81/sd-webui-tagcomplete-neo.git",
        },
        {"classic": "a1111-sd-webui-tagcomplete", "neo": "sd-webui-tagcomplete-neo"},
    ),
    (
        "Model Keyword（触发词库）",
        "自动补全模型/LoRA 触发词的关键词库，Tag Autocomplete 会读取它的 "
        "lora-keyword.txt 来补全 LoRA 触发词。装完后可以在扩展设置里禁用它本身，"
        "只留词库给 Tag Autocomplete 用。",
        "https://github.com/mix1009/model-keyword.git",
        "model-keyword",
    ),
    (
        "Prompt All-in-One",
        "提示词输入框全家桶：标签翻译、一键翻译中文提示词、权重快捷调整、"
        "收藏常用词、历史记录等，中文用户几乎必装。"
        "Neo 分支用的是 Prompt All-in-One NEO 分支（原版在 Neo 下界面不显示）。",
        {
            "classic": "https://github.com/Physton/sd-webui-prompt-all-in-one.git",
            "neo": "https://github.com/eduardoabreu81/sd-webui-prompt-all-in-one-neo.git",
        },
        {"classic": "sd-webui-prompt-all-in-one", "neo": "sd-webui-prompt-all-in-one-neo"},
    ),
    (
        "Dynamic Prompts",
        "支持通配符/随机组合/权重语法批量生成提示词，做批量出图很依赖它。"
        "Neo 分支用的是适配过 Neo 的分支版本。",
        {
            "classic": "https://github.com/adieyal/sd-dynamic-prompts.git",
            "neo": "https://github.com/abzaloff/sd-dynamic-prompts.git",
        },
        "sd-dynamic-prompts",
    ),
    (
        "TIPO（提示词自动生成）",
        "输入少量标签或一句描述，自动生成完整的 Danbooru 风格提示词。"
        "模型专为图像生成训练，体积小、NSFW 也能用，抽卡式出图很合适。"
        "首次使用会自动下载模型。",
        "https://github.com/KohakuBlueleaf/z-tipo-extension.git",
        "z-tipo-extension",
    ),
    (
        "Civitai Helper",
        "扫描本地模型，从 Civitai 拉取预览图和触发词等元数据，"
        "让 LoRA 卡片显示缩略图、配合 Tag Autocomplete / Prompt All-in-One 自动填触发词。"
        "Neo 分支用的是跟进 Civitai API 变更的 RED UPDATE 分支。",
        {
            "classic": "https://github.com/zixaphir/Stable-Diffusion-Webui-Civitai-Helper.git",
            "neo": "https://github.com/Replactionap/Stable-Diffusion-Webui-Civitai-Helper-RED-UPDATE.git",
        },
        {"classic": "Stable-Diffusion-Webui-Civitai-Helper",
         "neo": "Stable-Diffusion-Webui-Civitai-Helper-RED-UPDATE"},
    ),
    (
        "Infinite Image Browsing（图片浏览）",
        "独立的图片浏览页，毫秒级按生成参数检索/筛选历史出图，"
        "比自带的 PNG Info 一张张翻快得多，图多了以后离不开。"
        "Neo 分支用的是适配 Neo 的分支版本。",
        {
            "classic": "https://github.com/zanllp/sd-webui-infinite-image-browsing.git",
            "neo": "https://github.com/Dusky-dev/sd-forge_neo-infinite-image-browsing-xl.git",
        },
        {"classic": "sd-webui-infinite-image-browsing",
         "neo": "sd-forge_neo-infinite-image-browsing-xl"},
    ),
    (
        "PNG Info 美化",
        "给图片信息/生成参数显示上色排版，一眼看清提示词、参数和 LoRA，"
        "还支持显示 Dynamic Prompts 的原始通配符模板。",
        "https://github.com/bluelovers/sd-webui-pnginfo-beautify.git",
        "sd-webui-pnginfo-beautify",
    ),
    (
        "宽高比/分辨率快捷按钮",
        "在尺寸设置旁加一排常用宽高比和分辨率按钮，点一下直接填好，"
        "不用每次手输数字。按钮值可以编辑扩展目录下的 txt 文件自定义。",
        "https://github.com/altoiddealer/--sd-webui-ar-plusplus.git",
        "--sd-webui-ar-plusplus",
    ),
    (
        "Ultimate SD Upscale",
        "分块超分放大，大图放大质量和速度比默认放大好不少。",
        "https://github.com/Coyote-A/ultimate-upscale-for-automatic1111.git",
        "ultimate-upscale-for-automatic1111",
    ),
    (
        "Attention Couple（区域提示词）",
        "把画面分成多个区域分别写提示词，适合多角色构图。"
        "由 Neo 作者本人维护，Classic 和 Neo 都能用，"
        "Neo 上用它代替已失效的 Regional Prompter。",
        "https://github.com/Haoming02/sd-forge-couple.git",
        "sd-forge-couple",
    ),
    (
        "Regional Prompter",
        "把画面分区域分别用不同提示词控制，适合多角色构图。"
        "注意：Neo 移除了 sd_hijack 导致它在 Neo 上无法加载，"
        "Neo 用户请改用上面的 Attention Couple。",
        {
            "classic": "https://github.com/hako-mikan/sd-webui-regional-prompter.git",
            "neo": None,
        },
        {"classic": "sd-webui-regional-prompter", "neo": None},
    ),
    (
        "NegPiP（正向框写负向词）",
        "在正向提示词框里直接写负向效果的词，比负向框的常规写法力度更强，"
        "而且 CFG=1（比如用 Turbo LoRA）时负向框会失效、它依然有效。",
        "https://github.com/Haoming02/sd-forge-negpip.git",
        "sd-forge-negpip",
    ),
    (
        "Openpose Editor",
        "网页内直接编辑人体骨架姿势，配合 ControlNet 的 openpose 模型用。",
        "https://github.com/huchenlei/sd-webui-openpose-editor.git",
        "sd-webui-openpose-editor",
    ),
]


# ============================================================
# 小工具
# ============================================================

def _fmt_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def kill_process_tree(pid):
    """杀掉整棵进程树（cmd.exe 只是壳，真正占端口的 python.exe 是孙进程）。
    返回是否杀成功；taskkill 本身也加超时，避免杀进程的动作自己卡死。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, creationflags=_NO_WINDOW, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
            return True
        except Exception:
            return False


def _probe_executable(cmd, timeout=15):
    """
    试运行一个可执行文件，返回 (能否运行, 失败原因)。
    “文件存在”不等于“能运行”——解压不完整、缺 DLL、被杀软拦截
    都会让 exe 一跑就挂，等到启动中途才暴雷就很难看懂。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return False, "文件不存在"
    except subprocess.TimeoutExpired:
        return False, "执行超时"
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        tail = [l for l in ((r.stderr or "") + "\n" + (r.stdout or "")).splitlines() if l.strip()]
        detail = f"返回码 {r.returncode}"
        if tail:
            detail += f"，输出: {tail[-1].strip()[:200]}"
        return False, detail
    return True, ""


def find_listening_pid(port):
    """返回正在 LISTEN 指定端口的进程 PID，找不到返回 None（仅 Windows）"""
    if os.name != "nt" or not port:
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-2].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
    return None


def process_name_of(pid):
    """查询 PID 对应的进程名（仅 Windows），查不到返回空字符串"""
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        ).stdout.strip()
        if out.startswith('"'):
            return out.split('","')[0].strip('"')
    except Exception:
        pass
    return ""


def _find_webui_user_bat_overrides(root):
    """返回 [(变量名, 值, 行号), ...]，跳过被注释掉的行"""
    bat_path = os.path.join(root, "webui-user.bat")
    if not os.path.exists(bat_path):
        return []
    hits = []
    try:
        with open(bat_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                stripped = line.strip()
                if stripped.upper().startswith(("REM", "::")):
                    continue
                m = _OVERRIDE_VAR_PATTERN.match(line)
                if m and m.group(2).strip():
                    hits.append((m.group(1).upper(), m.group(2).strip(), i))
    except OSError:
        return []
    return hits


def _clear_webui_user_bat_overrides(root):
    """清空 webui-user.bat 里生效的变量赋值（保留行本身），先备份 .bak"""
    bat_path = os.path.join(root, "webui-user.bat")
    backup_path = bat_path + ".bak"
    with open(bat_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    if not os.path.exists(backup_path):
        with open(backup_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith(("REM", "::")):
            new_lines.append(line)
            continue
        m = _OVERRIDE_VAR_PATTERN.match(line)
        if m and m.group(2).strip():
            new_lines.append(f"set {m.group(1).upper()}=\n")
        else:
            new_lines.append(line)

    with open(bat_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def _resolve_by_branch(value, branch):
    """扩展目录用的分支解析：neo2 在插件兼容性上归一到 neo"""
    if isinstance(value, dict):
        if branch == "neo2":
            branch = "neo"
        return value.get(branch, value.get("classic"))
    return value


def _expand_to_image_files(paths):
    """把混合的文件/文件夹路径展开成只包含图片文件的列表（文件夹不递归）"""
    resolved = []
    for p in paths:
        if os.path.isdir(p):
            try:
                for name in sorted(os.listdir(p)):
                    if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                        resolved.append(os.path.join(p, name))
            except OSError:
                pass
        elif os.path.splitext(p)[1].lower() in IMAGE_EXTS:
            resolved.append(p)
    return resolved


# ============================================================
# 预览图
# ============================================================
#
# 这里不再用 data URL 内联预览，原因是踩实了一个机制问题：
#
# pywebview 的 js_api 返回值是靠 evaluate_js 回传的 —— 也就是把返回的 JSON
# **拼进一段 JS 源码**交给引擎执行。所以一张 3MB 的图 base64 成 4MB 字符串
# 之后，每次打开图片都等于让 JS 引擎去 parse 4MB 的源代码；这些巨型字符串
# 还会留在 JS 堆里给 GC 加压，表现就是滚动和输入一起变迟钝，而且第二张、
# 第三张越来越慢（前面的还没被回收）。把预览拆成单独一次调用并不能绕开
# 这条路，因为返回值走的是同一个 evaluate_js。
#
# 换成：把当前图片在 web/_preview/ 下做一个硬链接（同盘瞬间完成，不复制
# 数据；跨盘才退回复制），前端用相对路径 <img src="_preview/xxx.png"> 引用。
# index.html 本身就是从 web/ 目录以 file:// 加载的，同目录相对路径是浏览器
# 最基本的能力，图片数据由渲染进程直接读盘，一个字节都不经过 Python↔JS 桥。
#
# 这样做还顺带解决了大图问题：不管多大都能预览，因为根本不存在"过桥"的成本。

PREVIEW_DIR = os.path.join(APP_DIR, "web", "_preview")
_preview_last_mode = ["未知"]
_PREVIEW_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def _preview_reset_dir():
    """启动时清空预览目录（上次运行残留的临时文件没必要留着）"""
    try:
        if os.path.isdir(PREVIEW_DIR):
            for n in os.listdir(PREVIEW_DIR):
                try:
                    os.remove(os.path.join(PREVIEW_DIR, n))
                except OSError:
                    pass
        else:
            os.makedirs(PREVIEW_DIR, exist_ok=True)
    except OSError:
        pass


def _preview_link(path, seq):
    """
    在 web/_preview/ 下给 path 建一个可被网页直接引用的副本，返回相对 URL。

    文件名带序号是必须的：同名文件浏览器会吃缓存，换了图还显示上一张。
    优先用硬链接 —— 同一磁盘分区内是瞬间完成且不占额外空间的元数据操作，
    10MB 的图也一样快。跨盘或文件系统不支持时才退回复制。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in _PREVIEW_EXTS:
        return None
    try:
        os.makedirs(PREVIEW_DIR, exist_ok=True)
    except OSError:
        return None

    name = f"p{seq}{ext}"
    dest = os.path.join(PREVIEW_DIR, name)
    try:
        if os.path.exists(dest):
            os.remove(dest)
    except OSError:
        pass

    try:
        os.link(path, dest)
        _preview_last_mode[0] = "硬链接"
    except (OSError, AttributeError, NotImplementedError):
        # 跨盘/文件系统不支持：只能整份复制。图片和启动器不在同一个盘时
        # 就是这条路，大图会明显比硬链接慢
        try:
            shutil.copyfile(path, dest)
            _preview_last_mode[0] = "跨盘复制"
        except OSError:
            return None
    return "_preview/" + name


def _preview_cleanup(keep_seq):
    """
    只留当前这一个。留多了没意义：浏览器已经加载完的图跟磁盘上的文件
    没关系了，而堆着一串临时文件只会白占空间。
    """
    try:
        for n in os.listdir(PREVIEW_DIR):
            if not n.startswith(f"p{keep_seq}."):
                try:
                    os.remove(os.path.join(PREVIEW_DIR, n))
                except OSError:
                    pass
    except OSError:
        pass


def _image_data_url(path, max_bytes=8 * 1024 * 1024):
    """
    data URL 版预览，现在只在硬链接那条路走不通时兜底
    （比如启动器装在只读目录里，web/_preview 建不出来）。
    """
    try:
        if os.path.getsize(path) > max_bytes:
            return None
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".bmp": "image/bmp",
                ".gif": "image/gif"}.get(os.path.splitext(path)[1].lower())
        if not mime:
            return None
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except OSError:
        return None


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ============================================================
# JS API 主体
# ============================================================

class LauncherApi:
    def __init__(self):
        self._window = None
        self.cfg = cm.load_config()
        self._ask_seq = 0
        self._asks = {}            # ask_id -> {"event": Event, "value": ...}
        self._threads_lock = threading.Lock()

        # 事件推送队列：_emit 只入队，由专门的 sender 线程串行 evaluate_js。
        # 以前是每个 worker 线程 emit 时同步阻塞到渲染进程完成 JS——
        # 日志一多渲染侧积压，背压直接拖慢 pip/git 子进程，UI 还持续
        # 高负载假死。队列满时只允许丢最旧的日志事件，state/ask 等
        # 控制事件宁可阻塞也绝不丢。
        self._emit_q = queue.Queue(maxsize=5000)
        self._emit_sender = None
        self._emit_sender_lock = threading.Lock()

        # 一键启动
        self._launch_proc = None
        self._launch_url = None
        self._launch_status_text = "尚未启动"
        self._launch_state = "idle"   # idle / starting / ready / stopped
        self._output_tail = ""
        # 就绪判定的并发保护。踩过的两个坑：
        #  1) 日志解析线程和端口探测线程会同时判定「就绪」，各开一次浏览器 ——
        #     两个一模一样的标签页。光靠各自读一遍 self._launch_url 挡不住：
        #     端口探测是 2 秒轮询，它在轮询开头读完 _launch_url 之后要花时间
        #     逐个端口探，这段时间里日志线程早就判完并开好浏览器了。
        #  2) 点「停止」时 launch_stop 会把 _launch_url 清成 None，端口探测线程
        #     的守卫当场失效；而正在被杀的 python 进程还没来得及释放端口，
        #     于是被当成「刚刚启动成功」，又开一个根本打不开的网页。
        # 解法：用代次（generation）标记每一轮启动，启动/停止各递增一次，
        # 所有监视线程带着自己那轮的代次干活，代次对不上就立刻收工；
        # 真正的「开浏览器」收敛到 _mark_launch_ready 一个加锁的入口。
        self._launch_lock = threading.Lock()
        self._launch_gen = 0
        self._launch_opened = False
        self._last_open_url = None
        self._last_open_url_ts = 0.0

        # 部署
        self._deploy_thread = None
        self._deploy_cancel = threading.Event()
        self._deploy_proc = None
        self._deploy_running = False
        # 双击「开始部署」会在前端连发两次请求，检查和置位必须原子，
        # 否则两个部署线程并发跑，日志/配置互相踩
        self._deploy_lock = threading.Lock()

        # 启动器自更新
        self._update_running = False
        self._update_lock = threading.Lock()

        # 模型下载页（Civitai / liblib 共用一套界面）
        self._civitai_version_info = None
        self._civitai_download_cancel = False
        self._download_source = "civitai"   # 最近一次「获取信息」的来源：civitai / liblib
        self._liblib_download_info = None

        # 模型管理
        self._models_categories = []
        self._hash_thread = None
        self._batch_thread = None
        self._batch_cancel = False

        # 常用插件
        self._ext_thread = None
        self._ext_cancel = False
        self._ext_proc = None

        # WD14
        self._wd14_model_dir = None
        self._wd14_load_thread = None
        self._wd14_load_cancel = False
        self._wd14_tag_thread = None
        self._wd14_tag_cancel = False
        self._wd14_proc = None

        # 图片信息
        self._meta_current = None        # 最近一次解析结果（展示层结构）
        self._meta_meta = None           # 最近一次解析结果（可编辑的 ImageMetadata）
        self._meta_path = ""             # 当前图片路径
        self._meta_batch = []            # 批量列表里的全部图片路径
        self._preview_seq = 0            # 预览文件名序号（换名才能绕开浏览器缓存）
        _preview_reset_dir()
        self._meta_refs = []
        self._meta_lookup_result = {}    # idx -> {"ok":...}
        self._meta_name_searches = {}    # idx -> [candidates]
        self._meta_dl_thread = None
        self._meta_dl_cancel = False

    # ---------------------------------------------------------- 基础设施 ----

    def _set_window(self, window):
        self._window = window

    def _emit(self, scope, type_, **data):
        """推送事件给前端：只放进发送队列，由 sender 线程统一 evaluate_js。
        worker 线程从此不会因渲染进程阻塞而产生背压。"""
        if not self._window:
            return
        self._ensure_emit_sender()
        evt = {"scope": scope, "type": type_, **data}
        if type_ != "log":
            # state/ask/progress 等控制事件不允许丢，队列满了就阻塞等 sender 消化
            self._emit_q.put(evt)
            return
        # 日志事件量大、丢了也不影响流程：队列满时丢掉最旧的一条日志
        try:
            self._emit_q.put_nowait(evt)
        except queue.Full:
            self._drop_oldest_log()
            try:
                self._emit_q.put_nowait(evt)
            except queue.Full:
                pass  # 极端情况下连最旧日志都腾不出位置，只好丢这条新的

    def _drop_oldest_log(self):
        """队列已满时腾位置：移除队列里最旧的一条 log 事件（不碰其他类型）"""
        kept = []
        dropped = False
        try:
            while True:
                evt = self._emit_q.get_nowait()
                if not dropped and evt.get("type") == "log":
                    dropped = True
                    continue
                kept.append(evt)
        except queue.Empty:
            pass
        for evt in kept:
            try:
                self._emit_q.put_nowait(evt)
            except queue.Full:
                break  # sender 消费得慢，保住排前面的，剩下的丢弃

    def _ensure_emit_sender(self):
        if self._emit_sender is not None:
            return
        with self._emit_sender_lock:
            if self._emit_sender is not None:
                return
            t = threading.Thread(target=self._emit_sender_loop,
                                 daemon=True, name="emit-sender")
            t.start()
            self._emit_sender = t

    def _emit_sender_loop(self):
        """唯一给前端发事件的线程：从队列取事件，相邻同 scope 的 log 事件
        贪心合并成一条再发，降低 evaluate_js 频率（前端日志追加是 O(n²)，
        发送太密会把渲染进程拖垮）"""
        pending = []  # 合并时取出但不属于本批的事件，按原顺序补发
        while True:
            if pending:
                evt = pending.pop(0)
            else:
                evt = self._emit_q.get()
            if evt is None:
                break
            try:
                if evt.get("type") == "log":
                    scope = evt.get("scope")
                    text = evt.get("text") or ""
                    while True:
                        try:
                            nxt = self._emit_q.get(timeout=0.05)
                        except queue.Empty:
                            break
                        if nxt.get("type") == "log" and nxt.get("scope") == scope:
                            piece = nxt.get("text") or ""
                            # 前端对每条事件之间自行补换行；合并后缺换行要补上，
                            # 否则两条日志会粘成一行
                            if text and not text.endswith("\n") and not piece.startswith("\n"):
                                text += "\n"
                            text += piece
                        else:
                            pending.append(nxt)
                            break
                    evt = {**evt, "text": text}
                self._send_event(evt)
            except Exception:
                pass  # 单条发送失败（窗口在销毁等）不影响后续事件

    def _send_event(self, evt):
        window = self._window
        if not window:
            return
        try:
            payload = json.dumps(evt, ensure_ascii=False)
            window.evaluate_js(f"window.App && window.App.onEvent({payload})")
        except Exception:
            pass

    def _ask(self, scope, title, body, buttons, cancel_event=None):
        """给前端弹一个选择框，阻塞当前工作线程直到用户点了某个按钮。
        cancel_event 被 set（如用户点了取消部署）时返回 None，调用方按
        用户取消处理——否则弹窗期间取消会让工作线程永久挂死。"""
        self._ask_seq += 1
        ask_id = f"ask_{self._ask_seq}"
        ev = threading.Event()
        self._asks[ask_id] = {"event": ev, "value": None}
        self._emit(scope, "ask", ask_id=ask_id, title=title, body=body, buttons=buttons)
        while not ev.wait(0.5):
            if cancel_event is not None and cancel_event.is_set():
                self._asks.pop(ask_id, None)
                return None
        return self._asks.pop(ask_id, {}).get("value")

    def answer(self, ask_id, value):
        a = self._asks.get(ask_id)
        if a:
            a["value"] = value
            a["event"].set()
        return {"ok": True}

    def handle_dropped_paths(self, paths):
        """由入口处的 DOM drop 事件调用：把拖入文件的完整路径推给前端处理"""
        paths = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
        if paths:
            self._emit("app", "dropped", paths=paths)
        return {"ok": True}

    def _spawn(self, target, name="worker"):
        t = threading.Thread(target=self._guarded, args=(target,), daemon=True, name=name)
        t.start()
        return t

    def _guarded(self, fn):
        """工作线程的兜底：任何没接住的异常都推到日志里，不让线程静悄悄死掉"""
        try:
            fn()
        except Exception:
            self._emit("app", "error", text="内部错误:\n" + traceback.format_exc()[-1500:])

    # ---------------------------------------------------------- 配置 ----

    def get_state(self):
        local_ver = updater.get_local_info(self.cfg)
        return {
            "ok": True,
            "config": self.cfg,
            "cmd_args": cm.build_commandline_args(self.cfg),
            "is_windows": os.name == "nt",
            "launch": self.launch_status(),
            "launcher": {"version": local_ver["version"], "commit": local_ver["commit"]},
            "mirror_status": self._mirror_status_text(),
            "settings_schema": self._settings_schema(),
            "deploy_branches": [{"label": l, "key": k} for l, k in DEPLOY_BRANCH_OPTIONS],
            "settings_branches": [{"label": l, "key": k} for l, k in SETTINGS_BRANCH_OPTIONS],
        }

    def update_config(self, changes):
        if isinstance(changes, dict):
            self.cfg.update(changes)
            try:
                cm.save_config(self.cfg)
            except OSError:
                pass
        return {"ok": True, "cmd_args": cm.build_commandline_args(self.cfg)}

    def get_cmd_args(self):
        return {"ok": True, "cmd_args": cm.build_commandline_args(self.cfg)}

    def _settings_schema(self):
        def opts(table):
            return [{"label": l, "key": k} for l, k, _a in table]
        return {
            "vram_legacy": opts(cm.VRAM_MODE_OPTIONS_LEGACY),
            "vram_neo2": opts(cm.VRAM_MODE_OPTIONS_NEO2),
            "precision_legacy": opts(cm.PRECISION_MODE_OPTIONS_LEGACY),
            "precision_neo2": opts(cm.PRECISION_MODE_OPTIONS_NEO2),
            "unet_neo2": opts(cm.UNET_PRECISION_OPTIONS_NEO2),
            "vae_neo2": opts(cm.VAE_PRECISION_OPTIONS_NEO2),
            "text_enc_neo2": opts(cm.TEXT_ENC_PRECISION_OPTIONS_NEO2),
            "attention_neo2": opts(cm.ATTENTION_OPTIONS_NEO2),
        }

    # ---------------------------------------------------------- 系统对话框 / 杂项 ----

    def choose_directory(self, title="选择文件夹", start=""):
        try:
            import webview
            res = self._window.create_file_dialog(
                webview.FOLDER_DIALOG, directory=start or os.getcwd())
            return {"ok": True, "path": res[0] if res else ""}
        except Exception as e:
            return {"ok": False, "error": str(e), "path": ""}

    def choose_images(self, multiple=True):
        try:
            import webview
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=multiple,
                file_types=("图片文件 (*.png;*.jpg;*.jpeg;*.webp;*.bmp)", "所有文件 (*.*)"))
            return {"ok": True, "paths": list(res) if res else []}
        except Exception as e:
            return {"ok": False, "error": str(e), "paths": []}

    def open_url(self, url):
        # 同一网址 1.5 秒内的重复请求直接忽略。前端按钮的点击事件在
        # WebView2 里偶尔会触发两次（也可能是用户手抖双击），结果就是
        # 一下开两个相同的网页，很莫名其妙。
        now = time.time()
        with self._launch_lock:
            if url == self._last_open_url and now - self._last_open_url_ts < 1.5:
                return {"ok": True, "deduped": True}
            self._last_open_url = url
            self._last_open_url_ts = now
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return {"ok": True}

    def _mark_launch_ready(self, gen, url, text, auto_open):
        """
        「WebUI 就绪」的唯一入口：日志解析线程和端口探测线程都走这里。

        返回 True 表示本次调用是真正的首次就绪（状态已更新、浏览器已开）；
        返回 False 表示这一轮启动已经作废（用户点了停止/又点了启动），
        或者另一条线程抢先判定过了 —— 两种情况都不该再开浏览器。
        """
        with self._launch_lock:
            if gen != self._launch_gen:
                return False          # 这轮启动已经被 停止/重启 作废
            if self._launch_opened:
                return False          # 另一条线程抢先判定过了
            self._launch_opened = True
            self._launch_url = url
        self._set_launch_status("ready", text, url=url)
        if auto_open:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return True

    def _launch_gen_alive(self, gen):
        """监视线程的心跳检查：代次对不上就说明该收工了。"""
        return gen == self._launch_gen

    def reveal_in_explorer(self, path):
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def webui_running(self):
        return bool(self._launch_proc and self._launch_proc.poll() is None)

    def deploy_running(self):
        return bool(self._deploy_running)

    def request_exit_confirm(self):
        """窗口关闭事件里调用：WebUI 还在跑就让前端弹确认框"""
        self._emit("app", "confirm_exit")

    def exit_app(self, kill_webui=True):
        if kill_webui:
            self._kill_launch_tree()
            if self._window:
                self._window.destroy()
            return {"ok": True}
        # 不杀 WebUI 时 destroy() 会被 on_closing 否决（窗口关不掉、确认框
        # 已经关了，观感就是「退出卡死」），所以这里不再尝试关窗，改为
        # 明确提示用户本次退出已被取消
        self._emit("app", "error",
                   text="已取消退出：Forge WebUI 仍在运行，直接退出会留下占端口/显存的孤儿进程。"
                        "请先停止 WebUI，或在关闭确认框里选「结束 WebUI 并退出」。")
        return {"ok": True}

    # ============================================================
    # 一键启动
    # ============================================================

    def launch_status(self):
        return {
            "running": self.webui_running(),
            "state": self._launch_state,
            "url": self._launch_url,
            "text": self._launch_status_text,
        }

    def _set_launch_status(self, state, text, url=None):
        self._launch_state = state
        self._launch_status_text = text
        if url is not None:
            self._launch_url = url
        self._emit("launch", "status", state=state, text=text, url=self._launch_url)

    def launch_env_detect(self, root):
        """检测便携版/系统 python/git，供前端展示提示"""
        root = (root or "").strip()
        out = {"python": "", "git": ""}
        if not root or not os.path.isdir(root):
            return {"ok": True, **out}
        if not (self.cfg.get("custom_python_path") or "").strip():
            found = cm.detect_bundled_python(root)
            out["python"] = f"检测到便携版 Python: {found}" if found else "未检测到便携版 Python，将使用系统 python"
        if not (self.cfg.get("custom_git_path") or "").strip():
            found = cm.detect_bundled_git(root)
            out["git"] = f"检测到便携版 Git: {found}" if found else "未检测到便携版 Git，将使用系统 git"
        return {"ok": True, **out}

    def launch_precheck(self, root):
        """
        启动前的全部检查，一次返回。前端根据 issues 决定直接启动还是弹窗确认。
        issue.level: error（不能启动）/ warn（可修复或跳过）
        """
        root = (root or "").strip()
        issues = []
        if not root or not os.path.isdir(root):
            issues.append({"level": "error", "text": "请先设置正确的 WebUI 根目录"})
            return {"ok": False, "issues": issues}

        entry = os.path.join(root, WEBUI_ENTRY_SCRIPT)
        if not os.path.exists(entry):
            issues.append({
                "level": "error",
                "text": f"该目录下没有找到 {WEBUI_ENTRY_SCRIPT}。\n"
                        "如果你还没安装，去「环境部署」页先部署一个；如果目录没错，"
                        "确认一下这是不是 WebUI 的根目录（应该和 webui.py 在同一层）。",
            })
            return {"ok": False, "issues": issues}

        try:
            root.encode("ascii")
        except UnicodeEncodeError:
            issues.append({
                "level": "warn", "id": "non_ascii_path",
                "text": f"WebUI 根目录：\n{root}\n\n包含中文（或其他非英文字符）。"
                        "启动器本身没问题，但 Forge 依赖的 torch / gradio / 部分扩展在中文路径下"
                        "有各自的历史 bug，出问题时很难排查。\n\n"
                        "强烈建议把整个文件夹移到纯英文路径（例如 F:\\forge）再启动。",
            })
        if " " in root:
            issues.append({
                "level": "warn", "id": "space_in_path",
                "text": f"WebUI 根目录：\n{root}\n\n包含空格。虽然多数情况下能跑，"
                        "但部分扩展/依赖对带空格的路径处理得不好。\n"
                        "建议换成不含空格的路径（例如 F:\\forge）。",
            })

        # 试运行 python / git：路径存在 ≠ 能执行（解压不完整、缺文件、
        # 被杀软拦截都会让 exe 一跑就挂）。git 挂了 Forge 会在启动中途抛
        # "ImportError: Bad git executable"，提前在这里用大白话拦下来。
        py_exe = (self.cfg.get("custom_python_path") or "").strip() \
            or cm.detect_bundled_python(root)
        if py_exe:
            ok, why = _probe_executable([py_exe.strip('"'), "--version"])
            if not ok:
                issues.append({
                    "level": "error",
                    "text": f"便携版 Python 无法运行：\n{py_exe}\n\n原因: {why}\n\n"
                            "常见情况是整合包解压不完整或文件被杀毒软件损坏，"
                            "建议重新解压整合包（解压前关掉杀毒/ Defender 实时保护，"
                            "或把目录加进排除列表）。",
                })
        git_exe = (self.cfg.get("custom_git_path") or "").strip() \
            or cm.detect_bundled_git(root)
        if git_exe:
            ok, why = _probe_executable([git_exe.strip('"'), "version"])
            if not ok:
                issues.append({
                    "level": "error",
                    "text": f"便携版 Git 无法运行：\n{git_exe}\n\n原因: {why}\n\n"
                            "Git 损坏会让 Forge 启动到一半报 Bad git executable 然后退出。\n"
                            "常见原因：整合包解压不完整（git 目录缺文件）、文件被杀毒软件拦截。\n"
                            "解决办法：重新解压整合包；或者把根目录下的 git 文件夹改名/删除，"
                            "启动器会改用系统里已安装的 Git（前提是系统装过）。",
                })

        if self.cfg.get("enable_listen") and self.cfg.get("enable_insecure_extension_access"):
            issues.append({
                "level": "warn", "id": "listen_insecure_combo",
                "text": "当前同时开启了 --listen（允许局域网访问）和\n"
                        "--enable-insecure-extension-access（允许网页安装扩展）。\n\n"
                        "这个组合意味着：同一局域网里的任何人都能从网页给你的 WebUI "
                        "装扩展——而扩展就是任意 Python 代码，等于把整台电脑交出去。\n\n"
                        "建议到「高级选项」至少关掉其中一个：\n"
                        "  · 只是自己用 → 关掉 --listen（仅本机访问）\n"
                        "  · 确需局域网访问 → 关掉 --enable-insecure-extension-access",
            })

        overrides = _find_webui_user_bat_overrides(root)
        if overrides:
            detail = "\n".join(f"  第 {ln} 行: set {name}={val}" for name, val, ln in overrides)
            issues.append({
                "level": "warn", "id": "user_bat_overrides",
                "text": "检测到 webui-user.bat 里写死了以下变量：\n\n" + detail + "\n\n"
                        "这几行是无条件执行的 set，会把启动器注入的路径/参数强行覆盖回去，"
                        "很可能导致启动失败（尤其是换过电脑、改过安装路径之后）。\n"
                        "选择「清空并启动」会自动清空这几行（原文件备份为 webui-user.bat.bak）。",
            })

        # 残留进程检查：不限于配置的端口，7860-7869 整个范围都扫。
        # 上次没退干净的 WebUI 会同时占着显存/内存，还会干扰就绪探测
        # （把旧进程误判成“刚启动成功”，浏览器秒开一个旧实例）。
        cfg_port = str(self.cfg.get("port", "")).strip()
        scan_ports = [int(cfg_port)] if cfg_port.isdigit() else list(range(7860, 7870))
        leftovers, seen = [], set()
        for p in scan_ports:
            pid = find_listening_pid(str(p))
            if pid and pid not in seen:
                seen.add(pid)
                leftovers.append((p, pid))
        if leftovers:
            detail = "\n".join(
                f"  端口 {p}: {process_name_of(pid) or '未知进程'} (PID {pid})"
                for p, pid in leftovers)
            issues.append({
                "level": "warn", "id": "port_occupied",
                "pid": leftovers[0][1],
                "pids": [pid for _, pid in leftovers],
                "text": f"检测到 {len(leftovers)} 个残留进程还占着端口：\n\n{detail}\n\n"
                        "这通常是之前没退干净的 WebUI 实例，还占着显存和内存，"
                        "而且会干扰启动器的就绪检测（把旧进程误判成刚启动成功的那个）。\n"
                        "选择「结束并启动」会把它们全部结束；"
                        "选择「直接启动」Forge 会自动换用空闲端口。",
            })

        return {"ok": True, "issues": issues}

    def launch_start(self, root, fix_overrides=False, kill_pid=0):
        if self.webui_running():
            return {"ok": False, "error": "WebUI 已在运行中"}
        root = (root or "").strip()
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": "WebUI 根目录无效"}
        if not os.path.exists(os.path.join(root, WEBUI_ENTRY_SCRIPT)):
            return {"ok": False, "error": f"目录下没有 {WEBUI_ENTRY_SCRIPT}"}

        self.cfg["webui_root"] = root
        cm.save_config(self.cfg)

        log = lambda t: self._emit("launch", "log", text=t)

        if kill_pid:
            pids = kill_pid if isinstance(kill_pid, (list, tuple)) else [kill_pid]
            for pid in pids:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    continue
                pname = process_name_of(pid) or "未知进程"
                kill_process_tree(pid)
                log(f"[启动器] 已结束占用端口的残留进程 {pname} (PID {pid})\n")

        if fix_overrides:
            try:
                _clear_webui_user_bat_overrides(root)
                log("[启动器] 已清空 webui-user.bat 里写死的 PYTHON/GIT/VENV_DIR/"
                    "COMMANDLINE_ARGS（原文件备份为 webui-user.bat.bak）\n")
            except OSError as e:
                return {"ok": False, "error": f"无法修改 webui-user.bat：{e}"}

        # 静默修复 venv\pyvenv.cfg 里过期的 home 路径（安装目录被移动/改名后
        # 最常见的启动失败原因）
        resolved_python = (self.cfg.get("custom_python_path") or "").strip()
        if not resolved_python:
            resolved_python = cm.detect_bundled_python(root) or ""
        if resolved_python:
            try:
                cm.sync_venv_pyvenv_cfg(root, resolved_python,
                                        lambda m: log(m + "\n"))
            except OSError as e:
                log(f"[启动器] 检查 venv pyvenv.cfg 时出错（不影响继续启动）: {e}\n")

        args_str = cm.build_commandline_args(self.cfg)
        env_overrides = cm.build_launch_env_overrides(self.cfg, root)

        log(f"[启动器] 正在启动 {WEBUI_ENTRY_SCRIPT} ...\n")
        log(f"[启动器] COMMANDLINE_ARGS = {args_str}\n")
        for k, v in env_overrides.items():
            if k != "COMMANDLINE_ARGS":
                log(f"[启动器] {k} = {v}\n")
        log("\n")

        # 就绪探测用的端口范围（跟 _launch_port_watcher 保持一致），
        # 并在启动前给这些端口的现有监听者拍快照——否则上次没退干净的
        # 残留 WebUI 会被误判成“刚启动成功”，浏览器秒开一个旧实例。
        cfg_port = str(self.cfg.get("port", "")).strip()
        watch_ports = [int(cfg_port)] if cfg_port.isdigit() else list(range(7860, 7870))
        pre_pids = set()
        for p in watch_ports:
            pid = find_listening_pid(str(p))
            if pid:
                pre_pids.add(pid)
        self._launch_watch_ports = watch_ports
        self._launch_pre_pids = pre_pids

        env = dict(os.environ)
        env.update(env_overrides)
        try:
            self._launch_proc = subprocess.Popen(
                ["cmd.exe", "/c", WEBUI_ENTRY_SCRIPT] if os.name == "nt" else ["sh", "webui.sh"],
                cwd=root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        except OSError as e:
            return {"ok": False, "error": f"启动失败: {e}"}

        # 开启新一轮：代次 +1，把上一轮可能还活着的监视线程全部作废，
        # 同时重置「本轮是否已经开过浏览器」的标记
        with self._launch_lock:
            self._launch_gen += 1
            gen = self._launch_gen
            self._launch_opened = False
            self._launch_url = None

        self._output_tail = ""
        self._set_launch_status("starting", "启动中，等待 Forge 输出监听地址...", url=None)
        self._emit("launch", "state", running=True)
        self._spawn(lambda: self._launch_reader(gen), name="launch-reader")
        # 兜底：有些整合包的 webui.bat 会把输出重定向到自己的 log 文件，
        # 管道里一个字节都没有，靠解析输出判断就绪就会永远卡在「启动中」——
        # 所以同时起一个端口监视线程，发现 python 在监听就直接判定就绪
        self._spawn(lambda: self._launch_port_watcher(gen), name="launch-port-watcher")
        return {"ok": True}

    def _launch_port_watcher(self, gen):
        ports = getattr(self, "_launch_watch_ports", None) or list(range(7860, 7870))
        pre_pids = getattr(self, "_launch_pre_pids", None) or set()
        auto_open = bool(self.cfg.get("auto_open_browser_on_ready", True))
        skipped_logged = set()
        for _ in range(600):  # 每 2 秒一次，最长 20 分钟
            if not self._launch_gen_alive(gen):
                return  # 用户已经点了停止 / 又点了启动，这轮作废
            if not self._launch_proc or self._launch_proc.poll() is not None:
                return
            if self._launch_url:
                return  # 日志解析已经拿到地址了
            for p in ports:
                # 每探一个端口都重查一次代次：探端口本身有耗时（netstat/
                # 系统调用），用户完全可能在这中间点了停止按钮
                if not self._launch_gen_alive(gen):
                    return
                pid = find_listening_pid(str(p))
                if not pid:
                    continue
                if pid in pre_pids:
                    # 启动前就存在的监听者 = 上次没退干净的残留，不算本次就绪
                    if p not in skipped_logged:
                        skipped_logged.add(p)
                        self._emit("launch", "log",
                                   text=f"[启动器] 端口 {p} 上有残留的旧 WebUI 进程 (PID {pid})，"
                                        "就绪判定将忽略它；建议下次启动前在预检弹窗里选「结束并启动」\n")
                    continue
                pname = (process_name_of(pid) or "").lower()
                if pname not in ("python.exe", "pythonw.exe", "python3.exe"):
                    continue
                url = f"http://127.0.0.1:{p}"
                if self._mark_launch_ready(
                        gen, url, f"已就绪（端口探测），实际访问地址: {url}", auto_open):
                    self._emit("launch", "log",
                               text=f"\n[启动器] 通过端口探测到 WebUI 已在监听: {url}\n")
                return
            time.sleep(2)

    def _launch_reader(self, gen):
        proc = self._launch_proc
        auto_open = bool(self.cfg.get("auto_open_browser_on_ready", True))
        try:
            while True:
                # read1() 而非 read()：后者会攒满 4096 字节才返回，
                # WebUI 输出停顿时段日志会一直卡住不显示
                data = proc.stdout.read1(4096)
                if not data:
                    break
                text = cm.decode_process_output(data)
                self._emit("launch", "log", text=text)

                combined = self._output_tail + text
                self._output_tail = combined[-500:]

                if _BIND_ERROR_RE.search(combined) and not self._launch_url:
                    self._set_launch_status(
                        "starting", "检测到端口被占用，Forge 正在自动尝试其他端口，请以后面出现的地址为准")

                m = _URL_RE.search(combined)
                if m and not self._launch_url:
                    url = m.group(1).replace("0.0.0.0", "127.0.0.1")
                    self._mark_launch_ready(gen, url, f"已就绪，实际访问地址: {url}", auto_open)
        except Exception:
            self._emit("launch", "log",
                       text="\n[启动器] 读取进程输出时出错:\n" + traceback.format_exc()[-800:] + "\n")
        finally:
            rc = proc.wait()
            self._emit("launch", "log", text=f"\n[启动器] 进程已结束，返回码: {rc}\n")
            # 只有当这一轮还是「当前那一轮」时才改全局状态。
            # 否则会出现这种错位：用户点停止 → 立刻又点启动 → 上一轮的
            # reader 这时才收尾，把刚起来的新一轮状态改成「已停止」。
            if self._launch_gen_alive(gen):
                self._launch_proc = None
                self._launch_url = None
                self._set_launch_status("stopped", "已停止")
                self._emit("launch", "state", running=False)

    def _kill_launch_tree(self):
        if self._launch_proc and self._launch_proc.poll() is None:
            kill_process_tree(self._launch_proc.pid)
            try:
                self._launch_proc.kill()
                self._launch_proc.wait(timeout=3)
            except Exception:
                pass
            return True
        return False

    def launch_stop(self):
        # 先作废代次，再动手杀进程。顺序很重要：反过来的话，杀进程那几秒里
        # 端口探测线程还认为自己有效，而正在退出的 python 仍占着端口没释放，
        # 就会被判成「刚就绪」，弹出一个根本打不开的网页。
        with self._launch_lock:
            self._launch_gen += 1
            self._launch_opened = True   # 本轮彻底封死开浏览器这条路
            self._launch_url = None

        killed = self._kill_launch_tree()
        if killed:
            self._emit("launch", "log", text="\n[启动器] 已终止 WebUI 进程树（含子进程，端口已释放）\n")
        self._launch_proc = None
        self._set_launch_status("stopped", "已停止")
        self._emit("launch", "state", running=False)
        return {"ok": True, "killed": killed}


class _DeployCancelled(Exception):
    pass


class LauncherApiDeployMixin:  # 仅为阅读分节，实际方法都挂在 LauncherApi 上
    pass


# ============================================================
# 环境部署
# ============================================================

def _api_deploy_env_detect(self):
    def _ver(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                               creationflags=_NO_WINDOW)
            return (r.stdout or r.stderr or "").strip()
        except Exception:
            return None

    git_path = shutil.which("git")
    py_path = shutil.which("python") or shutil.which("python3")
    return {
        "ok": True,
        "git": {"found": bool(git_path),
                "text": f"已检测到 ({_ver([git_path, '--version']) or git_path})" if git_path
                        else "未检测到 Git，请先安装：https://git-scm.com/download/win"},
        "python": {"found": bool(py_path),
                   "text": f"已检测到 ({_ver([py_path, '--version']) or py_path})" if py_path
                           else "未检测到 Python，请先安装：https://www.python.org/downloads/"},
    }


def _api_deploy_check_dir(self, target):
    target = (target or "").strip()
    if not target:
        return {"ok": True, "status": "warn", "message": "请先选择目录"}
    webui_bat = os.path.join(target, "webui.bat")
    if os.path.isdir(target) and os.path.exists(webui_bat):
        return {"ok": True, "status": "ok",
                "message": "检测到该目录已经是一个 WebUI 安装（存在 webui.bat），无需重新克隆，"
                           "可直接点击部署来补齐/更新虚拟环境依赖"}
    if os.path.isdir(target) and os.listdir(target):
        entries = set(os.listdir(target))
        if entries <= RESUMABLE_ENTRIES:
            return {"ok": True, "status": "warn",
                    "message": "检测到上一次部署的残留（" + "、".join(sorted(entries))
                               + "），可以直接点击部署继续，已完成的部分会被跳过"}
        return {"ok": True, "status": "bad",
                "message": "该目录已存在且不为空，但未检测到 webui.bat——请换一个空目录"}
    return {"ok": True, "status": "ok", "message": "目录为空或不存在，可以直接克隆到这里"}


def _api_deploy_precheck(self, target, branch, use_portable):
    target = (target or "").strip()
    issues = []
    if not target:
        return {"ok": False, "issues": [{"level": "error", "text": "请先选择安装目录"}]}

    webui_bat = os.path.join(target, "webui.bat")
    already_installed = os.path.isdir(target) and os.path.exists(webui_bat)

    if not already_installed and os.path.isdir(target):
        entries = set(os.listdir(target))
        if entries and not entries <= RESUMABLE_ENTRIES:
            issues.append({
                "level": "error",
                "text": "该目录已存在内容但不是有效的 WebUI 安装，请换一个空目录。\n\n目录里有："
                        + "、".join(sorted(entries)[:8]) + ("……" if len(entries) > 8 else ""),
            })

    try:
        target.encode("ascii")
    except UnicodeEncodeError:
        issues.append({
            "level": "warn", "id": "non_ascii",
            "text": f"安装目录：\n{target}\n\n包含中文（或其他非英文字符）。启动器本身没问题，"
                    "但 Forge 依赖的 torch / gradio / 部分扩展在中文路径下有各自的历史 bug，"
                    "出问题时很难排查——这也是秋叶整合包一直要求纯英文路径的原因。\n\n"
                    "强烈建议换成纯英文路径（例如 C:\\forge）。",
        })

    bad_chars = set("()&^%!") & set(target)
    if bad_chars:
        issues.append({
            "level": "warn", "id": "bad_chars",
            "text": f"安装目录：\n{target}\n\n包含字符 {' '.join(sorted(bad_chars))}——"
                    "Windows 批处理脚本（cmd.exe 的 if/for 代码块）对这几个字符很敏感，"
                    "Forge 自带的 webui.bat 内部用到这类结构时可能报「此时不应有 X。」这种"
                    "看似莫名其妙的错误（常见诱因：文件夹名带 \"(1)\"，往往是浏览器重复下载"
                    "同名文件自动生成的）。\n\n这不是启动器的 bug，是 cmd.exe 本身的限制，"
                    "无法从这里修复。建议换成不含这些字符的路径。",
        })

    if not use_portable:
        if not ((self.cfg.get("custom_git_path") or "").strip() or shutil.which("git")):
            issues.append({"level": "error",
                           "text": "未检测到 Git，请先安装：https://git-scm.com/download/win\n"
                                   "（或者勾选「自动下载便携版 Python + Git」跳过这个要求；"
                                   "也可以在「设置」页手动指定 Git 路径）"})
        if not (shutil.which("python") or shutil.which("python3")):
            issues.append({"level": "error",
                           "text": "未检测到 Python，请先安装：https://www.python.org/downloads/\n"
                                   "（或者勾选「自动下载便携版 Python + Git」跳过这个要求）"})

    has_error = any(i["level"] == "error" for i in issues)
    return {"ok": not has_error, "issues": issues, "already_installed": already_installed}


def _api_deploy_start(self, target, branch, use_portable):
    # 检查和置位必须原子，否则双击会并发跑两个部署线程
    with self._deploy_lock:
        if self._deploy_running:
            return {"ok": False, "error": "已有部署任务在进行中"}
        target = (target or "").strip()
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"无法创建目录：\n{target}\n\n{e}\n\n"
                                          "常见原因：盘符不存在、路径不合法、或没有写入权限。"}

        self.cfg["webui_branch"] = branch
        cm.save_config(self.cfg)

        self._deploy_cancel.clear()
        self._deploy_running = True
    self._emit("deploy", "state", running=True)
    self._spawn(lambda: self._deploy_flow(target, branch, use_portable), name="deploy")
    return {"ok": True}


def _api_deploy_cancel(self):
    self._deploy_cancel.set()
    if self._deploy_proc and self._deploy_proc.poll() is None:
        kill_process_tree(self._deploy_proc.pid)
    # 部署线程可能正阻塞在 _ask() 里等用户点弹窗，这里把它唤醒，
    # 否则取消后部署线程永久挂死、_deploy_running 永远 True
    for a in list(self._asks.values()):
        a["event"].set()
    return {"ok": True}


def _deploy_flow(self, target, branch, use_portable):
    log = lambda t: self._emit("deploy", "log", text=t)
    progress = lambda d, t: self._emit("deploy", "progress", downloaded=d, total=t)
    try:
        bundled_python = os.path.join(target, "python", "python.exe")
        bundled_git = os.path.join(target, "git", "cmd", "git.exe")
        need_python = use_portable and not os.path.exists(bundled_python)
        need_git = use_portable and not os.path.exists(bundled_git)

        # ---- 1. 便携版 Python / Git ----
        if need_python or need_git:
            log(f"[部署] 准备下载便携环境 (Python: {'需要' if need_python else '已存在，跳过'}, "
                f"Git: {'需要' if need_git else '已存在，跳过'})\n")
            self._deploy_portable_env(target, branch, need_python, need_git, log, progress)
            log("[部署] 便携环境准备完成\n\n")
            progress(0, 0)

        if self._deploy_cancel.is_set():
            raise _DeployCancelled()

        # ---- 2. git 拉源码（init -> config -> fetch -> checkout，幂等可续跑）----
        webui_bat = os.path.join(target, "webui.bat")
        already_installed = os.path.isdir(target) and os.path.exists(webui_bat)
        # git 选取顺序与启动/预检/插件安装保持一致：自定义路径 > 便携版 > 系统 PATH。
        # 整合包自带的便携 git 可能是坏的（杀软误删等），用户在设置里手动指了
        # git 的话必须处处优先，否则部署这步又会去用坏的那个。
        custom_git = (self.cfg.get("custom_git_path") or "").strip().strip('"')
        if custom_git and os.path.exists(custom_git):
            git_exe = custom_git
        else:
            git_exe = bundled_git if os.path.exists(bundled_git) else "git"
        if os.path.exists(bundled_git):
            # 便携 Git 必做：关掉 Schannel 吊销检查，否则 CRL 查询不可达的
            # 网络里 fetch 必然 128（CRYPT_E_NO_REVOCATION_CHECK）。幂等。
            pe.tune_bundled_git(os.path.join(target, "git"), log_cb=log)

        if not already_installed:
            repo, ref = (NEO_REPO, NEO_BRANCH) if branch in _NEO_KEYS else (CLASSIC_REPO, "main")
            # 候选地址：判定需要加速时把所有代理都排进来（逐个重试），
            # 原站永远兜底。踩过的坑：以前只用 proxy[0]，那个代理一挂
            # 部署就直接失败，用户只能干等或手动换。
            repo_candidates = [repo]
            try:
                import mirror_manager as mm
                if mm.resolve_github_mode(self.cfg, log):
                    mirrored = [mm.github_url(repo, True, i) for i in range(len(mm.GITHUB_PROXIES))]
                    repo_candidates = [u for u in mirrored if u != repo] + [repo]
                    log(f"[网络] 使用 GitHub 加速（{len(repo_candidates)} 个候选地址，失败自动切换）\n")
            except Exception:
                pass
            parent = os.path.dirname(os.path.abspath(target)) or "."
            base_steps = [
                (git_exe, ["init", target], parent),
                (git_exe, ["-C", target, "config", "remote.origin.fetch",
                           "+refs/heads/*:refs/remotes/origin/*"], parent),
            ]
            for program, args, cwd in base_steps:
                if self._deploy_cancel.is_set():
                    raise _DeployCancelled()
                rc = self._deploy_run_cmd(program, args, cwd, log)
                if rc != 0:
                    log(f"\n[部署] 上一步骤返回非零退出码 ({rc})，请检查日志确认是否有报错，"
                        "确认无误后可以重新点击「开始部署」继续（已完成的步骤会被跳过）\n")
                    return

            # fetch 是唯一的网络步骤：每个候选地址各试一次
            fetched = False
            for u in repo_candidates:
                if self._deploy_cancel.is_set():
                    raise _DeployCancelled()
                rc = self._deploy_run_cmd(
                    git_exe, ["-C", target, "config", "remote.origin.url", u], parent, log)
                if rc != 0:
                    log(f"\n[部署] 设置远程地址失败（退出码 {rc}），请检查日志\n")
                    return
                rc = self._deploy_run_cmd(
                    git_exe, ["-C", target, "-c", "http.lowSpeedLimit=1000",
                              "-c", "http.lowSpeedTime=120", "fetch", "origin", ref],
                    parent, log, timeout=1800)
                if rc == 0:
                    fetched = True
                    break
                log(f"\n[部署] 从该地址拉取失败（退出码 {rc}），换下一个地址重试 ...\n")
            if not fetched:
                log("\n[部署] 所有候选地址都拉取失败，请检查网络/代理设置后重新点击「开始部署」\n")
                return

            if self._deploy_cancel.is_set():
                raise _DeployCancelled()
            rc = self._deploy_run_cmd(
                git_exe, ["-C", target, "checkout", "-f", "-B", ref, f"origin/{ref}"], parent, log)
            if rc != 0:
                log(f"\n[部署] 检出代码失败（退出码 {rc}），请检查日志\n")
                return
        else:
            log("[部署] 检测到已有安装，跳过 git clone 步骤\n")

        if self._deploy_cancel.is_set():
            raise _DeployCancelled()

        # ---- 3. venv 版本检测（不一致时问用户）----
        required = pe.PYTHON_VERSION_BY_BRANCH.get(branch, "3.10")
        venv_dir = os.path.join(target, "venv")
        mismatch, detail = pe.check_venv_version_mismatch(venv_dir, required)
        if mismatch:
            log(f"[部署] {detail}\n")
            choice = self._ask(
                "deploy", "检测到 venv 版本不一致",
                detail + "\n\n要现在删除这个 venv 让它用正确的 Python 重建吗？"
                         "（会重新下载安装依赖，比较花时间；选「继续使用」大概率还会遇到版本相关的报错）",
                [
                    {"id": "delete", "label": "删除并重建（推荐）", "kind": "primary"},
                    {"id": "keep", "label": "继续使用现有 venv", "kind": "normal"},
                    {"id": "cancel", "label": "取消部署", "kind": "danger"},
                ],
                cancel_event=self._deploy_cancel)
            if choice == "delete":
                log(f"[部署] 正在删除 {venv_dir} ...\n")
                shutil.rmtree(venv_dir, ignore_errors=True)
                log("[部署] 已删除，稍后会用正确的 Python 重新创建\n")
            elif choice == "keep":
                log("[部署] 用户选择继续使用现有 venv（可能仍会遇到版本相关问题）\n")
            else:
                raise _DeployCancelled()
        else:
            log(f"[部署] venv 检测: {detail}\n")

        if self._deploy_cancel.is_set():
            raise _DeployCancelled()

        # ---- 4. 运行一次 webui.bat，让 Forge 自己建 venv 装依赖 ----
        resolved_python = (self.cfg.get("custom_python_path") or "").strip()
        if not resolved_python:
            resolved_python = cm.detect_bundled_python(target) or ""
        if resolved_python:
            try:
                cm.sync_venv_pyvenv_cfg(target, resolved_python, lambda m: log(m + "\n"))
            except OSError as e:
                log(f"[部署] 检查 venv pyvenv.cfg 时出错（不影响继续部署）: {e}\n")

        env_overrides = cm.build_launch_env_overrides(self.cfg, target)
        log("\n[部署] 首次运行 webui.bat（自动创建虚拟环境并安装依赖，可能需要较长时间）\n")
        log(f"[部署] COMMANDLINE_ARGS = {env_overrides.get('COMMANDLINE_ARGS', '')}\n")
        for k, v in env_overrides.items():
            if k != "COMMANDLINE_ARGS":
                log(f"[部署] {k} = {v}\n")
        log("\n")

        env = dict(os.environ)
        env.update(env_overrides)
        # 注意：webui.bat 装完依赖会真的把 WebUI 服务起起来，然后一直占着
        # 进程不退出。部署的目标是把环境装到「能启动」，所以流式读输出、
        # 出现监听地址就判定成功并停掉这个临时进程继续收尾——否则部署会
        # 永远卡在这里（之前唯一的出路是点「取消」，而取消又被当成失败
        # 处理，配置写回/hashlib 补丁全部跳过）。
        if not self._deploy_run_webui_until_ready(target, log, env):
            if self._deploy_cancel.is_set():
                raise _DeployCancelled()
            log("\n[部署] 依赖安装或启动失败（上面的日志应有具体报错），"
                "排查修复后可以重新点击「开始部署」继续（已完成的步骤会被跳过）\n")
            return
        log("\n[部署] WebUI 已能正常启动（验证用临时进程已停止），继续收尾 ...\n")

        # ---- 5. hashlib 兼容补丁（保险步骤，新版 Python 下补丁自动不生效）----
        pe.write_hashlib_patch(venv_dir, log_cb=lambda m: log(m + "\n"))

        # ---- 6. 收尾：路径/分支写回配置，通知前端刷新 ----
        self.cfg["webui_root"] = target
        self.cfg["webui_branch"] = branch
        cm.save_config(self.cfg)
        log("\n[部署] 全部完成！\n")
        self._emit("deploy", "done", target=target, branch=branch)
    except _DeployCancelled:
        log("\n[部署] 已取消\n")
        self._emit("deploy", "cancelled")
    except pe.PortableEnvError as e:
        log(f"\n[部署] 便携环境下载失败: {e}\n")
        self._emit("deploy", "error", title="便携环境下载失败", text=str(e))
    except Exception:
        detail = traceback.format_exc()
        log(f"\n[部署] 发生未预期的错误:\n{detail}\n")
        self._emit("deploy", "error", title="部署失败", text=detail[-1500:])
    finally:
        self._deploy_running = False
        self._deploy_proc = None
        self._emit("deploy", "state", running=False)


def _deploy_portable_env(self, target, branch, need_python, need_git, log, progress):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        if need_python:
            version_prefix = pe.PYTHON_VERSION_BY_BRANCH.get(branch, "3.10")
            log(f"[便携环境] 查询 python-build-standalone 最新版本 (目标 Python {version_prefix}.x) ...\n")
            net_log = lambda m: log(m + "\n")
            tag = pe.get_latest_pbs_tag(cfg=self.cfg, log_cb=net_log)
            # 防篡改：先取 release 官方清单 SHA256SUMS 作为哈希基准，
            # 下载完必须校验通过才会解压/执行（加速代理是第三方中间人）。
            log("[便携环境] 获取官方校验清单 SHA256SUMS ...\n")
            sha_map = pe.fetch_pbs_sha256sums(tag, cfg=self.cfg, log_cb=net_log)
            assets = pe.list_release_assets(pe.PBS_REPO, tag, cfg=self.cfg, log_cb=net_log)
            asset = pe.pick_python_asset(assets, version_prefix)
            if not asset:
                raise pe.PortableEnvError(
                    f"没有找到匹配 Python {version_prefix}.x / Windows x86_64 的构建，可能需要手动安装")
            expected = sha_map.get(asset["name"])
            if not expected:
                raise pe.PortableEnvError(
                    f"官方 SHA256SUMS 中没有 {asset['name']} 的哈希记录，无法验证下载内容，已中止")
            # 交叉核对：GitHub API 的资产摘要应当与官方清单一致，不一致说明
            # 元数据链路也可能被代理篡改，直接中止
            api_digest = (asset.get("digest") or "")
            if api_digest.startswith("sha256:"):
                api_digest = api_digest[len("sha256:"):].lower()
                if api_digest != expected:
                    raise pe.PortableEnvError(
                        "GitHub API 摘要与官方 SHA256SUMS 不一致，元数据可能被篡改，已中止")
            log(f"[便携环境] 下载 {asset['name']} ...\n")
            archive_path = os.path.join(tmp, asset["name"])
            pe.download_file(asset["url"], archive_path,
                             progress_cb=progress,
                             cancel_flag=self._deploy_cancel.is_set,
                             cfg=self.cfg, log_cb=lambda m: log(m + "\n"))
            pe.verify_downloaded_file(archive_path, expected,
                                      what=f"Python 归档 {asset['name']}")
            log("[便携环境] 校验通过，开始解压\n")
            python_exe = pe.extract_python_tar(archive_path, target,
                                               log_cb=lambda m: log(m + "\n"))
            log(f"[便携环境] Python 部署完成: {python_exe}\n")
        if self._deploy_cancel.is_set():
            raise _DeployCancelled()
        if need_git:
            log("[便携环境] 查询 git-for-windows 最新版本 ...\n")
            assets, body = pe.list_release_assets_with_meta(
                pe.GIT_REPO, tag=None, cfg=self.cfg, log_cb=lambda m: log(m + "\n"))
            asset = pe.pick_git_asset(assets)
            if not asset:
                raise pe.PortableEnvError("没有找到 PortableGit 64位安装包")
            expected = pe.pick_git_asset_sha256(body, asset["name"])
            if not expected:
                raise pe.PortableEnvError(
                    f"git-for-windows 发布信息中没有 {asset['name']} 的校验和，无法验证下载内容，已中止")
            log(f"[便携环境] 下载 {asset['name']} ...\n")
            archive_path = os.path.join(tmp, asset["name"])
            pe.download_file(asset["url"], archive_path,
                             progress_cb=progress,
                             cancel_flag=self._deploy_cancel.is_set,
                             cfg=self.cfg, log_cb=lambda m: log(m + "\n"))
            pe.verify_downloaded_file(archive_path, expected,
                                      what=f"Git 安装包 {asset['name']}")
            # 自解压包执行前再验 Authenticode 签名（双保险）
            pe.verify_pe_signature(archive_path, log_cb=lambda m: log(m + "\n"))
            git_exe = pe.extract_portable_git(archive_path, target,
                                              log_cb=lambda m: log(m + "\n"))
            log(f"[便携环境] Git 部署完成: {git_exe}\n")


def _deploy_run_cmd(self, program, args, cwd, log, env=None, timeout=None):
    """跑一条部署命令，流式回显输出。

    timeout 以秒计；网络型命令（git fetch 这类）必须传超时——加速代理
    常见的死法是「接受 TCP 连接后永久不返回数据」，不设超时就会永远
    静默卡死，轮不到下一个候选地址。超时返回 -9，让调用方的候选轮换
    逻辑生效。返回进程退出码。
    """
    log(f"\n[部署] 执行: {program} {' '.join(args)}  (工作目录: {cwd})\n\n")
    try:
        self._deploy_proc = subprocess.Popen(
            [program] + list(args), cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
    except OSError as e:
        log(f"[部署] 无法启动进程: {e}\n")
        return 1
    proc = self._deploy_proc
    # 读线程 + 队列 + 主循环轮询，模式同 _deploy_run_webui_until_ready：
    # 主循环才能响应取消和超时，阻塞在 read1 里什么都轮不到
    out_q = queue.Queue()

    def _reader():
        try:
            while True:
                # read1()：见 _launch_reader 的注释，read() 会攒满 4096 字节
                # 才返回，git 长时间静默时日志卡住
                data = proc.stdout.read1(4096)
                if not data:
                    break
                out_q.put(data)
        except Exception:
            pass
        finally:
            out_q.put(None)  # EOF 哨兵

    threading.Thread(target=_reader, daemon=True, name="deploy-cmd-reader").start()
    start_ts = time.monotonic()
    last_out_ts = start_ts
    last_beat_ts = start_ts
    try:
        while True:
            if self._deploy_cancel.is_set():
                log("\n[部署] 用户取消，终止当前命令\n")
                kill_process_tree(proc.pid)
                return -9
            now = time.monotonic()
            if timeout and now - start_ts > timeout:
                el = int(now - start_ts)
                log(f"\n[部署] 该步骤超时（已运行 {el} 秒），终止进程并视作失败，"
                    "以便切换到下一个候选地址或提示用户\n")
                kill_process_tree(proc.pid)
                return -9
            if now - last_out_ts > 30 and now - last_beat_ts > 30:
                # 长时间没输出不一定是死了（大文件下载中），但不能让用户
                # 对着一动不动界面干等——发心跳说明还在跑
                last_beat_ts = now
                el = int(now - start_ts)
                log(f"[部署] 仍在执行 {os.path.basename(program)}，"
                    f"已运行 {el // 60} 分 {el % 60} 秒...\n")
            try:
                chunk = out_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                break  # 进程输出结束（进程已退出）
            last_out_ts = time.monotonic()
            log(cm.decode_process_output(chunk))
    except Exception:
        pass
    finally:
        if proc.poll() is None:
            kill_process_tree(proc.pid)
        self._deploy_proc = None
    return proc.wait()


def _deploy_run_webui_until_ready(self, target, log, env, timeout=5400):
    """
    首次运行 webui.bat：创建 venv、装依赖、把 WebUI 起起来验证能跑通。

    返回 True 表示成功（输出里出现了 "Running on local URL"，或端口探测
    发现 WebUI 已在监听——有些 webui.bat 会把输出重定向走，管道里一个字
    节都没有，靠解析输出判断就绪会永远卡住；此时停掉这个临时进程，部署
    继续收尾）。
    返回 False 表示失败（进程在就绪之前就退出了，或超过 timeout 秒的总
    时限——webui.bat 失败路径的 exit code 并不可靠，不能拿返回码当判断
    依据；总时限默认 5400 秒 = 90 分钟，torch 几个 GB 的慢网也要装得下）。

    部署期间需要用户做的决策（venv 版本不一致）已经在此之前处理完，
    这个方法只管跑和看。
    """
    program = "cmd.exe" if os.name == "nt" else "sh"
    args = ["/c", "webui.bat"] if os.name == "nt" else ["webui.sh"]
    log(f"\n[部署] 执行: {program} {' '.join(args)}  (工作目录: {target})\n\n")
    try:
        self._deploy_proc = subprocess.Popen(
            [program] + args, cwd=target, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
    except OSError as e:
        log(f"[部署] 无法启动进程: {e}\n")
        return False
    proc = self._deploy_proc
    # 单独起一个读线程：主循环用队列拿输出并轮询取消标记。
    # 不能直接在主循环里 read()——管道没输出时 read 会一直阻塞，
    # 「取消部署」按钮点了也没反应（要等下一次输出或进程退出）。
    out_q = queue.Queue()

    def _reader():
        try:
            while True:
                # 必须用 read1() 而不是 read()：BufferedReader.read(4096)
                # 会循环底层读取、攒满 4096 字节才返回，子进程输出停顿
                # 时（pip 静默下载阶段）会把整条管道卡死；read1() 单次
                # 底层读取，有字节就返回。
                data = proc.stdout.read1(4096)
                if not data:
                    break
                out_q.put(data)
        except Exception:
            pass
        finally:
            out_q.put(None)  # EOF 哨兵

    threading.Thread(target=_reader, daemon=True, name="deploy-webui-reader").start()
    # 端口兜底（跟 _launch_port_watcher 同一思路）：输出被重定向时靠
    # TCP 探测判定就绪。必须至少先见过一行输出再探，否则可能连上的
    # 是上一次没退干净的残留服务。探的配置端口，没配就探 7860-7869。
    cfg_port = str(self.cfg.get("port", "")).strip()
    probe_ports = [int(cfg_port)] if cfg_port.isdigit() else list(range(7860, 7870))
    tail = ""
    ready = False
    saw_output = False
    start_ts = time.monotonic()
    last_out_ts = start_ts
    last_beat_ts = start_ts
    last_probe_ts = start_ts
    try:
        while True:
            if self._deploy_cancel.is_set():
                raise _DeployCancelled()
            now = time.monotonic()
            if timeout and now - start_ts > timeout:
                log(f"\n[部署] 装依赖步骤超时（已运行 {int(now - start_ts)} 秒），"
                    "终止进程并按失败收尾；网络好转后可重新点击「开始部署」继续\n")
                break
            if now - last_out_ts > 30 and now - last_beat_ts > 30:
                # pip 静默下载阶段一个字都不吐，发心跳让用户知道没死
                last_beat_ts = now
                el = int(now - start_ts)
                log(f"[部署] 仍在执行（装依赖/启动 WebUI），"
                    f"已运行 {el // 60} 分 {el % 60} 秒...\n")
            if saw_output and now - last_probe_ts >= 2:
                last_probe_ts = now
                for p in probe_ports:
                    try:
                        with socket.create_connection(("127.0.0.1", p), timeout=0.5):
                            pass
                    except OSError:
                        continue
                    ready = True
                    log(f"\n[部署] 通过端口探测到 WebUI 已在监听 "
                        f"http://127.0.0.1:{p}，首次运行验证通过\n")
                    break
                if ready:
                    break
            try:
                chunk = out_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                break  # 进程输出结束（进程已退出）
            saw_output = True
            last_out_ts = time.monotonic()
            text = cm.decode_process_output(chunk)
            log(text)
            combined = tail + text
            tail = combined[-2000:]
            if _URL_RE.search(combined):
                ready = True
                log("\n[部署] 检测到 WebUI 监听地址，首次运行验证通过\n")
                break
    finally:
        # 不管是就绪、失败还是取消，这里都确保进程树被收干净：
        # 就绪时它是我们拉起来的临时进程；取消/失败时直接杀树取消
        if proc.poll() is None:
            kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=15)
            except Exception:
                log("[部署] 等待临时进程退出超时（15 秒），进程可能仍有残留，"
                    "如后续端口被占用请手动结束 python.exe\n")
        self._deploy_proc = None
    return ready


def _api_deploy_venv_check(self, target, branch):
    target = (target or "").strip()
    if not target or not os.path.isdir(target):
        return {"ok": False, "error": "请先在「安装目录」里填好要检测的 WebUI 根目录"}
    required = pe.PYTHON_VERSION_BY_BRANCH.get(branch, "3.10")
    venv_dir = os.path.join(target, "venv")
    mismatch, detail = pe.check_venv_version_mismatch(venv_dir, required)
    return {"ok": True, "mismatch": mismatch, "detail": detail}


def _api_deploy_venv_delete(self, target):
    venv_dir = os.path.join((target or "").strip(), "venv")
    shutil.rmtree(venv_dir, ignore_errors=True)
    return {"ok": True}


def _api_deploy_patch_hashlib(self, target):
    target = (target or "").strip()
    if not target or not os.path.isdir(target):
        return {"ok": False, "error": "请先在「安装目录」里填好要处理的 WebUI 根目录"}
    venv_dir = os.path.join(target, "venv")
    if not os.path.isdir(venv_dir):
        return {"ok": False, "error": f"该目录下没有找到 venv 文件夹：\n{venv_dir}"}
    msgs = []
    ok = pe.write_hashlib_patch(venv_dir, log_cb=msgs.append)
    if ok:
        return {"ok": True, "message": (msgs[-1] if msgs else "") + "\n已写入 hashlib 兼容性补丁，重新启动 WebUI 试试"}
    return {"ok": False, "error": "没能找到 venv 的 site-packages 目录，可能不是标准 venv 结构"}


# ============================================================
# Civitai 模型下载
# ============================================================

def _api_civitai_fetch(self, text):
    def work():
        log = lambda t: self._emit("civitai", "log", text=t + "\n")
        try:
            log(f"正在获取: {text}")
            # liblib 链接优先识别（ Civitai 的 parse_input 不认识它）
            if "liblib" in (text or "").lower():
                self._fetch_liblib(text, log)
                return
            parsed = cd.parse_input(text)
            api_key = (self.cfg.get("civitai_api_key") or "").strip() or None
            info = cd.fetch_version_info(parsed, api_key)
            self._civitai_version_info = info
            self._download_source = "civitai"
            folder = cd.guess_folder(info["model_type"])
            root = (self.cfg.get("webui_root") or "").strip()
            dest = os.path.join(root, folder.replace("/", os.sep)) if root else folder
            subset = {
                "source": "civitai",
                "model_name": info["model_name"],
                "model_type": info["model_type"],
                "version_name": info["version_name"],
                "base_model": info.get("base_model", ""),
                "page_url": f"https://civitai.com/models/{info['model_id']}"
                            f"?modelVersionId={info['version_id']}",
                "files": [{"name": f["name"], "sizeKB": f.get("sizeKB") or 0,
                           "primary": bool(f.get("primary"))} for f in info["files"]],
            }
            self._emit("civitai", "info", ok=True, info=subset, folder=folder, dest=dest)
        except (cd.CivitaiError, lc.LiblibError) as e:
            self._emit("civitai", "info", ok=False, error=str(e))
        except Exception as e:
            self._emit("civitai", "info", ok=False, error=f"发生未知错误: {e}")
    self._spawn(work, name="civitai-fetch")
    return {"ok": True}


def _fetch_liblib(self, text, log):
    """模型下载页的 liblib 分支：拿完整信息（含每个版本的附件）。"""
    parsed = lc.parse_url(text)
    info = lc.fetch_download_info(parsed["model_uuid"], parsed.get("version_uuid"))
    self._liblib_download_info = info
    self._download_source = "liblib"
    versions = info["versions"]
    chosen = versions[info["chosen"]]
    folder, type_label = lc.guess_folder_by_size(chosen["file_size"],
                                                 chosen["file_name"])
    root = (self.cfg.get("webui_root") or "").strip()
    dest = os.path.join(root, folder.replace("/", os.sep)) if root else folder
    subset = {
        "source": "liblib",
        "model_name": info["model_name"],
        "model_type": type_label,
        "version_name": chosen["version_name"],
        "base_model": chosen["base_model"],
        "page_url": info["page_url"],
        "trigger_words": chosen["trigger_words"],
        "vip_used": chosen["vip_used"],
        "exclusive": chosen["exclusive"],
        # liblib 一个版本一个文件，把「选文件」变成「选版本」
        "files": [{
            "name": v["file_name"] or "（该版本未提供下载地址）",
            "version_name": v["version_name"],
            "sizeKB": (v["file_size"] or 0) // 1024,
            "primary": i == info["chosen"],
            "unavailable": not v["download_url"],
        } for i, v in enumerate(versions)],
    }
    token_ok = bool((self.cfg.get("liblib_token") or "").strip())
    log(f"来源: liblib | 模型: {info['model_name']} | 版本数: {len(versions)}"
        + ("" if token_ok else " | 未填 usertoken，下载前请先配置"))
    self._emit("civitai", "info", ok=True, info=subset, folder=folder, dest=dest)


def _api_civitai_download(self, file_index, dest_dir):
    if self._download_source == "liblib" and self._liblib_download_info:
        return self._liblib_download_start(file_index, dest_dir)
    info = self._civitai_version_info
    if not info:
        return {"ok": False, "error": "请先获取模型信息"}
    if not (0 <= int(file_index) < len(info["files"])):
        return {"ok": False, "error": "文件选择无效"}
    dest_dir = (dest_dir or "").strip()
    if not dest_dir:
        return {"ok": False, "error": "请先设置保存文件夹"}
    file_info = info["files"][int(file_index)]
    api_key = (self.cfg.get("civitai_api_key") or "").strip() or None

    def work():
        log = lambda t: self._emit("civitai", "log", text=t + "\n")
        try:
            log(f"开始下载: {file_info['name']} -> {dest_dir}")
            final_path, hash_ok = cd.download_file(
                file_info, dest_dir, api_key,
                progress_cb=lambda d, t: self._emit("civitai", "progress", downloaded=d, total=t))
            cd.save_sidecar_metadata(final_path, info, file_info)
            cd.download_preview_image(final_path, info, api_key)
            log(f"下载完成: {final_path}")
            self._emit("civitai", "done", ok=True, path=final_path,
                       hash_ok=hash_ok if hash_ok is not None else None)
        except cd.CivitaiError as e:
            log(f"错误: {e}")
            self._emit("civitai", "done", ok=False, error=str(e))
        except Exception as e:
            log(f"错误: {e}")
            self._emit("civitai", "done", ok=False, error=f"下载失败: {e}")
    self._spawn(work, name="civitai-download")
    return {"ok": True}


def _liblib_download_start(self, file_index, dest_dir):
    """模型下载页的 liblib 下载分支（复用 civitai_downloader 的断点续传/校验）。"""
    info = self._liblib_download_info
    versions = info["versions"]
    if not (0 <= int(file_index) < len(versions)):
        return {"ok": False, "error": "版本选择无效"}
    dest_dir = (dest_dir or "").strip()
    if not dest_dir:
        return {"ok": False, "error": "请先设置保存文件夹"}
    ver = versions[int(file_index)]
    if not ver["download_url"]:
        return {"ok": False, "error":
                "该版本未提供直接下载地址（会员/独家模型常见），"
                "请点信息卡里的「打开模型页面」在浏览器里登录后下载"}
    token = (self.cfg.get("liblib_token") or "").strip()
    if not token:
        return {"ok": False, "error":
                "liblib 的下载接口强制登录（匿名一律被拒）。请先在下方"
                "「liblib usertoken」卡片里填入登录凭证，再重新点下载"}
    file_info = {
        "downloadUrl": ver["download_url"],
        "name": ver["file_name"] or f"{info['model_name']}.safetensors",
        "sizeKB": (ver["file_size"] or 0) // 1024,
        "hashes": {"SHA256": ver["sha256"]} if ver["sha256"] else {},
    }

    def work():
        log = lambda t: self._emit("civitai", "log", text=t + "\n")
        try:
            log(f"开始下载: {file_info['name']} -> {dest_dir}（来源: liblib）")
            final_path, hash_ok = cd.download_file(
                file_info, dest_dir,
                progress_cb=lambda d, t: self._emit("civitai", "progress",
                                                    downloaded=d, total=t),
                extra_headers=lc.download_headers(token))
            if hash_ok is not False:
                # 校验通过（或网站没给哈希）才写元数据；损坏的 .broken 不写，
                # 免得模型管理器把一个坏文件当成已登记模型
                lb = {
                    "model_uuid": info["model_uuid"],
                    "version_uuid": ver["version_uuid"],
                    "model_name": info["model_name"],
                    "version_name": ver["version_name"],
                    "base_model": ver["base_model"],
                    "trigger_words": ver["trigger_words"],
                    "page_url": lc.page_url(info["model_uuid"], ver["version_uuid"]),
                }
                merge_sidecar_from_liblib(final_path, lb, digest=ver["sha256"] or None)
                log(f"已写入模型信息（含触发词）: {os.path.basename(final_path)}.civitai.info")
            log(f"下载完成: {final_path}")
            self._emit("civitai", "done", ok=True, path=final_path,
                       hash_ok=hash_ok if hash_ok is not None else None)
        except cd.CivitaiError as e:
            msg = str(e)
            # liblib 端的典型报错翻译成可操作的指引
            if "未登录" in msg or "登录" in msg:
                msg = ("liblib 拒绝了下载（" + msg + "）。请检查「liblib usertoken」"
                       "是否填了、是否过期（浏览器里重新复制一次），然后重试")
            log(f"错误: {msg}")
            self._emit("civitai", "done", ok=False, error=msg)
        except Exception as e:
            log(f"错误: {e}")
            self._emit("civitai", "done", ok=False, error=f"下载失败: {e}")
    self._spawn(work, name="liblib-download")
    return {"ok": True}


def _api_settings_verify_liblib_token(self, token):
    """验证并保存 liblib usertoken（token 为空 = 清除）。"""
    token = (token or "").strip()
    if not token:
        self.cfg["liblib_token"] = ""
        cm.save_config(self.cfg)
        return {"ok": True, "nickname": ""}
    try:
        nickname = lc.fetch_user_info(token)
    except lc.LiblibError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"验证请求失败: {e}"}
    self.cfg["liblib_token"] = token
    cm.save_config(self.cfg)
    return {"ok": True, "nickname": nickname}


# ============================================================
# 模型管理
# ============================================================

def load_hash_cache():
    if os.path.exists(HASH_CACHE_PATH):
        try:
            with open(HASH_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_hash_cache(cache):
    try:
        with open(HASH_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


def _hash_cache_get(self, path):
    """
    读取单个缓存条目。必须在锁内整个「读 json」——单个查询和批量查询可能
    同时跑，两者都是“读整个 json → 改 → 写整个 json”，不加锁的话后写的
    会把前者的结果整个抹掉。
    """
    with self._threads_lock:
        return load_hash_cache().get(path)


def _hash_cache_set(self, path, entry):
    """同上：锁内完成「读 → 改 → 写」整个读改写周期"""
    with self._threads_lock:
        cache = load_hash_cache()
        cache[path] = entry
        save_hash_cache(cache)


def cache_entry_valid(entry, stat):
    return (isinstance(entry, dict)
            and entry.get("size") == stat.st_size
            and int(entry.get("mtime", -1)) == int(stat.st_mtime))


def read_sidecar_base_model(model_path):
    info_path = os.path.splitext(model_path)[0] + ".civitai.info"
    if not os.path.exists(info_path):
        return ""
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            return str(json.load(f).get("baseModel") or "")
    except Exception:
        return ""


def _api_headers(api_key):
    headers = {"User-Agent": "ForgeLauncher/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def compute_sha256(path, progress_cb=None, cancel_flag=None):
    import hashlib
    sha = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel_flag and cancel_flag():
                raise InterruptedError("已取消")
            chunk = f.read(1024 * 1024 * 4)
            if not chunk:
                break
            sha.update(chunk)
            done += len(chunk)
            if progress_cb and total:
                progress_cb(int(done / total * 100))
    return sha.hexdigest()


def query_by_hash(digest, api_key, retry_on_429=True):
    """返回 (json_data or None, status_code)。404 表示 Civitai 无记录。"""
    import requests
    resp = requests.get(
        f"https://civitai.com/api/v1/model-versions/by-hash/{digest}",
        headers=_api_headers(api_key), timeout=30)
    if resp.status_code == 429 and retry_on_429:
        time.sleep(30)
        return query_by_hash(digest, api_key, retry_on_429=False)
    if resp.status_code == 404:
        return None, 404
    resp.raise_for_status()
    return resp.json(), resp.status_code


def read_sidecar(model_path):
    """读取 .civitai.info（没有或损坏都返回空 dict）"""
    info_path = os.path.splitext(model_path)[0] + ".civitai.info"
    if not os.path.exists(info_path):
        return {}
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_sidecar(model_path, info):
    info_path = os.path.splitext(model_path)[0] + ".civitai.info"
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)


def _merge_trained_words(existing_info, new_words, source):
    """
    触发词合并策略：手动输入的永远不被站点数据覆盖；
    站点抓的可以被另一个站点更新（内容同源，新的更全）。
    """
    if not new_words:
        return
    if existing_info.get("trainedWordsSource") == "manual":
        return
    old = existing_info.get("trainedWords") or []
    if set(map(str, old)) != set(map(str, new_words)):
        existing_info["trainedWords"] = list(new_words)
        existing_info["trainedWordsSource"] = source


def write_sidecar_from_version(model_path, data, digest, api_key=None, fetch_preview=True):
    """按查询结果写 .civitai.info（格式与 civitai_downloader 一致），顺带补预览图。
    已有触发词是手动输入的则保留（站点数据不覆盖手动）。"""
    import requests
    model = data.get("model", {}) or {}
    info = {
        "modelId": data.get("modelId"),
        "modelName": model.get("name", ""),
        "modelType": model.get("type", ""),
        "versionId": data.get("id"),
        "versionName": data.get("name", ""),
        "baseModel": data.get("baseModel", ""),
        "fileName": os.path.basename(model_path),
        "hashes": {"SHA256": digest},
    }
    old = read_sidecar(model_path)
    _merge_trained_words(info, old.get("trainedWords") if old.get("trainedWordsSource") == "manual" else None, "manual")
    _merge_trained_words(info, data.get("trainedWords") or [], "civitai")
    # 保留已有的 liblib 绑定（同一个模型两边站都可能有页面）
    for k in ("liblibUuid", "liblibVersionUuid", "liblibPage"):
        if old.get(k):
            info[k] = old[k]
    base, _ext = os.path.splitext(model_path)
    write_sidecar(model_path, info)

    if fetch_preview and not any(os.path.exists(base + s) for s in
                                 (".preview.png", ".png", ".jpg", ".jpeg", ".webp")):
        for img in (data.get("images") or [])[:1]:
            url = img.get("url")
            if not url:
                continue
            try:
                resp = requests.get(url, headers=_api_headers(api_key), timeout=30)
                resp.raise_for_status()
                with open(base + ".preview.png", "wb") as f:
                    f.write(resp.content)
            except Exception:
                pass
    return info


def merge_sidecar_from_liblib(model_path, lb, digest=None):
    """把 liblib 查询结果合进 .civitai.info（不存在则新建）。返回合并后的 info。"""
    info = read_sidecar(model_path)
    info.setdefault("fileName", os.path.basename(model_path))
    if digest and not (info.get("hashes") or {}).get("SHA256"):
        info.setdefault("hashes", {})["SHA256"] = digest
    if lb.get("model_name") and not info.get("modelName"):
        info["modelName"] = lb["model_name"]
    if lb.get("version_name") and not info.get("versionName"):
        info["versionName"] = lb["version_name"]
    if lb.get("base_model") and not info.get("baseModel"):
        info["baseModel"] = lb["base_model"]
    if lb.get("model_uuid"):
        info["liblibUuid"] = lb["model_uuid"]
    if lb.get("version_uuid"):
        info["liblibVersionUuid"] = lb["version_uuid"]
    if lb.get("page_url"):
        info["liblibPage"] = lb["page_url"]
    _merge_trained_words(info, lb.get("trigger_words") or [], "liblib")
    write_sidecar(model_path, info)
    return info


def _api_models_categories(self):
    root = (self.cfg.get("webui_root") or "").strip()
    cats = []
    if root and os.path.isdir(root):
        for label, rel, is_lora in BASE_CATEGORIES:
            path = os.path.join(root, rel.replace("/", os.sep))
            if os.path.isdir(path):
                cats.append({"label": label, "path": path, "is_lora": is_lora})
        models_dir = os.path.join(root, "models")
        if os.path.isdir(models_dir):
            for name in sorted(os.listdir(models_dir)):
                sub = os.path.join(models_dir, name)
                if not os.path.isdir(sub) or name.lower() in _SKIP_AUTO_DIRS:
                    continue
                if any(os.path.normpath(sub) == os.path.normpath(c["path"]) for c in cats):
                    continue
                try:
                    has_model = any(f.lower().endswith(MODEL_EXTS) for f in os.listdir(sub)
                                    if os.path.isfile(os.path.join(sub, f)))
                except OSError:
                    has_model = False
                if has_model:
                    cats.append({"label": f"其他: {name}", "path": sub,
                                 "is_lora": "lora" in name.lower()})
    self._models_categories = cats
    return {"ok": True, "root": root, "categories":
            [{"label": c["label"], "is_lora": c["is_lora"]} for c in cats]}


def _api_models_list(self, cat_index):
    try:
        cat = self._models_categories[int(cat_index)]
    except (IndexError, ValueError):
        return {"ok": False, "error": "类别无效", "files": []}
    files = []
    try:
        for dirpath, dirnames, filenames in os.walk(cat["path"]):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if not fn.lower().endswith(MODEL_EXTS):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                    size, size_text = st.st_size, _fmt_size(st.st_size)
                    mtime_text = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    size, size_text, mtime_text = 0, "?", "?"
                files.append({
                    "path": full,
                    "rel": os.path.relpath(full, cat["path"]),
                    "base": read_sidecar_base_model(full),
                    "size": size, "size_text": size_text, "mtime_text": mtime_text,
                })
    except OSError as e:
        return {"ok": False, "error": str(e), "files": []}
    files.sort(key=lambda x: x["rel"].lower())
    no_info = sum(1 for f in files if not f["base"])
    return {"ok": True, "is_lora": cat["is_lora"], "files": files,
            "total": len(files), "no_info": no_info}


def _api_models_detail(self, path):
    out = {"ok": True, "path": path, "preview": None, "file": None,
           "civitai": None, "safetensors": None}
    try:
        st = os.stat(path)
        out["file"] = {
            "name": os.path.basename(path),
            "size_text": _fmt_size(st.st_size),
            "mtime_text": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
    except OSError:
        pass

    base, _ext = os.path.splitext(path)
    for cand in (base + ".preview.png", base + ".png", base + ".jpg",
                 base + ".jpeg", base + ".webp"):
        if os.path.exists(cand):
            url = _image_data_url(cand)
            if url:
                out["preview"] = url
                break

    info_path = base + ".civitai.info"
    if os.path.exists(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            sha = (info.get("hashes") or {}).get("SHA256") or ""
            words = info.get("trainedWords") or []
            if isinstance(words, str):
                words = [words]
            out["civitai"] = {
                "modelName": info.get("modelName", ""),
                "modelType": info.get("modelType", ""),
                "versionName": info.get("versionName", ""),
                "baseModel": info.get("baseModel", ""),
                "modelId": info.get("modelId"),
                "versionId": info.get("versionId"),
                "sha256_short": (sha[:16] + "...") if sha else "",
                "trainedWords": [str(w) for w in words if str(w).strip()],
                "trainedWordsSource": info.get("trainedWordsSource") or "",
                "liblibUuid": info.get("liblibUuid") or "",
                "liblibVersionUuid": info.get("liblibVersionUuid") or "",
            }
        except Exception:
            out["civitai"] = {"parse_error": True}

    if path.lower().endswith(".safetensors"):
        header = sm.read_safetensors_header(path)
        if header:
            meta, keys = header
            kind, arch, note = sm.guess_architecture(meta, keys)
            tags = sm.top_trained_tags(meta)
            out["safetensors"] = {
                "kind": kind, "arch": arch, "note": note,
                "train_rows": [[l, v] for l, v in sm.summarize_training_meta(meta)],
                "tags": [t for t, _c in tags],
            }
        else:
            out["safetensors"] = {"unreadable": True}
    return out


def _api_models_hash_lookup(self, path):
    if self._hash_thread and self._hash_thread.is_alive():
        return {"ok": False, "error": "已有查询在进行中"}
    api_key = (self.cfg.get("civitai_api_key") or "").strip() or None

    def work():
        status = lambda t: self._emit("models", "lookup_status", text=t)
        try:
            st = os.stat(path)
            entry = _hash_cache_get(self, path)
            digest = entry.get("sha256") if (entry and cache_entry_valid(entry, st)) else None
            if digest:
                status(f"使用缓存的哈希: {digest[:12]}...")
            else:
                status("正在计算 SHA256（大模型文件会需要一会儿）...")
                digest = compute_sha256(
                    path, progress_cb=lambda p: self._emit("models", "lookup_progress", pct=p))
            status(f"哈希: {digest[:12]}... 正在查询 Civitai ...")
            data, status_code = query_by_hash(digest, api_key)
            if status_code == 404:
                # Civitai 查不到，再试试 liblib（国内模型很多只发在 liblib）
                status("Civitai 无记录，正在查询 liblib ...")
                lb = None
                try:
                    import liblib_client as lc
                    lb = lc.query_by_hash(digest)
                except Exception:
                    lb = None
                _hash_cache_set(self, path, {"size": st.st_size, "mtime": int(st.st_mtime),
                                             "sha256": digest, "found": bool(lb)})
                if not lb:
                    self._emit("models", "lookup_done", ok=False, not_found=True, digest=digest)
                    return
                info = merge_sidecar_from_liblib(path, lb, digest)
                self._emit("models", "lookup_done", ok=True, digest=digest, source="liblib", info={
                    "modelName": info.get("modelName", ""),
                    "baseModel": info.get("baseModel", ""),
                })
                return
            _hash_cache_set(self, path, {"size": st.st_size, "mtime": int(st.st_mtime),
                                         "sha256": digest, "found": True})
            info = write_sidecar_from_version(path, data, digest, api_key)
            self._emit("models", "lookup_done", ok=True, digest=digest, source="civitai", info={
                "modelName": info.get("modelName", ""),
                "baseModel": info.get("baseModel", ""),
            })
        except InterruptedError:
            self._emit("models", "lookup_done", ok=False, error="已取消")
        except Exception as e:
            import requests  # noqa: F401  (仅用于异常类型判断的可读性)
            self._emit("models", "lookup_done", ok=False, error=f"{type(e).__name__}: {e}")

    self._hash_thread = self._spawn(work, name="hash-lookup")
    return {"ok": True}


def _api_models_batch(self, cat_index):
    if self._batch_thread and self._batch_thread.is_alive():
        return {"ok": False, "error": "已有批量查询在进行中"}
    try:
        cat = self._models_categories[int(cat_index)]
    except (IndexError, ValueError):
        return {"ok": False, "error": "类别无效"}
    files = _api_models_list(self, cat_index).get("files", [])
    targets = [f["path"] for f in files
               if not f["base"] and f["path"].lower().endswith(".safetensors")
               and not os.path.exists(os.path.splitext(f["path"])[0] + ".civitai.info")]
    if not targets:
        return {"ok": False, "error": "当前类别所有模型都已有 Civitai 信息", "no_targets": True}
    api_key = (self.cfg.get("civitai_api_key") or "").strip() or None
    self._batch_cancel = False

    def work():
        import requests
        found = notfound = failed = 0
        total = len(targets)
        for i, path in enumerate(targets):
            if self._batch_cancel:
                break
            name = os.path.basename(path)
            self._emit("models", "batch_progress", i=i, total=total, name=name)
            try:
                st = os.stat(path)
                entry = _hash_cache_get(self, path)  # 锁内读，和单个查询互不覆盖
                digest = None
                if entry and cache_entry_valid(entry, st):
                    if entry.get("found") is False:
                        notfound += 1
                        self._emit("models", "batch_item", name=name, msg="跳过（之前已确认无记录）")
                        continue
                    digest = entry.get("sha256")
                if not digest:
                    digest = compute_sha256(path, cancel_flag=lambda: self._batch_cancel)
                data, status_code = query_by_hash(digest, api_key)
                if status_code == 404:
                    # Civitai 查不到，再试 liblib
                    lb = None
                    try:
                        import liblib_client as lc
                        lb = lc.query_by_hash(digest)
                    except Exception:
                        lb = None
                    _hash_cache_set(self, path, {"size": st.st_size, "mtime": int(st.st_mtime),
                                                 "sha256": digest, "found": bool(lb)})
                    if lb:
                        info = merge_sidecar_from_liblib(path, lb, digest)
                        found += 1
                        self._emit("models", "batch_item", name=name,
                                   msg=f"✓ [liblib] {info['modelName']}  [{info['baseModel'] or '未标注底模'}]")
                    else:
                        notfound += 1
                        self._emit("models", "batch_item", name=name, msg="Civitai / liblib 均无记录")
                else:
                    _hash_cache_set(self, path, {"size": st.st_size, "mtime": int(st.st_mtime),
                                                 "sha256": digest, "found": True})
                    info = write_sidecar_from_version(path, data, digest, api_key)
                    found += 1
                    self._emit("models", "batch_item", name=name,
                               msg=f"✓ {info['modelName']}  [{info['baseModel'] or '未标注底模'}]")
                time.sleep(0.6)  # 别把 Civitai API 打限流
            except InterruptedError:
                break
            except requests.exceptions.RequestException as e:
                failed += 1
                self._emit("models", "batch_item", name=name, msg=f"网络失败: {e}")
                time.sleep(2)
            except Exception as e:
                failed += 1
                self._emit("models", "batch_item", name=name, msg=f"失败: {type(e).__name__}: {e}")
        self._emit("models", "batch_done", found=found, notfound=notfound,
                   failed=failed, cancelled=self._batch_cancel)

    self._batch_thread = self._spawn(work, name="batch-lookup")
    return {"ok": True, "total": len(targets)}


def _api_models_batch_cancel(self):
    self._batch_cancel = True
    return {"ok": True}


def _api_models_delete(self, path):
    base, _ext = os.path.splitext(path)
    sidecars = [p for p in (base + ".civitai.info", base + ".preview.png",
                            base + ".png", base + ".json")
                if os.path.exists(p) and p != path]
    deleted = []
    try:
        os.remove(path)
        deleted.append(path)
        for p in sidecars:
            try:
                os.remove(p)
                deleted.append(p)
            except OSError:
                pass
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "deleted": deleted}


def _api_models_set_trained_words(self, path, words):
    """手动设置触发词（多组，每组一个字符串）。手动输入的优先级最高，
    之后站点查询不会覆盖（见 _merge_trained_words）。"""
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "模型文件不存在"}
    if not isinstance(words, (list, tuple)):
        return {"ok": False, "error": "触发词格式无效"}
    cleaned = []
    for w in words:
        s = str(w or "").strip()
        if s and s not in cleaned:
            cleaned.append(s)
    info = read_sidecar(path)
    info.setdefault("fileName", os.path.basename(path))
    info["trainedWords"] = cleaned
    info["trainedWordsSource"] = "manual"
    try:
        write_sidecar(path, info)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "count": len(cleaned)}


def _api_models_bind_liblib(self, path, url):
    """粘贴 liblib 模型链接 → 拉取模型信息合进 sidecar（触发词/底模/跳转链接）"""
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "模型文件不存在"}
    try:
        import liblib_client as lc
        parsed = lc.parse_url(url)
        lb = lc.fetch_model(parsed["model_uuid"], parsed.get("version_uuid"))
    except lc.LiblibError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"访问 liblib 失败: {e}"}
    try:
        info = merge_sidecar_from_liblib(path, lb)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "modelName": info.get("modelName", ""),
            "words": len(info.get("trainedWords") or [])}


def _read_webui_config(root):
    """
    读 WebUI 自己的 config.json。出图目录是可以在设置里改的，
    写死路径必然有人对不上 —— 官方默认是 outputs（复数），但不少整合包
    和改过设置的用户那里是 output（单数）或者干脆指到别的盘。
    """
    for name in ("config.json", "ui-config.json"):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            continue
    return {}


def _resolve_output_dirs(root, branch=""):
    """
    算出实际的出图目录。

    优先级：
      1. config.json 里的 outdir_samples（设了就是总开关，txt2img/img2img 不再分开）
      2. config.json 里的 outdir_txt2img_samples / outdir_img2img_samples
      3. 都没有就探测 outputs/ 和 output/，哪个真实存在用哪个
      4. 还是没有就退回官方默认 outputs

    返回 {"txt2img": 绝对路径, "img2img": ..., "root": ...,
          "date_subdir": 是否按日期分子目录}
    """
    cfg = _read_webui_config(root)

    def abspath(v):
        v = str(v or "").strip()
        if not v:
            return ""
        return v if os.path.isabs(v) else os.path.normpath(os.path.join(root, v))

    shared = abspath(cfg.get("outdir_samples"))
    t2i = shared or abspath(cfg.get("outdir_txt2img_samples"))
    i2i = shared or abspath(cfg.get("outdir_img2img_samples"))

    # config 里没写就自己找。两种拼写都试，真实存在的优先。
    # 顺序按分支排：Forge Neo 把出图目录从 outputs 改成了 output（单数），
    # A1111 / Forge Classic 仍然是复数。两个目录同时存在时（从 Classic 升到
    # Neo 的人常见），按分支挑对应的那个，不然会打开一个不再更新的旧目录。
    is_neo = str(branch or "").strip().lower() in ("neo", "neo2")
    order = ("output", "outputs") if is_neo else ("outputs", "output")
    base = ""
    for cand in order:
        d = os.path.join(root, cand)
        if os.path.isdir(d):
            base = d
            break
    if not base:
        base = os.path.join(root, order[0])   # 都不存在时按分支给默认值

    if not t2i:
        t2i = os.path.join(base, "txt2img-images")
    if not i2i:
        i2i = os.path.join(base, "img2img-images")

    # 日期子目录只有开了 save_to_dirs 才有。默认模式是 [date]，
    # 用户改成别的（比如 [prompt_words]）就没法预测了，这时不提供"当天"入口
    save_to_dirs = cfg.get("save_to_dirs", True)
    pattern = str(cfg.get("directories_filename_pattern", "[date]")).strip()
    date_subdir = bool(save_to_dirs) and pattern in ("[date]", "")

    # root 取两个出图目录的共同上级，取不到就用 base
    try:
        common = os.path.commonpath([t2i, i2i]) if t2i and i2i else base
    except ValueError:      # 不同盘符
        common = base
    if not os.path.isdir(common):
        common = base

    return {"root": common, "txt2img": t2i, "img2img": i2i, "date_subdir": date_subdir}


def _api_output_dirs_info(self):
    """给前端用：告诉它实际路径是什么、要不要显示「当天」那两个按钮。"""
    root = (self.cfg.get("webui_root") or "").strip()
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "请先设置正确的 WebUI 根目录"}
    d = _resolve_output_dirs(root, self.cfg.get("webui_branch", ""))
    return {"ok": True, "root": d["root"], "txt2img": d["txt2img"],
            "img2img": d["img2img"], "date_subdir": d["date_subdir"],
            "exists": {k: os.path.isdir(d[k]) for k in ("root", "txt2img", "img2img")}}


def _api_open_output_folder(self, which):
    """打开出图目录：root / txt2img / txt2img_today / img2img / img2img_today"""
    root = (self.cfg.get("webui_root") or "").strip()
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "请先设置正确的 WebUI 根目录"}

    dirs = _resolve_output_dirs(root, self.cfg.get("webui_branch", ""))
    which = str(which)
    base_key = which.replace("_today", "")
    target = dirs.get(base_key if base_key in dirs else "root")
    if not target:
        return {"ok": False, "error": "未知的目录类型"}

    if which.endswith("_today"):
        if dirs["date_subdir"]:
            target = os.path.join(target, datetime.now().strftime("%Y-%m-%d"))
            if not os.path.isdir(target):
                # 今天还没跑过图：建出来再打开，省得用户自己翻
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as e:
                    return {"ok": False, "error": f"无法创建目录: {e}"}
        # 没开按日期分目录的话，"当天"就等于出图目录本身，直接打开父目录

    if not os.path.isdir(target):
        return {"ok": False,
                "error": f"目录不存在：{target}\n\n"
                         "这个路径是按 WebUI 的 config.json 算出来的。"
                         "如果你改过出图目录设置，或者还没跑出过图，就会是这样。"}
    try:
        if os.name == "nt":
            os.startfile(target)  # noqa: S606  # 用户本机目录，非外部输入
        else:
            subprocess.Popen(["xdg-open", target])
    except OSError as e:
        return {"ok": False, "error": f"无法打开目录: {e}"}
    return {"ok": True, "path": target}


# ============================================================
# 常用插件
# ============================================================

def _api_ext_list(self):
    root = (self.cfg.get("webui_root") or "").strip()
    ext_dir = os.path.join(root, "extensions") if root else None
    branch = self.cfg.get("webui_branch", "classic")
    items = []
    for name, desc, url_raw, folder_raw in EXTENSION_CATALOG:
        folder = _resolve_by_branch(folder_raw, branch)
        if not folder or not _resolve_by_branch(url_raw, branch):
            continue  # 该分支不可用的扩展直接不显示
        installed = bool(ext_dir) and os.path.isdir(os.path.join(ext_dir, folder))
        items.append({"name": name, "desc": desc, "installed": installed})
    return {"ok": True, "items": items, "has_root": bool(root and os.path.isdir(root))}


def _api_ext_install(self, names):
    if self._ext_thread and self._ext_thread.is_alive():
        return {"ok": False, "error": "已有安装任务在进行中"}
    root = (self.cfg.get("webui_root") or "").strip()
    if not root:
        return {"ok": False, "error": "请先在「一键启动」页设置好 WebUI 根目录"}
    ext_dir = os.path.join(root, "extensions")
    branch = self.cfg.get("webui_branch", "classic")

    # git 选取顺序与部署/启动保持一致：自定义路径 > 便携版 > 系统 PATH
    #（用户手动指定的 git 优先——便携版可能损坏，正是用户绕开它的原因）
    custom_git = (self.cfg.get("custom_git_path") or "").strip().strip('"')
    bundled_git = os.path.join(root, "git", "cmd", "git.exe")
    if custom_git and os.path.exists(custom_git):
        git_exe = custom_git
    elif os.path.exists(bundled_git):
        git_exe = bundled_git
    elif shutil.which("git"):
        git_exe = "git"
    else:
        return {"ok": False, "error": "未检测到 Git。可以先在「环境部署」页部署便携环境，"
                                      "或手动安装：https://git-scm.com/download/win"}

    selected = []
    for name, desc, url_raw, folder_raw in EXTENSION_CATALOG:
        if name not in (names or []):
            continue
        folder = _resolve_by_branch(folder_raw, branch)
        url = _resolve_by_branch(url_raw, branch)
        if not folder or not url:
            continue  # 该分支不可用
        if os.path.isdir(os.path.join(ext_dir, folder)):
            continue  # 已安装的自动跳过
        selected.append((name, url, folder))
    if not selected:
        return {"ok": False, "error": "请先勾选要安装的扩展（已安装的会自动跳过）"}

    self._ext_cancel = False

    def work():
        log = lambda t: self._emit("ext", "log", text=t)
        try:
            os.makedirs(ext_dir, exist_ok=True)
            use_mirror = False
            try:
                import mirror_manager as mm
                use_mirror = mm.resolve_github_mode(self.cfg, lambda m: log(m + "\n"))
            except Exception:
                pass
            for name, url, folder in selected:
                if self._ext_cancel:
                    break
                u = url
                if use_mirror:
                    try:
                        import mirror_manager as mm
                        mirrored = mm.github_url(url, True, 0)
                        if mirrored != url:
                            u = mirrored
                    except Exception:
                        pass
                target = os.path.join(ext_dir, folder)
                log(f"\n[插件] 正在安装: {name}\n")
                try:
                    self._ext_proc = subprocess.Popen(
                        [git_exe, "clone", u, target], cwd=ext_dir,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
                except OSError as e:
                    log(f"[插件] 无法启动 git: {e}\n")
                    continue
                proc = self._ext_proc
                try:
                    while True:
                        # read1() 而非 read()：read 会攒满 4096 字节才返回，
                        # git 输出停顿时段日志会一直卡住不显示
                        data = proc.stdout.read1(4096)
                        if not data:
                            break
                        log(cm.decode_process_output(data))
                except Exception:
                    pass
                rc = proc.wait()
                if self._ext_cancel:
                    break
                if rc != 0:
                    log(f"[插件] 「{name}」安装返回非零退出码 ({rc})，跳过继续下一个\n")
            if self._ext_cancel:
                log("\n[插件] 已取消\n")
            else:
                log("\n[插件] 全部安装完成！建议重启一下 WebUI 让新扩展生效。\n")
            self._emit("ext", "done")
        finally:
            self._ext_proc = None
            self._emit("ext", "state", running=False)

    self._emit("ext", "state", running=True)
    self._ext_thread = self._spawn(work, name="ext-install")
    return {"ok": True}


def _api_ext_cancel(self):
    self._ext_cancel = True
    if self._ext_proc and self._ext_proc.poll() is None:
        kill_process_tree(self._ext_proc.pid)
    return {"ok": True}


# ============================================================
# WD14 反推
# ============================================================

def _api_wd14_models(self):
    return {"ok": True, "models": [
        {"key": key, "label": info["label"],
         "cached": wt.is_model_cached(APP_DIR, key),
         "urls": wt.model_repo_urls(key)}
        for key, info in wt.MODEL_CATALOG.items()
    ], "model_ready": bool(self._wd14_model_dir)}


def _api_wd14_import_model(self, model_key, src_dir):
    """
    手动导入模型：用户自己把 model.onnx + selected_tags.csv 下好之后，
    选中那个文件夹导进缓存目录。这是自动下载走不通时的兜底 ——
    国内网络下 HuggingFace 有时无论换哪个镜像都连不上，但用浏览器
    或者迅雷去拖同一个链接反而能下动，与其让用户卡死，不如给条退路。
    """
    if self._wd14_load_thread and self._wd14_load_thread.is_alive():
        return {"ok": False, "error": "模型正在加载中，请等它结束"}
    try:
        model_dir = wt.import_model_files(APP_DIR, model_key, (src_dir or "").strip())
    except wt.WD14TaggerError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"导入失败：{type(e).__name__}: {e}"}

    # 导进来之后还得把隔离 venv 准备好，否则点「开始反推」才发现环境没装
    self._wd14_load_cancel = False

    def work():
        log = lambda t: self._emit("wd14", "model_log", text=t)
        try:
            log("模型文件已导入，正在准备独立运行环境 ...")
            venv.ensure_ready(log_cb=log, cfg=self.cfg)
            self._wd14_model_dir = model_dir
            self._emit("wd14", "model_ready")
        except venv.VenvError as e:
            self._emit("wd14", "model_failed", msg=str(e) or type(e).__name__)
        except Exception as e:
            self._emit("wd14", "model_failed",
                       msg=f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")

    self._wd14_load_thread = self._spawn(work, name="wd14-import")
    return {"ok": True}


def _api_wd14_load_model(self, model_key):
    if self._wd14_load_thread and self._wd14_load_thread.is_alive():
        return {"ok": False, "error": "模型正在加载中"}
    if model_key not in wt.MODEL_CATALOG:
        return {"ok": False, "error": "未知的模型"}
    self._wd14_load_cancel = False

    def work():
        log = lambda t: self._emit("wd14", "model_log", text=t)
        try:
            if not wt.is_model_cached(APP_DIR, model_key):
                log(f"模型未缓存，开始下载 {model_key} ...")
                wt.download_model(
                    APP_DIR, model_key,
                    progress_cb=lambda name, d, t: self._emit(
                        "wd14", "model_progress", name=name, downloaded=d, total=t),
                    log_cb=log,
                    cancel_flag=lambda: self._wd14_load_cancel,
                    cfg=self.cfg,
                )
            else:
                log("模型已缓存")
            model_dir = wt.model_cache_dir(APP_DIR, model_key)

            if self._wd14_load_cancel:
                raise wt.WD14TaggerError("已取消")

            log("正在准备独立运行环境（首次使用需要下载安装依赖，之后会跳过）...")
            venv.ensure_ready(log_cb=log, cfg=self.cfg)

            self._wd14_model_dir = model_dir
            self._emit("wd14", "model_ready")
        except (wt.WD14TaggerError, venv.VenvError) as e:
            self._emit("wd14", "model_failed", msg=str(e) or type(e).__name__)
        except Exception as e:
            detail = str(e).strip()
            msg = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
            self._emit("wd14", "model_failed",
                       msg=f"发生未知错误\n{msg}\n\n完整堆栈:\n{traceback.format_exc()}")

    self._wd14_load_thread = self._spawn(work, name="wd14-load")
    return {"ok": True}


def _api_wd14_add_clipboard_image(self, b64_data, name=""):
    """保存从剪贴板粘贴的图片（前端把 File 读成 base64 传过来），
    落到启动器目录下的 clipboard_inbox，返回完整路径供 wd14_expand 使用"""
    import base64
    if not b64_data:
        return {"ok": False, "error": "没有图片数据"}
    try:
        raw = base64.b64decode(b64_data)
    except Exception:
        return {"ok": False, "error": "图片数据解码失败"}
    if len(raw) > 100 * 1024 * 1024:
        return {"ok": False, "error": "图片太大（超过 100MB）"}
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        ext = ".png"
    inbox = os.path.join(APP_DIR, "clipboard_inbox")
    os.makedirs(inbox, exist_ok=True)
    fname = f"paste_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
    path = os.path.join(inbox, fname)
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except OSError as e:
        return {"ok": False, "error": f"保存失败: {e}"}
    return {"ok": True, "path": path}


def _api_wd14_expand(self, paths):
    return {"ok": True, "paths": _expand_to_image_files(paths or [])}


def _api_wd14_tag(self, images, general_thresh, character_thresh, include_rating, save_txt):
    if not self._wd14_model_dir:
        return {"ok": False, "error": "请先加载模型"}
    images = [p for p in (images or []) if isinstance(p, str)]
    if not images:
        return {"ok": False, "error": "请先添加图片"}
    if self._wd14_tag_thread and self._wd14_tag_thread.is_alive():
        return {"ok": False, "error": "反推正在进行中"}
    self._wd14_tag_cancel = False

    def work():
        emit_log = lambda t: self._emit("wd14", "tag_log", text=t)
        try:
            if self._wd14_tag_cancel:
                emit_log("已取消")
                self._emit("wd14", "tag_done")
                return
            import tempfile
            with tempfile.TemporaryDirectory() as tmp_dir:
                input_json = os.path.join(tmp_dir, "input.json")
                output_json = os.path.join(tmp_dir, "output.json")
                with open(input_json, "w", encoding="utf-8") as f:
                    json.dump({
                        "images": images,
                        "general_thresh": float(general_thresh),
                        "character_thresh": float(character_thresh),
                        "include_rating": bool(include_rating),
                    }, f)

                py = venv.venv_python_path()
                script = os.path.join(APP_DIR, "wd14_infer_cli.py")
                emit_log(f"正在隔离环境中反推 {len(images)} 张图片 ...")

                self._wd14_proc = subprocess.Popen(
                    [py, script, "--model_dir", self._wd14_model_dir,
                     "--input_json", input_json, "--output_json", output_json],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    bufsize=1, creationflags=_NO_WINDOW,
                )
                proc = self._wd14_proc

                # 按行实时读取：每张图处理完子进程会打印一行 ##ITEM##{...}
                other_lines = []
                total = len(images)
                done = 0
                for line in proc.stdout:
                    if self._wd14_tag_cancel:
                        break
                    line = line.rstrip("\n")
                    if line.startswith("##ITEM##"):
                        try:
                            item = json.loads(line[len("##ITEM##"):])
                        except ValueError:
                            other_lines.append(line)
                            continue
                        done += 1
                        self._emit("wd14", "tag_progress",
                                   done=done, total=item.get("total", total))
                        if item.get("ok"):
                            tag_string = item["tag_string"]
                            if save_txt:
                                try:
                                    txt_path = os.path.splitext(item["path"])[0] + ".txt"
                                    with open(txt_path, "w", encoding="utf-8") as f:
                                        f.write(tag_string)
                                except OSError:
                                    pass
                            self._emit("wd14", "tag_item",
                                       path=item["path"], tags=tag_string)
                        else:
                            emit_log(f"处理失败 {os.path.basename(item['path'])}: "
                                     f"{item.get('error')}")
                    elif line:
                        other_lines.append(line)

                try:
                    proc.stdout.close()
                except Exception:
                    pass
                returncode = proc.wait()

                if self._wd14_tag_cancel:
                    emit_log("已取消")
                    self._emit("wd14", "tag_done")
                    return

                if not os.path.exists(output_json):
                    detail = "\n".join(other_lines).strip() or "（子进程没有任何输出）"
                    self._emit("wd14", "tag_failed",
                               msg=f"反推子进程异常退出 (返回码 {returncode})\n\n完整输出:\n{detail}")
                    return

            self._emit("wd14", "tag_done")
        except Exception as e:
            detail = str(e).strip()
            msg = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
            self._emit("wd14", "tag_failed",
                       msg=f"发生未知错误\n{msg}\n\n完整堆栈:\n{traceback.format_exc()}")
        finally:
            self._wd14_proc = None

    self._wd14_tag_thread = self._spawn(work, name="wd14-tag")
    return {"ok": True}


def _api_wd14_cancel(self):
    self._wd14_tag_cancel = True
    self._wd14_load_cancel = True
    if self._wd14_proc and self._wd14_proc.poll() is None:
        kill_process_tree(self._wd14_proc.pid)
    return {"ok": True}


# ============================================================
# 图片信息
# ============================================================

def _api_meta_load(self, path):
    path = (path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在"}
    t_all = time.perf_counter()
    t0 = time.perf_counter()
    try:
        meta = imc.extract_image_metadata(path)
    except imc.ImageMetaError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    ms_parse = (time.perf_counter() - t0) * 1000

    # 目录没变就不写盘。原来每打开一张图都 save_config 一次，
    # 同一个文件夹里连着看几十张图 = 几十次无谓的磁盘写入
    t0 = time.perf_counter()
    new_dir = os.path.dirname(path)
    if self.cfg.get("meta_last_dir") != new_dir:
        try:
            self.cfg["meta_last_dir"] = new_dir
            cm.save_config(self.cfg)
        except OSError:
            pass
    ms_cfg = (time.perf_counter() - t0) * 1000

    self._meta_current = meta
    self._meta_meta = meta.pop("_meta")
    self._meta_path = path
    self._meta_refs = meta["refs"]
    self._meta_lookup_result = {}
    self._meta_name_searches = {}

    f = meta["file"]
    file_bits = []
    if f["width"] and f["height"]:
        file_bits.append(f"{f['width']}×{f['height']}")
    if f["format"]:
        file_bits.append(f["format"])
    if f["size_kb"]:
        file_bits.append(f"{f['size_kb']:.0f} KB" if f["size_kb"] >= 1
                         else f"{f['size_kb']} KB")

    resp = {
        "ok": True,
        "path": path,
        # 预览只回一个相对路径（几十字节），图片数据由浏览器自己读盘。
        # 不再有"大图 = 大载荷"这回事，所以也不需要什么大小阈值了。
        "preview": None,   # 占位，下面测完耗时再填
        "preview_size": _safe_size(path),
        "source": meta["source"],
        "tone": meta["tone"],
        "file_info": "　·　".join(file_bits),
        "has_meta": meta["has_meta"],
        "prompt": meta["prompt"],
        "negative": meta["negative"],
        "rows": meta["rows"],
        "loras": meta["loras"],
        "characters": meta["characters"],
        "civitai": meta["civitai"],
        "from_sidecar": meta["from_sidecar"],
        "raw_text": meta["raw_text"],
        "refs_count": len(self._meta_refs),
    }

    t0 = time.perf_counter()
    resp["preview"] = self._make_preview(path)
    ms_preview = (time.perf_counter() - t0) * 1000

    # 各阶段耗时一起回去。图片信息页会把它显示在来源徽章旁边——
    # 与其凭感觉争论"卡不卡"，不如让数字直接摆在界面上。
    resp["timing"] = {
        "parse": round(ms_parse, 1),
        "preview": round(ms_preview, 1),
        "config": round(ms_cfg, 1),
        "total": round((time.perf_counter() - t_all) * 1000, 1),
        "preview_mode": (_preview_last_mode[0] if (resp["preview"] or "").startswith("_preview/")
                         else ("data URL 兜底" if resp["preview"] else "无")),
    }
    if not meta["has_meta"]:
        resp["prompt"] = ("（这张图里没有找到生成参数）\n\n"
                          "常见原因：截图、微信/QQ 传输、网站二次压缩都会把元数据整段抹掉。"
                          "顺带一提，这个解析器连隐写 PNG（参数藏在像素里的那种）也会尝试读取，"
                          "读不到基本就是真的没有了。")
    return resp


def _api_meta_scan_missing(self):
    if not self._meta_refs:
        return {"ok": False, "error": "当前图片没有可检测的模型引用"}
    root = (self.cfg.get("webui_root") or "").strip()
    if not root:
        return {"ok": False, "error": "请先在「一键启动」页设置好 WebUI 根目录，才能判断本地是否已有这些模型"}

    self._meta_lookup_result = {}
    rows = []
    lookups = []
    for idx, ref in enumerate(self._meta_refs):
        row = {"idx": idx, "role": ref["role"], "name": ref["name"]}
        local_path = imc.find_local_model_file(
            root, ref["role"], ref["name"], self.cfg.get("webui_branch", ""))
        if local_path:
            row["state"] = "local"
        elif not ref.get("hash"):
            row["state"] = "no_hash"
        else:
            row["state"] = "querying"
            lookups.append((idx, ref))
        rows.append(row)

    if lookups:
        api_key = (self.cfg.get("civitai_api_key") or "").strip() or None

        def work():
            for idx, ref in lookups:
                try:
                    info = cd.fetch_version_info_by_hash(ref["hash"], api_key)
                    primary = next((f for f in info["files"] if f["primary"]),
                                   info["files"][0] if info["files"] else None)
                    if primary:
                        self._meta_lookup_result[idx] = {
                            "ok": True, "version_info": info, "file_info": primary}
                        self._emit("meta", "missing_row", idx=idx, state="found",
                                   file_name=primary["name"],
                                   size_mb=round((primary.get("sizeKB") or 0) / 1024, 1))
                    else:
                        self._emit("meta", "missing_row", idx=idx, state="not_found",
                                   error="该版本在 Civitai 上没有可下载的文件")
                except cd.CivitaiError as e:
                    self._emit("meta", "missing_row", idx=idx, state="not_found", error=str(e))
                except Exception as e:
                    self._emit("meta", "missing_row", idx=idx, state="not_found",
                               error=f"{type(e).__name__}: {e}")
            has_dl = any(r.get("ok") for r in self._meta_lookup_result.values())
            self._emit("meta", "scan_done", has_downloadable=has_dl)

        self._spawn(work, name="meta-lookup")
    return {"ok": True, "rows": rows, "querying": len(lookups)}


def _api_meta_name_search(self, idx):
    idx = int(idx)
    if not (0 <= idx < len(self._meta_refs)):
        return {"ok": False, "error": "引用无效"}
    query = self._meta_refs[idx]["name"]
    api_key = (self.cfg.get("civitai_api_key") or "").strip() or None

    def work():
        try:
            results = cd.search_models_by_name(query, api_key)
            self._meta_name_searches[idx] = results
            candidates = []
            for r in results:
                primary = next((f for f in r["files"] if f["primary"]),
                               r["files"][0] if r["files"] else None)
                fname = primary["name"] if primary else "（无可下载文件）"
                base_model = r.get("base_model") or "未知底模"
                candidates.append({"label": f"{r['model_name']} | {fname} | {base_model}"})
            self._emit("meta", "name_result", idx=idx, ok=True, candidates=candidates)
        except cd.CivitaiError as e:
            self._emit("meta", "name_result", idx=idx, ok=False, error=str(e))
        except Exception as e:
            self._emit("meta", "name_result", idx=idx, ok=False,
                       error=f"{type(e).__name__}: {e}")

    self._spawn(work, name="meta-name-search")
    return {"ok": True}


def _api_meta_choose_candidate(self, idx, cand_idx):
    idx, cand_idx = int(idx), int(cand_idx)
    results = self._meta_name_searches.get(idx) or []
    if not (0 <= cand_idx < len(results)):
        return {"ok": False, "error": "候选无效"}
    chosen = results[cand_idx]
    primary = next((f for f in chosen["files"] if f["primary"]),
                   chosen["files"][0] if chosen["files"] else None)
    if not primary:
        return {"ok": False, "error": "选中的候选没有可下载的文件"}
    self._meta_lookup_result[idx] = {"ok": True, "version_info": chosen, "file_info": primary}
    return {"ok": True, "file_name": primary["name"],
            "size_mb": round((primary.get("sizeKB") or 0) / 1024, 1)}


def _api_meta_download(self, indexes):
    if self._meta_dl_thread and self._meta_dl_thread.is_alive():
        return {"ok": False, "error": "已有下载任务在进行中"}
    root = (self.cfg.get("webui_root") or "").strip()
    if not root:
        return {"ok": False, "error": "请先在「一键启动」页设置好 WebUI 根目录"}

    items = []
    for idx in (indexes or []):
        idx = int(idx)
        result = self._meta_lookup_result.get(idx)
        if not result or not result.get("ok"):
            continue
        ref = self._meta_refs[idx]
        folder = imc.role_folder(ref["role"], self.cfg.get("webui_branch", "")) or "models/Other"
        dest_dir = os.path.join(root, *folder.split("/"))
        items.append((idx, result["version_info"], result["file_info"], dest_dir))
    if not items:
        return {"ok": False, "error": "请先在列表里勾选要下载的模型"}

    api_key = (self.cfg.get("civitai_api_key") or "").strip() or None
    self._meta_dl_cancel = False
    total_count = len(items)

    def work():
        done_count = 0
        for idx, version_info, file_info, dest_dir in items:
            if self._meta_dl_cancel:
                break
            try:
                final_path, _hash_ok = cd.download_file(
                    file_info, dest_dir, api_key,
                    progress_cb=lambda d, t, i=idx: self._emit(
                        "meta", "dl_progress", idx=i, downloaded=d, total=t),
                    cancel_flag=lambda: self._meta_dl_cancel,
                )
                cd.save_sidecar_metadata(final_path, version_info, file_info)
                cd.download_preview_image(final_path, version_info, api_key)
                self._emit("meta", "dl_item", idx=idx, ok=True,
                           name=os.path.basename(final_path))
            except cd.CivitaiError as e:
                self._emit("meta", "dl_item", idx=idx, ok=False, error=str(e))
            except Exception as e:
                self._emit("meta", "dl_item", idx=idx, ok=False,
                           error=f"{type(e).__name__}: {e}")
            done_count += 1
            self._emit("meta", "dl_overall", done=done_count, total=total_count)
        self._emit("meta", "dl_done")

    self._meta_dl_thread = self._spawn(work, name="meta-download")
    return {"ok": True, "total": total_count}


def _api_mirror_status_text(self):
    mode = self.cfg.get("mirror_mode", "auto")
    if mode == "always":
        return "当前：强制使用国内镜像"
    if mode == "never":
        return "当前：强制使用官方源"
    cached = self.cfg.get("mirror_detected", "")
    if cached == "cn":
        return "当前：已检测为国内网络，下载走镜像加速"
    if cached == "global":
        return "当前：可直连国外站点，使用官方源"
    return "当前：尚未检测，首次下载时会自动检测一次"


def _api_redetect_network(self):
    """重新探测网络环境（线程里跑，探测要约 6 秒）"""
    def work():
        try:
            import mirror_manager as mm
            self.cfg["mirror_detected"] = ""
            self.cfg["github_mirror_detected"] = ""
            result = mm.detect_network_environment()
            self.cfg["mirror_detected"] = result
            cm.save_config(self.cfg)
            self._emit("settings", "mirror_status", ok=True,
                       text=self._mirror_status_text())
        except Exception as e:
            self._emit("settings", "mirror_status", ok=False, text=f"检测失败: {e}")
    self._spawn(work, name="redetect-network")
    return {"ok": True}


def _meta_require_current(self):
    if not self._meta_current or not self._meta_meta:
        return None
    return self._meta_meta


def _api_meta_save(self, prompt, negative, rows, output_path=None, include_workflow=True):
    """
    把编辑后的提示词/参数写回图片。

    走的是容器块手术：像素字节一个都不动，所以 JPEG 反复保存不掉画质、
    动图的帧不会丢、第三方工具写的数据块原样留着。
    """
    m = self._meta_meta
    if m is None:
        return {"ok": False, "error": "请先打开一张图片"}
    path = self._meta_path
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "源文件已不存在"}

    m.positive_prompt = prompt if prompt is not None else m.positive_prompt
    m.negative_prompt = negative if negative is not None else m.negative_prompt
    _apply_edited_rows(m, rows or [])

    try:
        mode = me.save_metadata(path, m, output_path=(output_path or None),
                                include_comfy_workflow=bool(include_workflow))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    saved_to = output_path or path
    label = {"comfy": "已按 ComfyUI 工作流写回（提示词落进了节点，拖回 ComfyUI 可直接重跑）",
             "nai": "已按 NovelAI 原生格式写回（专有参数和角色插槽都保留了）",
             "webui": "已按 A1111 标准参数文本写回",
             "json": "已保存为工作流 JSON"}.get(mode, "已保存")
    return {"ok": True, "path": saved_to, "mode": mode, "message": label}


def _api_meta_save_warnings(self, prompt, negative, rows, output_path=None):
    """保存前的损失预告。"""
    m = self._meta_meta
    if m is None:
        return {"ok": False, "error": "请先打开一张图片"}
    m.positive_prompt = prompt if prompt is not None else m.positive_prompt
    m.negative_prompt = negative if negative is not None else m.negative_prompt
    _apply_edited_rows(m, rows or [])
    try:
        fatal, warns = me.collect_save_warnings(
            self._meta_path, m, output_path=(output_path or None))
    except Exception as e:
        return {"ok": True, "fatal": None, "warnings": [f"检查时出错：{e}"]}
    return {"ok": True, "fatal": fatal, "warnings": warns}


def _api_meta_strip(self, paths=None, in_place=True):
    """清除元数据（分享前去掉提示词）。paths 为空时处理当前图片。"""
    targets = [p for p in (paths or ([self._meta_path] if self._meta_path else [])) if p]
    if not targets:
        return {"ok": False, "error": "没有要处理的图片"}
    done, failed = [], []
    for p in targets:
        try:
            out = p if in_place else _suffixed_path(p, "_clean")
            me.strip_metadata(p, output_path=None if in_place else out)
            done.append(out if not in_place else p)
        except Exception as e:
            failed.append({"path": p, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "done": len(done), "failed": failed, "paths": done}


def _api_meta_batch_load(self, paths):
    """批量：展开路径（含文件夹）并逐张解析出摘要，给列表面板用。"""
    files = []
    for p in (paths or []):
        if os.path.isdir(p):
            for dirpath, _d, names in os.walk(p):
                for n in sorted(names):
                    if os.path.splitext(n)[1].lower() in imc.IMAGE_EXTS:
                        files.append(os.path.join(dirpath, n))
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in (
                imc.IMAGE_EXTS + (".json",)):
            files.append(p)
    files = list(dict.fromkeys(files))
    if not files:
        return {"ok": False, "error": "没有找到图片文件"}

    self._meta_batch = files
    items = []
    for i, p in enumerate(files):
        try:
            r = imc.extract_image_metadata(p)
            items.append({"idx": i, "path": p, "name": os.path.basename(p),
                          "source": r["source"], "tone": r["tone"],
                          "has_meta": r["has_meta"],
                          "prompt": (r["prompt"] or "")[:120]})
        except Exception as e:
            items.append({"idx": i, "path": p, "name": os.path.basename(p),
                          "source": "读取失败", "tone": "plain",
                          "has_meta": False, "prompt": str(e)[:120]})
    return {"ok": True, "items": items, "total": len(files)}


def _api_meta_batch_export_txt(self, indexes=None, out_dir=None):
    """
    批量导出提示词为同名 .txt —— 拿现成的图直接凑 LoRA 训练集时很省事。
    out_dir 为空则写在图片旁边。
    """
    files = self._meta_batch or ([self._meta_path] if self._meta_path else [])
    if indexes:
        files = [files[i] for i in indexes if 0 <= i < len(files)]
    if not files:
        return {"ok": False, "error": "没有要导出的图片"}
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    done, skipped, failed = 0, 0, []
    for p in files:
        try:
            r = imc.extract_image_metadata(p)
            text = (r["prompt"] or "").strip()
            if not text:
                skipped += 1
                continue
            base = os.path.splitext(os.path.basename(p))[0] + ".txt"
            dst = os.path.join(out_dir, base) if out_dir else \
                os.path.splitext(p)[0] + ".txt"
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
            done += 1
        except Exception as e:
            failed.append({"path": p, "error": str(e)})
    return {"ok": True, "done": done, "skipped": skipped, "failed": failed}


def _api_meta_diff(self, idx_a, idx_b):
    """两张图的参数对比：返回并排的 [[键, A值, B值, 是否不同], ...]。"""
    files = self._meta_batch or []
    try:
        pa, pb = files[idx_a], files[idx_b]
    except (IndexError, TypeError):
        return {"ok": False, "error": "请先在列表里选中两张图片"}
    try:
        ra, rb = imc.extract_image_metadata(pa), imc.extract_image_metadata(pb)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def as_map(r):
        d = {"正向提示词": r["prompt"], "负向提示词": r["negative"], "来源": r["source"]}
        d.update({k: v for k, v in r["rows"]})
        return d

    ma, mb = as_map(ra), as_map(rb)
    keys = list(ma) + [k for k in mb if k not in ma]
    rows = [[k, ma.get(k, ""), mb.get(k, ""), ma.get(k, "") != mb.get(k, "")]
            for k in keys]
    return {"ok": True, "a": {"path": pa, "name": os.path.basename(pa)},
            "b": {"path": pb, "name": os.path.basename(pb)},
            "rows": rows,
            "diff_count": sum(1 for r in rows if r[3])}


def _api_meta_lora_info(self, path):
    """
    读 LoRA / Checkpoint 的 safetensors 元数据：架构、训练超参、
    触发词词频、ModelSpec、AutoV2 哈希（可直接拿去 Civitai 查）。
    """
    path = (path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在"}
    try:
        lm = me.LoraParser.read_lora_metadata(path)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if getattr(lm, "error", ""):
        return {"ok": False, "error": lm.error}

    rows = []
    for label, attr in (("模型名", "model_name"), ("底模", "base_model"),
                        ("网络类型", "network_module"), ("Network Dim", "network_dim"),
                        ("Network Alpha", "network_alpha"), ("学习率", "learning_rate"),
                        ("UNet 学习率", "unet_lr"), ("文本编码器学习率", "text_encoder_lr"),
                        ("优化器", "optimizer"), ("学习率调度", "lr_scheduler"),
                        ("训练分辨率", "resolution"), ("训练图片数", "num_train_images"),
                        ("重复次数", "num_repeats"), ("Epoch 数", "num_epochs"),
                        ("批大小", "batch_size"), ("混合精度", "mixed_precision"),
                        ("Clip Skip", "clip_skip"), ("噪声偏移", "noise_offset"),
                        ("训练备注", "training_comment"), ("训练时间", "training_started_at")):
        v = getattr(lm, attr, None)
        if v not in (None, "", "None"):
            rows.append([label, str(v)])

    return {
        "ok": True,
        "file": {"name": os.path.basename(path),
                 "size_text": _fmt_size(os.path.getsize(path))},
        "kind": getattr(lm, "kind", "") or "LoRA",
        "arch": getattr(lm, "architecture", "") or getattr(lm, "base_model", "") or "未识别",
        "sha256": getattr(lm, "sha256", "") or "",
        "autov2": getattr(lm, "autov2", "") or "",
        "rows": rows,
        "tags": [{"tag": t.tag, "count": t.count}
                 for t in (getattr(lm, "tag_frequency", None) or [])[:40]],
        "editable": {k: v for k, v in (getattr(lm, "raw_metadata", None) or {}).items()
                     if k.startswith("modelspec.")},
    }


def _apply_edited_rows(m, rows):
    """把界面上改过的参数行写回 SamplerParameters。只认我们确实支持的字段。"""
    setters = {
        "步数": ("steps", _to_int), "CFG": ("cfg_scale", _to_float),
        "种子": ("seed", _to_int), "采样器": ("sampler_name", str),
        "调度器": ("scheduler", str), "尺寸": ("size", str),
        "模型": ("model_name", str), "模型哈希": ("model_hash", str),
        "重绘幅度": ("denoising_strength", _to_float),
        "Clip skip": ("clip_skip", _to_int),
    }
    for item in rows:
        try:
            key, val = item[0], item[1]
        except (IndexError, TypeError):
            continue
        hit = setters.get(key)
        if not hit:
            continue
        attr, conv = hit
        val = (val or "").strip() if isinstance(val, str) else val
        if val == "":
            continue
        try:
            setattr(m.params, attr, conv(val))
        except (TypeError, ValueError):
            continue


def _to_int(v):
    return int(float(str(v).strip()))


def _to_float(v):
    return float(str(v).strip())


def _suffixed_path(path, suffix):
    stem, ext = os.path.splitext(path)
    return stem + suffix + ext


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.2f} GB"


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _make_preview(self, path):
    """给一张图准备可被网页直接引用的预览地址。"""
    self._preview_seq += 1
    url = _preview_link(path, self._preview_seq)
    if url:
        _preview_cleanup(self._preview_seq)
        return url
    # 兜底：web/_preview 建不出来（只读安装目录等），退回 data URL。
    # 这条路才受大小限制，因为它真的要过桥。
    return _image_data_url(path)


def _api_meta_preview(self, path, force=False):
    """保留给前端的显式重取入口（比如用户点了「重新加载预览」）。"""
    path = (path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在"}
    return {"ok": True, "preview": self._make_preview(path), "size": _safe_size(path)}


def _api_reveal_image(self, path):
    """用系统默认看图工具打开（预览太大时的退路）"""
    path = (path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在"}
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True}



def _meta_response(meta, path):
    """组装给前端的响应。按路径读和前端直读两条路共用，免得两边慢慢跑偏。"""
    f = meta["file"]
    bits = []
    if f["width"] and f["height"]:
        bits.append(f"{f['width']}×{f['height']}")
    if f["format"]:
        bits.append(f["format"])
    if f["size_kb"]:
        bits.append(f"{f['size_kb']:.0f} KB")

    resp = {
        "ok": True,
        "path": path,
        "source": meta["source"],
        "tone": meta["tone"],
        "file_info": "　·　".join(bits),
        "has_meta": meta["has_meta"],
        "prompt": meta["prompt"],
        "negative": meta["negative"],
        "rows": meta["rows"],
        "loras": meta["loras"],
        "characters": meta["characters"],
        "civitai": meta["civitai"],
        "from_sidecar": meta["from_sidecar"],
        "raw_text": meta["raw_text"],
        "refs_count": len(meta["refs"]),
    }
    if not meta["has_meta"]:
        resp["prompt"] = ("（这张图里没有找到生成参数）\n\n"
                          "常见原因：截图、微信/QQ 传输、网站二次压缩都会把元数据整段抹掉。")
    return resp


def _api_meta_parse(self, container, name="", size=0, path=""):
    """
    直接解析前端送来的容器数据（PNG 文本块 / EXIF），不读盘、不要路径。

    这是「拖一张图进来」的主通道。原来那条路是：drop 事件绕到 Python 侧
    去取 pywebviewFullPath，拿到路径再回前端，然后后端按路径重新读一遍文件。
    那条桥要序列化整个事件对象，实测是整个流程里最慢的一环 —— 界面上显示的
    「解析 1ms」是真的，但它只覆盖了后端读文件那一小段，前面等路径的时间
    根本没被算进去。

    现在浏览器手里有完整字节，就地把元数据抠出来（web/js/imgcontainer.js），
    只把这几 KB 文本送过来。图片本体一个字节都不过桥，路径也不需要。
    解析调度和各家格式的门禁跟按路径读完全共用，不存在两套解析器。
    """
    if not isinstance(container, dict):
        return {"ok": False, "error": "无效的图片数据"}
    t0 = time.perf_counter()
    try:
        m = me.reader.parse_container(container, path or "", int(size or 0))
        meta = imc.present(m)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    ms_parse = (time.perf_counter() - t0) * 1000

    self._meta_meta = meta.pop("_meta")
    self._meta_current = meta
    self._meta_path = path or ""
    self._meta_refs = meta["refs"]
    self._meta_lookup_result = {}
    self._meta_name_searches = {}

    resp = _meta_response(meta, path or name)
    resp["name"] = name or os.path.basename(path or "")
    resp["timing"] = {"parse": round(ms_parse, 1), "mode": "前端直读"}
    # 前端拿不到路径时，隐写 PNG 和同名 txt 这两条要读原文件的路径走不了。
    # 真遇到「什么都没读出来」，前端会再走一次按路径的完整解析补上。
    resp["needs_deep"] = (not meta["has_meta"]) and not path
    return resp


def _api_meta_deep(self, path):
    """
    按磁盘路径做完整解析（含隐写 PNG 像素扫描、同名 sidecar）。
    只在前端直读什么都没读出来时才调，属于兜底而不是主路径。
    """
    return _api_meta_load(self, path)



# ============================================================
# 启动器自更新（updater.py 干重活，这里只做线程调度和事件推送）
# ============================================================

def _api_launcher_check_update(self):
    """检查启动器有没有新版本（线程里跑，要访问 GitHub API）"""
    def work():
        try:
            result = updater.check_update(
                self.cfg, log_cb=lambda t: self._emit("launcher", "log", text=t))
            self._emit("launcher", "update_info", ok=True, **result)
        except Exception as e:
            self._emit("launcher", "update_info", ok=False, error=str(e))
    self._spawn(work, name="launcher-check-update")
    return {"ok": True}


def _api_launcher_update(self):
    """一键更新启动器本体。覆盖的是源码文件，当前进程不受影响，重启后生效。"""
    with self._update_lock:
        if self._update_running:
            return {"ok": False, "error": "更新正在进行中"}
        self._update_running = True

    def work():
        try:
            def progress(done, total):
                pct = int(done * 100 / total) if total else 0
                self._emit("launcher", "update_progress", pct=pct,
                           label=f"下载中 {done/1024/1024:.1f} / {total/1024/1024:.1f} MB"
                           if total else "下载中 ...")
            result = updater.perform_update(
                self.cfg,
                log_cb=lambda t: self._emit("launcher", "log", text=t),
                progress_cb=progress)
            self._emit("launcher", "update_done", ok=True,
                       version=result["version"], commit=result["commit"],
                       need_restart=True)
        except Exception as e:
            self._emit("launcher", "update_done", ok=False, error=str(e))
        finally:
            with self._update_lock:
                self._update_running = False
    self._spawn(work, name="launcher-update")
    return {"ok": True}


def _api_launcher_restart(self):
    """更新完成后重启启动器：拉起 start一键启动.bat 再关掉当前窗口。"""
    if self.webui_running() or self.deploy_running():
        return {"ok": False, "error": "WebUI 或部署任务仍在运行，请先停止再重启启动器"}
    try:
        bat = os.path.join(APP_DIR, "start一键启动.bat")
        if os.name == "nt" and os.path.exists(bat):
            os.startfile(bat)  # noqa: S606 - 拉起本启动器自己的 bat
        else:
            subprocess.Popen([sys.executable, os.path.join(APP_DIR, "webview_main.py")],
                             cwd=APP_DIR)
    except Exception as e:
        return {"ok": False, "error": f"重启失败: {e}"}
    if self._window:
        # 延迟一点再销毁窗口，让本次 JS 调用的返回值先送回去
        threading.Timer(0.5, self._window.destroy).start()
    return {"ok": True}


# ============================================================
# 把分节写的函数挂到 LauncherApi 上
# ============================================================

for _name, _fn in {
    "_mirror_status_text": _api_mirror_status_text,
    "redetect_network": _api_redetect_network,
    "deploy_env_detect": _api_deploy_env_detect,
    "deploy_check_dir": _api_deploy_check_dir,
    "deploy_precheck": _api_deploy_precheck,
    "deploy_start": _api_deploy_start,
    "deploy_cancel": _api_deploy_cancel,
    "deploy_venv_check": _api_deploy_venv_check,
    "deploy_venv_delete": _api_deploy_venv_delete,
    "deploy_patch_hashlib": _api_deploy_patch_hashlib,
    "_deploy_flow": _deploy_flow,
    "_deploy_portable_env": _deploy_portable_env,
    "_deploy_run_cmd": _deploy_run_cmd,
    "_deploy_run_webui_until_ready": _deploy_run_webui_until_ready,
    "civitai_fetch": _api_civitai_fetch,
    "civitai_download": _api_civitai_download,
    "_fetch_liblib": _fetch_liblib,
    "_liblib_download_start": _liblib_download_start,
    "settings_verify_liblib_token": _api_settings_verify_liblib_token,
    "models_categories": _api_models_categories,
    "models_list": _api_models_list,
    "models_detail": _api_models_detail,
    "models_hash_lookup": _api_models_hash_lookup,
    "models_batch": _api_models_batch,
    "models_batch_cancel": _api_models_batch_cancel,
    "models_delete": _api_models_delete,
    "models_set_trained_words": _api_models_set_trained_words,
    "models_bind_liblib": _api_models_bind_liblib,
    "open_output_folder": _api_open_output_folder,
    "output_dirs_info": _api_output_dirs_info,
    "wd14_add_clipboard_image": _api_wd14_add_clipboard_image,
    "ext_list": _api_ext_list,
    "ext_install": _api_ext_install,
    "ext_cancel": _api_ext_cancel,
    "wd14_models": _api_wd14_models,
    "wd14_load_model": _api_wd14_load_model,
    "wd14_import_model": _api_wd14_import_model,
    "wd14_expand": _api_wd14_expand,
    "wd14_tag": _api_wd14_tag,
    "wd14_cancel": _api_wd14_cancel,
    "meta_load": _api_meta_load,
    "meta_parse": _api_meta_parse,
    "meta_deep": _api_meta_deep,
    "meta_preview": _api_meta_preview,
    "_make_preview": _make_preview,
    "reveal_image": _api_reveal_image,
    "meta_save": _api_meta_save,
    "meta_save_warnings": _api_meta_save_warnings,
    "meta_strip": _api_meta_strip,
    "meta_batch_load": _api_meta_batch_load,
    "meta_batch_export_txt": _api_meta_batch_export_txt,
    "meta_diff": _api_meta_diff,
    "meta_lora_info": _api_meta_lora_info,
    "meta_scan_missing": _api_meta_scan_missing,
    "meta_name_search": _api_meta_name_search,
    "meta_choose_candidate": _api_meta_choose_candidate,
    "meta_download": _api_meta_download,
    "launcher_check_update": _api_launcher_check_update,
    "launcher_update": _api_launcher_update,
    "launcher_restart": _api_launcher_restart,
}.items():
    setattr(LauncherApi, _name, _fn)
