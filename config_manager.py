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

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher_config.json")

DEFAULT_CONFIG = {
    "webui_root": "",
    "bat_file_name": "webui-user.bat",
    "custom_python_path": "",     # 留空则自动检测(如秋叶整合包的 python\python.exe)或用系统python
    "custom_git_path": "",        # 留空则自动检测或用系统git
    "webui_branch": "neo2",  # 默认新装/新配置就用新版 Neo 参数体系
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
    # 国内网络加速：auto=自动检测 / always=总是用镜像 / never=总是用原站
    "mirror_mode": "auto",
    "mirror_detected": "",   # 自动检测结果缓存（cn / global），清空可重新检测
    "github_mirror_detected": "",  # GitHub 专用探测缓存（ok / blocked），跟上面那个是独立的两套判断
    # 新版 Neo (neo2) 专属参数
    "reserve_vram_gb": "",       # --reserve-vram GB
    "unet_precision": "auto",
    "vae_precision": "auto",
    "text_enc_precision": "auto",
    "attention_impl": "auto",
    "fast_fp16": False,
    "pin_shared_memory": False,
    "expandable_segments": False,
}

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
    """当前分支是否使用新版 Neo 参数体系"""
    return cfg.get("webui_branch") == "neo2"


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
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update(saved)
        except Exception:
            pass
    return cfg


def save_config(cfg):
    # 先写临时文件再原子替换：直接 "w" 打开会先截断原文件，
    # 写入中断（断电/磁盘满/杀软）就留下一份空配置，下次启动回到默认
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
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


def build_launch_env_overrides(cfg, root_dir):
    """
    生成要覆盖注入子进程环境变量的字典（只包含需要覆盖的项，值为空的不放进去，
    这样 webui.bat 内部 `if not defined XXX` 的判断才能按预期工作，
    走它自己的默认逻辑，而不是被我们塞进去一个空字符串搞坏判断）。
    """
    overrides = {}

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
    overrides["COMMANDLINE_ARGS"] = build_commandline_args(cfg)

    # Forge 的 launch.py 会自己调 pip 装 torch 等依赖（2GB 级别），
    # 那部分不经过我们的代码，只能靠环境变量让它也走国内镜像。
    try:
        import mirror_manager as mm
        if mm.resolve_mode(cfg):
            overrides.update(mm.pip_env_overrides(True))
    except Exception:
        pass  # 加速是锦上添花，任何异常都不该影响正常启动

    return overrides


def build_commandline_args(cfg):
    """根据配置生成 COMMANDLINE_ARGS 字符串（只包含真实存在、真的会生效的命令行参数）"""
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

    extra = cfg.get("extra_args", "").strip()
    if extra:
        args.append(extra)

    return " ".join(args)


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
