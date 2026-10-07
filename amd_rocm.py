"""
amd_rocm.py — AMD 显卡（ROCm / TheRock）环境支持

做法照 CS1o 的教程（Forge Neo README 里给 A 卡用户指的就是它）：
  https://github.com/CS1o/Stable-Diffusion-Info/wiki/Webui-Installation-Guides#amd-forge-neo-with-rocm
  1. 卸掉 CUDA 版 torch（以及残留的 rocm 包）
  2. 从 AMD 的 wheel 源按显卡架构装 torch：
       pip install --index-url https://stable.repo.amd.com/rocm/whl-next/
           "torch[device-gfx1100]" "torchvision[device-gfx1100]" torchaudio "rocm[devel]"
  3. rocm-sdk init
  4. 环境变量 TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1、FLASH_ATTENTION_TRITON_AMD_ENABLE=TRUE
  5. Forge 参数 --cuda-stream --use-pytorch-cross-attention --disable-smart-memory --pin-shared-memory；
     ComfyUI 参数 --use-pytorch-cross-attention --bf16-vae --disable-smart-memory
ROCm 版 torch 也走 torch.cuda 这套接口（HIP），所以 Forge / ComfyUI 本身不用改。

这里只放纯逻辑（型号表、命令、参数），真正执行命令在 webview_api 里。
"""

import json
import os
import re
import subprocess

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

ROCM_INDEX = "https://stable.repo.amd.com/rocm/whl-next/"
ROCM_NIGHTLY_INDEX = "https://nightly.repo.amd.com/rocm/whl-next/"

# (gfx 架构, 显示名, 是否实验性) —— 顺序就是下拉框顺序；
# 实验性 = 教程说「支持」但没给安装命令的架构，装不上时提示用户
GFX_TARGETS = [
    ("gfx1201", "RX 9070 / 9070 XT、AI PRO R9700", False),
    ("gfx1200", "RX 9060 / 9060 XT", False),
    ("gfx1100", "RX 7900 XTX / 7900 XT / 7900 GRE、PRO W7900 / W7800", False),
    ("gfx1101", "RX 7800 XT / 7700 XT、PRO W7700", False),
    ("gfx1102", "RX 7600 / 7600 XT / 7650 GRE / 7700S", False),
    ("gfx1030", "RX 6950 XT / 6900 XT / 6800 XT / 6800、PRO W6800", False),
    ("gfx1031", "RX 6750 XT / 6700 XT / 6700、6800M", False),
    ("gfx1032", "RX 6650 XT / 6600 XT / 6600、6800S / 6700S", False),
    ("gfx1034", "RX 6500 XT / 6400", False),
    ("gfx1035", "Radeon 680M / 660M 核显", False),
    ("gfx1010", "RX 5700 / 5700 XT / 5600", False),
    ("gfx1151", "Ryzen AI Max（Radeon 8060S / 8050S）", True),
    ("gfx1103", "Radeon 780M / 760M 核显", True),
]
GFX_IDS = [g for g, _l, _e in GFX_TARGETS]

# 型号 → 架构。顺序有讲究：7700S 是 gfx1102（不是 7700 的 1101），
# 6800S/6700S 是 gfx1032、6800M 是 gfx1031，所以带后缀的先判断
_GFX_RULES = [
    (r"\b9070\b|R9700|R9600", "gfx1201"),
    (r"\b9060\b", "gfx1200"),
    (r"\b7700S\b|\b7600\b|\b7650\b|\b7400\b|W7600|W7500", "gfx1102"),
    (r"\b7900\b|W7900|W7800", "gfx1100"),
    (r"\b7800\b|\b7700\b|W7700|V710", "gfx1101"),
    (r"\b6800S\b|\b6700S\b|\b66[05]0\b|W6600", "gfx1032"),
    (r"\b6800M\b|\b67[05]0\b", "gfx1031"),
    (r"\b69[05]0\b|\b6800\b|W6800|V620", "gfx1030"),
    (r"\b6500\b|\b6400\b", "gfx1034"),
    (r"\b680M\b|\b660M\b", "gfx1035"),
    (r"\b5700\b|\b5600\b", "gfx1010"),
    (r"80[456]0S|Ryzen AI Max", "gfx1151"),
    (r"\b7[468]0M\b", "gfx1103"),
]

ROCM_ENV = {
    "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
    "FLASH_ATTENTION_TRITON_AMD_ENABLE": "TRUE",
}
FORGE_ARGS = ["--cuda-stream", "--use-pytorch-cross-attention", "--disable-smart-memory",
              "--pin-shared-memory"]
COMFY_ARGS = ["--use-pytorch-cross-attention", "--bf16-vae", "--disable-smart-memory"]
# 只对 N 卡有意义（或在 A 卡上直接报错）的参数：ROCm 环境启动时剔掉
NVIDIA_ONLY_ARGS = {"--xformers", "--sage", "--flash", "--cuda-malloc", "--pynvml",
                    "--use-sage-attention", "--use-flash-attention", "--use-ck-attention"}

# 切换环境时要卸掉的包：torch 三件套 + CUDA 专用的 xformers；rocm* 另外按已装列表找
UNINSTALL_BASE = ["torch", "torchvision", "torchaudio", "xformers"]


def is_rocm(cfg):
    return (cfg or {}).get("gpu_backend") == "rocm"


def gfx_label(gfx):
    for g, label, exp in GFX_TARGETS:
        if g == gfx:
            return f"{label}（{g}{'，实验性' if exp else ''}）"
    return gfx or "未选择"


def guess_gfx(name):
    """显卡名 → gfx 架构；认不出返回 None"""
    for pat, gfx in _GFX_RULES:
        if re.search(pat, name or "", re.IGNORECASE):
            return gfx
    return None


def detect_amd_gpus():
    """本机的 AMD 显卡 [{name, gfx}]（Windows 用 WMI 查；查不到返回空列表）"""
    if os.name != "nt":
        return []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=_NO_WINDOW)
        names = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    except Exception:
        return []
    out = []
    for n in names:
        if re.search(r"\bAMD\b|Radeon", n, re.IGNORECASE):
            out.append({"name": n, "gfx": guess_gfx(n)})
    # 独显排前面：核显（Radeon(TM) Graphics / 7x0M）通常不是要用的那块
    out.sort(key=lambda g: g["gfx"] in (None, "gfx1035", "gfx1103"))
    return out


def pip_install_args(gfx, nightly=False):
    """python -m pip 后面的参数（列表形式，给启动器自己跑命令用）"""
    args = ["install", "--index-url", ROCM_NIGHTLY_INDEX if nightly else ROCM_INDEX]
    if nightly:
        args.append("--pre")
    args += [f"torch[device-{gfx}]", f"torchvision[device-{gfx}]", "torchaudio", "rocm[devel]"]
    return args


def rocm_sdk_exe(python_exe):
    """python 对应的 rocm-sdk.exe：venv 里和 python.exe 同目录，便携/系统 Python 在 Scripts 下"""
    d = os.path.dirname((python_exe or "").strip('"'))
    for c in (os.path.join(d, "rocm-sdk.exe"), os.path.join(d, "Scripts", "rocm-sdk.exe"),
              os.path.join(d, "rocm-sdk"), os.path.join(d, "bin", "rocm-sdk")):
        if os.path.isfile(c):
            return c
    return os.path.join(d, "Scripts", "rocm-sdk.exe")


def forge_torch_command(gfx, scripts_dir):
    """
    给 Forge 的 TORCH_COMMAND：Forge 执行的是 `"<python>" -m <TORCH_COMMAND>`（shell=True），
    所以开头必须是 pip；装完紧接着 rocm-sdk init（scripts_dir 是 rocm-sdk.exe 所在目录）。
    """
    pkgs = " ".join(f'"{p}"' if "[" in p else p for p in pip_install_args(gfx)[3:])
    cmd = f"pip install --index-url {ROCM_INDEX} {pkgs}"
    if scripts_dir:
        cmd += f' && "{os.path.join(scripts_dir, "rocm-sdk.exe")}" init'
    return cmd


def apply_args(args, extra):
    """args（列表）里剔掉 N 卡专用参数，再补上 extra 里还没有的（保持顺序、去重）"""
    out = []
    for a in args:
        if a in NVIDIA_ONLY_ARGS:
            continue
        out.append(a)
    for a in extra:
        if a not in out:
            out.append(a)
    return out


def apply_args_str(arg_str, extra):
    """COMMANDLINE_ARGS 字符串版：按空白切开逐个处理（带引号的路径参数原样保留）"""
    parts = re.findall(r'"[^"]*"|\S+', arg_str or "")
    return " ".join(apply_args(parts, extra))


# ------------------------------------------------------------ 环境探测 ----

_PROBE = ("import json,torch;print(json.dumps({'v':torch.__version__,"
          "'hip':getattr(torch.version,'hip',None),'cuda':torch.version.cuda,"
          "'ok':bool(torch.cuda.is_available()),"
          "'name':(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')}))")


def probe_torch(python_exe, env=None, timeout=180):
    """
    {'installed': bool, 'backend': 'rocm'/'cuda'/'cpu'/None, 'version', 'ok', 'name', 'error'}
    import torch 要几秒，ROCm 首次还会更久，超时给宽一点
    """
    try:
        r = subprocess.run([python_exe, "-c", _PROBE], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=timeout,
                           creationflags=_NO_WINDOW)
    except Exception as e:
        return {"installed": False, "backend": None, "error": str(e)}
    line = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not line:
        err = (r.stderr or "").strip().splitlines()
        no_torch = any("No module named 'torch'" in x for x in err)
        return {"installed": not no_torch, "backend": None,
                "error": err[-1] if err else f"退出码 {r.returncode}"}
    try:
        d = json.loads(line[-1])
    except ValueError:
        return {"installed": True, "backend": None, "error": line[-1]}
    backend = "rocm" if d.get("hip") or "rocm" in str(d.get("v", "")).lower() else (
        "cuda" if d.get("cuda") else "cpu")
    return {"installed": True, "backend": backend, "version": d.get("v"), "ok": d.get("ok"),
            "name": d.get("name") or "", "error": ""}


def installed_rocm_packages(python_exe, env=None):
    """已装的 rocm* 包名（切换时一起卸掉，免得新旧版本混装）"""
    try:
        r = subprocess.run([python_exe, "-m", "pip", "list", "--format=json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=120, creationflags=_NO_WINDOW)
        pkgs = json.loads(r.stdout or "[]")
    except Exception:
        return []
    return [p["name"] for p in pkgs
            if str(p.get("name", "")).lower().startswith(("rocm", "_rocm"))]
