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
import urllib.parse
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
import model_library as ml
import output_index as oi
import comfy_nodes

APP_DIR = os.path.dirname(os.path.abspath(__file__))

from process_manager import (  # noqa: E402  进程工具与常量（V3 搬到 process_manager）
    WEBUI_ENTRY_SCRIPT, _NO_WINDOW, _URL_RE, _BIND_ERROR_RE, _OVERRIDE_VAR_PATTERN,
    kill_process_tree, _probe_executable, find_listening_pid, process_name_of,
    _find_webui_user_bat_overrides, _clear_webui_user_bat_overrides, InstanceRunner,
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

# 自动部署反复失败时的最后退路：完整 Forge Neo 整合包（百度网盘）。
# 用户手动下载解压后，在「环境部署」页选择解压目录再点「开始部署」，
# 会检测到已有安装、跳过源码下载，只补齐环境。
PAN_FALLBACK_TEXT = (
    "备用方案：如果自动部署反复失败，可以手动下载完整的 Forge Neo 整合包——\n\n"
    "百度网盘: https://pan.baidu.com/s/1SV-YxTe-5DhA4PPjDk-neQ?pwd=26u8\n"
    "提取码: 26u8\n\n"
    "下载解压后，在「环境部署」页的「安装目录」里选择解压出来的文件夹，"
    "再点一次「开始部署」即可（检测到已有安装会自动跳过源码下载，只补齐环境）。"
)

DEPLOY_BRANCH_OPTIONS = [
    ("Neo 版（Haoming02 社区维护分支，推荐）", "neo2"),
    ("常规版 / Classic（lllyasviel 官方仓库）", "classic"),
    ("ComfyUI（官方仓库）", "comfyui"),
]

SETTINGS_BRANCH_OPTIONS = [
    ("Neo 版（新版参数：--normalvram / --force-fpXX，推荐）", "neo2"),
    ("Neo 版（旧版参数：--always-xxx-vram / --all-in-fpXX）", "neo"),
    ("常规版 / Classic", "classic"),
    ("ComfyUI", "comfyui"),
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

        # 一键启动：每个实例一个 InstanceRunner（process_manager.py）
        self._runners = {}
        self._runners_lock = threading.Lock()
        self._open_lock = threading.Lock()
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
        self._deploy_cfg = dict(self.cfg)

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
        self._import_thread = None      # 拖拽上传
        self._import_cancel = False
        self._dnd_state = None          # pywebview 的拖放路径池，由 webview_main 注入

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
        # 回填时非日志事件（ask/state/done 等）排前面：ask 事件丢了，
        # 等待回答的线程会永远卡住；日志丢几条只是显示缺几行
        kept.sort(key=lambda e: e.get("type") == "log")
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
        self._drop_debug_write("PY", f"handle_dropped_paths(旧通道) paths={paths}")
        if paths:
            self._emit("app", "dropped", paths=paths)
        return {"ok": True}

    # ---- 临时诊断（定位拖放问题用，定位完删掉）----
    def _drop_debug_write(self, who, msg):
        try:
            os.makedirs(os.path.join(APP_DIR, "launcher_data"), exist_ok=True)
            with open(os.path.join(APP_DIR, "launcher_data", "drop_debug.log"),
                      "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {who}: {msg}\n")
        except OSError:
            pass

    def drop_debug(self, msg):
        self._drop_debug_write("JS", msg)
        return {"ok": True}

    def drop_files(self, names):
        """前端拖放拿完整路径（web/js/core.js bindFileDrop）。

        放下文件时前端先 postMessageWithAdditionalObjects("FilesDropped", files)，
        pywebview 原生侧把 (文件名, 完整路径) 存进 _dnd_state["paths"]；WebView2
        的消息是按发出顺序处理的，这个调用到达时路径一定已经存好。这里按文件名
        匹配取出（取过的从池里删掉，免得下次拖同名文件拿错）。
        _dnd_state 不可用（pywebview 内部结构变了）时返回 legacy=True，前端改等
        webview_main 里旧 DOM 事件通道推过来的 app/dropped。"""
        st = self._dnd_state
        pool = st.get("paths") if isinstance(st, dict) else None
        self._drop_debug_write("PY", f"drop_files({names}) pool={pool if pool else type(st).__name__}")
        if not isinstance(st, dict) or not isinstance(pool, list):
            return {"ok": False, "legacy": True}
        pool = st["paths"]
        paths = []
        for name in names or []:
            hit = None
            for i, item in enumerate(pool):
                if urllib.parse.unquote(str(item[0])) == name:
                    hit = i
                    break
            if hit is None:
                continue
            full = urllib.parse.unquote(str(pool.pop(hit)[1]))
            if full and full not in paths:
                paths.append(full)
        del pool[32:]   # 没匹配上的残留（拖了又取消等）别无限攒
        if paths:
            return {"ok": True, "paths": paths}
        return {"ok": False, "error": "原生侧没有记下这些文件的路径"}

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
            "instances": _instances_payload(self),
            "hidden_pages": self.cfg.get("hidden_pages") or [],
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
            "vram_comfy": opts(cm.VRAM_MODE_OPTIONS_COMFY),
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
        with self._open_lock:
            if url == self._last_open_url and now - self._last_open_url_ts < 1.5:
                return {"ok": True, "deduped": True}
            self._last_open_url = url
            self._last_open_url_ts = now
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return {"ok": True}

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
        """任一实例在运行（关窗确认、重启启动器都看这个）"""
        return any(r.running() for r in list(self._runners.values()))

    def _kill_all_runners(self):
        killed = False
        for r in list(self._runners.values()):
            if r.running():
                r.stop()
                killed = True
        return killed

    def deploy_running(self):
        return bool(self._deploy_running)

    def request_exit_confirm(self):
        """窗口关闭事件里调用：还有实例在跑就让前端弹确认框（带上是哪些实例）"""
        names = []
        for iid, r in list(self._runners.items()):
            if r.running():
                inst = cm.find_instance(self.cfg, iid) or {}
                names.append(inst.get("name") or r.label())
        self._emit("app", "confirm_exit", names=names)

    def exit_app(self, kill_webui=True):
        if kill_webui:
            self._kill_all_runners()
            if self._window:
                self._window.destroy()
            return {"ok": True}
        # 不杀 WebUI 时 destroy() 会被 on_closing 否决（窗口关不掉、确认框
        # 已经关了，观感就是「退出卡死」），所以这里不再尝试关窗，改为
        # 明确提示用户本次退出已被取消
        self._emit("app", "error",
                   text="已取消退出：还有实例在运行，直接退出会留下占端口/显存的孤儿进程。"
                        "请先停止，或在关闭确认框里选「结束并退出」。")
        return {"ok": True}

    # ============================================================
    # 一键启动
    # ============================================================

    def _runner(self, iid=None):
        iid = iid or self.cfg.get("active_instance")
        with self._runners_lock:
            r = self._runners.get(iid)
            if r is None:
                r = InstanceRunner(self, iid)
                self._runners[iid] = r
            return r

    def instance_cfg(self, iid):
        return cm.instance_cfg(self.cfg, iid)

    def _instance_set_root(self, iid, root):
        inst = cm.find_instance(self.cfg, iid)
        if inst is None:
            return
        if iid == self.cfg.get("active_instance"):
            self.cfg["webui_root"] = root
        inst["webui_root"] = root
        try:
            cm.save_config(self.cfg)
        except OSError:
            pass

    def _runner_ports_in_use(self, except_iid):
        """其他正在运行的实例占用的端口（多开时分配端口要避开）"""
        ports = set()
        for iid, r in list(self._runners.items()):
            if iid == except_iid or not r.running():
                continue
            if r.port:
                ports.add(int(r.port))
            else:
                # 端口还没确定（启动中）：把它可能用到的端口都算上
                c = self.instance_cfg(iid) or {}
                p = str(c.get("port", "")).strip()
                ports.add(int(p) if p.isdigit() else r._base_port())
        return ports

    def _instance_launch_extras(self, iid, cfg, log):
        """启动前把共享模型库挂到实例上（见 _library_launch_extras）"""
        _library_launch_extras(self, iid, cfg, log)

    # 以下几个是 V2 的接口，iid 省略 = 当前实例；前端启动页照旧调用
    def launch_status(self, iid=None):
        return self._runner(iid).status()

    def launch_env_detect(self, root, iid=None):
        return self._runner(iid).env_detect(root)

    def launch_precheck(self, root, iid=None):
        return self._runner(iid).precheck(root)

    def launch_start(self, root, fix_overrides=False, kill_pid=0, iid=None):
        return self._runner(iid).start(root, fix_overrides, kill_pid)

    def launch_stop(self, iid=None):
        return self._runner(iid).stop()


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
    # 系统里没装 Python/Git 不再是部署的门槛：检测不到就在部署时自动下载
    # 便携版补上（见 _deploy_flow 的 need_python/need_git 兜底），所以这里
    # 直接如实告知"将自动下载"，而不是叫用户自己去装
    git_text = (f"已检测到 ({_ver([git_path, '--version']) or git_path})" if git_path
                else "未检测到系统 Git——不影响，部署时会自动下载便携版 Git")
    py_text = (f"已检测到 ({_ver([py_path, '--version']) or py_path})" if py_path
               else "未检测到系统 Python——不影响，部署时会自动下载便携版 Python")
    return {
        "ok": True,
        "git": {"found": True, "text": git_text},
        "python": {"found": True, "text": py_text},
    }


def _api_deploy_check_dir(self, target):
    target = (target or "").strip()
    if not target:
        return {"ok": True, "status": "warn", "message": "请先选择目录"}
    webui_bat = os.path.join(target, "webui.bat")
    if cm.comfy_layout(target)[0]:
        return {"ok": True, "status": "ok",
                "message": "检测到该目录已经是一个 ComfyUI 安装，无需重新克隆，"
                           "可直接点击部署来补齐依赖（请把上面的分支选成 ComfyUI）"}
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
    if branch == "comfyui":
        already_installed = bool(cm.comfy_layout(target)[0])
    else:
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

    # 系统没装 Python/Git 不再拦截部署：部署流程会自动下载便携版补上
    # （_deploy_flow 里的 need_python/need_git 兜底），这里只作提醒
    missing = []
    if not ((self.cfg.get("custom_git_path") or "").strip() or shutil.which("git")):
        missing.append("Git")
    if not (shutil.which("python") or shutil.which("python3")):
        missing.append("Python")
    if missing:
        issues.append({
            "level": "warn", "id": "auto_portable_env",
            "text": f"没有检测到系统 {' 和 '.join(missing)}。不用担心——部署时会自动"
                    "下载便携版补上（约 100~150MB，只装进安装目录，不影响系统）。",
        })

    # 硬件/磁盘/杀软等环境预检（几秒就能查完，放到下载几个 GB 之前）
    try:
        import deploy_preflight as pf
        issues.extend(pf.precheck_issues(target))
    except Exception:
        pass

    has_error = any(i["level"] == "error" for i in issues)
    return {"ok": not has_error, "issues": issues, "already_installed": already_installed}


def _api_deploy_start(self, target, branch, use_portable, comfy_nodes_sel=None):
    # 检查和置位必须原子，否则双击会并发跑两个部署线程
    with self._deploy_lock:
        if self._deploy_running:
            return {"ok": False, "error": "已有部署任务在进行中"}
        target = (target or "").strip()
        if not target:
            return {"ok": False, "error": "请先选择安装目录"}
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"无法创建目录：\n{target}\n\n{e}\n\n"
                                          "常见原因：盘符不存在、路径不合法、或没有写入权限。"}

        # 部署用一份独立配置：装到别的目录（= 新实例）时不能沿用当前实例的
        # Python/Git 路径和启动参数，也不能提前改掉当前实例的分支
        self._deploy_cfg = _deploy_cfg_for(self, target, branch)
        if branch == "comfyui" and isinstance(comfy_nodes_sel, list):
            # 记住这次的勾选，下次部署默认还是它
            self.cfg["deploy_comfy_nodes"] = [i for i in comfy_nodes_sel if comfy_nodes.find(i)]
            self._deploy_cfg["deploy_comfy_nodes"] = self.cfg["deploy_comfy_nodes"]
            try:
                cm.save_config(self.cfg)
            except Exception:
                pass

        self._deploy_cancel.clear()
        self._deploy_running = True
    self._emit("deploy", "state", running=True)
    try:
        self._spawn(lambda: self._deploy_flow(target, branch, use_portable), name="deploy")
    except Exception:
        # 线程没能起来就回滚状态，否则 _deploy_running 永远卡在 True，
        # 之后每次都只提示"已有部署任务在进行中"
        self._deploy_running = False
        self._emit("deploy", "state", running=False)
        return {"ok": False, "error": "无法启动部署线程：\n" + traceback.format_exc()[-500:]}
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
    if branch == "comfyui":
        return _deploy_flow_comfy(self, target, use_portable)
    log = lambda t: self._emit("deploy", "log", text=t)
    progress = lambda d, t: self._emit("deploy", "progress", downloaded=d, total=t)
    try:
        # 环境预警：杀软拦截和时钟错乱是部署失败的两大隐形杀手，先告诉用户
        try:
            import deploy_preflight as pf
            avs = pf.detect_antivirus()
            if avs:
                log(f"[环境] 检测到安全软件运行中（{'、'.join(avs)}），"
                    "如遇拦截/查杀弹窗请选择「允许」或「信任」\n")
            skew = pf.clock_skew_seconds()
            if skew and skew > 300:
                log(f"[环境] 警告：系统时钟偏差约 {int(skew / 60)} 分钟，"
                    "所有 HTTPS 证书校验都会失败——请先到系统设置里校准时间\n")
        except Exception:
            pass

        # 子进程环境：消毒（剥掉 PYTHON*/PIP_*/GIT_* 等污染变量）+ 启动器
        # 覆盖变量 + git 交互隔离。部署专用：TEMP/缓存重定向到目标盘，
        # 防 C 盘 TEMP 爆满（pip 装 torch 的临时文件好几个 GB）
        def make_env():
            e = cm.build_subprocess_env(self._deploy_cfg, target)
            try:
                tmp_dir = os.path.join(target, ".launcher_tmp")
                os.makedirs(tmp_dir, exist_ok=True)
                e["TEMP"] = tmp_dir
                e["TMP"] = tmp_dir
                # pip 缓存放在启动器自己的目录里，所有实例共用：部署第二个实例几乎不用重新下载
                e["PIP_CACHE_DIR"] = SHARED_CACHE_PIP
            except OSError:
                pass
            return e
        _drop_broken_bundled_git(target, log)
        deploy_env = make_env()

        bundled_python = os.path.join(target, "python", "python.exe")
        bundled_git = os.path.join(target, "git", "cmd", "git.exe")
        # 便携版下载的触发条件：用户勾选了便携环境，或者系统里根本检测不到。
        # 后者是自动兜底——没装 Python/Git 的电脑不该被一句"请先安装"拦在门外
        custom_git = (self._deploy_cfg.get("custom_git_path") or "").strip().strip('"')
        has_sys_git = bool(shutil.which("git")) or bool(custom_git and os.path.exists(custom_git))
        has_sys_python = bool(shutil.which("python") or shutil.which("python3"))
        need_python = not os.path.exists(bundled_python) and (use_portable or not has_sys_python)
        need_git = not os.path.exists(bundled_git) and (use_portable or not has_sys_git)

        # ---- 1. 便携版 Python / Git ----
        if need_python or need_git:
            if not use_portable:
                log("[部署] 系统未检测到 Python/Git，自动改为下载便携版补齐\n")
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
        custom_git = (self._deploy_cfg.get("custom_git_path") or "").strip().strip('"')
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
                if mm.resolve_github_mode(self._deploy_cfg, log):
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
                rc = self._deploy_run_cmd(program, args, cwd, log, env=deploy_env)
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
                    git_exe, ["-C", target, "config", "remote.origin.url", u],
                    parent, log, env=deploy_env)
                if rc != 0:
                    log(f"\n[部署] 设置远程地址失败（退出码 {rc}），请检查日志\n")
                    return
                rc = self._deploy_run_cmd(
                    git_exe, ["-C", target, "-c", "http.lowSpeedLimit=1000",
                              "-c", "http.lowSpeedTime=120", "fetch", "origin", ref],
                    parent, log, env=deploy_env, timeout=1800)
                if rc == 0:
                    fetched = True
                    break
                log(f"\n[部署] 从该地址拉取失败（退出码 {rc}），换下一个地址重试 ...\n")
            if not fetched:
                log("\n[部署] 所有候选地址都拉取失败，请检查网络/代理设置后重新点击「开始部署」\n")
                self._emit("deploy", "error", title="源码下载失败",
                           text="从 GitHub 及所有加速代理拉取 Forge 源码都失败了。"
                                "请检查网络/代理后重新点击「开始部署」。\n\n" + PAN_FALLBACK_TEXT)
                return

            if self._deploy_cancel.is_set():
                raise _DeployCancelled()
            rc = self._deploy_run_cmd(
                git_exe, ["-C", target, "checkout", "-f", "-B", ref, f"origin/{ref}"],
                parent, log, env=deploy_env)
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
        # 先修 pyvenv.cfg 的过期 home 路径（目录被移动/改名后 venv 启动桩会
        # 失效），再判断 venv 是否残缺——顺序反了会把完好的 venv 误删
        resolved_python = (self._deploy_cfg.get("custom_python_path") or "").strip()
        if not resolved_python:
            resolved_python = cm.detect_bundled_python(target) or ""
        if resolved_python:
            try:
                cm.sync_venv_pyvenv_cfg(target, resolved_python, lambda m: log(m + "\n"))
            except OSError as e:
                log(f"[部署] 检查 venv pyvenv.cfg 时出错（不影响继续部署）: {e}\n")
        # 确认损坏的 venv（缺 python.exe/pip）没有保留价值，先清掉再做版本判断；
        # 检测超时/失败的不算确认损坏，不删
        if cm.venv_is_definitely_broken(venv_dir):
            log(f"[部署] 检测到残缺的 venv（缺 python.exe 或 pip），删除后按需重建: {venv_dir}\n")
            shutil.rmtree(venv_dir, ignore_errors=True)
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

        # ---- 3.5 torch 预下载（断点续传 + 多源轮换，绕开 pip 下载器）----
        # pip 下 2GB+ 的 wheel 没有断点续传，下到 90% 断线就整个重来；
        # 启动器先自己把 wheel 拉下来（Range 续传 + sha256 校验 + 多个镜像
        # 按顺序试），本地装好后 Forge 检测到 torch 已存在就会跳过自己安装。
        # 任何失败都不阻断——Forge 的自装路径（已注入 TORCH_INDEX_URL）兜底。
        try:
            import torch_bootstrap
            torch_bootstrap.ensure_torch(target, self._deploy_cfg, log, progress,
                                         self._deploy_cancel)
        except _DeployCancelled:
            raise
        except Exception as e:
            log(f"[部署] torch 预下载异常（不影响继续，Forge 会自行安装）: {e}\n")

        if self._deploy_cancel.is_set():
            raise _DeployCancelled()

        # ---- 4. 运行一次 webui.bat，让 Forge 自己建 venv 装依赖 ----
        env_overrides = cm.build_launch_env_overrides(self._deploy_cfg, target)
        log("\n[部署] 首次运行 webui.bat（自动安装依赖，可能需要较长时间）\n")
        log(f"[部署] COMMANDLINE_ARGS = {env_overrides.get('COMMANDLINE_ARGS', '')}\n")
        for k, v in env_overrides.items():
            if k != "COMMANDLINE_ARGS":
                log(f"[部署] {k} = {v}\n")
        log("\n")

        # 注意：环境在 clone 完之后重新构造——torch 索引镜像要从克隆下来的
        # launch_utils.py 里提取 cuda tag，流程开头构造时源码还不存在
        env = make_env()
        # 注意：webui.bat 装完依赖会真的把 WebUI 服务起起来，然后一直占着
        # 进程不退出。部署的目标是把环境装到「能启动」，所以流式读输出、
        # 出现监听地址就判定成功并停掉这个临时进程继续收尾——否则部署会
        # 永远卡在这里（之前唯一的出路是点「取消」，而取消又被当成失败
        # 处理，配置写回/hashlib 补丁全部跳过）。
        ready, tail = self._deploy_run_webui_until_ready(target, log, env)
        if not ready and not self._deploy_cancel.is_set():
            # 失败分类：只有特征符合网络问题才换源重试（镜像→官方），
            # 其他错误（版本冲突/磁盘满/权限）换源纯属浪费时间
            if _looks_like_network_failure(tail) and env.get("TORCH_INDEX_URL"):
                log("\n[部署] 失败特征疑似网络问题，自动改用官方源重试一次 ...\n")
                env2 = dict(env)
                env2.pop("TORCH_INDEX_URL", None)  # 回到 Forge 默认官方源
                ready, tail = self._deploy_run_webui_until_ready(target, log, env2)
        if not ready:
            if self._deploy_cancel.is_set():
                raise _DeployCancelled()
            log("\n[部署] 依赖安装或启动失败（上面的日志应有具体报错），"
                "排查修复后可以重新点击「开始部署」继续（已完成的步骤会被跳过）\n")
            self._emit("deploy", "error", title="部署失败",
                       text="依赖安装或启动失败（部署日志里应有具体报错）。"
                            "排查修复后重新点击「开始部署」即可继续，已完成的步骤会自动跳过。\n\n"
                            + PAN_FALLBACK_TEXT)
            return
        log("\n[部署] WebUI 已能正常启动（验证用临时进程已停止），继续收尾 ...\n")

        # ---- 5. hashlib 兼容补丁（保险步骤，新版 Python 下补丁自动不生效）----
        # 有 venv 写 venv 里；走 VENV_DIR=-（没用 venv）时写进便携 Python 本体
        patch_target = venv_dir if os.path.isdir(venv_dir) else os.path.join(target, "python")
        pe.write_hashlib_patch(patch_target, log_cb=lambda m: log(m + "\n"))
        # 补丁属于"收尾改动"，写完后必须复测——上面验证通过的是打补丁前的状态
        try:
            smoke_py = (os.path.join(venv_dir, "Scripts", "python.exe")
                        if os.path.isdir(venv_dir)
                        else os.path.join(target, "python", "python.exe"))
            if os.path.exists(smoke_py):
                r = subprocess.run(
                    [smoke_py, "-c",
                     "import hashlib; assert hasattr(hashlib, 'file_digest')"],
                    capture_output=True, timeout=60, creationflags=_NO_WINDOW)
                if r.returncode == 0:
                    log("[部署] hashlib 补丁验证通过\n")
                else:
                    log("[部署] 警告：hashlib 补丁未生效，如果启动报 "
                        "hashlib has no attribute file_digest，"
                        "请点本页的「写入 hashlib 兼容补丁」重试\n")
        except Exception as e:
            log(f"[部署] hashlib 补丁验证异常（不影响结果）: {e}\n")

        # ---- 6. 收尾：路径/分支写回配置，通知前端刷新 ----
        _deploy_register_instance(self, target, branch, log)
        log("\n[部署] 全部完成！\n")
        self._emit("deploy", "done", target=target, branch=branch)
    except _DeployCancelled:
        log("\n[部署] 已取消\n")
        self._emit("deploy", "cancelled")
    except pe.PortableEnvError as e:
        log(f"\n[部署] 便携环境下载失败: {e}\n")
        self._emit("deploy", "error", title="便携环境下载失败",
                   text=str(e) + "\n\n" + PAN_FALLBACK_TEXT)
    except Exception:
        detail = traceback.format_exc()
        log(f"\n[部署] 发生未预期的错误:\n{detail}\n")
        self._emit("deploy", "error", title="部署失败",
                   text=detail[-1500:] + "\n\n" + PAN_FALLBACK_TEXT)
    finally:
        self._deploy_running = False
        self._deploy_proc = None
        self._emit("deploy", "state", running=False)


def _deploy_portable_env(self, target, branch, need_python, need_git, log, progress):
    # 下载的便携 Python / Git 安装包缓存在启动器目录，所有实例共用；
    # 已有且校验通过就直接用，部署第二个实例不用再下载一遍
    tmp = SHARED_CACHE_PORTABLE
    os.makedirs(tmp, exist_ok=True)
    if True:
        if need_python:
            version_prefix = pe.PYTHON_VERSION_BY_BRANCH.get(branch, "3.10")
            log(f"[便携环境] 查询 python-build-standalone 最新版本 (目标 Python {version_prefix}.x) ...\n")
            net_log = lambda m: log(m + "\n")
            tag = pe.get_latest_pbs_tag(cfg=self._deploy_cfg, log_cb=net_log)
            # 防篡改：先取 release 官方清单 SHA256SUMS 作为哈希基准，
            # 下载完必须校验通过才会解压/执行（加速代理是第三方中间人）。
            log("[便携环境] 获取官方校验清单 SHA256SUMS ...\n")
            sha_map = pe.fetch_pbs_sha256sums(tag, cfg=self._deploy_cfg, log_cb=net_log)
            assets = pe.list_release_assets(pe.PBS_REPO, tag, cfg=self._deploy_cfg, log_cb=net_log)
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
            if not _cached_archive_ok(archive_path, expected, log):
                pe.download_file(asset["url"], archive_path,
                                 progress_cb=progress,
                                 cancel_flag=self._deploy_cancel.is_set,
                                 cfg=self._deploy_cfg, log_cb=lambda m: log(m + "\n"))
            pe.verify_downloaded_file(archive_path, expected,
                                      what=f"Python 归档 {asset['name']}")
            log("[便携环境] 校验通过，开始解压\n")
            python_exe = pe.extract_python_tar(archive_path, target,
                                               log_cb=lambda m: log(m + "\n"))
            log(f"[便携环境] Python 部署完成: {python_exe}\n")
        if self._deploy_cancel.is_set():
            raise _DeployCancelled()
        if need_git:
            _deploy_portable_git(self, target, tmp, log, progress)


def _deploy_portable_git(self, target, tmp, log, progress):
    """
    下载并解压便携 Git，解压后必须真的能跑（git --version）才算成功。

    Git for Windows 新版偶尔在部分电脑上跑不起来（2.56 刚把主程序从 mingw64
    挪到 ucrt64；新发布的程序装机量低，也更容易被杀软按"信誉"拦截），表现为
    cmd\\git.exe 报 "error launching git: ????"。所以从最新版开始，跑不起来就
    自动退回上一个正式版，最多试 3 个；都不行再看系统里有没有能用的 Git。
    """
    net_log = lambda m: log(m + "\n")
    git_root = os.path.join(target, "git")
    log("[便携环境] 查询 git-for-windows 最近的版本 ...\n")
    try:
        releases = pe.list_recent_releases(pe.GIT_REPO, 4, cfg=self._deploy_cfg, log_cb=net_log)
    except pe.PortableEnvError:
        releases = []
    if not releases:   # 列表接口不通时退回只查最新版（老逻辑）
        assets, body = pe.list_release_assets_with_meta(pe.GIT_REPO, tag=None, cfg=self._deploy_cfg, log_cb=net_log)
        releases = [("latest", assets, body)]
    failures = []
    tried = 0
    for tag, assets, body in releases:
        asset = pe.pick_git_asset(assets)
        if not asset:
            continue
        expected = pe.pick_git_asset_sha256(body, asset["name"])
        if not expected:
            log(f"[便携环境] {asset['name']} 的发布信息里没有校验和，跳过这个版本\n")
            continue
        if tried >= 3:
            break
        tried += 1
        if self._deploy_cancel.is_set():
            raise _DeployCancelled()
        log(f"[便携环境] 下载 {asset['name']} ...\n")
        archive_path = os.path.join(tmp, asset["name"])
        if not _cached_archive_ok(archive_path, expected, log):
            pe.download_file(asset["url"], archive_path,
                             progress_cb=progress,
                             cancel_flag=self._deploy_cancel.is_set,
                             cfg=self._deploy_cfg, log_cb=net_log)
        pe.verify_downloaded_file(archive_path, expected, what=f"Git 安装包 {asset['name']}")
        # 自解压包执行前再验 Authenticode 签名（双保险）
        pe.verify_pe_signature(archive_path, log_cb=net_log)
        shutil.rmtree(git_root, ignore_errors=True)   # 上一个版本没跑起来的残留先清掉
        git_exe = pe.extract_portable_git(archive_path, target, log_cb=net_log)
        ok, detail = pe.check_portable_git(git_root)
        if ok:
            log(f"[便携环境] Git 部署完成: {git_exe}（{detail}）\n")
            return
        failures.append(f"{asset['name']}：{detail}")
        log(f"[便携环境] {asset['name']} 解压好了但在这台电脑上运行不了：\n    {detail}\n"
            "    改用上一个版本再试 ...\n")
    shutil.rmtree(git_root, ignore_errors=True)
    sys_ok, sys_info = pe.system_git_ok()
    if sys_ok:
        log(f"[便携环境] 便携 Git 都运行不了，改用系统里已安装的 Git：{sys_info}\n")
        return
    raise pe.PortableEnvError(
        "便携 Git 解压后无法运行：\n  " + "\n  ".join(failures or ["没有找到可用的 PortableGit 安装包"]) +
        "\n\n常见原因是杀毒软件（360、火绒、Windows 安全中心等）拦截或删除了 Git 的程序文件。"
        "可以把安装目录加入杀软白名单后重新点「开始部署」；"
        "或者先安装官方 Git（https://git-scm.com/download/win，一路下一步即可），"
        "再取消勾选「自动下载便携版 Python + Git」重新部署。")


def _drop_broken_bundled_git(target, log):
    """目标目录里已有的便携 Git 跑不起来就删掉，让部署重新准备（否则会一直用坏的那份）"""
    git_root = os.path.join(target, "git")
    if not os.path.exists(os.path.join(git_root, "cmd", "git.exe")):
        return
    ok, detail = pe.check_portable_git(git_root)
    if ok:
        return
    log(f"[部署] 目录里已有的便携 Git 运行不了（{detail}），删除后重新准备\n")
    shutil.rmtree(git_root, ignore_errors=True)


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
    首次运行 webui.bat：装依赖（建 venv 或直接装进便携 Python，取决于
    build_launch_env_overrides 给出的 VENV_DIR）、把 WebUI 起起来验证能跑通。

    返回 (ready, tail)：ready 表示成功（输出里出现了 "Running on local URL"，
    或端口探测发现 WebUI 已在监听——有些 webui.bat 会把输出重定向走，管道里
    一个字节都没有，靠解析输出判断就绪会永远卡住；此时停掉这个临时进程，
    部署继续收尾）；tail 是进程输出的最后 8KB，供调用方做失败原因分类
    （网络问题换源重试，其他问题直接报错）。
    失败：进程在就绪之前就退出了，或超过 timeout 秒的总时限——webui.bat
    失败路径的 exit code 并不可靠，不能拿返回码当判断依据；总时限默认
    5400 秒 = 90 分钟，torch 几个 GB 的慢网也要装得下）。

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
        return False, ""
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
    cfg_port = str(self._deploy_cfg.get("port", "")).strip()
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
                        with socket.create_connection(("127.0.0.1", p), timeout=0.5) as s:
                            # 光 TCP 连通不够——可能是误占端口的其他服务，
                            # 发个 HTTP 请求确认对端真的是 Web 服务
                            s.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                            resp = s.recv(64)
                        if not resp.startswith(b"HTTP/"):
                            continue
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
            tail = combined[-8000:]
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
    return ready, tail


# webui.bat 失败输出里的网络类特征。只有这些才值得换源重试；
# 版本冲突/磁盘满/权限错误换源纯属浪费时间。
_NETWORK_FAIL_RE = re.compile(
    r"ReadTimeout|ConnectTimeout|ConnectionError|SSLError|"
    r"Failed to establish|Connection reset|timed out|"
    r"Couldn't install PyTorch|No matching distribution|"
    r"Temporary failure|10054|10060|10061", re.IGNORECASE)


# ============================================================
# 输出管理器（V3 阶段 5）
# ============================================================

def _outidx(self):
    """索引和文件服务都是用到时才建，不拖慢启动"""
    if getattr(self, "_out_index", None) is None:
        self._out_index = oi.OutputIndex(char_dirs=[os.path.join(APP_DIR, "wd_tagger_models")])
        self._out_server, self._out_base, self._out_token = oi.start_file_server(self._out_index)
        self._out_scan_thread = None
    return self._out_index


def _output_sources(self):
    out = []
    for iid, _name, c in _all_instance_cfgs(self):
        info = instance_output_info(c)
        if info:
            out.append((iid, info["scan"]))
    return out


def _api_outputs_info(self):
    idx = _outidx(self)
    insts = [{"id": iid, "name": n} for iid, n, _c in _all_instance_cfgs(self)]
    return {"ok": True, "base": self._out_base, "token": self._out_token, "count": idx.count(),
            "scanning": idx.scanning, "instances": insts, "multi": cm.multi_enabled(self.cfg),
            "collections": idx.collections(), "fts": idx.fts,
            "char_dict": len(idx.chars.names),
            "running": _any_instance_running(self),
            "dirs": [d for _iid, ds in _output_sources(self) for d in ds]}


def _api_outputs_scan(self):
    idx = _outidx(self)
    if self._out_scan_thread and self._out_scan_thread.is_alive():
        return {"ok": True, "busy": True}

    def work():
        res = idx.scan(_output_sources(self),
                       progress=lambda p: self._emit("outputs", "scan_progress", **p))
        self._emit("outputs", "scan_done", count=idx.count(), **res)
    self._out_scan_thread = self._spawn(work, name="outputs-scan")
    return {"ok": True}


def _api_outputs_query(self, filters):
    return {"ok": True, **_outidx(self).query(filters or {})}


def _api_outputs_facets(self, filters):
    return {"ok": True, **_outidx(self).facets(filters or {})}


def _api_outputs_detail(self, oid):
    d = _outidx(self).detail(int(oid))
    if not d:
        return {"ok": False, "error": "记录不存在"}
    names = {iid: n for iid, n, _c in _all_instance_cfgs(self)}
    d["instance_name"] = names.get(d["iid"], "")
    d["size_text"] = _fmt_size(d["size"] or 0)
    d["time_text"] = datetime.fromtimestamp(d["mtime"] or 0).strftime("%Y-%m-%d %H:%M:%S")
    d["exists"] = bool(d["path"] and os.path.isfile(d["path"]))
    return {"ok": True, **d}


def _api_outputs_reveal(self, oid):
    path, _m = _outidx(self).path_of(int(oid))
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "文件已不存在"}
    return self.reveal_in_explorer(path)


def _api_outputs_open(self, oid):
    path, _m = _outidx(self).path_of(int(oid))
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "文件已不存在"}
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 - 用系统默认程序打开用户自己的出图
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _api_outputs_delete(self, oids):
    idx = _outidx(self)
    done, failed = 0, []
    for oid in oids or []:
        path, _m = idx.path_of(int(oid))
        if path and os.path.exists(path):
            if not ml.send_to_trash(path):
                failed.append(os.path.basename(path))
                continue
        idx.remove(int(oid))
        done += 1
    return {"ok": True, "deleted": done, "failed": failed}


def _api_outputs_collections(self):
    return {"ok": True, "collections": _outidx(self).collections()}


def _api_outputs_collection_op(self, op, cid=None, name="", oids=None, on=True):
    idx = _outidx(self)
    try:
        if op == "create":
            cid = idx.collection_create(name)
        elif op == "rename":
            idx.collection_rename(cid, name)
        elif op == "delete":
            idx.collection_delete(cid)
        elif op == "set":
            idx.collection_set(cid, [int(o) for o in (oids or [])], bool(on))
        else:
            return {"ok": False, "error": "未知操作"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "id": cid, "collections": idx.collections()}


def _api_outputs_collection_export(self, cid, dest):
    dest = (dest or "").strip()
    if not dest:
        return {"ok": False, "error": "请选择导出位置"}
    paths = _outidx(self).collection_paths(cid)
    if not paths:
        return {"ok": False, "error": "收藏夹是空的"}

    def work():
        n, failed = 0, 0
        os.makedirs(dest, exist_ok=True)
        for i, p in enumerate(paths, 1):
            target = os.path.join(dest, os.path.basename(p))
            if os.path.exists(target):
                root, ext = os.path.splitext(target)
                k = 1
                while os.path.exists(f"{root}_{k}{ext}"):
                    k += 1
                target = f"{root}_{k}{ext}"
            try:
                shutil.copy2(p, target)
                n += 1
            except OSError:
                failed += 1
            self._emit("outputs", "export_progress", i=i, n=len(paths))
        self._emit("outputs", "export_done", copied=n, failed=failed, dest=dest)
    self._spawn(work, name="outputs-export")
    return {"ok": True, "count": len(paths)}


# ============================================================
# 实例管理（V3）
# ============================================================

def _guess_forge_branch(root):
    """从 .git/config 的远程地址猜 Forge 分支：Haoming02 的仓库 = Neo，否则 Classic"""
    try:
        with open(os.path.join(root, ".git", "config"), "r", encoding="utf-8", errors="replace") as f:
            txt = f.read().lower()
        if "sd-webui-forge-classic" in txt:
            return "neo2"
        if "stable-diffusion-webui-forge" in txt:
            return "classic"
    except OSError:
        pass
    return "neo2"


def _instances_payload(self):
    cm.absorb_active(self.cfg)
    out = []
    for inst in self.cfg.get("instances") or []:
        iid = inst["id"]
        r = self._runners.get(iid)
        st = r.status() if r else {"iid": iid, "running": False, "state": "idle",
                                   "url": None, "text": "尚未启动", "port": None}
        out.append({
            "id": iid, "name": inst.get("name", ""),
            "branch": inst.get("webui_branch", ""),
            "kind": "comfyui" if inst.get("webui_branch") == "comfyui" else "forge",
            "kind_label": cm.KIND_LABELS.get(inst.get("webui_branch"), "WebUI"),
            "root": inst.get("webui_root", ""),
            "port": inst.get("port", ""),
            "active": iid == self.cfg.get("active_instance"),
            "status": st,
        })
    return {"instances": out, "active": self.cfg.get("active_instance"),
            "multi": cm.multi_enabled(self.cfg),
            "multi_manual": bool(self.cfg.get("multi_instance_ui"))}


def _api_instances_list(self):
    return {"ok": True, **_instances_payload(self)}


def _api_instance_add(self, root, name=""):
    root = (root or "").strip().strip('"')
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "目录不存在"}
    kind = cm.detect_kind(root)
    if not kind:
        return {"ok": False, "error": "这个目录看起来既不是 ComfyUI（没有 main.py）也不是 WebUI"
                                      "（没有 webui.bat）。还没安装的话，去「环境部署」页部署一个。"}
    for inst in self.cfg.get("instances") or []:
        if inst.get("webui_root") and _same_path(inst["webui_root"], root):
            return {"ok": False, "error": f"这个目录已经是实例「{inst.get('name')}」了"}
    branch = "comfyui" if kind == "comfyui" else _guess_forge_branch(root)
    # 当前实例还是空的（新用户第一次添加）→ 直接填进当前实例
    if not (self.cfg.get("webui_root") or "").strip() and len(self.cfg.get("instances") or []) == 1:
        self.cfg["webui_root"] = root
        self.cfg["webui_branch"] = branch
        cm.active_instance(self.cfg)["name"] = (name or "").strip() or cm.KIND_LABELS.get(branch, "WebUI")
        cm.save_config(self.cfg)
        self._emit("instances", "changed")
        return {"ok": True, "id": self.cfg["active_instance"], **_instances_payload(self)}
    inst = cm.make_instance(None, webui_root=root, webui_branch=branch,
                            name=(name or "").strip() or _unique_instance_name(
                                self, cm.KIND_LABELS.get(branch, "WebUI")))
    self.cfg["instances"].append(inst)
    cm.save_config(self.cfg)
    self._emit("instances", "changed")
    return {"ok": True, "id": inst["id"], **_instances_payload(self)}


def _api_instance_remove(self, iid):
    insts = self.cfg.get("instances") or []
    if len(insts) <= 1:
        return {"ok": False, "error": "至少要保留一个实例"}
    r = self._runners.get(iid)
    if r and r.running():
        return {"ok": False, "error": "这个实例正在运行，请先停止"}
    inst = cm.find_instance(self.cfg, iid)
    if not inst:
        return {"ok": False, "error": "实例不存在"}
    cm.absorb_active(self.cfg)
    insts.remove(inst)
    self._runners.pop(iid, None)
    if self.cfg.get("active_instance") == iid:
        self.cfg["active_instance"] = insts[0]["id"]
        cm.project_active(self.cfg)
    cm.save_config(self.cfg)
    self._emit("instances", "changed")
    return {"ok": True, "switched": self.cfg["active_instance"] != iid, **_instances_payload(self)}


def _api_instance_update(self, iid, changes):
    """实例管理页可以直接改的几项：名字 / 端口 / 根目录"""
    inst = cm.find_instance(self.cfg, iid)
    if not inst or not isinstance(changes, dict):
        return {"ok": False, "error": "实例不存在"}
    cm.absorb_active(self.cfg)
    for k in ("name", "port", "webui_root"):
        if k in changes:
            v = str(changes[k] or "").strip()
            if k == "port" and v and not v.isdigit():
                return {"ok": False, "error": "端口必须是数字"}
            if k == "name" and not v:
                continue
            inst[k] = v
            if k == "webui_root" and v:
                kind = cm.detect_kind(v)
                if kind == "comfyui":
                    inst["webui_branch"] = "comfyui"
                elif kind == "forge" and inst.get("webui_branch") == "comfyui":
                    inst["webui_branch"] = _guess_forge_branch(v)
    if iid == self.cfg.get("active_instance"):
        cm.project_active(self.cfg)
    cm.save_config(self.cfg)
    self._emit("instances", "changed")
    return {"ok": True, **_instances_payload(self)}


def _api_instance_switch(self, iid):
    if not cm.find_instance(self.cfg, iid):
        return {"ok": False, "error": "实例不存在"}
    cm.absorb_active(self.cfg)
    self.cfg["active_instance"] = iid
    cm.project_active(self.cfg)
    cm.save_config(self.cfg)
    return {"ok": True}


def _api_instance_config_get(self, iid):
    """高级选项页按实例读取：全局键 + 该实例的实例键的合并视图"""
    cfg = cm.instance_cfg(self.cfg, iid)
    if cfg is None:
        return {"ok": False, "error": "实例不存在"}
    return {"ok": True, "iid": iid, "config": cfg,
            "cmd_args": cm.build_commandline_args(cfg)}


def _api_instance_config_update(self, iid, changes):
    """高级选项页按实例保存：只接受实例级键，写进指定实例（可以不是当前实例）"""
    inst = cm.find_instance(self.cfg, iid)
    if not inst or not isinstance(changes, dict):
        return {"ok": False, "error": "实例不存在"}
    # 先把当前实例的顶层投影收回去，避免 save_config 时把旧值盖回来
    cm.absorb_active(self.cfg)
    for k, v in changes.items():
        if k in cm.INSTANCE_KEYS:
            inst[k] = v
    if iid == self.cfg.get("active_instance"):
        cm.project_active(self.cfg)
    cm.save_config(self.cfg)
    eff = cm.instance_cfg(self.cfg, iid)
    return {"ok": True, "cmd_args": cm.build_commandline_args(eff)}


def _api_set_multi_ui(self, on):
    self.cfg["multi_instance_ui"] = bool(on)
    cm.save_config(self.cfg)
    return {"ok": True, **_instances_payload(self)}


def _api_set_hidden_pages(self, pages):
    allowed = {"settings", "launcher", "deploy", "civitai", "models", "extensions", "wd14", "meta", "outputs"}
    self.cfg["hidden_pages"] = [p for p in (pages or []) if p in allowed]
    cm.save_config(self.cfg)
    return {"ok": True, "hidden_pages": self.cfg["hidden_pages"]}


# ============================================================
# 部署：共享缓存 / 实例登记 / ComfyUI
# ============================================================
SHARED_CACHE_DIR = os.path.join(APP_DIR, "launcher_data", "cache")
SHARED_CACHE_PIP = os.path.join(SHARED_CACHE_DIR, "pip")
SHARED_CACHE_PORTABLE = os.path.join(SHARED_CACHE_DIR, "portable")

COMFY_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
COMFY_REF = "master"
# ComfyUI 官方 README 推荐的 CUDA 版本 torch；RTX 50 系需要 cu128 及以上
COMFY_TORCH_TAG = "cu128"


def _cached_archive_ok(path, expected_sha, log):
    """共享缓存里已有同名安装包且哈希对得上 → 直接复用，不用再下载"""
    if not os.path.isfile(path):
        return False
    try:
        if pe.sha256_file(path).lower() == (expected_sha or "").lower():
            log(f"[便携环境] 复用已缓存的安装包：{os.path.basename(path)}\n")
            return True
    except OSError:
        pass
    try:
        os.remove(path)     # 半截或损坏的旧文件，删掉重下
    except OSError:
        pass
    return False


def _same_path(a, b):
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def _deploy_cfg_for(self, target, branch):
    """部署用配置：目标就是当前实例的目录 → 沿用当前实例设置；否则用默认实例设置"""
    cfg = dict(self.cfg)
    active_root = (self.cfg.get("webui_root") or "").strip()
    if not (active_root and _same_path(active_root, target)):
        for k in cm.INSTANCE_KEYS:
            cfg[k] = cm.DEFAULT_CONFIG.get(k)
    cfg["webui_branch"] = branch
    cfg["webui_root"] = target
    return cfg


def _deploy_register_instance(self, target, branch, log):
    """
    部署成功后登记实例：
      - 已有实例就是这个目录 → 更新它的分支
      - 当前实例还没设置目录（新用户）→ 直接用当前实例
      - 否则新建一个实例（实例数变成 2，多实例功能自动出现）
    """
    for inst in self.cfg.get("instances") or []:
        if inst.get("webui_root") and _same_path(inst["webui_root"], target):
            inst["webui_branch"] = branch
            if inst["id"] == self.cfg.get("active_instance"):
                self.cfg["webui_branch"] = branch
            cm.save_config(self.cfg)
            self._emit("instances", "changed")
            return inst["id"]
    if not (self.cfg.get("webui_root") or "").strip():
        self.cfg["webui_root"] = target
        self.cfg["webui_branch"] = branch
        inst = cm.active_instance(self.cfg)
        if inst is not None:
            inst["name"] = cm.KIND_LABELS.get(branch, inst.get("name", ""))
        cm.save_config(self.cfg)
        self._emit("instances", "changed")
        return self.cfg.get("active_instance")
    inst = cm.make_instance(None, webui_root=target, webui_branch=branch,
                            name=_unique_instance_name(self, cm.KIND_LABELS.get(branch, "WebUI")))
    self.cfg["instances"].append(inst)
    cm.save_config(self.cfg)
    log(f"[部署] 已添加为新实例「{inst['name']}」，可以在侧栏切换实例\n")
    self._emit("instances", "changed")
    return inst["id"]


def _unique_instance_name(self, base):
    names = {i.get("name") for i in self.cfg.get("instances") or []}
    if base not in names:
        return base
    n = 2
    while f"{base} {n}" in names:
        n += 1
    return f"{base} {n}"


def _deploy_flow_comfy(self, target, use_portable):
    """部署 ComfyUI：便携 Python/Git → 拉源码 → 装 torch → 装依赖 → 冒烟测试 → 登记实例"""
    log = lambda t: self._emit("deploy", "log", text=t)
    progress = lambda d, t: self._emit("deploy", "progress", downloaded=d, total=t)
    dcfg = self._deploy_cfg
    try:
        def make_env():
            e = cm.build_comfy_env(dcfg, target)
            try:
                tmp_dir = os.path.join(target, ".launcher_tmp")
                os.makedirs(tmp_dir, exist_ok=True)
                e["TEMP"] = tmp_dir
                e["TMP"] = tmp_dir
                e["PIP_CACHE_DIR"] = SHARED_CACHE_PIP
            except OSError:
                pass
            return e
        _drop_broken_bundled_git(target, log)
        env = make_env()

        bundled_python = os.path.join(target, "python", "python.exe")
        bundled_git = os.path.join(target, "git", "cmd", "git.exe")
        custom_git = (dcfg.get("custom_git_path") or "").strip().strip('"')
        has_sys_git = bool(shutil.which("git")) or bool(custom_git and os.path.exists(custom_git))
        has_sys_python = bool(shutil.which("python") or shutil.which("python3"))
        need_python = not os.path.exists(bundled_python) and (use_portable or not has_sys_python)
        need_git = not os.path.exists(bundled_git) and (use_portable or not has_sys_git)

        # ---- 1. 便携 Python / Git ----
        if need_python or need_git:
            log(f"[部署] 准备便携环境 (Python: {'需要' if need_python else '跳过'}, "
                f"Git: {'需要' if need_git else '跳过'})\n")
            self._deploy_portable_env(target, "comfyui", need_python, need_git, log, progress)
            progress(0, 0)
            env = make_env()   # 便携 Git 刚装好：重建环境，让 GIT 路径进 PATH
        if self._deploy_cancel.is_set():
            raise _DeployCancelled()

        git_exe = custom_git if (custom_git and os.path.exists(custom_git)) else (
            bundled_git if os.path.exists(bundled_git) else "git")
        if os.path.exists(bundled_git):
            pe.tune_bundled_git(os.path.join(target, "git"), log_cb=log)

        # ---- 2. 拉源码（幂等：已有 main.py 就跳过）----
        if not os.path.isfile(os.path.join(target, "main.py")):
            candidates = [COMFY_REPO]
            try:
                import mirror_manager as mm
                if mm.resolve_github_mode(dcfg, log):
                    mirrored = [mm.github_url(COMFY_REPO, True, i) for i in range(len(mm.GITHUB_PROXIES))]
                    candidates = [u for u in mirrored if u != COMFY_REPO] + [COMFY_REPO]
            except Exception:
                pass
            parent = os.path.dirname(os.path.abspath(target)) or "."
            for program, args in ((git_exe, ["init", target]),
                                  (git_exe, ["-C", target, "config", "remote.origin.fetch",
                                             "+refs/heads/*:refs/remotes/origin/*"])):
                if self._deploy_run_cmd(program, args, parent, log, env=env) != 0:
                    log("\n[部署] git 初始化失败，请检查日志\n")
                    self._emit("deploy", "error", title="部署失败", text="git 初始化失败，请查看部署日志。")
                    return
            fetched = False
            for u in candidates:
                if self._deploy_cancel.is_set():
                    raise _DeployCancelled()
                self._deploy_run_cmd(git_exe, ["-C", target, "config", "remote.origin.url", u],
                                     parent, log, env=env)
                rc = self._deploy_run_cmd(
                    git_exe, ["-C", target, "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=120",
                              "fetch", "--depth", "1", "origin", COMFY_REF],
                    parent, log, env=env, timeout=1800)
                if rc == 0:
                    fetched = True
                    break
                log(f"\n[部署] 从该地址拉取失败（退出码 {rc}），换下一个地址 ...\n")
            if not fetched:
                self._emit("deploy", "error", title="源码下载失败",
                           text="从 GitHub 及所有加速代理拉取 ComfyUI 源码都失败了，请检查网络后重试。")
                return
            if self._deploy_run_cmd(git_exe, ["-C", target, "checkout", "-f", "-B", COMFY_REF,
                                              f"origin/{COMFY_REF}"], parent, log, env=env) != 0:
                self._emit("deploy", "error", title="部署失败", text="检出 ComfyUI 代码失败，请查看部署日志。")
                return
        else:
            log("[部署] 检测到已有 ComfyUI 源码，跳过拉取\n")
        if self._deploy_cancel.is_set():
            raise _DeployCancelled()

        # ---- 3. 选 Python：便携版直接装进去；系统 Python 必须套 venv ----
        if os.path.exists(bundled_python):
            py = bundled_python
        else:
            venv_py = os.path.join(target, "venv", "Scripts" if os.name == "nt" else "bin",
                                   "python.exe" if os.name == "nt" else "python")
            if not os.path.exists(venv_py):
                sys_py = shutil.which("python") or shutil.which("python3") or "python"
                log("[部署] 使用系统 Python 创建独立 venv（不污染系统环境）\n")
                if self._deploy_run_cmd(sys_py, ["-m", "venv", os.path.join(target, "venv")],
                                        target, log, env=env) != 0:
                    self._emit("deploy", "error", title="部署失败", text="创建 venv 失败，请查看部署日志。")
                    return
            py = venv_py

        pip_mirror = []
        torch_indexes = [f"{mm_official()}/{COMFY_TORCH_TAG}"]
        try:
            import mirror_manager as mm
            if mm.resolve_mode(dcfg, log):
                pip_mirror = mm.pip_index_args(True)
                torch_indexes = [f"{b}/{COMFY_TORCH_TAG}" for b in mm.PYTORCH_MIRROR_BASES] + torch_indexes
        except Exception:
            pass

        # ---- 4. torch（已装就跳过；镜像失败自动换官方源）----
        has_torch = subprocess.run([py, "-c", "import torch"], capture_output=True,
                                   creationflags=_NO_WINDOW).returncode == 0
        if has_torch:
            log("[部署] torch 已安装，跳过\n")
        else:
            ok = False
            for idx in torch_indexes:
                if self._deploy_cancel.is_set():
                    raise _DeployCancelled()
                log(f"[部署] 安装 torch（{idx}）...\n")
                rc = self._deploy_run_cmd(py, ["-m", "pip", "install", "torch", "torchvision", "torchaudio",
                                               "--index-url", idx], target, log, env=env, timeout=5400)
                if rc == 0:
                    ok = True
                    break
            if not ok:
                self._emit("deploy", "error", title="torch 安装失败",
                           text="所有源都没能装上 torch，请查看部署日志（常见原因：网络中断、磁盘空间不足）。")
                return
        if self._deploy_cancel.is_set():
            raise _DeployCancelled()

        # ---- 5. ComfyUI 依赖 ----
        req = os.path.join(target, "requirements.txt")
        rc = self._deploy_run_cmd(py, ["-m", "pip", "install", "-r", req] + pip_mirror,
                                  target, log, env=env, timeout=3600)
        if rc != 0 and pip_mirror:
            log("\n[部署] 镜像安装失败，改用官方源重试 ...\n")
            rc = self._deploy_run_cmd(py, ["-m", "pip", "install", "-r", req], target, log, env=env, timeout=3600)
        if rc != 0:
            self._emit("deploy", "error", title="依赖安装失败",
                       text="ComfyUI 依赖安装失败，请查看部署日志。修复后重新点「开始部署」会跳过已完成的步骤。")
            return

        # ---- 6. 常用节点（失败只提醒，不算部署失败）----
        node_ids = dcfg.get("deploy_comfy_nodes")
        if not isinstance(node_ids, list):
            node_ids = comfy_nodes.default_ids()
        if node_ids:
            log(f"\n[部署] 安装常用节点（{len(node_ids)} 个，单个失败不影响部署）...\n")
            res = comfy_nodes.install(
                node_ids, target, py, git_exe,
                run=lambda prog, args, cwd, e, t: self._deploy_run_cmd(prog, args, cwd, log, env=e, timeout=t),
                log=log, env=env, cancelled=self._deploy_cancel.is_set,
                gh_proxies=_gh_proxies(dcfg, lambda m: log(m + "\n")), pip_mirror_args=pip_mirror)
            if self._deploy_cancel.is_set():
                raise _DeployCancelled()
            log("[部署] 常用节点：" + comfy_nodes.summary_text(res) + "\n")
            if res["failed"]:
                log("[部署] 失败的节点可以之后在「常用插件」页重新安装\n")

        # ---- 7. 冒烟测试（失败只提醒，不算部署失败）----
        log("\n[部署] 冒烟测试：以 CPU 模式初始化一次 ComfyUI ...\n")
        rc = self._deploy_run_cmd(py, ["-s", "main.py", "--quick-test-for-ci", "--cpu"],
                                  target, log, env=env, timeout=900)
        if rc == 0:
            log("[部署] ComfyUI 初始化正常\n")
        else:
            log("[部署] 冒烟测试没通过（上面有具体报错）。大多数情况下直接启动仍可使用，"
                "如果启动失败请把日志发给作者\n")

        _deploy_register_instance(self, target, "comfyui", log)
        log("\n[部署] 全部完成！\n")
        self._emit("deploy", "done", target=target, branch="comfyui")
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


def mm_official():
    try:
        import mirror_manager as mm
        return mm.PYTORCH_OFFICIAL_WHL
    except Exception:
        return "https://download.pytorch.org/whl"


def _looks_like_network_failure(tail_text):
    return bool(tail_text) and bool(_NETWORK_FAIL_RE.search(tail_text))


def _api_deploy_venv_check(self, target, branch):
    target = (target or "").strip()
    if not target or not os.path.isdir(target):
        return {"ok": False, "error": "请先在「安装目录」里填好要检测的 WebUI 根目录"}
    required = pe.PYTHON_VERSION_BY_BRANCH.get(branch, "3.10")
    venv_dir = os.path.join(target, "venv")
    mismatch, detail = pe.check_venv_version_mismatch(venv_dir, required)
    return {"ok": True, "mismatch": mismatch, "detail": detail}


def _api_deploy_venv_delete(self, target):
    target = (target or "").strip()
    if not target or not os.path.isdir(target):
        return {"ok": False, "error": "目录无效，已取消删除"}
    venv_dir = os.path.join(target, "venv")
    if not os.path.isdir(venv_dir):
        return {"ok": False, "error": "该目录下没有 venv 文件夹"}
    shutil.rmtree(venv_dir, ignore_errors=True)
    if os.path.isdir(venv_dir):
        return {"ok": False, "error": "删除失败（文件可能被占用），请手动删除"}
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
            _auto_organize_if_lora(self, [final_path])
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
            _auto_organize_if_lora(self, [final_path])
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


_PREVIEW_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".gif")


def _pick_preview_url(images):
    """从 Civitai images 里挑第一张真正的图片：跳过视频（预览图列表里经常混着
    mp4，直接当 png 存下来浏览器显示不出来），并把 original=true 换成 width=450
    小图——原图动辄几十 MB，预览用不着。"""
    for img in images or []:
        url = img.get("url") if isinstance(img, dict) else (img if isinstance(img, str) else None)
        if not url:
            continue
        if url.split("?", 1)[0].lower().endswith(_PREVIEW_VIDEO_EXTS):
            continue
        return url.replace("/original=true/", "/width=450/")
    return None


def _preview_is_video(path):
    """已存在的预览文件其实是视频（旧版本会把 mp4 存成 .preview.png）——要重下"""
    try:
        with open(path, "rb") as f:
            return f.read(12)[4:8] == b"ftyp"   # ISO-BMFF 视频容器（mp4/mov）
    except OSError:
        return False


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

    if fetch_preview:
        existing = [base + s for s in (".preview.png", ".png", ".jpg", ".jpeg", ".webp")
                    if os.path.exists(base + s)]
        # 有预览但其实是旧版本误存的视频 → 当没有处理，重新下载
        if existing and not (len(existing) == 1 and _preview_is_video(existing[0])):
            return info
        url = _pick_preview_url(data.get("images"))
        if url:
            try:
                # 图片 URL 来自 API 数据，可能指向站外主机——Authorization
                # 只发给 civitai 自己的域名，避免 API Key 泄露给第三方
                headers = _api_headers(api_key if cd._is_civitai_host(url) else None)
                resp = requests.get(url, headers=headers, timeout=30)
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
    lib = _library_path(self)
    if lib:
        # 开启共享模型库后，模型管理页管的是模型库（所有实例共用）
        cats = [{"label": c["label"], "path": c["path"], "is_lora": c["is_lora"]}
                for c in ml.library_categories(lib)]
        self._models_categories = cats
        return {"ok": True, "root": lib, "library": True, "categories":
                [{"label": c["label"], "is_lora": c["is_lora"], "path": c["path"]} for c in cats]}
    if cm.is_comfy(self.cfg):
        dirs = ml.instance_model_dirs(self.cfg, cm.comfy_layout)
        for k, label, is_lora, *_ in ml.LIBRARY_CATEGORIES:
            if k in dirs:
                cats.append({"label": label, "path": dirs[k], "is_lora": is_lora})
        base = cm.comfy_layout(root)[0]
        models_dir = os.path.join(base, "models") if base else ""
        if models_dir and os.path.isdir(models_dir):
            for name in sorted(os.listdir(models_dir)):
                sub = os.path.join(models_dir, name)
                if not os.path.isdir(sub) or any(_same_path(sub, c["path"]) for c in cats):
                    continue
                try:
                    has_model = any(f.lower().endswith(MODEL_EXTS) for f in os.listdir(sub))
                except OSError:
                    has_model = False
                if has_model:
                    cats.append({"label": f"其他: {name}", "path": sub, "is_lora": "lora" in name.lower()})
        self._models_categories = cats
        return {"ok": True, "root": root, "categories":
                [{"label": c["label"], "is_lora": c["is_lora"], "path": c["path"]} for c in cats]}
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
            [{"label": c["label"], "is_lora": c["is_lora"], "path": c["path"]} for c in cats]}


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


def _import_suggest(self, cat, counts):
    """拖进来的文件和当前分类明显不符时，给出应该去的分类下标（没有则 None）"""
    lora, full = counts.get("lora", 0), counts.get("full", 0)
    want_lora = None
    if cat["is_lora"] and full and not lora:
        want_lora = False
    elif not cat["is_lora"] and lora and not full:
        want_lora = True
    if want_lora is None:
        return None
    for i, c in enumerate(self._models_categories):
        # 大模型只建议去第一个非 LoRA 分类（即 Checkpoint），不往 VAE 之类里塞
        if c["is_lora"] == want_lora:
            return i
    return None


def _api_models_import_plan(self, cat_index, paths):
    """拖拽上传第一步：只读，算出会复制什么、有没有放错分类、空间够不够"""
    try:
        cat = self._models_categories[int(cat_index)]
    except (IndexError, ValueError, TypeError):
        return {"ok": False, "error": "请先在左侧选一个模型分类"}
    if not isinstance(paths, list) or not paths:
        return {"ok": False, "error": "没有收到文件"}
    last = [0.0]

    def scan_progress(i, n, name):   # 文件多的时候让界面看到「分析文件 i/n」而不是卡住
        now = time.monotonic()
        if i == n or now - last[0] >= 0.1:
            last[0] = now
            self._emit("models", "import_scan", i=i, n=n, name=name)
    plan = ml.plan_import(cat["path"], paths, progress=scan_progress)
    items = plan["items"]
    sug = _import_suggest(self, cat, plan["counts"])
    free = plan["free_bytes"]
    return {
        "ok": True,
        "target_label": cat["label"], "target_dir": cat["path"],
        "count": len(items),
        "to_copy": sum(1 for it in items if it["action"] != "skip"),
        "skip": sum(1 for it in items if it["action"] == "skip"),
        "rename": [{"from": os.path.basename(it["src"]), "to": os.path.basename(it["dest"])}
                   for it in items if it["action"] == "rename"],
        "ignored": plan["ignored"][:50], "ignored_count": len(plan["ignored"]),
        "counts": plan["counts"],
        "total_text": _fmt_size(plan["total_bytes"]),
        "no_space": free is not None and plan["total_bytes"] > free,
        "free_text": _fmt_size(free) if free is not None else "",
        "suggest_index": sug,
        "suggest_label": self._models_categories[sug]["label"] if sug is not None else "",
    }


def _api_models_import_start(self, cat_index, paths):
    """拖拽上传第二步：后台复制。进度走 models/import_progress，结束走 models/import_done"""
    if self._import_thread and self._import_thread.is_alive():
        return {"ok": False, "error": "上一批还在复制，请等它完成或先取消"}
    try:
        cat = self._models_categories[int(cat_index)]
    except (IndexError, ValueError, TypeError):
        return {"ok": False, "error": "分类无效"}
    plan = ml.plan_import(cat["path"], paths or [])
    if not plan["items"]:
        return {"ok": False, "error": "没有可上传的模型文件"}
    if plan["free_bytes"] is not None and plan["total_bytes"] > plan["free_bytes"]:
        return {"ok": False, "error": f"目标磁盘空间不足：需要 {_fmt_size(plan['total_bytes'])}，"
                                      f"剩余 {_fmt_size(plan['free_bytes'])}"}
    self._import_cancel = False

    def work():
        res = ml.run_import(
            plan,
            progress=lambda p: self._emit("models", "import_progress", **p),
            cancelled=lambda: self._import_cancel)
        self._emit("models", "import_done", cat_index=int(cat_index),
                   copied=res["copied"], renamed=res["renamed"], skipped=res["skipped"],
                   failed=[{"name": n, "error": e} for n, e in res["failed"]],
                   cancelled=res["cancelled"], last_dest=res["last_dest"])
        if cat["is_lora"] and res.get("dests"):
            _auto_organize_if_lora(self, res["dests"])

    self._import_thread = self._spawn(work, "model-import")
    return {"ok": True, "total_text": _fmt_size(plan["total_bytes"])}


def _api_models_import_cancel(self):
    self._import_cancel = True
    return {"ok": True}


# ============================================================
# 共享模型库 / LoRA 自动整理（V3 阶段 4）
# ============================================================
LIBRARY_YAML = os.path.join(APP_DIR, "launcher_data", "comfy_library_paths.yaml")


def _library_path(self):
    if not self.cfg.get("model_library_enabled"):
        return ""
    p = (self.cfg.get("model_library_path") or "").strip()
    return p if p and os.path.isdir(p) else ""


def _library_launch_extras(self, iid, cfg, log):
    """启动某个实例前，把共享模型库挂上去：ComfyUI 用 extra_model_paths 配置，WebUI 用 --xxx-dir 参数"""
    lib = _library_path(self)
    if not lib:
        return
    if cm.is_comfy(cfg):
        os.makedirs(os.path.dirname(LIBRARY_YAML), exist_ok=True)
        with open(LIBRARY_YAML, "w", encoding="utf-8") as f:
            f.write(ml.comfy_yaml_text(lib))
        cfg["_extra_model_paths"] = LIBRARY_YAML
        log(f"[启动器] 已挂载共享模型库: {lib}\n")
    else:
        cfg["extra_args"] = ((cfg.get("extra_args") or "") + " " + ml.forge_library_args(lib)).strip()
        log(f"[启动器] 已挂载共享模型库: {lib}\n")


def _any_instance_running(self):
    return any(r.running() for r in list(self._runners.values()))


def _all_instance_cfgs(self):
    cm.absorb_active(self.cfg)
    return [(i["id"], i.get("name", ""), cm.instance_cfg(self.cfg, i["id"]))
            for i in self.cfg.get("instances") or []]


def _api_library_status(self):
    lib = (self.cfg.get("model_library_path") or "").strip()
    jr = ml.list_journals()
    return {"ok": True, "enabled": bool(self.cfg.get("model_library_enabled")), "path": lib,
            "exists": bool(lib and os.path.isdir(lib)),
            "journals": [{"id": d["id"], "kind": d["kind"], "title": d["title"],
                          "time": d["time"], "undone": d.get("undone", False),
                          "count": len(d.get("moves") or []) + len(d.get("trashed") or [])}
                         for d in jr[:20]]}


def _api_library_set(self, path, enabled):
    path = (path or "").strip().strip('"')
    if enabled:
        if not path:
            return {"ok": False, "error": "请先选择模型库文件夹"}
        try:
            os.makedirs(path, exist_ok=True)
            for c in ml.library_categories(path):
                os.makedirs(c["path"], exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"无法创建模型库目录: {e}"}
        for _iid, name, c in _all_instance_cfgs(self):
            root = (c.get("webui_root") or "").strip()
            if root and (_same_path(path, root) or os.path.abspath(path).startswith(os.path.abspath(root) + os.sep)):
                return {"ok": False, "error": f"模型库不能放在实例「{name}」的目录里面，请选一个独立的文件夹"}
    self.cfg["model_library_path"] = path
    self.cfg["model_library_enabled"] = bool(enabled)
    cm.save_config(self.cfg)
    note = "正在运行的实例需要重启后才会用上模型库" if _any_instance_running(self) else ""
    return {"ok": True, "note": note, **_api_library_status(self)}


def _api_library_merge_plan(self):
    lib = _library_path(self)
    if not lib:
        return {"ok": False, "error": "请先开启共享模型库"}
    plan = ml.plan_merge(_all_instance_cfgs(self), lib, cm.comfy_layout)
    self._merge_plan = plan
    by_inst = {}
    names = {iid: n for iid, n, _c in _all_instance_cfgs(self)}
    for mv in plan["moves"]:
        d = by_inst.setdefault(mv["iid"], {"name": names.get(mv["iid"], ""), "move": 0, "dup": 0})
        d["dup" if mv["dup"] else "move"] += 1
    return {"ok": True, "count": plan["count"], "total_text": _fmt_size(plan["total_bytes"]),
            "dup_text": _fmt_size(plan["dup_bytes"]), "cross_text": _fmt_size(plan["cross_bytes"]),
            "cross": plan["cross_bytes"] > 0, "by_instance": list(by_inst.values()),
            "sample": [{"from": mv["src"], "to": mv["dst"], "dup": mv["dup"]} for mv in plan["moves"][:40]]}


def _api_library_merge_start(self):
    plan = getattr(self, "_merge_plan", None)
    lib = _library_path(self)
    if not plan or not lib:
        return {"ok": False, "error": "请先生成合并预览"}
    if _any_instance_running(self):
        return {"ok": False, "error": "有实例正在运行，模型文件可能被占用。请先停止所有实例再合并。"}
    self._merge_plan = None

    def work():
        res = ml.run_merge(plan, lib, progress=lambda p: self._emit("library", "progress", **p))
        self._emit("library", "merge_done", moved=res["moved"], trashed=res["trashed"],
                   failed=[{"name": n, "error": e} for n, e in res["failed"]], journal=res["journal"])
    self._spawn(work, name="library-merge")
    return {"ok": True}


def _api_journal_undo(self, jid):
    if _any_instance_running(self):
        return {"ok": False, "error": "有实例正在运行，请先停止再撤销"}
    try:
        res = ml.undo_journal(jid)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"读取移动记录失败: {e}"}
    return {"ok": True, **res, "failed": [{"name": n, "error": e} for n, e in res["failed"]]}


def _lookup_quiet(self, path, api_key, cancelled=lambda: False):
    """按哈希查 Civitai（查不到再查 liblib）并写 sidecar；返回 info 或 None。整理时用，不推界面事件。"""
    st = os.stat(path)
    entry = _hash_cache_get(self, path)
    digest = None
    if entry and cache_entry_valid(entry, st):
        if entry.get("found") is False:
            return None
        digest = entry.get("sha256")
    if not digest:
        digest = compute_sha256(path, cancel_flag=cancelled)
    data, status_code = query_by_hash(digest, api_key)
    if status_code == 404:
        lb = None
        try:
            lb = lc.query_by_hash(digest)
        except Exception:
            lb = None
        _hash_cache_set(self, path, {"size": st.st_size, "mtime": int(st.st_mtime),
                                     "sha256": digest, "found": bool(lb)})
        return merge_sidecar_from_liblib(path, lb, digest) if lb else None
    _hash_cache_set(self, path, {"size": st.st_size, "mtime": int(st.st_mtime),
                                 "sha256": digest, "found": True})
    return write_sidecar_from_version(path, data, digest, api_key)


def _civitai_model_tags(model_id, api_key):
    """Civitai 模型页的标签（character / style / concept …），整理时用来分类"""
    import requests
    r = requests.get(f"https://civitai.com/api/v1/models/{int(model_id)}",
                     headers=_api_headers(api_key), timeout=20)
    if r.status_code != 200:
        return None
    tags = r.json().get("tags") or []
    return [t if isinstance(t, str) else (t or {}).get("name", "") for t in tags]


def _lora_root(self):
    """当前要整理的 LoRA 根目录：开了模型库就是库里的 loras，否则是当前实例的 LoRA 目录"""
    lib = _library_path(self)
    if lib:
        return os.path.join(lib, "loras")
    dirs = ml.instance_model_dirs(self.cfg, cm.comfy_layout)
    return dirs.get("loras", "")


def _workflow_dirs_for(self, lora_root):
    """哪些 ComfyUI 实例引用这个 LoRA 目录 → 它们保存的工作流需要同步改路径"""
    out = []
    lib = _library_path(self)
    for _iid, _n, c in _all_instance_cfgs(self):
        if not cm.is_comfy(c):
            continue
        comfy_dir = cm.comfy_layout(c.get("webui_root") or "")[0]
        if not comfy_dir:
            continue
        uses = (lib and _same_path(lora_root, os.path.join(lib, "loras"))) or \
            _same_path(ml.instance_model_dirs(c, cm.comfy_layout).get("loras", ""), lora_root)
        if uses:
            out += ml.comfy_workflow_dirs(comfy_dir)
    return out


def _organize_prepare(self, lora_root, only, cancelled, progress):
    """整理前补信息：没查过的先按哈希查 Civitai，有 Civitai 编号但没有标签的补标签"""
    import requests
    api_key = (self.cfg.get("civitai_api_key") or "").strip() or None
    template = self.cfg.get("lora_organize_template") or "base/type"
    include_sub = bool(self.cfg.get("lora_organize_include_sub"))
    files = ml.organize_candidates(lora_root, include_sub, only)
    n = len(files)
    for i, path in enumerate(files, 1):
        if cancelled():
            break
        progress({"i": i, "n": n, "name": os.path.basename(path), "stage": "查询模型信息"})
        info = read_sidecar(path)
        try:
            if not info.get("baseModel") and path.lower().endswith(".safetensors"):
                info = _lookup_quiet(self, path, api_key, cancelled) or {}
                time.sleep(0.5)     # 别把 Civitai 打限流
            if info.get("modelId") and "tags" not in info:
                tags = _civitai_model_tags(info["modelId"], api_key)
                if tags is not None:
                    info["tags"] = tags
                    write_sidecar(path, info)
                time.sleep(0.3)
        except (requests.exceptions.RequestException, InterruptedError):
            continue
        except Exception:
            continue

    def arch_of(p):
        try:
            head = sm.read_safetensors_header(p)
            if head:
                return sm.guess_architecture(*head)[1]
        except Exception:
            pass
        return ""
    inst = cm.active_instance(self.cfg) or {}
    return ml.plan_organize(lora_root, template, include_sub, read_sidecar, arch_of,
                            instance_name=inst.get("name", ""), only=only)


def _organize_summary(plans):
    groups = {}
    for pl in plans:
        groups[pl["sub"]] = groups.get(pl["sub"], 0) + 1
    return {"count": len(plans),
            "groups": sorted([{"sub": k, "count": v} for k, v in groups.items()], key=lambda g: -g["count"]),
            "sample": [{"from": pl["rel_from"], "to": pl["rel_to"]} for pl in plans[:60]]}


def _api_lora_organize_scan(self):
    """手动整理第一步：后台补信息并算出计划，结果走 models/organize_plan 事件"""
    if getattr(self, "_organize_thread", None) and self._organize_thread.is_alive():
        return {"ok": False, "error": "整理正在进行中"}
    root = _lora_root(self)
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "没找到 LoRA 文件夹，请先设置根目录"}
    self._organize_cancel = False

    def work():
        plans = _organize_prepare(self, root, None, lambda: self._organize_cancel,
                                  lambda p: self._emit("models", "organize_progress", **p))
        self._organize_plan = (root, plans)
        self._emit("models", "organize_plan", root=root, cancelled=self._organize_cancel,
                   template=self.cfg.get("lora_organize_template") or "base/type",
                   running=_any_instance_running(self), **_organize_summary(plans))
    self._organize_thread = self._spawn(work, name="lora-organize-scan")
    return {"ok": True}


def _api_lora_organize_cancel(self):
    self._organize_cancel = True
    return {"ok": True}


def _api_lora_organize_apply(self):
    pending = getattr(self, "_organize_plan", None)
    if not pending or not pending[1]:
        return {"ok": False, "error": "没有需要整理的文件"}
    if _any_instance_running(self):
        return {"ok": False, "error": "有实例正在运行，LoRA 可能正被加载。请先停止所有实例再整理。"}
    root, plans = pending
    self._organize_plan = None

    def work():
        res = ml.run_organize(plans, root, _workflow_dirs_for(self, root),
                              progress=lambda p: self._emit("models", "organize_progress",
                                                            stage="移动文件", **p))
        self._emit("models", "organize_done", auto=False, moved=res["moved"], workflows=res["workflows"],
                   failed=[{"name": n, "error": e} for n, e in res["failed"]], journal=res["journal"])
    self._organize_thread = self._spawn(work, name="lora-organize-apply")
    return {"ok": True}


def _auto_organize_if_lora(self, paths):
    """新上传 / 新下载的 LoRA 自动归类（默认开）。只动这几个新文件，运行中的实例不受影响。"""
    if not self.cfg.get("lora_organize_enabled", True):
        return
    root = _lora_root(self)
    if not root:
        return
    root_n = os.path.normcase(os.path.abspath(root))
    mine = [p for p in paths if p and os.path.normcase(os.path.abspath(p)).startswith(root_n + os.sep)
            and ml._is_model(p)]
    if not mine:
        return

    def work():
        plans = _organize_prepare(self, root, mine, lambda: False, lambda p: None)
        if not plans:
            return
        res = ml.run_organize(plans, root, _workflow_dirs_for(self, root), title="新 LoRA 自动归类")
        self._emit("models", "organize_done", auto=True, moved=res["moved"], workflows=res["workflows"],
                   failed=[{"name": n, "error": e} for n, e in res["failed"]], journal=res["journal"],
                   dests=[pl["dst"] for pl in plans], subs=[pl["sub"] for pl in plans])
    self._spawn(work, name="lora-auto-organize")


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


def _comfy_output_dir(cfg):
    """ComfyUI 的输出目录：--output-directory 参数优先，否则 <ComfyUI>/output"""
    root = (cfg.get("webui_root") or "").strip()
    comfy_dir, _ = cm.comfy_layout(root)
    extra = cfg.get("extra_args") or ""
    m = re.search(r'--output-directory\s+("([^"]+)"|(\S+))', extra)
    if m:
        d = m.group(2) or m.group(3)
        if not os.path.isabs(d) and comfy_dir:
            d = os.path.join(comfy_dir, d)
        return os.path.normpath(d)
    return os.path.join(comfy_dir or root, "output")


def instance_output_info(cfg):
    """
    一个实例的出图目录信息（启动页按钮、输出管理器扫描共用）：
    {"root","txt2img","img2img","date_subdir","scan":[要扫描的目录…]}
    """
    root = (cfg.get("webui_root") or "").strip()
    if not root or not os.path.isdir(root):
        return None
    if cm.is_comfy(cfg):
        od = _comfy_output_dir(cfg)
        return {"root": od, "txt2img": od, "img2img": od, "date_subdir": False, "scan": [od]}
    d = _resolve_output_dirs(root, cfg.get("webui_branch", ""))
    scan = [d["root"]]
    for k in ("txt2img", "img2img"):
        p = d[k]
        try:
            inside = os.path.commonpath([p, d["root"]]) == os.path.normpath(d["root"])
        except ValueError:
            inside = False
        if not inside:
            scan.append(p)
    wc = _read_webui_config(root)
    # outdir_grids（阵列图目录）有意不加：阵列图不收录进输出管理
    for key in ("outdir_extras_samples", "outdir_save"):
        v = str(wc.get(key) or "").strip()
        if v:
            v = v if os.path.isabs(v) else os.path.normpath(os.path.join(root, v))
            if not any(_same_path(v, x) or v.startswith(x + os.sep) for x in scan):
                scan.append(v)
    d["scan"] = scan
    return d


def _api_output_dirs_info(self):
    """给前端用：告诉它实际路径是什么、要不要显示「当天」那两个按钮。"""
    d = instance_output_info(self.cfg)
    if not d:
        return {"ok": False, "error": "请先设置正确的根目录"}
    return {"ok": True, "root": d["root"], "txt2img": d["txt2img"],
            "img2img": d["img2img"], "date_subdir": d["date_subdir"],
            "exists": {k: os.path.isdir(d[k]) for k in ("root", "txt2img", "img2img")}}


def _api_open_output_folder(self, which):
    """打开出图目录：root / txt2img / txt2img_today / img2img / img2img_today"""
    dirs = instance_output_info(self.cfg)
    if not dirs:
        return {"ok": False, "error": "请先设置正确的根目录"}
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

def _ext_target(self, iid=None):
    """「常用插件」页的安装目标实例：(实例配置, 实例信息 dict)。iid 为空 = 当前实例"""
    iid = iid or self.cfg.get("active_instance")
    cfg = cm.instance_cfg(self.cfg, iid) if iid else None
    if cfg is None:
        cfg, iid = self.cfg, self.cfg.get("active_instance")
    root = (cfg.get("webui_root") or "").strip()
    comfy = cm.is_comfy(cfg)
    if comfy:
        comfy_dir = cm.comfy_layout(root)[0]
        target_dir = os.path.join(comfy_dir, "custom_nodes") if comfy_dir else None
    else:
        comfy_dir = None
        target_dir = os.path.join(root, "extensions") if root else None
    r = self._runners.get(iid) if iid else None
    info = {
        "id": iid, "name": cfg.get("_name") or cm.KIND_LABELS.get(cfg.get("webui_branch"), "WebUI"),
        "kind": "comfyui" if comfy else "forge",
        "kind_label": cm.KIND_LABELS.get(cfg.get("webui_branch"), "WebUI"),
        "root": root, "target_dir": target_dir or "",
        "running": bool(r and r.running()),
    }
    return cfg, info, comfy_dir


def _git_for(cfg, root):
    """git 选取顺序与部署/启动一致：自定义路径 > 便携版 > 系统 PATH"""
    custom_git = (cfg.get("custom_git_path") or "").strip().strip('"')
    cands = [custom_git] if custom_git else []
    cands += [os.path.join(root, "git", "cmd", "git.exe"),
              os.path.join(os.path.dirname(root.rstrip("\\/")), "git", "cmd", "git.exe")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return "git" if shutil.which("git") else None


def _gh_proxies(cfg, log=None):
    """要走 GitHub 加速时返回代理前缀列表（按顺序尝试，最后直连），否则空"""
    try:
        import mirror_manager as mm
        if mm.resolve_github_mode(cfg, log):
            return list(mm.GITHUB_PROXIES)
    except Exception:
        pass
    return []


def _pip_mirror(cfg, log=None):
    try:
        import mirror_manager as mm
        if mm.resolve_mode(cfg, log):
            return mm.pip_index_args(True)
    except Exception:
        pass
    return []


def _api_ext_list(self, iid=None):
    cfg, info, comfy_dir = _ext_target(self, iid)
    root = info["root"]
    has_root = bool(root and os.path.isdir(root))
    if info["kind"] == "comfyui":
        py = cm.comfy_python(cfg, root)
        items = comfy_nodes.list_status(comfy_dir, py) if comfy_dir else []
        return {"ok": True, "items": items, "has_root": has_root and bool(comfy_dir),
                "comfy": True, "target": info, "groups": comfy_nodes.GROUP_LABELS,
                "manager_builtin": comfy_nodes.manager_builtin_supported(comfy_dir)}
    branch = cfg.get("webui_branch", "neo2")
    ext_dir = info["target_dir"]
    items = []
    for name, desc, url_raw, folder_raw in EXTENSION_CATALOG:
        folder = _resolve_by_branch(folder_raw, branch)
        if not folder or not _resolve_by_branch(url_raw, branch):
            continue  # 该分支不可用的扩展直接不显示
        installed = bool(ext_dir) and os.path.isdir(os.path.join(ext_dir, folder))
        items.append({"id": name, "name": name, "desc": desc, "installed": installed})
    return {"ok": True, "items": items, "has_root": has_root, "comfy": False, "target": info}


def _ext_run_cmd(self, program, args, cwd, env=None, timeout=None):
    """「常用插件」页跑命令：流式写日志、可取消、有超时（加速代理卡死时不至于永远等）"""
    log = lambda t: self._emit("ext", "log", text=t)
    try:
        proc = subprocess.Popen([program] + list(args), cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except OSError as e:
        log(f"[插件] 无法启动 {program}: {e}\n")
        return 1
    self._ext_proc = proc
    q = queue.Queue()

    def reader():
        try:
            while True:
                data = proc.stdout.read1(4096)
                if not data:
                    break
                q.put(data)
        except Exception:
            pass
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True, name="ext-cmd-reader").start()
    t0 = time.monotonic()
    while True:
        try:
            data = q.get(timeout=0.5)
        except queue.Empty:
            data = b""
        if data is None:
            break
        if data:
            log(cm.decode_process_output(data))
        if self._ext_cancel or (timeout and time.monotonic() - t0 > timeout):
            if not self._ext_cancel:
                log(f"\n[插件] 超过 {timeout} 秒没完成，已中止\n")
            kill_process_tree(proc.pid)
            break
    try:
        return proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return -9


def _api_ext_install(self, names, iid=None):
    if self._ext_thread and self._ext_thread.is_alive():
        return {"ok": False, "error": "已有安装任务在进行中"}
    cfg, info, comfy_dir = _ext_target(self, iid)
    root = info["root"]
    if not root or not info["target_dir"]:
        return {"ok": False, "error": f"实例「{info['name']}」还没有设置正确的根目录"}
    git_exe = _git_for(cfg, root)
    if not git_exe:
        return {"ok": False, "error": "未检测到 Git。可以先在「环境部署」页部署便携环境，"
                                      "或手动安装：https://git-scm.com/download/win"}
    names = list(names or [])

    if info["kind"] == "comfyui":
        # 节点要往该实例的 Python 里装依赖，运行中的 .pyd 被占用会装失败
        if info["running"]:
            return {"ok": False, "error": f"「{info['name']}」正在运行。安装节点要往它的 Python 环境里装依赖，"
                                          "运行中文件被占用会失败——请先停止这个实例再安装。"}
        py = cm.comfy_python(cfg, root)
        ids = [n for n in names if comfy_nodes.find(n)
               and not comfy_nodes.is_installed(comfy_nodes.find(n), comfy_dir, py)]
        if not ids:
            return {"ok": False, "error": "请先勾选要安装的节点（已安装的会自动跳过）"}
        self._ext_cancel = False

        def work_comfy():
            log = lambda t: self._emit("ext", "log", text=t)
            try:
                log(f"[插件] 安装到：{info['name']}（{info['target_dir']}）\n")
                env = cm.build_comfy_env(cfg, root)
                res = comfy_nodes.install(
                    ids, comfy_dir, py, git_exe,
                    run=lambda prog, args, cwd, e, t: _ext_run_cmd(self, prog, args, cwd, e, t),
                    log=log, env=env, cancelled=lambda: self._ext_cancel,
                    gh_proxies=_gh_proxies(cfg, lambda m: log(m + "\n")),
                    pip_mirror_args=_pip_mirror(cfg, lambda m: log(m + "\n")))
                if self._ext_cancel:
                    log("\n[插件] 已取消\n")
                else:
                    log("\n[插件] 完成：" + comfy_nodes.summary_text(res) + "\n"
                        "重启 ComfyUI 后新节点生效。\n")
                self._emit("ext", "done")
            finally:
                self._ext_proc = None
                self._emit("ext", "state", running=False)

        self._emit("ext", "state", running=True)
        self._ext_thread = self._spawn(work_comfy, name="ext-install")
        return {"ok": True}

    ext_dir = info["target_dir"]
    branch = cfg.get("webui_branch", "neo2")
    selected = []
    for name, desc, url_raw, folder_raw in EXTENSION_CATALOG:
        if name not in names:
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
            log(f"[插件] 安装到：{info['name']}（{ext_dir}）\n")
            os.makedirs(ext_dir, exist_ok=True)
            use_mirror = bool(_gh_proxies(cfg, lambda m: log(m + "\n")))
            for name, url, folder in selected:
                if self._ext_cancel:
                    break
                u = url
                if use_mirror:
                    try:
                        import mirror_manager as mm
                        u = mm.github_url(url, True, 0)
                    except Exception:
                        pass
                log(f"\n[插件] 正在安装: {name}\n")
                rc = _ext_run_cmd(self, git_exe, ["clone", u, os.path.join(ext_dir, folder)],
                                  ext_dir, None, 900)
                if self._ext_cancel:
                    shutil.rmtree(os.path.join(ext_dir, folder), ignore_errors=True)
                    break
                if rc != 0:
                    shutil.rmtree(os.path.join(ext_dir, folder), ignore_errors=True)
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


def _api_comfy_node_catalog(self):
    """部署页：随 ComfyUI 一起装的节点清单 + 上次的勾选"""
    sel = self.cfg.get("deploy_comfy_nodes")
    if not isinstance(sel, list):
        sel = comfy_nodes.default_ids()
    items = [{"id": n["id"], "name": n["name"], "desc": n["desc"], "group": n["group"],
              "default": bool(n.get("default"))} for n in comfy_nodes.CATALOG]
    return {"ok": True, "items": items, "selected": sel, "groups": comfy_nodes.GROUP_LABELS}


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
    "models_import_plan": _api_models_import_plan,
    "models_import_start": _api_models_import_start,
    "models_import_cancel": _api_models_import_cancel,
    "library_status": _api_library_status,
    "library_set": _api_library_set,
    "library_merge_plan": _api_library_merge_plan,
    "library_merge_start": _api_library_merge_start,
    "journal_undo": _api_journal_undo,
    "lora_organize_scan": _api_lora_organize_scan,
    "lora_organize_cancel": _api_lora_organize_cancel,
    "lora_organize_apply": _api_lora_organize_apply,
    "open_output_folder": _api_open_output_folder,
    "output_dirs_info": _api_output_dirs_info,
    "wd14_add_clipboard_image": _api_wd14_add_clipboard_image,
    "ext_list": _api_ext_list,
    "ext_install": _api_ext_install,
    "ext_cancel": _api_ext_cancel,
    "comfy_node_catalog": _api_comfy_node_catalog,
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
    "outputs_info": _api_outputs_info,
    "outputs_scan": _api_outputs_scan,
    "outputs_query": _api_outputs_query,
    "outputs_facets": _api_outputs_facets,
    "outputs_detail": _api_outputs_detail,
    "outputs_reveal": _api_outputs_reveal,
    "outputs_open": _api_outputs_open,
    "outputs_delete": _api_outputs_delete,
    "outputs_collections": _api_outputs_collections,
    "outputs_collection_op": _api_outputs_collection_op,
    "outputs_collection_export": _api_outputs_collection_export,
    "instances_list": _api_instances_list,
    "instance_add": _api_instance_add,
    "instance_remove": _api_instance_remove,
    "instance_update": _api_instance_update,
    "instance_switch": _api_instance_switch,
    "instance_config_get": _api_instance_config_get,
    "instance_config_update": _api_instance_config_update,
    "set_multi_ui": _api_set_multi_ui,
    "set_hidden_pages": _api_set_hidden_pages,
}.items():
    setattr(LauncherApi, _name, _fn)
