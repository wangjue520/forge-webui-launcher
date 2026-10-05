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

# 由新到旧；cu118 之前的就不管了
FALLBACK_TAGS = ("cu130", "cu129", "cu128", "cu126", "cu124", "cu121", "cu118")

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
    驱动带得动 wanted_tag 就原样返回；带不动返回驱动支持的最高候选 tag；
    驱动信息未知 / wanted_tag 不是 cuXXX 时原样返回（不插手）。
    """
    need = tag_cuda(wanted_tag)
    driver = driver if driver is not None else driver_cuda_version()
    if not need or not driver or driver >= need:
        return wanted_tag
    for t in FALLBACK_TAGS:
        tv = tag_cuda(t)
        if tv and tv <= driver and tv < need:
            return t
    return wanted_tag


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


def _index_versions(index_base, name, tag):
    """索引页里 {name} 在该 tag 下有 Windows wheel 的版本号集合（不含 +tag）；失败返回 None"""
    import requests
    try:
        r = requests.get(f"{index_base}/{name}/", timeout=20)
        r.raise_for_status()
    except Exception:
        return None
    vers = set()
    pat = re.compile(rf"{re.escape(name)}-([0-9][0-9a-z.]*)(?:\+|%2B){re.escape(tag)}-[^\"#]*win_amd64\.whl",
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


def resolve_specs(specs, new_tag, index_bases, log=None):
    """
    把 [(torch, '2.13.0+cu130'), (torchvision, '0.28.0+cu130')] 换成 new_tag 下真实存在的版本。
    优先同版本号；没有就挑该 tag 下最新的 torch + 配套 torchvision。
    所有索引都联不上时退回「同版本号直接换 tag」（离线也不至于什么都不做）。
    返回 (specs, 找到版本的索引地址或 None)。
    """
    base_ver = {n: v.split("+", 1)[0] for n, v in specs}
    key = f"{new_tag}|" + ",".join(f"{n}=={v}" for n, v in sorted(base_ver.items()))
    cache = _load_cache()
    hit = cache.get(key)
    if hit and time.time() - hit.get("t", 0) < _CACHE_TTL and hit.get("base") in index_bases:
        return [tuple(x) for x in hit["specs"]], hit["base"]

    result, used = None, None
    for base in index_bases:
        tv = _index_versions(base, "torch", new_tag)
        if not tv:
            continue
        torch_v = base_ver.get("torch")
        if torch_v not in tv:
            torch_v = max(tv, key=_vkey)
            if log:
                log(f"[CUDA 兼容] {new_tag} 下没有 torch {base_ver.get('torch')}，改用该系列最新的 {torch_v}\n")
        out = [("torch", f"{torch_v}+{new_tag}")]
        if "torchvision" in base_ver:
            tvv = _index_versions(base, "torchvision", new_tag) or set()
            vis = base_ver["torchvision"] if torch_v == base_ver.get("torch") and base_ver["torchvision"] in tvv \
                else _torchvision_for(torch_v, tvv)
            if not vis:
                continue
            out.append(("torchvision", f"{vis}+{new_tag}"))
        result, used = out, base
        break
    if result is None:
        return [(n, f"{v}+{new_tag}") for n, v in base_ver.items()], None
    cache[key] = {"t": time.time(), "specs": result, "base": used}
    _save_cache(cache)
    return result, used


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
