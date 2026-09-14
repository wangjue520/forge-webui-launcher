# -*- coding: utf-8 -*-
"""
comfy_backend.py —— ComfyUI 后端定义。

启动器从 Forge 转向 ComfyUI，两边几乎没有共用的东西：启动方式、命令行参数、
模型目录名、扩展机制、默认端口、就绪日志，全都不一样。这个文件把这些差异
集中成几张表和几个函数，别的模块只管调，不要再到处写 if。

参数表依据 ComfyUI 官方的 comfy/cli_args.py 文档（docs.comfy.org 的
Startup Flags 页），不是凭记忆写的。几个容易记错的点单独记在下面。
"""
import os
import re
import subprocess

# ============================================================
# 目录布局
# ============================================================
#
# ComfyUI 有两种常见的目录摆法，必须都认：
#
#   官方便携版 ComfyUI_windows_portable/       秋叶整合包 ComfyUI-aki-vX/
#     ├─ python_embeded/python.exe               ├─ python/python.exe
#     ├─ ComfyUI/                                ├─ models/
#     │   ├─ main.py                             ├─ custom_nodes/
#     │   ├─ models/                             ├─ main.py
#     │   └─ custom_nodes/                       └─ A绘世启动器.exe
#     └─ run_nvidia_gpu.bat
#
# 也就是说「解压出来的那一层」不一定就是 ComfyUI 本体所在的那一层。
# 一律以 main.py 的位置为准来定位本体。

def find_comfy_root(path):
    """
    从用户选的目录里找出 ComfyUI 本体（main.py 所在的那一层）。
    找不到返回 None。
    """
    if not path or not os.path.isdir(path):
        return None
    if os.path.isfile(os.path.join(path, "main.py")):
        return path
    # 便携版/整合包常见的一层嵌套
    for sub in ("ComfyUI", "comfyui", "ComfyUI_windows_portable/ComfyUI"):
        cand = os.path.join(path, *sub.split("/"))
        if os.path.isfile(os.path.join(cand, "main.py")):
            return cand
    return None


def is_comfy_dir(path):
    return find_comfy_root(path) is not None


# ============================================================
# Python 环境探测
# ============================================================
#
# 这是接管一个"已经装好的 ComfyUI"时最关键的一步：绝不能用系统 Python 去跑它。
# 整合包自带的环境里装着特定版本的 torch/cuda 和一堆编译好的轮子，
# 换个解释器基本必炸。
#
# 探测顺序按"越专属越优先"排：整合包自带 > 项目内 venv > conda > 系统。
# 每一条都要真的能跑起来并且 import 得到 torch 才算数——光有个 python.exe
# 不代表它就是那个装了依赖的环境。

# (相对路径, 这种布局的来源说明)
_PYTHON_CANDIDATES = [
    ("python_embeded/python.exe",     "官方便携版自带环境"),
    ("python/python.exe",             "秋叶整合包自带环境"),
    ("venv/Scripts/python.exe",       "项目内 venv"),
    (".venv/Scripts/python.exe",      "项目内 .venv"),
    ("system/python/python.exe",      "整合包自带环境"),
    ("py310/python.exe",              "整合包自带环境"),
    ("python310/python.exe",          "整合包自带环境"),
    # Linux / macOS
    ("venv/bin/python",               "项目内 venv"),
    (".venv/bin/python",              "项目内 .venv"),
]


def detect_python(root_dir, comfy_root=None):
    """
    找出该用哪个 Python 跑这个 ComfyUI。

    返回 {"path": ..., "source": 说明文字, "version": "3.12.4",
          "torch": "2.5.1+cu124" 或 None, "ok": bool}
    一个都找不到时 path 为空。

    搜索范围包含用户选的目录**和** main.py 所在目录的上一级——便携版的
    python_embeded 跟 ComfyUI/ 是平级的，只在 main.py 那一层找会漏掉。
    """
    roots = []
    for r in (root_dir, comfy_root, os.path.dirname(comfy_root or "")):
        if r and os.path.isdir(r) and r not in roots:
            roots.append(r)

    for base in roots:
        for rel, source in _PYTHON_CANDIDATES:
            py = os.path.join(base, *rel.split("/"))
            if not os.path.isfile(py):
                continue
            info = _probe_python(py)
            if info:
                info["source"] = source
                return info
    return {"path": "", "source": "", "version": "", "torch": None, "ok": False}


def _probe_python(py):
    """真的跑一次，确认它能用、并且看看 torch 装了没。"""
    code = (
        "import sys, json\n"
        "d={'v':'%d.%d.%d'%sys.version_info[:3],'t':None,'cuda':None}\n"
        "try:\n"
        "    import torch\n"
        "    d['t']=torch.__version__\n"
        "    d['cuda']=torch.cuda.is_available()\n"
        "except Exception: pass\n"
        "print(json.dumps(d))"
    )
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           timeout=30, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        import json
        d = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return {"path": py, "version": d.get("v", ""), "torch": d.get("t"),
            "cuda": d.get("cuda"), "ok": True}


def describe_python(info):
    """给界面用的一行说明。"""
    if not info or not info.get("path"):
        return "没有找到可用的 Python 环境"
    bits = [f"Python {info['version']}"]
    if info.get("source"):
        bits.append(info["source"])
    if info.get("torch"):
        bits.append(f"torch {info['torch']}")
        if info.get("cuda") is False:
            bits.append("⚠ 检测不到 CUDA")
    else:
        bits.append("⚠ 没装 torch，多半跑不起来")
    return "　|　".join(bits)


# ============================================================
# 模型目录
# ============================================================
#
# 名字跟 Forge 几乎没有一个对得上。这里按 ComfyUI 的 folder_paths.py 来，
# 顺带把改过名的旧目录也列进去（ComfyUI 两边都读，用户机器上两种都可能有）。
#
# (显示名, 相对路径, 是不是 LoRA 类)
MODEL_CATEGORIES = [
    ("Checkpoint 大模型",   "models/checkpoints",       False),
    ("LoRA",                "models/loras",             True),
    ("VAE",                 "models/vae",               False),
    ("扩散模型 (UNet)",     "models/diffusion_models",  False),
    ("文本编码器 (CLIP)",   "models/text_encoders",     False),
    ("CLIP Vision",         "models/clip_vision",       False),
    ("ControlNet",          "models/controlnet",        False),
    ("放大模型",            "models/upscale_models",    False),
    ("Embedding 嵌入",      "models/embeddings",        False),
    ("Hypernetwork",        "models/hypernetworks",     False),
    ("风格模型",            "models/style_models",      False),
    ("GLIGEN",              "models/gligen",            False),
    ("PhotoMaker",          "models/photomaker",        False),
    ("音频编码器",          "models/audio_encoders",    False),
    ("Diffusers",           "models/diffusers",         False),
]

# ComfyUI 改过名的目录：新名 -> 仍然会被读取的旧名。
# 用户从老版本升上来的话机器上放的是旧名，扫描时两边都得看。
LEGACY_ALIASES = {
    "models/diffusion_models": "models/unet",
    "models/text_encoders": "models/clip",
}


def category_dirs(comfy_root, rel):
    """某个分类实际要扫的目录列表（含旧名目录，存在才返回）。"""
    out = []
    for r in (rel, LEGACY_ALIASES.get(rel)):
        if not r:
            continue
        d = os.path.join(comfy_root, *r.split("/"))
        if os.path.isdir(d):
            out.append(d)
    return out


# 「图片信息」页检测到缺模型时往哪放。
# 键名跟 image_meta_core 的 role 对齐，两边必须一致。
ROLE_TO_LOCAL_FOLDER = {
    "Checkpoint":  "models/checkpoints",
    "LoRA":        "models/loras",
    "Embedding":   "models/embeddings",
    "VAE":         "models/vae",
    "ControlNet":  "models/controlnet",
    "Upscaler":    "models/upscale_models",
    "TextEncoder": "models/text_encoders",
}

# Civitai / liblib 的模型类型 -> 本地目录
CIVITAI_TYPE_TO_FOLDER = {
    "Checkpoint":        "models/checkpoints",
    "LORA":              "models/loras",
    "LoCon":             "models/loras",
    "DoRA":              "models/loras",
    "TextualInversion":  "models/embeddings",
    "VAE":               "models/vae",
    "Controlnet":        "models/controlnet",
    "Upscaler":          "models/upscale_models",
    "Hypernetwork":      "models/hypernetworks",
    "MotionModule":      "models/diffusion_models",
    "Poses":             "models/other",
    "Wildcards":         "models/other",
}


def guess_folder(model_type):
    return CIVITAI_TYPE_TO_FOLDER.get(model_type, "models/checkpoints")


# ============================================================
# extra_model_paths.yaml
# ============================================================

def read_extra_model_paths(comfy_root):
    """
    读 extra_model_paths.yaml，返回 {分类名: [绝对路径, ...]}。

    很多人（尤其是从 WebUI 转过来的）把模型放在别处共用，不读这个文件的话
    模型管理页会显示"空空如也"，但 ComfyUI 里明明有一堆模型。

    故意不引入 PyYAML —— 启动器的依赖只有 pywebview + requests。
    这个文件的结构很浅（两层缩进 + key: value），手写解析足够，
    遇到看不懂的行跳过就是，不会因为格式花哨而整个失败。
    """
    path = os.path.join(comfy_root, "extra_model_paths.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return {}

    result = {}
    base_path = ""
    cur_indent = None
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if indent == 0:
            base_path = ""          # 换了一个配置段
            cur_indent = None
            continue
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key, val = key.strip(), val.strip().strip("'\"")

        if key == "base_path":
            base_path = val
            cur_indent = indent
            continue
        if key in ("is_default", "download_model_base"):
            continue
        if not val:
            continue
        # 一个 key 下可能有多行路径，这里只认同行的写法（占绝大多数）
        for piece in val.replace("\r", "").split("\n"):
            piece = piece.strip()
            if not piece:
                continue
            full = piece if os.path.isabs(piece) else os.path.join(base_path, piece)
            result.setdefault(key, []).append(os.path.normpath(full))
    return result


# ============================================================
# 命令行参数
# ============================================================
#
# 下面几组互斥，每组最多选一个（官方文档明确写了）：
#   显存：--gpu-only / --highvram / --lowvram / --novram / --cpu
#   全局精度：--force-fp32 / --force-fp16
#   UNet 精度：--fp32-unet / --bf16-unet / --fp16-unet / --fp8_e4m3fn-unet / ...
#   VAE 精度：--fp16-vae / --fp32-vae / --bf16-vae
#   文本编码器精度：--fp16-text-enc / --fp32-text-enc / --bf16-text-enc / --fp8_*
#   注意力：--use-split-cross-attention / --use-quad-cross-attention /
#           --use-pytorch-cross-attention / --use-sage-attention / --use-flash-attention
#   缓存：--cache-ram / --cache-classic / --cache-lru / --cache-none

VRAM_OPTIONS = [
    ("自动（推荐）",                      "auto",     None),
    ("全部放显存 --gpu-only",             "gpu_only", "--gpu-only"),
    ("高显存 --highvram",                 "high",     "--highvram"),
    ("低显存 --lowvram",                  "low",      "--lowvram"),
    ("极低显存 --novram",                 "novram",   "--novram"),
    ("纯 CPU --cpu",                      "cpu",      "--cpu"),
]

GLOBAL_PRECISION_OPTIONS = [
    ("自动（推荐）",        "auto", None),
    ("强制 FP32",           "fp32", "--force-fp32"),
    ("强制 FP16",           "fp16", "--force-fp16"),
]

UNET_PRECISION_OPTIONS = [
    ("自动（推荐）",        "auto",      None),
    ("FP32",                "fp32",      "--fp32-unet"),
    ("BF16",                "bf16",      "--bf16-unet"),
    ("FP16",                "fp16",      "--fp16-unet"),
    ("FP8 e4m3fn（省显存）", "fp8_e4m3fn", "--fp8_e4m3fn-unet"),
    ("FP8 e5m2",            "fp8_e5m2",  "--fp8_e5m2-unet"),
]

VAE_PRECISION_OPTIONS = [
    ("自动（推荐）",        "auto", None),
    ("FP16（可能出黑图）",  "fp16", "--fp16-vae"),
    ("FP32（最稳）",        "fp32", "--fp32-vae"),
    ("BF16",                "bf16", "--bf16-vae"),
]

TEXT_ENC_PRECISION_OPTIONS = [
    ("自动（推荐）",         "auto",       None),
    ("FP8 e4m3fn（省显存）", "fp8_e4m3fn", "--fp8_e4m3fn-text-enc"),
    ("FP8 e5m2",             "fp8_e5m2",   "--fp8_e5m2-text-enc"),
    ("FP16",                 "fp16",       "--fp16-text-enc"),
    ("FP32",                 "fp32",       "--fp32-text-enc"),
    ("BF16",                 "bf16",       "--bf16-text-enc"),
]

ATTENTION_OPTIONS = [
    ("自动（推荐）",             "auto",    None),
    ("PyTorch 2.0",              "pytorch", "--use-pytorch-cross-attention"),
    ("Sage Attention（需另装）", "sage",    "--use-sage-attention"),
    ("Flash Attention（需另装）", "flash",  "--use-flash-attention"),
    ("Split（省显存）",          "split",   "--use-split-cross-attention"),
    ("Quad（次二次方）",         "quad",    "--use-quad-cross-attention"),
]

CACHE_OPTIONS = [
    ("默认（按内存压力）", "auto",    None),
    ("经典激进缓存",       "classic", "--cache-classic"),
    ("LRU 缓存",           "lru",     "--cache-lru"),
    ("不缓存（最省内存）", "none",    "--cache-none"),
]

PREVIEW_OPTIONS = [
    ("不显示（默认，最快）", "none",       None),
    ("自动",                 "auto",       "--preview-method auto"),
    ("latent2rgb（快）",     "latent2rgb", "--preview-method latent2rgb"),
    ("TAESD（清晰但慢）",    "taesd",      "--preview-method taesd"),
]

_OPTION_GROUPS = {
    "vram_mode":          VRAM_OPTIONS,
    "global_precision":   GLOBAL_PRECISION_OPTIONS,
    "unet_precision":     UNET_PRECISION_OPTIONS,
    "vae_precision":      VAE_PRECISION_OPTIONS,
    "text_enc_precision": TEXT_ENC_PRECISION_OPTIONS,
    "attention_impl":     ATTENTION_OPTIONS,
    "cache_mode":         CACHE_OPTIONS,
    "preview_method":     PREVIEW_OPTIONS,
}

# 勾选类开关：配置键 -> 参数
_FLAGS = [
    ("cpu_vae",                "--cpu-vae"),
    ("force_channels_last",    "--force-channels-last"),
    ("disable_smart_memory",   "--disable-smart-memory"),
    ("disable_xformers",       "--disable-xformers"),
    ("deterministic",          "--deterministic"),
    ("fast_mode",              "--fast"),
    ("disable_api_nodes",      "--disable-api-nodes"),
    ("disable_metadata",       "--disable-metadata"),
    ("enable_manager",         "--enable-manager"),
    ("multi_user",             "--multi-user"),
    ("mmap_torch_files",       "--mmap-torch-files"),
    ("disable_mmap",           "--disable-mmap"),
    ("fast_disk",              "--fast-disk"),
    ("disable_all_custom_nodes", "--disable-all-custom-nodes"),
]

DEFAULT_PORT = 8188


def build_args(cfg):
    """
    按配置生成 ComfyUI 的命令行参数列表。

    有两个参数是启动器**无条件**加的，不给用户关：

      --disable-auto-launch
          不加的话 ComfyUI 会自己开一个浏览器标签，而启动器就绪后也会开一个,
          结果就是每次启动弹两个一模一样的页面。整合包的 bat 里普遍带着
          --windows-standalone-build，那个等价于 --auto-launch，所以这个
          必须显式关掉才压得住（官方文档写明 --disable-auto-launch 优先级更高）。

      --log-stdout
          ComfyUI 默认把普通日志写到 stderr。启动器是合并读取的，本来也能收到,
          但显式走 stdout 能让输出顺序稳定，日志里不会出现前后错位。
    """
    args = ["--disable-auto-launch", "--log-stdout"]

    port = str(cfg.get("port") or "").strip()
    if port.isdigit():
        args += ["--port", port]

    if cfg.get("enable_listen"):
        listen = str(cfg.get("listen_host") or "").strip()
        args += (["--listen", listen] if listen else ["--listen"])

    if cfg.get("enable_cors"):
        args.append("--enable-cors-header")

    for key, group in _OPTION_GROUPS.items():
        val = cfg.get(key, "auto")
        for _label, k, flag in group:
            if k == val and flag:
                args += flag.split()
                break

    for key, flag in _FLAGS:
        if cfg.get(key):
            args.append(flag)

    reserve = str(cfg.get("reserve_vram_gb") or "").strip()
    if reserve:
        try:
            args += ["--reserve-vram", str(float(reserve))]
        except ValueError:
            pass

    dev = str(cfg.get("gpu_device_id") or "").strip()
    if dev:
        args += ["--cuda-device", dev]

    if cfg.get("cuda_malloc"):
        args.append("--cuda-malloc")
    elif cfg.get("disable_cuda_malloc"):
        args.append("--disable-cuda-malloc")

    lru = str(cfg.get("cache_lru_size") or "").strip()
    if cfg.get("cache_mode") == "lru" and lru.isdigit():
        args.append(lru)   # --cache-lru 后面直接跟数字

    for key, flag in (("output_dir", "--output-directory"),
                      ("input_dir", "--input-directory"),
                      ("temp_dir", "--temp-directory"),
                      ("base_dir", "--base-directory")):
        v = str(cfg.get(key) or "").strip()
        if v:
            args += [flag, v]

    extra = str(cfg.get("extra_args") or "").strip()
    if extra:
        args += extra.split()
    return args


def build_command(python_path, comfy_root, cfg):
    """完整的启动命令。"""
    return [python_path, "-s", os.path.join(comfy_root, "main.py")] + build_args(cfg)


def preview_command(cfg):
    """界面上显示的参数预览（不含 python 和 main.py）。"""
    return " ".join(build_args(cfg))


# ============================================================
# 就绪检测
# ============================================================
#
# ComfyUI 启动成功时会打印：
#     Starting server
#     To see the GUI go to: http://127.0.0.1:8188
# 端口被占用时抛的是 OSError [Errno 10048]，跟 Forge 一样。

READY_RE = re.compile(r"To see the GUI go to:\s*(https?://[^\s]+)", re.I)
# 兜底：某些版本/汉化整合包会改掉上面那句提示，退而求其次认监听地址
FALLBACK_READY_RE = re.compile(r"Starting server.*?(https?://[\d.]+:\d+)", re.I | re.S)
BIND_ERROR_RE = re.compile(r"10048|Address already in use|error while attempting to bind")


def parse_ready_url(text):
    """从日志里找出访问地址，没有返回 None。"""
    m = READY_RE.search(text) or FALLBACK_READY_RE.search(text)
    if not m:
        return None
    return m.group(1).replace("0.0.0.0", "127.0.0.1").rstrip("/")


def watch_ports(cfg):
    """端口探测的候选范围（日志没抓到时的后备判定）。"""
    port = str(cfg.get("port") or "").strip()
    if port.isdigit():
        return [int(port)]
    return list(range(DEFAULT_PORT, DEFAULT_PORT + 10))


# ============================================================
# 自定义节点
# ============================================================

def custom_nodes_dir(comfy_root):
    return os.path.join(comfy_root, "custom_nodes")


def installed_custom_nodes(comfy_root):
    d = custom_nodes_dir(comfy_root)
    if not os.path.isdir(d):
        return set()
    out = set()
    for n in os.listdir(d):
        if n.startswith(".") or n == "__pycache__":
            continue
        if os.path.isdir(os.path.join(d, n)):
            out.add(n.lower())
    return out


# (显示名, 简介, 仓库地址, 落地文件夹名)
NODE_CATALOG = [
    ("ComfyUI-Manager",
     "节点管理器。装它之后其他节点都能在 ComfyUI 界面里搜索安装，强烈建议第一个装。"
     "注意新版 ComfyUI 需要在高级选项里勾上「启用 Manager」才会加载。",
     "https://github.com/ltdrdata/ComfyUI-Manager", "ComfyUI-Manager"),
    ("ComfyUI-Custom-Scripts",
     "一堆实用小功能：提示词自动补全、节点对齐、工作流图片预览等。",
     "https://github.com/pythongosssss/ComfyUI-Custom-Scripts", "ComfyUI-Custom-Scripts"),
    ("ComfyUI_essentials",
     "常用基础节点合集，很多工作流会依赖它。",
     "https://github.com/cubiq/ComfyUI_essentials", "ComfyUI_essentials"),
    ("ComfyUI-KJNodes",
     "KJ 的工具节点集，视频和批处理相关工作流常用。",
     "https://github.com/kijai/ComfyUI-KJNodes", "ComfyUI-KJNodes"),
    ("rgthree-comfy",
     "节点组、快捷开关、进度条，工作流一大了很依赖这个。",
     "https://github.com/rgthree/rgthree-comfy", "rgthree-comfy"),
    ("ComfyUI-Impact-Pack",
     "面部修复、细节增强、检测器，相当于 WebUI 的 ADetailer。",
     "https://github.com/ltdrdata/ComfyUI-Impact-Pack", "ComfyUI-Impact-Pack"),
    ("ComfyUI_Comfyroll_CustomNodes",
     "大量构图/流程控制节点。",
     "https://github.com/Suzie1/ComfyUI_Comfyroll_CustomNodes", "ComfyUI_Comfyroll_CustomNodes"),
    ("comfyui_controlnet_aux",
     "ControlNet 预处理器合集（OpenPose、Depth、Canny 等）。",
     "https://github.com/Fannovel16/comfyui_controlnet_aux", "comfyui_controlnet_aux"),
    ("ComfyUI-VideoHelperSuite",
     "视频帧序列的读写与合成，做视频必备。",
     "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite", "ComfyUI-VideoHelperSuite"),
    ("ComfyUI-WD14-Tagger",
     "在工作流里直接反推标签。启动器自带的「WD14 反推」页是独立实现，两者不冲突。",
     "https://github.com/pythongosssss/ComfyUI-WD14-Tagger", "ComfyUI-WD14-Tagger"),
]


# ============================================================
# 输出目录
# ============================================================
#
# ComfyUI 的输出结构比 Forge 简单得多：全部进 output/，没有按日期和
# txt2img/img2img 分的子目录。所以 Forge 那套「当天文生图」按钮在这里没有对应物。

def output_dirs(comfy_root, cfg):
    """返回 [(按钮显示名, 绝对路径), ...]，只给实际存在的。"""
    custom_out = str(cfg.get("output_dir") or "").strip()
    base = custom_out or os.path.join(comfy_root, "output")
    items = [("输出目录", base)]
    for label, rel in (("输入目录", "input"), ("临时目录", "temp"),
                       ("工作流", os.path.join("user", "default", "workflows"))):
        d = os.path.join(comfy_root, rel)
        if os.path.isdir(d):
            items.append((label, d))
    return [(k, v) for k, v in items if os.path.isdir(v)]


# ============================================================
# 部署
# ============================================================

REPO_URL = "https://github.com/comfyanonymous/ComfyUI.git"

# torch 索引。ComfyUI 官方推荐的 CUDA 版本会变，这里按主流显卡给个稳的。
TORCH_INDEX = {
    "cu128": "https://download.pytorch.org/whl/cu128",
    "cu126": "https://download.pytorch.org/whl/cu126",
    "cu124": "https://download.pytorch.org/whl/cu124",
    "cpu":   "https://download.pytorch.org/whl/cpu",
}
DEFAULT_CUDA_TAG = "cu126"
