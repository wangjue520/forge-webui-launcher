# -*- coding: utf-8 -*-
"""
显卡驱动 ↔ torch CUDA 版本兼容。

背景：Forge Neo 的 launch_utils.py 写死了 torch==2.13.0+cu130（ComfyUI 官方也推荐 cu130），
cu130 要求驱动支持 CUDA 13.0（Windows 驱动 580 以上）。驱动没那么新时，torch 装得上
但 torch.cuda.is_available() 报 "driver is too old"，Neo 抛
    SystemError: Please update your GPU driver or manually install older version of PyTorch
而且 Neo 只在「没装 torch」或带 --reinstall-torch 时才会重装，所以已经装了 cu130 的
环境会一直卡在这里。

这里做三件事：
  1. 读 nvidia-smi 表头的 "CUDA Version: 12.8"——驱动能支持的最高 CUDA 版本
  2. 目标 tag 超过驱动能力时，换成驱动支持得了的最高 tag（cu128 → cu126 → …），
     并到索引里确认这个版本真有对应的 wheel，没有就挑该 tag 下最新的
  3. 检查环境里已经装好的 torch 是不是驱动带不动的版本（读 dist-info 目录名，不起 Python）

全部是尽力而为：拿不到驱动信息、联不上索引时不做任何改动，保持 Forge 默认行为。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 降级候选，由新到旧（cu129 只有个别版本有，不当候选）；cu118 之前的就不管了。
# 同一个 CUDA 大版本内驱动是「小版本兼容」的：驱动显示 CUDA 12.2 照样能跑 cu128 的 torch，
# 所以只按大版本降，12.x 驱动一律用 12 系最新的 cu128，不往更老的 tag 退。
FALLBACK_TAGS = ("cu128", "cu126", "cu124", "cu121", "cu118")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.path.join(APP_DIR, "launcher_data", "torch_compat_cache.json")
_CACHE_TTL = 7 * 86400

_driver_cache = {"t": 0.0, "v": None}


def find_nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    for c in (r"C:\Windows\System32\nvidia-smi.exe",
              r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.isfile(c):
            return c
    return None


def driver_cuda_version():
    """驱动支持的最高 CUDA 版本 (major, minor)；没有 N 卡 / 读不到返回 None。缓存 10 分钟。"""
    if time.time() - _driver_cache["t"] < 600:
        return _driver_cache["v"]
    v = None
    exe = find_nvidia_smi()
    if exe:
        try:
            r = subprocess.run([exe], capture_output=True, timeout=15, creationflags=_NO_WINDOW)
            out = (r.stdout or b"").decode("utf-8", errors="replace")
            m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out)
            if m:
                v = (int(m.group(1)), int(m.group(2)))
        except Exception:
            v = None
    _driver_cache.update(t=time.time(), v=v)
    return v


def tag_cuda(tag):
    """'cu130' → (13, 0)；'cu128' → (12, 8)；不是 cuXXX 返回 None"""
    m = re.match(r"^cu(\d+)(\d)$", tag or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def fmt_cuda(v):
    return f"{v[0]}.{v[1]}" if v else "未知"


def supported_tag(wanted_tag, driver=None):
    """
    驱动带得动 wanted_tag 就原样返回；带不动返回驱动支持的大版本里最新的候选 tag；
    驱动信息未知 / wanted_tag 不是 cuXXX 时原样返回（不插手）。

    只比大版本：CUDA 12.x 的程序在任何 12 系驱动上都能跑（小版本兼容），
    真正跨不过去的只有 12 → 13 这种大版本。
    """
    need = tag_cuda(wanted_tag)
    driver = driver if driver is not None else driver_cuda_version()
    if not need or not driver or driver[0] >= need[0]:
        return wanted_tag
    for t in FALLBACK_TAGS:
        tv = tag_cuda(t)
        if tv and tv[0] <= driver[0] and tv < need:
            return t
    return wanted_tag


_pytag_cache = {}


def python_tag(python_exe):
    """目标解释器的 wheel tag（cp313 等）；拿不到返回 None。按路径缓存。"""
    exe = (python_exe or "").strip().strip('"')
    if not exe or not os.path.isfile(exe):
        return None
    if exe in _pytag_cache:
        return _pytag_cache[exe]
    tag = None
    try:
        r = subprocess.run([exe, "-c", "import sys; print('cp%d%d' % sys.version_info[:2])"],
                           capture_output=True, timeout=20, creationflags=_NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        if re.match(r"^cp\d+$", out):
            tag = out
    except Exception:
        tag = None
    _pytag_cache[exe] = tag
    return tag


# ------------------------------------------------------------ 索引核对 ----

def _load_cache():
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data):
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _index_versions(index_base, name, tag, pytag=None):
    """索引页里 {name} 在该 tag 下有 Windows wheel 的版本号集合（不含 +tag）；失败返回 None。
    给了 pytag 就只算该 Python 版本能装的（cp313 的环境装不了只有 cp312 的 wheel）"""
    import requests
    try:
        r = requests.get(f"{index_base}/{name}/", timeout=20)
        r.raise_for_status()
    except Exception:
        return None
    vers = set()
    py = re.escape(pytag) + "-" if pytag else ""
    pat = re.compile(rf"{re.escape(name)}-([0-9][0-9a-z.]*)(?:\+|%2B){re.escape(tag)}-{py}[^\"#]*win_amd64\.whl",
                     re.IGNORECASE)
    for m in pat.finditer(r.text):
        vers.add(m.group(1))
    return vers


def _vkey(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.]", v))


def _torchvision_for(torch_ver, tv_versions):
    """torch 2.M.p 对应 torchvision 0.(M+15).x：取该系列里最新的"""
    parts = torch_ver.split(".")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    series = f"0.{int(parts[1]) + 15}."
    cands = [v for v in tv_versions if v.startswith(series)]
    return max(cands, key=_vkey) if cands else None


def resolve_specs(specs, new_tag, index_bases, pytag=None, log=None):
    """
    把 [(torch, '2.13.0+cu130'), (torchvision, '0.28.0+cu130')] 换成 new_tag 下的同一版本。

    只接受「同版本号」或「同一 x.y 系列里最新的补丁版」——WebUI 写死某个 torch 版本
    是有原因的，退到老好几代的 torch（比如 2.13 → 2.5）不但跑不起来，新 Python 上
    往往连 wheel 都没有。找不到合适版本时返回 (None, 索引地址)，调用方不改装、提示升级驱动。
    所有索引都联不上时（离线）返回同版本号直接换 tag，(specs, None)。
    """
    base_ver = {n: v.split("+", 1)[0] for n, v in specs}
    key = f"{new_tag}|{pytag or ''}|" + ",".join(f"{n}=={v}" for n, v in sorted(base_ver.items()))
    cache = _load_cache()
    hit = cache.get(key)
    if hit and time.time() - hit.get("t", 0) < _CACHE_TTL and hit.get("base") in index_bases:
        sp = hit.get("specs")
        return ([tuple(x) for x in sp] if sp else None), hit["base"]

    def series(v):
        return ".".join(v.split(".")[:2]) + "."

    reached = None
    for base in index_bases:
        tv = _index_versions(base, "torch", new_tag, pytag)
        if tv is None:
            continue
        reached = base
        want = base_ver.get("torch", "")
        torch_v = want if want in tv else max((v for v in tv if v.startswith(series(want))),
                                               key=_vkey, default=None)
        if not torch_v:
            continue
        out = [("torch", f"{torch_v}+{new_tag}")]
        if "torchvision" in base_ver:
            tvv = _index_versions(base, "torchvision", new_tag, pytag) or set()
            vis = base_ver["torchvision"] if base_ver["torchvision"] in tvv else _torchvision_for(torch_v, tvv)
            if not vis:
                continue
            out.append(("torchvision", f"{vis}+{new_tag}"))
        if torch_v != want and log:
            log(f"[CUDA 兼容] {new_tag} 下没有 torch {want}，改用同系列的 {torch_v}\n")
        cache[key] = {"t": time.time(), "specs": out, "base": base}
        _save_cache(cache)
        return out, base
    if reached is None:
        return [(n, f"{v}+{new_tag}") for n, v in base_ver.items()], None
    cache[key] = {"t": time.time(), "specs": None, "base": reached}
    _save_cache(cache)
    return None, reached


# ------------------------------------------------------------ 已装环境 ----

def installed_torch_tag(site_packages_dirs):
    """在给定的 site-packages 里找 torch-*.dist-info，返回 (版本, tag)；找不到返回 (None, None)"""
    for sp in site_packages_dirs:
        for d in glob.glob(os.path.join(sp, "torch-*.dist-info")):
            m = re.match(r"torch-([0-9][^+]*)\+(cu\d+|cpu|rocm[\d.]+|xpu)\.dist-info$",
                         os.path.basename(d), re.IGNORECASE)
            if m:
                return m.group(1), m.group(2).lower()
            m = re.match(r"torch-([0-9][^-]*)\.dist-info$", os.path.basename(d))
            if m:
                return m.group(1), None
    return None, None


def forge_site_packages(root_dir, python_exe=""):
    """Forge 环境可能的 site-packages：venv、自带便携 Python、自定义 Python"""
    cands = [os.path.join(root_dir, "venv", "Lib", "site-packages"),
             os.path.join(root_dir, "python", "Lib", "site-packages")]
    if python_exe:
        cands.append(os.path.join(os.path.dirname(python_exe.strip('"')), "Lib", "site-packages"))
    return [c for c in cands if os.path.isdir(c)]


# ------------------------------------------------------------ 启动前提示 ----

DRIVER_URL = "https://www.nvidia.cn/drivers/lookup/"
DRIVER_MIN = 580

# CUDA 13 放弃了这些老架构（GTX 9xx/10xx、TITAN X/V、Tesla P/M）：驱动再新也跑不了 cu130
_LEGACY_GPU_RE = re.compile(
    r"GTX?\s*(9\d\d|10\d\d)\b|TITAN\s+X|TITAN\s+V\b|Tesla\s+[PM]\d+", re.IGNORECASE)


_gpu_cache = {"t": 0.0, "v": []}


def gpu_names():
    if time.time() - _gpu_cache["t"] < 600:
        return _gpu_cache["v"]
    names = []
    exe = find_nvidia_smi()
    if exe:
        try:
            r = subprocess.run([exe, "--query-gpu=name", "--format=csv,noheader"],
                               capture_output=True, timeout=15, creationflags=_NO_WINDOW)
            names = [x.strip() for x in (r.stdout or b"").decode("utf-8", "replace").splitlines() if x.strip()]
        except Exception:
            names = []
    _gpu_cache.update(t=time.time(), v=names)
    return names


def is_legacy_gpu():
    return any(_LEGACY_GPU_RE.search(n) for n in gpu_names())


LEGACY_TAG = "cu126"   # PyTorch 给老架构卡保留的最后一个 CUDA 系列（cu128 起已不含 sm_50/sm_60）


def effective_tag(wanted_tag, driver=None):
    """
    实际该装的 tag：老架构卡（GTX 9xx/10xx 等）一律 cu126——驱动再新，cu128/cu130 也没有它们的
    内核（报 no kernel image）；其余按驱动能力降级（supported_tag）。
    """
    need = tag_cuda(wanted_tag)
    if need and need > tag_cuda(LEGACY_TAG) and is_legacy_gpu():
        return LEGACY_TAG
    return supported_tag(wanted_tag, driver)


def driver_issue(need_tag, what, can_auto_fix):
    """
    环境要用的 torch（need_tag，如 cu130）驱动带不动时，返回给启动前检查用的提示；没问题返回 None。
    can_auto_fix：启动器这次能不能自动换装兼容 torch（Forge 能，ComfyUI 不能）。
    """
    need = tag_cuda(need_tag)
    names = gpu_names()
    gpu = "、".join(names) or "NVIDIA 显卡"
    if need and need > tag_cuda(LEGACY_TAG) and is_legacy_gpu():
        return {
            "level": "warn", "id": "gpu_too_old",
            "text": f"这张显卡（{gpu}）架构比较老，新版 torch（{need_tag}）已经不支持它，"
                    "升级驱动也没用。\n\n"
                    + ("启动器会自动换装老卡专用的 cu126 版 torch（需要下载约 3GB）。" if can_auto_fix
                       else "需要把 torch 换成老卡专用的 cu126 版本（在「环境部署」页重新部署 ComfyUI 会自动选对）。"),
        }
    driver = driver_cuda_version()
    if not need or not driver or driver[0] >= need[0]:
        return None
    return {
        "level": "warn", "id": "driver_outdated", "url": DRIVER_URL,
        "text": f"显卡驱动太旧：{gpu} 当前的驱动最高支持 CUDA {fmt_cuda(driver)}，"
                f"而{what}的 torch（{need_tag}）需要 CUDA {need[0]}，也就是 {DRIVER_MIN} 以上的驱动。\n\n"
                f"建议先升级显卡驱动（NVIDIA App 里一键更新，或去官网下载），升级完直接启动就行，不用换 torch。\n\n"
                + ("不想升级的话也可以直接启动：启动器会自动换装兼容的 torch（CUDA 12 版，需要下载约 3GB）。"
                   if can_auto_fix else "不升级驱动的话这次启动会失败。"),
    }
