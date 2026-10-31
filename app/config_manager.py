# -*- coding: utf-8 -*-
"""
配置管理模块
负责启动器配置的读取、保存，以及把配置转换成真实的 webui 命令行参数。

重要更正说明：
Forge（不管是常规版还是 Neo 分支）在底层重写了资源管理逻辑，早已经把老
A1111 那一套 --medvram / --lowvram / --medvram-sdxl / --precision full /
--no-half / --no-half-vae / --opt-xxx-attention 全部废弃——这些参数加了
不会报错，但完全不起作用，显存怎么分配是 Forge 自动决定的（对应网页内
Settings 里的 "GPU Weight" 滑块）。所以这里改用 Forge 真正认识的参数：
--always-gpu / --always-high-vram / --always-normal-vram / --always-low-vram /
--always-no-vram / --always-cpu / --always-offload-from-vram，以及
--all-in-fp16 / --all-in-fp32。

Neo 分支（Haoming02/sd-webui-forge-classic）额外支持 --flash / --sage，
用来触发安装 Flash-Attention / Sage-Attention（这两个参数本身只负责安装
对应的包，装好之后实际会不会用由 Forge 自动判断，不是强制开关）。

INFO_ONLY_SETTINGS 涉及的选项（例如"预留显存/GPU Weight 滑块"、"共享显存
回退策略"）实际上是在 webui 网页内部的 Settings 页面里配置的，或者是
Windows 显卡驱动层面的设置，并不存在对应的命令行参数。启动器只是帮你记录
偏好、并在界面上提示你去哪里设置，不会假装通过命令行控制它们。
"""
import json
import os
import re
import shutil
import subprocess

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launcher_config.json")   # 配置放在项目根目录

DEFAULT_CONFIG = {
    "webui_root": "",
    "bat_file_name": "webui-user.bat",
    "custom_python_path": "",     # 留空则自动检测(如秋叶整合包的 python\python.exe)或用系统python
    "custom_git_path": "",        # 留空则自动检测或用系统git
    "webui_branch": "neo2",  # 默认新装/新配置就用新版 Neo 参数体系
    "webui_variant": "",      # "h3" = Forge Neo · H3 视频分支（参数体系同 neo2，只是仓库不同）
    "use_portable_env": True,    # classic / neo —— 由“环境部署”页部署后自动写入，也可以手动切换
    "gpu_device_id": "0",
    "vram_mode": "auto",          # auto / always_gpu / high / normal / low / no_vram / cpu
    "precision_mode": "auto",     # auto / fp16 / fp32
    "always_offload_from_vram": False,
    "cuda_malloc": False,
    "install_xformers": False,
    "install_flash": False,       # 仅 Neo 分支支持
    "install_sage": False,        # 仅 Neo 分支支持
    "enable_listen": False,           # 默认仅本机访问；--listen 会把 WebUI 暴露给整个局域网
    "enable_api": False,
    "enable_share": False,
    "enable_insecure_extension_access": False,  # 网页装扩展=任意 Python 代码，和 --listen 同开极其危险
    "autolaunch": False,
    "auto_open_browser_on_ready": True,
    "skip_python_version_check": False,
    "no_hashing": False,
    "theme": "auto",              # auto / dark / light
    "port": "",
    "extra_args": "",
    # 仅作记录、不产生命令行参数的选项
    "info_reserved_vram_gb": 0,
    "info_shared_vram_fallback": True,
    "info_model_hash_calc": True,
    # Civitai / liblib 相关
    "civitai_api_key": "",
    "civitai_last_dir": "",
    # liblib 的下载接口强制登录（匿名一律返回"用户未登录"），
    # 这里存用户从自己浏览器 Cookie 里复制的 usertoken，仅保存在本地配置
    "liblib_token": "",
    # 报错诊断：AI 层用用户自己的 Key（diagnose_llm.PROVIDERS）
    "diag_llm_provider": "deepseek",
    "diag_llm_api_key": "",
    "diag_llm_base_url": "",      # 留空用提供商默认地址
    "diag_llm_model": "",         # 留空用提供商默认模型
    "diag_llm_format": "openai",  # 只对「自定义」有效：openai / anthropic
    "diag_auto_fix": True,        # 规则认定的低风险修复自动执行
    "diag_auto_launcher": True,   # 启动器报错后自动诊断
    "diag_auto_ai": True,         # 启动器自动诊断时规则认不出就自动问 AI
    "diag_comfy_bridge": True,    # 启动 / 部署 ComfyUI 时放桥接插件（网页里的缺节点、缺模型报给诊断）
    "diag_notes": {},             # 给 AI 的长期说明，按诊断对象记（"inst:<id>" / "launcher"），每次诊断和接管都带上
    # 国内网络加速：auto=自动检测 / always=总是用镜像 / never=总是用原站
    "mirror_mode": "auto",
    "mirror_detected": "",   # 自动检测结果缓存（cn / global），清空可重新检测
    "github_mirror_detected": "",  # GitHub 专用探测缓存（ok / blocked），跟上面那个是独立的两套判断
    # 手动网络代理（Clash 等本机代理的 HTTP 端口），全局生效，见 proxy_manager
    "proxy_enabled": False,
    "proxy_url": "",
    # 新版 Neo (neo2) 专属参数
    "reserve_vram_gb": "",       # --reserve-vram GB
    "unet_precision": "auto",
    "vae_precision": "auto",
    "text_enc_precision": "auto",
    "attention_impl": "auto",
    "fast_fp16": False,
    "pin_shared_memory": False,
    "expandable_segments": False,
    # ComfyUI 专属（运行时开关，不是安装器）
    "comfy_sage": False,          # --use-sage-attention
    "comfy_flash": False,         # --use-flash-attention
    "comfy_fast": False,          # --fast
    "comfy_enable_manager": True,  # 装了内置 ComfyUI-Manager 依赖时自动加 --enable-manager
    # 显卡环境：""=NVIDIA（CUDA，默认）/ "rocm"=AMD（ROCm）；amd_gfx 是 A 卡架构（gfx1100 等），见 amd_rocm.py
    "gpu_backend": "",
    "amd_gfx": "",
    "deploy_comfy_nodes": None,    # 部署 ComfyUI 时顺带装的节点 id 列表；None = comfy_nodes 里的默认推荐
    # ---------------- V3：多实例 ----------------
    # 实例表：每个实例保存下面 INSTANCE_KEYS 里的全部键。顶层同名键始终是
    # 「当前实例」的投影——老代码照旧读写 cfg["webui_root"] 等，保存时由
    # save_config 自动同步回当前实例，所以单实例用户感觉不到任何变化。
    "config_version": 0,
    "instances": [],
    "active_instance": "",
    "multi_instance_ui": False,   # 手动打开多实例模式（实例数 ≥ 2 时自动打开）
    "hidden_pages": [],           # 从侧栏藏掉的页面
    "ui_theme": "terminal",       # 界面风格（web/js/themes.js 的注册表，含开屏变体）
    # 共享模型库
    "model_library_enabled": False,
    "model_library_path": "",
    # LoRA 自动整理（默认开）
    "lora_organize_enabled": True,
    "lora_organize_template": "base/type",   # base / type / base/type / type/base / instance/base/type
    "lora_organize_include_sub": False,      # 是否连已在子文件夹里的也整理
    "wd14_model": "",             # WD14 反推页上次选的模型，下次打开自动选中
    "wd14_general": 0.35,         # WD14 反推页的参数，改了就记住
    "wd14_character": 0.85,
    "wd14_include_rating": False,
    "wd14_save_txt": True,
    "wd14_auto_run": True,        # 加进图片后自动开始反推（模型已下载时）
    "wd14_view": "tags",          # 结果显示成 tags（标签块）/ text（一段文本）
    # 「图片反推」页的自然语言反推（和 LoRA 训练器的自然语言打标同一套）
    "caption_mode": "tags",               # tags（WD14 标签）/ text（自然语言）/ hybrid（标签 + 自然语言）
    "caption_source": "local",            # local（本地模型）/ api（云端 API）
    "caption_model": "toriigate",         # 本地模型，见 vlm_caption_core.VLM_MODELS
    "caption_quant": "auto",              # auto / bf16 / 8bit / 4bit
    "caption_preset": "detailed",         # 提示词模板，custom = 用 caption_prompt
    "caption_prompt": "",
    "caption_torii_format": "long",       # ToriiGate 的描述格式 long / short
    "caption_use_names": True,            # ToriiGate 识别并写出角色名
    "caption_feed_tags": True,            # 「标签 + 自然语言」时把 WD14 标签交给模型参考
    "caption_keep_tags": True,            # 「标签 + 自然语言」时结果里保留 WD14 标签
    "caption_max_tokens": 512,
    "caption_api_preset": "Google Gemini",
    "caption_api_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "caption_api_model": "gemini-2.5-flash",
    "caption_api_key": "",
    "caption_api_concurrency": 4,
    "caption_api_fallback": True,         # API 拒答时用已经下好的本地模型补上
}

CONFIG_VERSION = 3

# 属于「实例」的键：每个实例各有一份。其余键（镜像、API Key、多实例开关等）是全局的。
INSTANCE_KEYS = (
    "webui_root", "bat_file_name", "custom_python_path", "custom_git_path",
    "webui_branch", "webui_variant", "use_portable_env", "gpu_device_id", "vram_mode", "precision_mode",
    "always_offload_from_vram", "cuda_malloc", "install_xformers", "install_flash",
    "install_sage", "enable_listen", "enable_api", "enable_share",
    "enable_insecure_extension_access", "autolaunch", "auto_open_browser_on_ready",
    "skip_python_version_check", "no_hashing", "theme", "port", "extra_args",
    "info_reserved_vram_gb", "info_shared_vram_fallback", "info_model_hash_calc",
    "reserve_vram_gb", "unet_precision", "vae_precision", "text_enc_precision",
    "attention_impl", "fast_fp16", "pin_shared_memory", "expandable_segments",
    "comfy_sage", "comfy_flash", "comfy_fast",
    "gpu_backend", "amd_gfx",
)

KIND_LABELS = {"comfyui": "ComfyUI", "neo2": "Forge Neo", "neo": "Forge Neo",
               "classic": "Forge Classic"}


def kind_label(branch, variant=None):
    """实例显示名：Forge Neo 的 H3 视频分支单独叫「Forge Neo H3」"""
    if variant == "h3":
        return "Forge Neo H3"
    return KIND_LABELS.get(branch, "WebUI")


A1111_LABEL = "A1111"


def is_a1111(root):
    """A1111 原版 WebUI（秋叶 sd-webui-aki 整合包等）：有 webui.bat / launch.py，但没有 Forge 才有的
    modules_forge / backend。启动器按 Forge 的方式启动它没问题，只是它根目录也有 pyproject.toml，
    会被 guess_forge_branch 认成 Neo，界面上一直叫「Forge Neo」，用户以为自己用的就是 Neo。"""
    root = (root or "").strip().strip('"')
    return bool(root) and os.path.isfile(os.path.join(root, "webui.bat")) \
        and os.path.isfile(os.path.join(root, "launch.py")) \
        and not os.path.isdir(os.path.join(root, "modules_forge")) \
        and not os.path.isdir(os.path.join(root, "backend"))


def instance_display(inst):
    """界面上显示的 (类型标签, 名字)。A1111 显示成「A1111」；名字还是自动起的（「Forge Neo」「Forge Neo 2」）
    也跟着显示「A1111」，用户自己起的名字不动。只改显示，不动 webui_branch（启动参数照旧）。"""
    label = kind_label(inst.get("webui_branch"), inst.get("webui_variant"))
    name = inst.get("name", "")
    if inst.get("webui_branch") != "comfyui" and is_a1111(inst.get("webui_root")):
        if not name.strip() or re.fullmatch(re.escape(label) + r"( \d+)?", name.strip()):
            name = A1111_LABEL
        label = A1111_LABEL
    return label, name


def is_comfy(cfg):
    return (cfg or {}).get("webui_branch") == "comfyui"


def new_instance_id():
    import uuid
    return uuid.uuid4().hex[:8]


def make_instance(cfg=None, **over):
    """新实例：实例键取默认值（或从 cfg 拷），再叠加 over"""
    src = cfg or DEFAULT_CONFIG
    inst = {k: src.get(k, DEFAULT_CONFIG.get(k)) for k in INSTANCE_KEYS}
    inst.update({k: v for k, v in over.items() if k in INSTANCE_KEYS})
    inst["id"] = over.get("id") or new_instance_id()
    inst["name"] = over.get("name") or KIND_LABELS.get(inst.get("webui_branch"), "WebUI")
    return inst


def find_instance(cfg, iid):
    for inst in cfg.get("instances") or []:
        if inst.get("id") == iid:
            return inst
    return None


def active_instance(cfg):
    return find_instance(cfg, cfg.get("active_instance")) or \
        ((cfg.get("instances") or [None])[0])


def migrate_config(cfg):
    """V2 → V3：把顶层的启动相关键搬进「实例 1」。返回是否做了迁移。"""
    changed = False
    insts = cfg.get("instances")
    if not isinstance(insts, list):
        insts = []
        cfg["instances"] = insts
    if not insts:
        insts.append(make_instance(cfg))
        changed = True
    for inst in insts:                      # 旧版本实例缺新键时补默认值
        for k in INSTANCE_KEYS:
            if k not in inst:
                inst[k] = DEFAULT_CONFIG.get(k)
                changed = True
        if not inst.get("id"):
            inst["id"] = new_instance_id()
            changed = True
        if not inst.get("name"):
            inst["name"] = KIND_LABELS.get(inst.get("webui_branch"), "WebUI")
    if not find_instance(cfg, cfg.get("active_instance")):
        cfg["active_instance"] = insts[0]["id"]
        changed = True
    if cfg.get("config_version", 0) < CONFIG_VERSION:
        cfg["config_version"] = CONFIG_VERSION
        changed = True
    return changed


def project_active(cfg):
    """当前实例的键 → 顶层（老代码读的位置）"""
    inst = active_instance(cfg)
    if inst:
        for k in INSTANCE_KEYS:
            cfg[k] = inst.get(k, DEFAULT_CONFIG.get(k))


def absorb_active(cfg):
    """顶层 → 当前实例（保存前调用，老代码对顶层的修改不会丢）"""
    inst = active_instance(cfg)
    if inst:
        for k in INSTANCE_KEYS:
            if k in cfg:
                inst[k] = cfg[k]


def instance_cfg(cfg, iid):
    """某个实例的「有效配置」：全局键 + 该实例的实例键。给非当前实例的启动/扫描用。"""
    inst = find_instance(cfg, iid)
    if inst is None:
        return None
    if iid == cfg.get("active_instance"):
        absorb_active(cfg)
    out = {k: v for k, v in cfg.items() if k not in INSTANCE_KEYS}
    for k in INSTANCE_KEYS:
        out[k] = inst.get(k, DEFAULT_CONFIG.get(k))
    out["_iid"] = iid
    out["_name"] = inst.get("name", "")
    return out


def multi_enabled(cfg):
    return len(cfg.get("instances") or []) >= 2 or bool(cfg.get("multi_instance_ui"))

# ============================================================
# 命令行参数表（按分支区分）
#
# 踩过的坑：Forge 系一共存在两套互不兼容的参数体系，用错了会直接
# "unrecognized arguments" 启动失败：
#
#   [legacy] lllyasviel 官方 Forge 和早期 Neo：
#       显存 --always-gpu / --always-high-vram / --always-normal-vram ...
#       精度 --all-in-fp16 / --all-in-fp32
#
#   [neo2]   新版 Neo（sd-webui-forge-classic 的 neo 分支近期版本）：
#       显存 --gpu-only / --highvram / --normalvram / --lowvram /
#            --novram / --cpu，外加 --reserve-vram GB
#       精度 --force-fp32 / --force-fp16，以及分部件的
#            --fp32-unet / --bf16-vae / --fp8_e4m3fn-text-enc ...
#
# 判断办法：启动失败时看它打印的 usage，或直接跑一次 launch.py -h。
# 界面上分支选"Neo（新版参数）"就会切到 neo2 这套表。
# ============================================================

# ---- legacy（Classic / 旧版 Neo）----
VRAM_MODE_OPTIONS_LEGACY = [
    ("由 Forge 自动管理（推荐，不加任何参数）", "auto", None),
    ("始终使用 GPU 承载全部内容 (--always-gpu)", "always_gpu", "--always-gpu"),
    ("始终高显存模式 (--always-high-vram)", "high", "--always-high-vram"),
    ("始终普通显存模式 (--always-normal-vram)", "normal", "--always-normal-vram"),
    ("始终低显存模式 (--always-low-vram)", "low", "--always-low-vram"),
    ("极限低显存模式 (--always-no-vram)", "no_vram", "--always-no-vram"),
    ("仅用 CPU，极慢 (--always-cpu)", "cpu", "--always-cpu"),
]

PRECISION_MODE_OPTIONS_LEGACY = [
    ("自动（推荐）", "auto", None),
    ("全部使用 FP16 (--all-in-fp16)", "fp16", "--all-in-fp16"),
    ("全部使用 FP32，兼容性更好但更慢更占显存 (--all-in-fp32)", "fp32", "--all-in-fp32"),
]

# ---- neo2（新版 Neo）----
VRAM_MODE_OPTIONS_NEO2 = [
    ("由 Forge 自动管理（推荐，不加任何参数）", "auto", None),
    ("全部放 GPU (--gpu-only)", "always_gpu", "--gpu-only"),
    ("高显存模式 (--highvram)", "high", "--highvram"),
    ("普通显存模式 (--normalvram)", "normal", "--normalvram"),
    ("低显存模式 (--lowvram)", "low", "--lowvram"),
    ("极限低显存模式 (--novram)", "no_vram", "--novram"),
    ("仅用 CPU，极慢 (--cpu)", "cpu", "--cpu"),
]

# ComfyUI 的显存参数跟新版 Neo 同名同义，只有文案不同（不说 Forge）
VRAM_MODE_OPTIONS_COMFY = [
    ("由 ComfyUI 自动管理（推荐，不加任何参数）", "auto", None),
] + [item for item in VRAM_MODE_OPTIONS_NEO2 if item[1] != "auto"]

PRECISION_MODE_OPTIONS_NEO2 = [
    ("自动（推荐）", "auto", None),
    ("全部使用 FP16 (--force-fp16)", "fp16", "--force-fp16"),
    ("全部使用 FP32，兼容性更好但更慢更占显存 (--force-fp32)", "fp32", "--force-fp32"),
]

# 新版 Neo 独有：分部件精度。每一项都是一组互斥选择。
UNET_PRECISION_OPTIONS_NEO2 = [
    ("跟随全局设置", "auto", None),
    ("UNet FP32 (--fp32-unet)", "fp32", "--fp32-unet"),
    ("UNet BF16 (--bf16-unet)", "bf16", "--bf16-unet"),
    ("UNet FP16 (--fp16-unet)", "fp16", "--fp16-unet"),
    ("UNet FP8 e4m3fn (--fp8_e4m3fn-unet)，省显存", "fp8_e4m3fn", "--fp8_e4m3fn-unet"),
    ("UNet FP8 e5m2 (--fp8_e5m2-unet)", "fp8_e5m2", "--fp8_e5m2-unet"),
]

VAE_PRECISION_OPTIONS_NEO2 = [
    ("跟随全局设置", "auto", None),
    ("VAE FP32 (--fp32-vae)", "fp32", "--fp32-vae"),
    ("VAE BF16 (--bf16-vae)，常用", "bf16", "--bf16-vae"),
    ("VAE FP16 (--fp16-vae)", "fp16", "--fp16-vae"),
    ("VAE 放 CPU (--cpu-vae)", "cpu", "--cpu-vae"),
]

TEXT_ENC_PRECISION_OPTIONS_NEO2 = [
    ("跟随全局设置", "auto", None),
    ("文本编码器 FP32 (--fp32-text-enc)", "fp32", "--fp32-text-enc"),
    ("文本编码器 BF16 (--bf16-text-enc)", "bf16", "--bf16-text-enc"),
    ("文本编码器 FP16 (--fp16-text-enc)", "fp16", "--fp16-text-enc"),
    ("文本编码器 FP8 e4m3fn (--fp8_e4m3fn-text-enc)", "fp8_e4m3fn", "--fp8_e4m3fn-text-enc"),
    ("文本编码器放 CPU (--cpu-text-enc)", "cpu", "--cpu-text-enc"),
]

ATTENTION_OPTIONS_NEO2 = [
    ("自动（推荐）", "auto", None),
    ("PyTorch 原生 cross attention (--use-pytorch-cross-attention)", "pytorch",
     "--use-pytorch-cross-attention"),
]

# 兼容旧代码/旧配置的别名：默认仍指向 legacy 那套
VRAM_MODE_OPTIONS = VRAM_MODE_OPTIONS_LEGACY
PRECISION_MODE_OPTIONS = PRECISION_MODE_OPTIONS_LEGACY


def uses_new_neo_args(cfg):
    """当前分支是否使用新版 Neo 参数体系。
    ComfyUI 也算：新版 Neo 的显存/精度参数本来就是照搬 ComfyUI 的，名字完全一样。"""
    return cfg.get("webui_branch") in ("neo2", "comfyui")


def vram_options_for(cfg):
    return VRAM_MODE_OPTIONS_NEO2 if uses_new_neo_args(cfg) else VRAM_MODE_OPTIONS_LEGACY


def precision_options_for(cfg):
    return PRECISION_MODE_OPTIONS_NEO2 if uses_new_neo_args(cfg) else PRECISION_MODE_OPTIONS_LEGACY


def decode_process_output(data):
    """
    解码子进程（git / cmd.exe / webui.bat 等）输出的原始字节。

    这些输出的编码并不统一：Git for Windows 的信息通常是 UTF-8，但
    cmd.exe 自身、webui.bat 里 echo 出来的中文/日文提示、以及不少
    Windows 原生工具走的是系统 ANSI 代码页——简体中文 Windows 是
    GBK/CP936，日文 Windows 是 Shift-JIS/CP932。

    之前统一按 "utf-8" + errors="replace" 硬解码：一旦字节其实是本地
    代码页，非法序列会被替换成 U+FFFD 且不可逆——原始内容永久丢失，
    界面上看到的就是一片乱码问号。

    没有直接按 "utf-8 -> gbk -> cp932" 依次硬试：GBK 对双字节的校验很
    宽松，很容易把本来是 CP932（日文）的字节"成功"解码成完全不相干的
    错误文本而不报错，试的顺序反而会挑出错的那个——所以优先用运行本机
    当前实际的 ANSI 代码页（locale.getpreferredencoding()，在目标 Windows
    机器上会准确得到 cp936 或 cp932），只有连这个都失败时才依次尝试
    utf-8/gbk/cp932，最后再退到 utf-8+replace 兜底。
    """
    if not data.isascii():
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        try:
            import locale
            local_enc = locale.getpreferredencoding(False)
            if local_enc and local_enc.lower() not in ("utf-8", "utf8", "ascii"):
                return data.decode(local_enc)
        except (UnicodeDecodeError, LookupError, ImportError):
            pass
        for enc in ("gbk", "cp932"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    return data.decode("ascii")


def _lookup(options, value):
    for label, k, arg in options:
        if k == value:
            return arg
    return None


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    loaded = False
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update(saved)
            loaded = True
        except Exception:
            pass
    old_version = cfg.get("config_version", 0)
    if migrate_config(cfg):
        if loaded and old_version < CONFIG_VERSION:
            # 迁移前备份一份 V2 配置，出问题可以手动还原
            try:
                bak = CONFIG_PATH + ".v2.bak"
                if not os.path.exists(bak):
                    shutil.copyfile(CONFIG_PATH, bak)
            except OSError:
                pass
    project_active(cfg)
    adapt_to_machine(cfg, loaded)
    return cfg


# ============================================================
# 换电脑（分发给别人）
# ============================================================
# 启动器经常是整个文件夹打包发给别人的，launcher_config.json 也跟着过去了：
# 里面全是原主人电脑上的路径（WebUI 根目录、Python、模型库……）和原主人的
# Civitai API Key / liblib 登录凭证。配置里记一个本机指纹，读到别的电脑写的
# 配置就把这些「本机专属」的东西清掉，界面偏好（主题、镜像模式等）保留。

# 顶层里属于「这台电脑」或「这个人」的键（实例里的路径另外处理）
_MACHINE_KEYS = ("model_library_path", "civitai_api_key", "liblib_token", "diag_llm_api_key", "caption_api_key",
                 "mirror_detected", "github_mirror_detected")
_PATH_SUFFIXES = ("_dir", "_path", "_root", "_dirs")


def machine_id():
    """这台电脑的指纹（Windows 的 MachineGuid + 计算机名，取哈希）"""
    import hashlib
    import platform
    parts = []
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                               winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            parts.append(str(winreg.QueryValueEx(k, "MachineGuid")[0]))
        except OSError:
            pass
    parts.append(platform.node() or "")
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def _reset_machine_specific(cfg):
    # 实例：只留当前实例的设置（显存、精度等偏好），路径清空；其余实例是原主人的，丢掉
    inst = active_instance(cfg) or make_instance(DEFAULT_CONFIG)
    for k in ("webui_root", "custom_python_path", "custom_git_path", "port"):
        inst[k] = DEFAULT_CONFIG.get(k, "")
    inst["name"] = KIND_LABELS.get(inst.get("webui_branch"), "WebUI")
    cfg["instances"] = [inst]
    cfg["active_instance"] = inst["id"]
    cfg["multi_instance_ui"] = False
    for k in _MACHINE_KEYS:
        cfg[k] = DEFAULT_CONFIG.get(k, "")
    cfg["model_library_enabled"] = False
    for k, v in list(cfg.items()):
        if k in INSTANCE_KEYS or k.startswith("_") or not isinstance(v, (str, list)):
            continue
        if k.endswith(_PATH_SUFFIXES):
            cfg[k] = DEFAULT_CONFIG.get(k, "" if isinstance(v, str) else [])
    project_active(cfg)


def adapt_to_machine(cfg, loaded):
    """
    读到别的电脑写的配置 → 清掉本机专属内容，并标记 cfg["_new_machine"]（不落盘）。
    第一次运行 / 老版本配置（还没有指纹）→ 只记下本机指纹，什么都不动。
    另外，填了但这台电脑上不存在的 Python / Git 路径一律清空（留空会自动检测）。
    """
    mid = machine_id()
    saved = cfg.get("machine_id") or ""
    changed = False
    if loaded and saved and saved != mid:
        _reset_machine_specific(cfg)
        cfg["_new_machine"] = True
        changed = True
    if saved != mid:
        cfg["machine_id"] = mid
        changed = True
    for inst in cfg.get("instances") or []:
        for k in ("custom_python_path", "custom_git_path"):
            v = (inst.get(k) or "").strip().strip('"')
            if v and not os.path.exists(v):
                inst[k] = ""
                changed = True
    if changed:
        project_active(cfg)
        try:
            save_config(cfg)
        except OSError:
            pass


def write_theme_file(cfg):
    """把界面风格写成 web/js/current_theme.js。

    开屏动画在后端应答之前就要知道用哪套风格，而 pywebview 每次启动的端口
    都随机，localStorage 按 origin（含端口）隔离、跨启动根本拿不到——所以
    风格走磁盘文件。每次保存配置和启动时都重写，ZIP 覆盖更新也冲不掉。
    """
    theme = str(cfg.get("ui_theme") or "terminal")
    content = ("/* 由启动器按当前配置自动生成，请勿手改（改了也会被覆盖） */\n"
               f"window.WWY_THEME_ID = {json.dumps(theme)};\n")
    path = os.path.join(os.path.dirname(CONFIG_PATH), "web", "js", "current_theme.js")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == content:
                    return
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        pass


def save_config(cfg):
    # 老代码直接改顶层键（webui_root 等），保存前先同步回当前实例
    if cfg.get("instances"):
        absorb_active(cfg)
    write_theme_file(cfg)
    # 先写临时文件再原子替换：直接 "w" 打开会先截断原文件，
    # 写入中断（断电/磁盘满/杀软）就留下一份空配置，下次启动回到默认
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in cfg.items() if not k.startswith("_")},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


def _lookup(options, value):
    for label, k, arg in options:
        if k == value:
            return arg
    return None


def detect_bundled_python(root_dir):
    """
    检测类似"秋叶整合包"这种自带 python\\python.exe 的目录结构。
    找到就返回绝对路径，否则返回 None（表示用系统 PATH 里的 python）。
    """
    candidate = os.path.join(root_dir, "python", "python.exe")
    return candidate if os.path.exists(candidate) else None


def detect_bundled_git(root_dir):
    """检测类似 git\\cmd\\git.exe 这种自带 git 的目录结构"""
    candidate = os.path.join(root_dir, "git", "cmd", "git.exe")
    return candidate if os.path.exists(candidate) else None


def _quote_if_needed(path):
    """
    路径里有空格或 cmd 元字符（&()^ 等）时补上引号。

    webui.bat 内部是直接 %PYTHON% 这样展开使用的，没有自己加引号，
    所以 "C:\\ai about\\webui\\python\\python.exe" 这种带空格的路径会被 cmd
    从空格处切断，把 C:\\ai 当成命令去执行，报 9009（找不到命令）；
    "D:\\AI&tools\\python.exe" 这种带 & 的更阴——会被当成两条命令拼接执行。
    这里预先把值包上引号，展开后就是一个完整参数。
    路径本身已经带引号时不重复添加。
    """
    path = path.strip()
    if not path or (path.startswith('"') and path.endswith('"')):
        return path
    if any(c in path for c in ' &()^!%,;~'):
        return f'"{path}"'
    return path


_PY_DLL_RE = re.compile(r"^python3(\d+)\.dll$", re.IGNORECASE)


def python_version_of(python_exe):
    """python.exe 的大版本号 "3.12"；拿不到返回 None。
    先看同目录的 python3XX.dll（不用启动进程，Windows 版 CPython / 嵌入式版都有），再退回实际运行一次。"""
    python_exe = (python_exe or "").strip().strip('"')
    if not python_exe or not os.path.isfile(python_exe):
        return None
    try:
        for name in os.listdir(os.path.dirname(python_exe)):
            m = _PY_DLL_RE.match(name)
            if m:
                return f"3.{m.group(1)}"
    except OSError:
        pass
    try:
        r = subprocess.run([python_exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip()
        return out if re.fullmatch(r"\d+\.\d+", out) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _read_pyvenv_cfg(root_dir):
    """venv\\pyvenv.cfg 的键值（键小写）；没有返回 {}"""
    cfg_path = os.path.join(root_dir, "venv", "pyvenv.cfg")
    out = {}
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                key, sep, val = line.partition("=")
                if sep:
                    out[key.strip().lower()] = val.strip()
    except OSError:
        pass
    return out


def venv_python_version(root_dir):
    """venv 建立时所用 Python 的大版本（pyvenv.cfg 的 version / version_info），如 "3.12"；拿不到返回 None"""
    info = _read_pyvenv_cfg(root_dir)
    m = re.match(r"(\d+)\.(\d+)", info.get("version") or info.get("version_info") or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


def venv_version_issue(root_dir, python_exe):
    """
    venv 和 Python 大版本对不上时返回说明文字，否则返回 None。

    venv\\Lib\\site-packages 里的 numpy / torch 等二进制包是按建 venv 时的 Python 版本编译的
    （文件名带 cp312 这类标记），换成别的大版本解释器就整批加载失败，表现为「检测不到显卡」秒退。
    两种情况：
      1) pyvenv.cfg 的 home 已指向另一个大版本（被改过——旧版启动器自动修复 home 时不比较版本）；
      2) home 指向的 Python 已不存在，而这次能用的 Python 是另一个大版本（无法安全修复）。
    """
    venv_ver = venv_python_version(root_dir)
    if not venv_ver:
        return None
    home = _read_pyvenv_cfg(root_dir).get("home", "")
    home_exe = os.path.join(home, "python.exe") if home else ""
    home_ver = python_version_of(home_exe) if home_exe and os.path.isfile(home_exe) else None
    if home_ver == venv_ver:
        return None  # venv 自己指向的解释器版本正确，能正常运行
    use_ver = python_version_of(python_exe) if python_exe else None
    if home_ver is None and (use_ver is None or use_ver == venv_ver):
        return None  # 只是目录搬过家：home 失效但版本一致，启动时会自动改 home
    actual = home_ver or use_ver
    return (
        f"WebUI 的 venv 是用 Python {venv_ver} 建的，但现在它会用 Python {actual} 来运行。\n\n"
        f"venv 里的 numpy、torch 等二进制包只能在 Python {venv_ver} 下加载，"
        "直接启动会报「检测不到显卡」或 DLL / 模块加载失败后闪退。\n\n"
        "解决办法（二选一）：\n"
        "  · 点「重建 venv」：删除旧 venv，WebUI 启动时用当前 Python 重新安装依赖"
        "（需要联网下载几个 GB，第一次启动会比较久）；\n"
        f"  · 或在「高级选项 → Python 解释器」里指定一个 Python {venv_ver}。"
    )


def sync_venv_pyvenv_cfg(root_dir, python_exe, log_cb=None):
    """
    检测并自动修复 venv\\pyvenv.cfg 里过期的 home 路径——不需要用户
    手动改文件，也不需要单独的修复工具，每次启动/部署前自动跑一遍。

    背景（这是这套启动器踩过最多次的一类坑）：venv 建立时会把创建它的
    Python 解释器所在目录，写死记录在 pyvenv.cfg 的 "home" 字段里。
    Windows 上 venv\\Scripts\\python.exe 实际是个"启动器桩"
    （venvlauncher），真正运行时会去读这个 home 字段来定位底层解释器——
    只要整个安装目录被移动、改名、换过盘符（哪怕只是去掉了路径里的一个
    空格），这个字段就会过期，表现为一句令人摸不着头脑的
    "did not find executable at 'X\\python\\python.exe': ???????????"。
    venv 里已经装好的 torch 等几个 GB 的依赖完全无损，只是这一条记录
    没跟着同步而已。

    做法：把 pyvenv.cfg 里的 home 值和"当前实际会用到的 Python 所在
    目录"比较（不区分大小写、统一斜杠方向），不一致就原地重写那一行，
    首次修改前备份一份 .bak。python_exe 传入的应该是已经解析好的、
    这次实际要用的 Python 可执行文件完整路径（不管是自动检测到的
    便携版，还是用户手动指定的路径）——跟真正用来启动 webui.bat 的
    PYTHON 环境变量必须是同一个值，这样"记录的路径"和"实际会用的路径"
    才有比较意义。

    返回 True 表示做了修复（本次调用重写了文件），False 表示不需要
    改动，或者没有 venv/pyvenv.cfg 可改。
    """
    if not python_exe:
        return False
    python_exe = python_exe.strip().strip('"')
    if not python_exe:
        return False

    cfg_path = os.path.join(root_dir, "venv", "pyvenv.cfg")
    if not os.path.exists(cfg_path):
        return False

    expected_home = os.path.dirname(python_exe)
    if not expected_home:
        return False

    # 大版本不同绝不能改 home：venv 里的二进制包是按原版本编译的，改过去会整批加载失败
    venv_ver, use_ver = venv_python_version(root_dir), python_version_of(python_exe)
    if venv_ver and use_ver and venv_ver != use_ver:
        if log_cb:
            log_cb(f"[启动器] venv 是 Python {venv_ver} 建的，要用的 Python 是 {use_ver}，"
                   "版本不同，不修改 venv\\pyvenv.cfg")
        return False

    def _norm(p):
        return p.replace("/", "\\").rstrip("\\").lower()

    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return False

    changed = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        key, sep, val = stripped.partition("=")
        if sep and key.strip().lower() == "home":
            current_home = val.strip()
            if _norm(current_home) != _norm(expected_home):
                new_lines.append(f"home = {expected_home}\n")
                changed = True
                continue
        new_lines.append(line)

    if not changed:
        return False

    backup_path = cfg_path + ".bak"
    if not os.path.exists(backup_path):
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass  # 备份失败不阻止修复本身，总比继续用过期路径强

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except OSError as e:
        if log_cb:
            log_cb(f"[启动器] 检测到 venv 记录的 Python 路径已过期，但自动同步失败: {e}")
        return False

    if log_cb:
        log_cb(f"[启动器] 检测到 venv 记录的 Python 路径已过期（安装目录被移动/改名过），已自动同步为: {expected_home}")
    return True


def _venv_health(venv_dir):
    """venv 健康检查，返回 "ok" / "broken" / "unknown"。

    "broken" 只表示【确认坏】：解释器缺失，或 pip 明确返回非零。
    超时、OSError 等【检测失败】一律归 "unknown"——慢磁盘、杀软扫描
    都能把 pip --version 拖过 30 秒，这时删错环境的代价（几个 GB 依赖
    重下）远大于暂时留着它，宁可保守。
    """
    if os.name == "nt":
        py = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        py = os.path.join(venv_dir, "bin", "python")
    if not os.path.exists(py):
        return "broken"
    try:
        r = subprocess.run([py, "-m", "pip", "--version"],
                           capture_output=True, timeout=30)
        return "ok" if r.returncode == 0 else "broken"
    except Exception:
        return "unknown"


def venv_is_usable(venv_dir):
    """能否继续使用：只有确认坏了才算不可用（检测失败时保守视为可用）"""
    return _venv_health(venv_dir) != "broken"


def venv_is_definitely_broken(venv_dir):
    """是否确认损坏（删除判断专用——拿不准的一律不删，防误删完好环境）"""
    return os.path.isdir(venv_dir) and _venv_health(venv_dir) == "broken"


def build_launch_env_overrides(cfg, root_dir, notes=None):
    """
    生成要覆盖注入子进程环境变量的字典（只包含需要覆盖的项，值为空的不放进去，
    这样 webui.bat 内部 `if not defined XXX` 的判断才能按预期工作，
    走它自己的默认逻辑，而不是被我们塞进去一个空字符串搞坏判断）。
    """
    # WebUI 的输出接的是管道：不开实时输出时 print 的内容攒满几 KB 才送出来，运行日志里长时间一片空白、
    # 认不出监听地址（A1111 启动信息几乎全走 print，装依赖时能好几分钟没一行）；
    # 不指定 UTF-8 时按系统代码页输出，中文 Windows 上打印 GBK 里没有的字会报 'gbk' codec can't encode。
    # 启动 ComfyUI 一直有这两项（build_comfy_env），WebUI 以前漏了
    overrides = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

    custom_python = (cfg.get("custom_python_path") or "").strip()
    bundled_python = ""
    if not custom_python:
        bundled_python = detect_bundled_python(root_dir) or ""
        custom_python = bundled_python
    if custom_python:
        overrides["PYTHON"] = _quote_if_needed(custom_python)

    custom_git = (cfg.get("custom_git_path") or "").strip()
    if not custom_git:
        custom_git = detect_bundled_git(root_dir) or ""
    if custom_git:
        # 注意：GIT 的值必须是未加引号的原始路径。Neo 的 webui.bat 有
        #   if defined GIT (set "GIT_PYTHON_GIT_EXECUTABLE=%GIT%")
        # 会把 GIT 原样拷给 GitPython——一旦带了引号（_quote_if_needed 对
        # 含空格路径加引号），GitPython 会把引号当成路径的一部分去执行，
        # 导入期直接抛 "Bad git executable"（部署目录带空格时必现，
        # 且会覆盖我们下面单独注入的正确值）。launch.py 里 GIT 也按
        # subprocess 列表参数/自行包引号使用，同样要原始路径。
        overrides["GIT"] = custom_git
        # Forge 的 modules/gitpython_hack.py 会 import git（GitPython 库），
        # 而 GitPython 在导入时就要定位 git 可执行文件——它只认 PATH 和自己的
        # GIT_PYTHON_GIT_EXECUTABLE 变量，完全不理会 Forge 的 GIT 变量。
        # 便携版 Git 不在 PATH 里时会直接抛
        # "ImportError: Failed to initialize: Bad git executable."
        # 所以这里额外注入 GitPython 专用变量，并把 git 目录加进 PATH。
        # 注意这个变量的值不能带引号（GitPython 是直接拿去当路径用的，
        # 不经过 shell 解析），所以用未加引号的原始路径。
        overrides["GIT_PYTHON_GIT_EXECUTABLE"] = custom_git
        # 把 git 所在目录挪到 PATH 最前面，这样即使有别的组件是直接
        # 调用 "git" 命令（而不是读环境变量）也能找到便携版。
        # 用 rsplit 而不是 os.path.dirname：路径是 Windows 风格的反斜杠，
        # 在非 Windows 上跑单元测试时 dirname 会返回空字符串。
        git_dir = custom_git.replace("/", "\\").rsplit("\\", 1)[0]
        if git_dir and git_dir != custom_git:
            overrides["PATH"] = git_dir + ";" + os.environ.get("PATH", "")

    if bundled_python:
        # 自带的便携 Python 是这个环境专用的，依赖直接装进去即可（秋叶整合包
        # 就是这么干的）。而且 bootstrap 下载的 python-build-standalone 便携
        # 构建 ensurepip 不全，由它建出的 venv 没有 pip，webui.bat 走 venv
        # 必炸 "No module named pip"。Forge/A1111 的约定：VENV_DIR=- 表示
        # 跳过 venv、直接用 %PYTHON%。
        # 例外：旧版本部署留下的完好 venv（依赖已装好）继续用，别浪费。
        overrides["VENV_DIR"] = (
            "venv" if venv_is_usable(os.path.join(root_dir, "venv")) else "-")
    else:
        # 用户自己的 Python 或系统 PATH 里的 Python（可能还装着别的工具）——
        # 必须套 venv，不能把 Forge 的 torch 等几个 GB 的依赖灌进去。
        overrides["VENV_DIR"] = "venv"
    overrides["COMMANDLINE_ARGS"] = build_commandline_args(cfg, root_dir, notes)

    # Forge 的 launch.py 会自己调 pip 装 torch 等依赖（2GB 级别），
    # 那部分不经过我们的代码，只能靠环境变量让它也走国内镜像。
    try:
        import mirror_manager as mm
        if mm.resolve_mode(cfg):
            overrides.update(mm.pip_env_overrides(True))
            # HuggingFace 模型下载也要走镜像——否则部署成功后的首次运行
            # 才撞墙（CLIP/VAE/模型下载超时），体验最差
            overrides["HF_ENDPOINT"] = mm.HF_MIRROR
            # torch 专用的关键一步：Forge 装 torch 时在命令行显式传了
            # --extra-index-url，会盖掉 PIP_EXTRA_INDEX_URL 环境变量，只能靠
            # Forge 原生支持的 TORCH_INDEX_URL 覆盖（否则国内裸连
            # download.pytorch.org 必炸 "Couldn't install PyTorch"）。
            # cuda tag 从克隆下来的 launch_utils.py 里提取，保证跟 Forge
            # 当前版本要装的完全一致；提取不到就不注入，保持官方默认。
            # 用户自己在系统环境里设过 TORCH_COMMAND / TORCH_INDEX_URL 的
            # 不插手——用户显式配置优先级最高。
            if "TORCH_COMMAND" not in os.environ and "TORCH_INDEX_URL" not in os.environ \
                    and cfg.get("gpu_backend") != "rocm":
                torch_mirror = mm.forge_torch_mirror_index(root_dir)
                if torch_mirror:
                    overrides["TORCH_INDEX_URL"] = torch_mirror
    except Exception:
        pass  # 加速是锦上添花，任何异常都不该影响正常启动

    if cfg.get("gpu_backend") == "rocm":
        # A 卡：环境变量照 CS1o 教程；还没装 torch 时让 Forge 直接装 ROCm 版
        # （Forge 执行 "<python>" -m <TORCH_COMMAND>，装完顺手 rocm-sdk init）
        import amd_rocm
        overrides.update(amd_rocm.ROCM_ENV)
        gfx = (cfg.get("amd_gfx") or "").strip()
        if gfx and "TORCH_COMMAND" not in os.environ:
            if overrides.get("VENV_DIR") == "-" and overrides.get("PYTHON"):
                scripts = os.path.join(os.path.dirname(overrides["PYTHON"].strip('"')), "Scripts")
            else:
                scripts = os.path.join(root_dir, "venv", "Scripts")
            overrides["TORCH_COMMAND"] = amd_rocm.forge_torch_command(gfx, scripts)
        return overrides

    # 显卡驱动带不动 Forge 默认的 torch（Neo 现在是 cu130，要 580+ 驱动）：
    # 换成驱动支持的 CUDA 版本；已经装了带不动的 torch 就让这次启动重装
    try:
        compat, reinstall = torch_compat_overrides(cfg, root_dir, notes)
        overrides.update(compat)
        if reinstall:
            extra = " --reinstall-torch"
            if "--xformers" in overrides.get("COMMANDLINE_ARGS", ""):
                extra += " --reinstall-xformers"
            overrides["COMMANDLINE_ARGS"] = (overrides.get("COMMANDLINE_ARGS", "") + extra).strip()
    except Exception:
        pass

    return overrides


def torch_compat_overrides(cfg, root_dir, notes=None):
    """
    返回 (环境变量覆盖, 是否需要重装 torch)。驱动信息拿不到、目标不是 cuXXX、
    用户自己设了 TORCH_COMMAND 时一律不插手。详见 cuda_compat.py。
    """
    if "TORCH_COMMAND" in os.environ:
        return {}, False
    import cuda_compat as cc
    import torch_bootstrap as tb
    specs, tag = tb._parse_torch_spec(root_dir)
    if not specs:
        return {}, False
    driver = cc.driver_cuda_version()
    new_tag = cc.effective_tag(tag, driver)
    if new_tag == tag:
        return {}, False
    try:
        import mirror_manager as mm
        use_mirror = mm.resolve_mode(cfg)
        off_base, mirror_bases = mm.PYTORCH_OFFICIAL_WHL, list(mm.PYTORCH_MIRROR_BASES)
    except Exception:
        use_mirror, off_base, mirror_bases = False, "https://download.pytorch.org/whl", []

    def bases_for(t):
        official, mirrors = f"{off_base}/{t}", [f"{b}/{t}" for b in mirror_bases]
        return mirrors + [official] if use_mirror else [official] + mirrors

    py = (cfg.get("custom_python_path") or "").strip() or detect_bundled_python(root_dir) or ""
    venv_py = os.path.join(root_dir, "venv", "Scripts", "python.exe")
    pytag = cc.python_tag(venv_py if os.path.isfile(venv_py) else py)
    # 同一个 CUDA 大版本里每个 tag 都试（2.12 起 CUDA 12 只有 cu126、没有 cu128），
    # 实在没有再退到 WebUI 自己写着的备选 torch 版本
    new_specs, new_tag, base = cc.resolve_compat(
        [specs] + tb._parse_alt_torch_specs(root_dir), tag, bases_for, pytag, driver)
    if not new_specs:
        # 这个 WebUI 要的 torch 在驱动支持的 CUDA 版本下没有构建：硬装老 torch 只会更糟
        if notes is not None:
            notes.append(f"[启动器] 显卡驱动最高支持 CUDA {cc.fmt_cuda(driver)}，而这个 WebUI 需要的 "
                         f"{'、'.join(f'{n} {v}' for n, v in specs)} 没有驱动能用的版本，"
                         "请把显卡驱动升级到 580 以上（NVIDIA 官网或 GeForce Experience / NVIDIA App 更新）")
        return {}, False
    index = base or bases_for(new_tag)[0]
    out = {
        "TORCH_INDEX_URL": index,
        "TORCH_COMMAND": "pip install " + " ".join(f"{n}=={v}" for n, v in new_specs)
                         + f" --extra-index-url {index}",
    }
    ver, itag = cc.installed_torch_tag(cc.forge_site_packages(root_dir, py))
    need = cc.tag_cuda(itag) if itag else None
    target_torch = dict(new_specs).get("torch", "")
    # 驱动带不动已装的 torch，或者已装的不是这次要的版本（比如之前装错的老版本）→ 重装
    reinstall = bool(ver and ((need and driver and need[0] > driver[0])
                              or (target_torch and f"{ver}+{itag}" != target_torch)))
    if notes is not None:
        target = "、".join(f"{n} {v}" for n, v in new_specs)
        if new_tag == cc.LEGACY_TAG and cc.is_legacy_gpu():
            notes.append(f"[启动器] 显卡（{'、'.join(cc.gpu_names())}）架构较老，新版 torch 已不支持，"
                         f"已自动改装老卡专用版本：{target}")
        else:
            notes.append(f"[启动器] 显卡驱动最高支持 CUDA {cc.fmt_cuda(driver)}，WebUI 默认要装的 torch 是 {tag}"
                         f"（需要 {cc.DRIVER_MIN} 以上的驱动），已自动改装兼容版本：{target}。"
                         "建议有空把显卡驱动升级一下，之后就能直接用新版")
        if reinstall:
            notes.append(f"[启动器] 当前环境里已装的 torch {ver}+{itag} 驱动带不动，这次启动会自动重装"
                         "（几个 GB，需要一点时间）。想继续用新版 torch 的话，把显卡驱动升级到 580 以上即可")
    return out, reinstall


# ============================================================
# 子进程环境卫生（环境污染消毒）
# ============================================================
# 用户系统里残留的这些变量会劫持便携环境的行为，而且都是小白自己绝对
# 查不出来的：PYTHONHOME/PYTHONPATH 让便携 Python 起不来或导入错包；
# PIP_TARGET/PIP_USER 让依赖装进系统目录、venv 里 import 不到；
# PIP_NO_INDEX/PIP_FIND_LINKS 让 pip 假装找不到包；GIT_DIR 等让 git
# 操作错误的仓库；CONDA*/VIRTUAL_ENV 干扰解释器选择；HF_HUB_OFFLINE
# 会害死首次模型下载。统一剥掉，再叠加我们自己的覆盖变量。
_ENV_STRIP_EXACT = {
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "__PYVENV_LAUNCHER__",
    "PIP_TARGET", "PIP_USER", "PIP_REQUIRE_VIRTUALENV",
    "PIP_NO_INDEX", "PIP_FIND_LINKS", "PIP_CONSTRAINT", "PIP_REQUIRE_HASHES",
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ASKPASS", "SSH_ASKPASS", "HF_HUB_OFFLINE",
    # 设了它 cmd.exe 就不在当前目录里找程序：`cmd /c webui.bat` 和 webui.bat 内部的
    # `call webui-user.bat` 全都报「不是内部或外部命令」（部分安全软件/开发工具会设它）
    "NODEFAULTCURRENTDIRECTORYINEXEPATH",
}
_ENV_STRIP_PREFIX = ("CONDA",)


def _empty_gitconfig_path():
    """供 GIT_CONFIG_GLOBAL 指向的空白配置文件：隔离用户全局 .gitconfig 里
    的残留代理、url.insteadOf 重写、credential helper（过期代理地址和弹
    凭据窗是 clone 失败/卡死的常见来源）。不用 NUL——Windows 上 git 读
    NUL 的行为不可靠。注意不设 GIT_CONFIG_NOSYSTEM：便携 Git 的吊销检查
    关闭写在它自己的 etc/gitconfig（系统级）里，禁掉会让那个修复失效。"""
    d = os.path.join(os.path.dirname(CONFIG_PATH), "tmp")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "empty_gitconfig")
    if not os.path.exists(f):
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("# 启动器故意留空：隔离用户全局 git 配置污染\n")
    return f


def build_subprocess_env(cfg, root_dir):
    """给 webui.bat / git 等子进程用的干净环境：剥掉污染变量 + 叠加启动器
    覆盖变量 + git 交互隔离。代理变量（HTTP_PROXY 等）保留——显式设过
    代理环境的用户通常需要它。"""
    env = {}
    for k, v in os.environ.items():
        ku = k.upper()  # Windows 环境变量名不区分大小写
        if ku in _ENV_STRIP_EXACT or any(ku.startswith(p) for p in _ENV_STRIP_PREFIX):
            continue
        env[k] = v
    env.update(build_launch_env_overrides(cfg, root_dir))
    try:
        env["GIT_CONFIG_GLOBAL"] = _empty_gitconfig_path()
    except OSError:
        pass
    # 任何需要交互的 git 场景（凭据弹窗/终端提问）都立刻报错，
    # 而不是把部署卡死在没有输出的黑盒里
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    import proxy_manager as pm
    return pm.with_local_bypass(env)   # 没开代理也要带，见 proxy_manager 开头的说明


# ============================================================
# ComfyUI
# ============================================================
COMFY_ENTRY = "main.py"


def comfy_layout(root):
    """
    识别 ComfyUI 的目录结构，返回 (comfy_dir, 自带 python 路径或 "")；不是 ComfyUI 返回 (None, "")。

    支持三种常见形态：
      官方便携包   ComfyUI_windows_portable/{python_embeded, ComfyUI/main.py}
      秋叶等整合包 root/{main.py, python/python.exe}；root 选到里层 ComfyUI
                   目录时，自带 python 在上一级（ComfyUI-aki-vX/{ComfyUI, python}）
      自己 git 的   root/{main.py, venv 或 .venv}
    根目录选到便携包外层或 ComfyUI 本体都能认。
    """
    if not root or not os.path.isdir(root):
        return None, ""
    cand = [root, os.path.join(root, "ComfyUI")]
    comfy_dir = None
    for d in cand:
        if os.path.isfile(os.path.join(d, COMFY_ENTRY)) and (
                os.path.isdir(os.path.join(d, "comfy")) or os.path.isfile(os.path.join(d, "nodes.py"))):
            comfy_dir = d
            break
    if not comfy_dir:
        return None, ""
    parent = os.path.dirname(comfy_dir)
    pys = [
        os.path.join(comfy_dir, "python_embeded", "python.exe"),
        os.path.join(parent, "python_embeded", "python.exe"),
        os.path.join(root, "python_embeded", "python.exe"),
        os.path.join(comfy_dir, "python", "python.exe"),
        os.path.join(root, "python", "python.exe"),
        # 秋叶 ComfyUI 整合包：ComfyUI-aki-vX/{ComfyUI, python/python.exe}，
        # root 选到里层 ComfyUI 目录时，自带 python 在它的上一级
        os.path.join(parent, "python", "python.exe"),
        os.path.join(comfy_dir, "venv", "Scripts", "python.exe"),
        os.path.join(comfy_dir, ".venv", "Scripts", "python.exe"),
        os.path.join(root, "venv", "Scripts", "python.exe"),
        os.path.join(root, ".venv", "Scripts", "python.exe"),
        os.path.join(parent, "venv", "Scripts", "python.exe"),
        os.path.join(parent, ".venv", "Scripts", "python.exe"),
    ]
    for p in pys:
        if os.path.isfile(p):
            return comfy_dir, p
    return comfy_dir, ""


def detect_kind(root):
    """'comfyui' / 'forge' / None"""
    if not root or not os.path.isdir(root):
        return None
    if comfy_layout(root)[0]:
        return "comfyui"
    if os.path.isfile(os.path.join(root, "webui.bat")) or (
            os.path.isfile(os.path.join(root, "launch.py"))
            and os.path.isdir(os.path.join(root, "modules"))):
        return "forge"
    return None


def reconcile_kind(inst):
    """
    按根目录的实际内容重新认定实例类型，纠正 ComfyUI ↔ Forge 的大类错位。

    实例类型只记在 webui_branch 里，而它可能和目录对不上：比如从「一键启动」页
    选目录时，全局 update_config 和 instance_update 两个调用并发，旧分支被
    absorb_active 盖回去；或者在高级选项 / 部署页误选了另一类分支。错位后扩展页
    会往 ComfyUI 目录里装 WebUI 扩展、启动参数也会用错。

    只纠正大类：目录是 ComfyUI 就一定是 comfyui；目录是 Forge 而记录成 comfyui
    时才重新猜 Forge 分支。同属 Forge 的 neo2 / neo / classic 是用户在高级选项里
    选的参数体系，不动。目录无效或还没设置时不猜。

    名字还是旧类型的自动名（「Forge Neo」「Forge Neo 2」这种）就跟着改；
    用户自己起的名字不动。

    返回 None 表示没有改动，否则返回 (旧分支, 新分支)。
    """
    if not inst:
        return None
    root = (inst.get("webui_root") or "").strip()
    kind = detect_kind(root)
    if not kind:
        return None
    old_b = inst.get("webui_branch") or ""
    old_v = inst.get("webui_variant") or ""
    if kind == "comfyui":
        if old_b == "comfyui":
            return None
        new_b, new_v = "comfyui", ""
    else:
        if old_b and old_b != "comfyui":
            return None
        import install_finder  # install_finder 反过来 import 本模块，放函数里避免循环导入
        new_b = install_finder.guess_forge_branch(root)
        new_v = install_finder.forge_variant(root)
    old_label = kind_label(old_b, old_v)
    name = (inst.get("name") or "").strip()
    inst["webui_branch"], inst["webui_variant"] = new_b, new_v
    if not name or name == old_label or re.fullmatch(re.escape(old_label) + r" \d+", name):
        inst["name"] = kind_label(new_b, new_v)
    return old_b, new_b


def reconcile_all_kinds(cfg, only=None):
    """
    对所有实例（或只对 only 里列出的实例 id）执行 reconcile_kind。
    返回被纠正的实例 id 列表；有改动时会同步顶层投影，但不负责保存。
    """
    if not cfg.get("instances"):
        return []
    absorb_active(cfg)
    fixed = []
    for inst in cfg["instances"]:
        if only is not None and inst.get("id") not in only:
            continue
        if reconcile_kind(inst):
            fixed.append(inst.get("id"))
    if fixed:
        project_active(cfg)
    return fixed


def comfy_python(cfg, root):
    custom = (cfg.get("custom_python_path") or "").strip().strip('"')
    if custom:
        return custom
    return comfy_layout(root)[1] or ""


def build_comfy_args(cfg):
    """ComfyUI 的 main.py 参数（列表）"""
    args = []
    if cfg.get("enable_listen"):
        args += ["--listen", "0.0.0.0"]
    port = str(cfg.get("port", "")).strip()
    if port:
        args += ["--port", port]
    gpu_id = str(cfg.get("gpu_device_id", "")).strip()
    if gpu_id:
        args += ["--cuda-device", gpu_id]
    for arg in (_lookup(VRAM_MODE_OPTIONS_NEO2, cfg.get("vram_mode", "auto")),
                _lookup(PRECISION_MODE_OPTIONS_NEO2, cfg.get("precision_mode", "auto")),
                _lookup(UNET_PRECISION_OPTIONS_NEO2, cfg.get("unet_precision", "auto")),
                _lookup(VAE_PRECISION_OPTIONS_NEO2, cfg.get("vae_precision", "auto")),
                _lookup(TEXT_ENC_PRECISION_OPTIONS_NEO2, cfg.get("text_enc_precision", "auto")),
                _lookup(ATTENTION_OPTIONS_NEO2, cfg.get("attention_impl", "auto"))):
        if arg:
            args.append(arg)
    reserve = str(cfg.get("reserve_vram_gb", "")).strip()
    if reserve and reserve not in ("0", "0.0"):
        args += ["--reserve-vram", reserve]
    if cfg.get("cuda_malloc"):
        args.append("--cuda-malloc")
    if cfg.get("comfy_sage"):
        args.append("--use-sage-attention")
    if cfg.get("comfy_flash"):
        args.append("--use-flash-attention")
    if cfg.get("comfy_fast"):
        args.append("--fast")
    if cfg.get("autolaunch"):
        args.append("--auto-launch")
    # 内置 ComfyUI-Manager：依赖装好了（pip 包 comfyui_manager 在）就自动启用。
    # 没装依赖时加了这个参数 ComfyUI 只会打警告，但也别给用户添乱，所以先检查
    if cfg.get("comfy_enable_manager", True) and "--enable-manager" not in (cfg.get("extra_args") or ""):
        try:
            import comfy_nodes
            root = (cfg.get("webui_root") or "").strip()
            comfy_dir = comfy_layout(root)[0]
            if comfy_nodes.manager_builtin_supported(comfy_dir) and \
                    comfy_nodes.manager_pip_installed(comfy_python(cfg, root)):
                args.append("--enable-manager")
        except Exception:
            pass
    extra_paths = cfg.get("_extra_model_paths")
    if extra_paths:
        args += ["--extra-model-paths-config", extra_paths]
    extra = (cfg.get("extra_args") or "").strip()
    if extra:
        import shlex
        try:
            args += shlex.split(extra, posix=False)
        except ValueError:
            args += extra.split()
    if cfg.get("gpu_backend") == "rocm":
        # A 卡：去掉 N 卡专用参数（sage/flash/cuda-malloc 等），补上 ROCm 推荐参数
        import amd_rocm
        args = amd_rocm.apply_args(args, amd_rocm.COMFY_ARGS)
    return args


def build_comfy_env(cfg, root_dir):
    """ComfyUI 子进程环境：消毒 + git 路径 + 镜像（ComfyUI 不走 webui.bat，不需要 PYTHON/VENV_DIR）"""
    env = {}
    for k, v in os.environ.items():
        ku = k.upper()  # Windows 环境变量名不区分大小写
        if ku in _ENV_STRIP_EXACT or any(ku.startswith(p) for p in _ENV_STRIP_PREFIX):
            continue
        env[k] = v
    custom_git = (cfg.get("custom_git_path") or "").strip() or detect_bundled_git(root_dir) or ""
    if custom_git:
        env["GIT_PYTHON_GIT_EXECUTABLE"] = custom_git
        git_dir = custom_git.replace("/", "\\").rsplit("\\", 1)[0]
        if git_dir and git_dir != custom_git:
            env["PATH"] = git_dir + ";" + env.get("PATH", "")
    try:
        import mirror_manager as mm
        if mm.resolve_mode(cfg):
            env.update(mm.pip_env_overrides(True))
            env["HF_ENDPOINT"] = mm.HF_MIRROR
    except Exception:
        pass
    if cfg.get("gpu_backend") == "rocm":
        import amd_rocm
        env.update(amd_rocm.ROCM_ENV)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        env["GIT_CONFIG_GLOBAL"] = _empty_gitconfig_path()
    except OSError:
        pass
    import proxy_manager as pm
    return pm.with_local_bypass(env)


# 启动器自己生成的开关型参数（不含用户的「额外参数」）。上游会删改参数，比如新版 Neo 去掉了
# --normalvram，旧配置照旧加上就会 "unrecognized arguments" 直接启动失败；启动前按安装实际声明的参数过滤。
_ARG_DECL_FILES = ("backend/args.py", "modules/cmd_args.py")
_ARG_LITERAL_RE = re.compile(r"""["'](--[A-Za-z0-9][\w-]*)["']""")


def _managed_flags():
    flags = {"--fast-fp16", "--pin-shared-memory", "--expandable-segments", "--xformers", "--flash", "--sage",
             "--cuda-malloc", "--always-offload-from-vram", "--reserve-vram", "--skip-python-version-check",
             "--no-hashing"}
    for options in (VRAM_MODE_OPTIONS_LEGACY, VRAM_MODE_OPTIONS_NEO2, PRECISION_MODE_OPTIONS_LEGACY,
                    PRECISION_MODE_OPTIONS_NEO2, UNET_PRECISION_OPTIONS_NEO2, VAE_PRECISION_OPTIONS_NEO2,
                    TEXT_ENC_PRECISION_OPTIONS_NEO2, ATTENTION_OPTIONS_NEO2):
        flags.update(arg for _, _, arg in options if arg)
    return flags


def declared_args(root_dir):
    """该 WebUI 的 argparse 里声明的全部参数名；找不到参数定义文件时返回 None（不做过滤）。"""
    if not root_dir:
        return None
    files = [os.path.join(root_dir, rel) for rel in _ARG_DECL_FILES]
    builtin = os.path.join(root_dir, "extensions-builtin")
    if os.path.isdir(builtin):
        files += [os.path.join(builtin, d, "preload.py") for d in os.listdir(builtin)]
    found, flags = False, set()
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if path.endswith(_ARG_DECL_FILES) and "add_argument" in text:
            found = True
        flags.update(_ARG_LITERAL_RE.findall(text))
    return flags if found else None


def _drop_unsupported(args, root_dir, notes):
    declared = declared_args(root_dir)
    if declared is None:
        return args
    managed, kept = _managed_flags(), []
    for item in args:
        name = item.split()[0]
        if name in managed and name not in declared:
            if notes is not None:
                notes.append(f"[启动器] 当前 WebUI 版本不支持 {name}（上游已删除或改名），本次启动已自动去掉；"
                             "可以在「高级选项」里换一个设置。")
            continue
        kept.append(item)
    return kept


def webui_opens_browser(cfg, root_dir):
    """Forge / A1111 启动后会不会自己打开浏览器。

    除了 --autolaunch，WebUI 设置里还有「Launch the webui in browser on startup」（config.json 的
    auto_launch_browser，默认 Local = 没有 --listen / --share 等对外监听时自动打开）。启动器就绪后
    也会打开一次，两边都开就是两个标签页。"""
    if is_comfy(cfg):
        return bool(cfg.get("autolaunch"))
    args = build_commandline_args(cfg, root_dir).split()
    if "--autolaunch" in args:
        return True
    mode = "Local"
    try:
        with open(os.path.join(root_dir, "config.json"), "r", encoding="utf-8") as f:
            mode = json.load(f).get("auto_launch_browser", "Local")
    except (OSError, ValueError, AttributeError):
        pass
    if mode == "Remote":
        return True
    if mode == "Local":
        return not any(a in args for a in ("--share", "--listen", "--ngrok", "--server-name"))
    return False


def build_commandline_args(cfg, root_dir=None, notes=None):
    """根据配置生成 COMMANDLINE_ARGS 字符串（只包含真实存在、真的会生效的命令行参数）。
    传入 root_dir 时，去掉该 WebUI 版本不认识的启动器参数（notes 收集提示）。"""
    if is_comfy(cfg):
        return " ".join(build_comfy_args(cfg))
    args = []

    if cfg.get("enable_listen"):
        args.append("--listen")

    if cfg.get("enable_api"):
        args.append("--api")

    if cfg.get("enable_share"):
        args.append("--share")

    if cfg.get("enable_insecure_extension_access"):
        args.append("--enable-insecure-extension-access")

    if cfg.get("autolaunch"):
        args.append("--autolaunch")

    if cfg.get("skip_python_version_check"):
        args.append("--skip-python-version-check")

    if cfg.get("no_hashing"):
        args.append("--no-hashing")

    gpu_id = str(cfg.get("gpu_device_id", "")).strip()
    if gpu_id != "":
        args.append(f"--device-id {gpu_id}")

    new_neo = uses_new_neo_args(cfg)

    vram_arg = _lookup(vram_options_for(cfg), cfg.get("vram_mode", "auto"))
    if vram_arg:
        args.append(vram_arg)

    if new_neo:
        # 新版 Neo：没有 --always-offload-from-vram，改用 --reserve-vram 预留显存
        reserve = str(cfg.get("reserve_vram_gb", "")).strip()
        if reserve and reserve not in ("0", "0.0"):
            args.append(f"--reserve-vram {reserve}")
    else:
        if cfg.get("always_offload_from_vram"):
            args.append("--always-offload-from-vram")

    if cfg.get("cuda_malloc"):
        args.append("--cuda-malloc")

    precision_arg = _lookup(precision_options_for(cfg), cfg.get("precision_mode", "auto"))
    if precision_arg:
        args.append(precision_arg)

    if new_neo:
        # 分部件精度（新版 Neo 独有）
        for key, options in (
            ("unet_precision", UNET_PRECISION_OPTIONS_NEO2),
            ("vae_precision", VAE_PRECISION_OPTIONS_NEO2),
            ("text_enc_precision", TEXT_ENC_PRECISION_OPTIONS_NEO2),
            ("attention_impl", ATTENTION_OPTIONS_NEO2),
        ):
            arg = _lookup(options, cfg.get(key, "auto"))
            if arg:
                args.append(arg)
        # 新版 Neo 的性能开关
        if cfg.get("fast_fp16"):
            args.append("--fast-fp16")
        if cfg.get("pin_shared_memory"):
            args.append("--pin-shared-memory")
        if cfg.get("expandable_segments"):
            args.append("--expandable-segments")

    if cfg.get("install_xformers"):
        args.append("--xformers")

    # --flash / --sage 两代 Neo 都支持；Classic 分支即使勾选了也不生成，
    # 避免报"未知参数"
    if cfg.get("webui_branch") in ("neo", "neo2"):
        if cfg.get("install_flash"):
            args.append("--flash")
        if cfg.get("install_sage"):
            args.append("--sage")

    theme = cfg.get("theme", "auto")
    if theme in ("dark", "light"):
        args.append(f"--theme {theme}")

    port = str(cfg.get("port", "")).strip()
    if port:
        args.append(f"--port {port}")

    args = _drop_unsupported(args, root_dir, notes)

    extra = cfg.get("extra_args", "").strip()
    if extra:
        args.append(extra)

    out = " ".join(args)
    if cfg.get("gpu_backend") == "rocm":
        # A 卡：去掉 N 卡专用参数（--xformers/--sage/--flash/--cuda-malloc），补上 ROCm 推荐参数。
        # 只有新版 Neo 有 --use-pytorch-cross-attention / --disable-smart-memory，老 Forge 用旧写法
        import amd_rocm
        out = amd_rocm.apply_args_str(
            out, amd_rocm.FORGE_ARGS if uses_new_neo_args(cfg)
            else ["--cuda-stream", "--attention-pytorch", "--pin-shared-memory"])
    return out


_BAT_LINE_RE = re.compile(r"^(set\s+COMMANDLINE_ARGS\s*=).*$", re.IGNORECASE)


def update_bat_commandline_args(bat_path, new_args_str):
    """
    只替换 bat 文件中 `set COMMANDLINE_ARGS=...` 这一行，其余内容原样保留。
    如果文件里没有这一行，则在第一行之后插入。
    会先备份原文件为 .bak（仅在还没有备份的情况下备份一次，避免多次运行时把备份覆盖成新内容）。
    """
    if not os.path.exists(bat_path):
        raise FileNotFoundError(f"找不到文件: {bat_path}")

    backup_path = bat_path + ".bak"
    if not os.path.exists(backup_path):
        shutil.copyfile(bat_path, backup_path)

    # bat 文件编码在中文 Windows 上可能是 gbk 也可能是 utf-8，这里尽量兼容。
    # 注意：只有原文件本身带 BOM 时才用 utf-8-sig（写回时才会保留BOM），
    # 否则一律用不带 BOM 的编码写回，避免给原本没有 BOM 的 bat 文件平白加上 BOM
    # （部分老版本 cmd.exe 在文件带 BOM 时会把第一行命令解析出错）。
    with open(bat_path, "rb") as f:
        raw_bytes = f.read()
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")

    if has_bom:
        # 原文件带 BOM，解码时用 utf-8-sig 去掉 BOM，写回时同样用 utf-8-sig 补回 BOM
        used_encoding = "utf-8-sig"
        raw = raw_bytes.decode("utf-8-sig")
    else:
        # 原文件不带 BOM：依次尝试 utf-8 / gbk，写回时用同一种编码，不引入 BOM
        raw = None
        used_encoding = "utf-8"
        for enc in ("utf-8", "gbk", "mbcs"):
            try:
                raw = raw_bytes.decode(enc)
                used_encoding = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if raw is None:
            raw = raw_bytes.decode("utf-8", errors="replace")
            used_encoding = "utf-8"

    lines = raw.splitlines()
    new_line = f"set COMMANDLINE_ARGS={new_args_str}"
    found = False
    for i, line in enumerate(lines):
        if _BAT_LINE_RE.match(line.strip()):
            lines[i] = new_line
            found = True
            break

    if not found:
        insert_at = 1 if lines else 0
        lines.insert(insert_at, new_line)

    with open(bat_path, "w", encoding=used_encoding) as f:
        f.write("\n".join(lines) + "\n")

    return backup_path
