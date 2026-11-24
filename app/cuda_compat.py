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
  4. 按显卡架构（nvidia-smi 的 compute_cap）分出老架构卡 / 50 系 / 完全不支持的卡，
     torch 和显卡对不上时两个方向都提示（见「显卡识别」一节）

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
# 所以只按大版本降。但同一大版本里哪个 tag 有目标 torch 不一定：PyTorch 从 2.12 起
# 不再发 cu128，CUDA 12 只剩 cu126（2.13 有 cu126、没有 cu128）——所以同大版本的
# 每个 tag 都要试（candidate_tags / resolve_compat），不能只认最新的那个。
FALLBACK_TAGS = ("cu128", "cu126", "cu124", "cu121", "cu118")

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根目录（代码在 app/ 子目录里）
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
    """驱动支持的最高 CUDA 版本 (major, minor)；没有 N 卡 / 读不到返回 None。缓存 10 分钟
    （有 nvidia-smi 却没读到——多半是一时超时——只缓存 30 秒）。"""
    if time.time() - _driver_cache["t"] < _driver_cache.get("ttl", 600):
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
    _driver_cache.update(t=time.time(), v=v, ttl=600 if (v or not exe) else 30)
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
# 各 CUDA 大版本要的最低驱动（Windows）：CUDA 12 的程序 528 起就能跑，CUDA 13 要 580
_DRIVER_MIN_BY_MAJOR = {12: 528, 13: 580}


def driver_min_for(major):
    return _DRIVER_MIN_BY_MAJOR.get(major, DRIVER_MIN)


# ------------------------------------------------------------ 显卡识别 ----
#
# 官方 Windows wheel 里带哪些架构的内核（PyTorch 仓库 .ci/wheel/windows/build_env_setup.py）：
#   cu126 及以前：sm_50 ~ sm_90   Maxwell / Pascal / Volta / Turing / Ampere / Ada
#   cu128 及以后：sm_75 ~ sm_120  Turing 起，外加 Blackwell（RTX 50 系）
# 所以按「计算能力」（compute capability，nvidia-smi 的 compute_cap）分三档：
#   < 5.0        Kepler 及更早（GTX 6xx / 7xx、Tesla K80）：现在的 torch 全都不支持
#   5.0 ~ 7.0    20 系之前的老架构（GTX 750 Ti / 9xx / 10xx、MX 系列、Quadro M / P、TITAN X / V、
#                Tesla M / P / V100）：只能用 cu126
#   >= 10.0      Blackwell（RTX 50 系 12.0、B200 10.0）：只能用 cu128 及以上
# 以前按型号名的正则判断，笔记本的 980M、Quadro、MX、750 Ti、V100 都漏掉，RTX 5000 又被当成 50 系。
SM_MIN = 5.0
SM_MODERN = 7.5
SM_BLACKWELL = 10.0

LEGACY_TAG = "cu126"          # 老架构卡能用的最后一个 CUDA 系列
BLACKWELL_MIN_TAG = "cu128"   # RTX 50 系能用的第一个 CUDA 系列

# nvidia-smi 太老查不到 compute_cap、或者 nvidia-smi 根本跑不起来（只能从系统里查到型号名）时，
# 按型号名估架构。从上往下第一个命中的算数，顺序有讲究：
#   · Quadro RTX 4000 / 5000 是 20 系同代，要排在 RTX 40 / 50 前面
#   · RTX 2000 / 5000 Ada 是 40 系同代，要排在 RTX 20 / 50 前面
_NAME_SM = [(re.compile(p, re.IGNORECASE), sm) for p, sm in (
    (r"Quadro\s+RTX|TITAN\s+RTX", 7.5),
    (r"RTX\s*50[5-9]0|Blackwell|RTX\s+PRO\b", 12.0),
    (r"\bB[12]00\b", 10.0),
    (r"\bH[12]00\b", 9.0),
    (r"\bAda\b|RTX\s*40\d0|\bL40S?\b|\bL4\b", 8.9),
    (r"RTX\s*30\d0|RTX\s*A\d{3,4}\b|\bA(2|10G?|16|30|40)\b|MX\s*570", 8.6),
    (r"\bA100\b", 8.0),
    (r"RTX\s*20\d0|GTX\s*16\d0|\bT4\b|\bT(400|500|550|600|1000|1200|2000)\b|MX\s*[45]50", 7.5),
    (r"TITAN\s+V\b|\bGV100\b|\bV100", 7.0),
    (r"GTX?\s*10[1-8]0|TITAN\s+Xp|TITAN\s+X\s*\(Pascal\)|Quadro\s+P\d{3,4}|\bGP100\b|Tesla\s+P\d+"
     r"|MX\s*(150|230|250|330|350)", 6.1),
    (r"GTX?\s*9\d\d|TITAN\s+X\b|Quadro\s+M\d{3,4}|Quadro\s+K(620|1200|2200)|Tesla\s+M\d+", 5.2),
    (r"GTX\s*7[45]\d|GTX\s*8[3-6]\dM|\b9[34]0MX?\b|\b920MX\b|\b8[34]0M\b|\b845M\b|MX\s*1[13]0", 5.0),
    (r"GTX?\s*[2-7]\d\d|GTX\s+TITAN|Tesla\s+K\d+|Quadro\s+K\d+|\bNVS\b|\b[6-8]\d\dM\b|\b9[12]0M\b", 3.5),
)]


def sm_from_name(name):
    """按 NVIDIA 显卡型号名估计算能力（如 8.6）；认不出返回 None"""
    for pat, sm in _NAME_SM:
        if pat.search(name or ""):
            return sm
    return None


_gpu_cache = {"t": 0.0, "v": [], "ttl": 600}


def _smi_lines(exe, fields):
    """nvidia-smi --query-gpu 的输出行；命令失败返回 None"""
    r = subprocess.run([exe, "--query-gpu=" + fields, "--format=csv,noheader"],
                       capture_output=True, timeout=10, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        return None
    return [x.strip() for x in (r.stdout or b"").decode("utf-8", "replace").splitlines() if x.strip()]


def _wmi_nvidia_names():
    """nvidia-smi 用不了时从系统设备列表里找 N 卡（驱动没装好 / 装的是 Windows 自带驱动时也查得到）"""
    if os.name != "nt":
        return []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        names = [x.strip() for x in (r.stdout or b"").decode("utf-8", "replace").splitlines() if x.strip()]
    except Exception:
        return []
    return [n for n in names if re.search(r"NVIDIA|GeForce|Quadro|Tesla", n, re.IGNORECASE)]


def gpu_info():
    """
    本机的 NVIDIA 显卡 [{"name", "sm"}]，sm 是计算能力（认不出为 None）；没有 N 卡返回空列表。
    先问 nvidia-smi（老驱动不认 compute_cap 就只查型号名），再不行从系统设备列表里找。
    查到了缓存 10 分钟；nvidia-smi 出错 / 超时的结果只留 30 秒——以前一次超时会让老卡在
    10 分钟里被当成新卡，正好够把带不动的 torch 装上去。
    """
    if time.time() - _gpu_cache["t"] < _gpu_cache.get("ttl", 600):
        return _gpu_cache["v"]
    info, ttl = [], 600
    exe = find_nvidia_smi()
    if exe:
        for fields in ("name,compute_cap", "name"):
            try:
                lines = _smi_lines(exe, fields)
            except Exception:
                ttl = 30
                break
            if not lines:
                ttl = 30
                continue
            ttl = 600
            for line in lines:
                name, _, cap = line.partition(",") if "," in fields else (line, "", "")
                try:
                    sm = float(cap)
                except ValueError:
                    sm = None
                info.append({"name": name.strip(), "sm": sm or sm_from_name(name)})
            break
    if not info:
        info = [{"name": n, "sm": sm_from_name(n), "src": "wmi"} for n in _wmi_nvidia_names()]
    _gpu_cache.update(t=time.time(), v=info, ttl=ttl)
    return info


def gpu_names():
    return [g["name"] for g in gpu_info()]


def sms_legacy(sms):
    """这些计算能力里有 20 系之前的老架构卡。和 50 系混插时没有哪个 torch 两张卡都能跑，按新卡来"""
    sms = [s for s in sms if s]
    return any(SM_MIN <= s < SM_MODERN for s in sms) and not any(s >= SM_BLACKWELL for s in sms)


def _sms():
    return [g.get("sm") for g in gpu_info()]


def is_legacy_gpu():
    """20 系之前的老架构卡（Maxwell / Pascal / Volta）：只能用 cu126"""
    return sms_legacy(_sms())


def is_blackwell_gpu():
    """RTX 50 系（Blackwell）：只有 cu128 及以上才有内核，cu126 装上会报 no kernel image"""
    return any(s and s >= SM_BLACKWELL for s in _sms())


def is_unsupported_gpu():
    """所有 N 卡都比 Maxwell 还老（Kepler：GTX 6xx / 7xx、Tesla K80 等）：现在的 torch 都不支持"""
    sms = _sms()
    return bool(sms) and all(s is not None and s < SM_MIN for s in sms)


def gpu_tag_mismatch(tag):
    """
    这个 CUDA tag 的 torch 本机显卡跑不了时返回原因，没问题 / 说不准返回 None：
      "unsupported" 显卡太老，什么版本都不行
      "legacy"      老架构卡配了 cu128 及以上的 torch
      "blackwell"   50 系配了 cu128 以下的 torch
    """
    v = tag_cuda(tag)
    if not v:
        return None
    if is_unsupported_gpu():
        return "unsupported"
    if v > tag_cuda(LEGACY_TAG) and is_legacy_gpu():
        return "legacy"
    if v < tag_cuda(BLACKWELL_MIN_TAG) and is_blackwell_gpu():
        return "blackwell"
    return None


def candidate_tags(wanted_tag, driver=None):
    """
    驱动 / 显卡带不动 wanted_tag 时依次可以试的 tag（优先的在前）；带得动返回 [wanted_tag]。
      · 老架构卡：只有 cu126
      · 其余：驱动所在 CUDA 大版本里的所有候选（12.x 驱动 → cu128、cu126、cu124、cu121）
      · RTX 50 系：去掉 cu128 以下的
    """
    first = effective_tag(wanted_tag, driver)
    if first == wanted_tag:
        return [wanted_tag]
    if first == LEGACY_TAG and is_legacy_gpu():
        return [LEGACY_TAG]
    major = tag_cuda(first)[0]
    need = tag_cuda(wanted_tag)
    out = [t for t in FALLBACK_TAGS if tag_cuda(t)[0] == major and tag_cuda(t) < need]
    if is_blackwell_gpu():
        out = [t for t in out if tag_cuda(t) >= (12, 8)]
    return out or [first]


def resolve_compat(spec_sets, wanted_tag, bases_for, pytag=None, driver=None, log=None):
    """
    在候选 tag 里找第一个真有 wheel 的组合。
    spec_sets：按优先顺序的 torch 规格列表（第一个是 WebUI 的主规格，后面是它代码里
               自己写着的备选版本，比如 Neo 给 Nunchaku 用的 torch 2.11）
    bases_for(tag) → 该 tag 的索引地址列表
    先保证 torch 版本不变（主规格在所有候选 tag 上都试一遍），实在没有才退到备选版本。
    返回 (specs, tag, base)；全部没有返回 (None, 首选 tag, 联上过的索引)；全部联不上（离线）
    时按首选 tag 原样换版本号返回 (specs, tag, None)，和以前的行为一致。
    """
    tags = candidate_tags(wanted_tag, driver)
    reached, offline = None, None
    for specs in spec_sets:
        for t in tags:
            got, base = resolve_specs(specs, t, bases_for(t), pytag, log)
            if got and base:
                return got, t, base
            if got and not base and offline is None:
                offline = (got, t)       # 索引全都联不上
            if base:
                reached = base
    if reached is None and offline:
        return offline[0], offline[1], None
    return None, tags[0], reached


def effective_tag(wanted_tag, driver=None):
    """
    实际该装的 tag：老架构卡（20 系之前，见 is_legacy_gpu）一律 cu126——驱动再新，cu128/cu130 也没有
    它们的内核（报 no kernel image）；其余按驱动能力降级（supported_tag）。
    反过来 50 系配上默认就是老 tag 的 WebUI（Forge Classic 的 cu121）这里不管：那种 WebUI 写死的
    torch 版本根本没有 cu128 的构建，换不了，由 driver_issue / 部署预检提示用户换 Neo。
    """
    need = tag_cuda(wanted_tag)
    if need and need > tag_cuda(LEGACY_TAG) and is_legacy_gpu():
        return LEGACY_TAG
    return supported_tag(wanted_tag, driver)


UNSUPPORTED_GPU_TEXT = (
    "这张显卡（{gpu}）太老了：现在的 PyTorch 只支持 GTX 750 Ti / GTX 9xx 及更新的显卡，"
    "NVIDIA 也已经不再给它出新驱动，升级驱动、换 torch 版本都没用。\n\n"
    "只能用 CPU 出图（非常慢），或者换一张新一些的显卡。")

REDEPLOY_HINT = "到「环境部署」页选中这个安装目录，再点一次「开始部署」，会自动换装合适的 torch。"


def driver_issue(need_tag, what, can_auto_fix, gpu_fix=None, redeploy_fixes=False):
    """
    环境要用的 torch（need_tag，如 cu130）这台电脑带不动时，返回给启动前检查用的提示；没问题返回 None。
    can_auto_fix：驱动带不动时，启动器这次能不能自动换装兼容 torch（Forge 能，ComfyUI 不能）。
    gpu_fix：显卡架构和 torch 对不上时这次启动能不能自动换装（默认同 can_auto_fix；Forge Classic 这类
             写死老 torch 的 WebUI 遇到 50 系就换不了）。
    redeploy_fixes：不能自动换装时，重新部署能不能修好（ComfyUI 能）。
    """
    need = tag_cuda(need_tag)
    names = gpu_names()
    gpu = "、".join(names) or "NVIDIA 显卡"
    gpu_fix = can_auto_fix if gpu_fix is None else gpu_fix
    mismatch = gpu_tag_mismatch(need_tag)
    if mismatch == "unsupported":
        return {"level": "warn", "id": "gpu_unsupported", "text": UNSUPPORTED_GPU_TEXT.format(gpu=gpu)}
    if mismatch == "legacy":
        return {
            "level": "warn", "id": "gpu_too_old",
            "text": f"这张显卡（{gpu}）架构比较老，新版 torch（{need_tag}）已经不支持它，"
                    "升级驱动也没用。\n\n"
                    + ("启动器会自动换装老卡专用的 cu126 版 torch（需要下载约 3GB）。" if gpu_fix
                       else "需要把 torch 换成老卡专用的 cu126 版本：" + REDEPLOY_HINT),
        }
    if mismatch == "blackwell":
        if gpu_fix:
            how = "启动器这次启动会自动换装新版 torch（需要下载约 3GB）。"
        elif redeploy_fixes:
            how = REDEPLOY_HINT
        else:
            how = ("Forge Classic、A1111 这类老版本 WebUI 固定用老 torch，不支持 50 系显卡，"
                   "请到「环境部署」页部署 Forge Neo 或 ComfyUI（模型可以共用，不用重新下载）。")
        return {
            "level": "warn", "id": "gpu_too_new",
            "text": f"这张显卡（{gpu}）是 RTX 50 系，需要 {BLACKWELL_MIN_TAG} 及以上的 torch，而{what}的 torch"
                    f"（{need_tag}）太旧，里面没有这张卡能用的内核，出图时会报 no kernel image。\n\n" + how,
        }
    driver = driver_cuda_version()
    if not need or not driver or driver[0] >= need[0]:
        return None
    return {
        "level": "warn", "id": "driver_outdated", "url": DRIVER_URL,
        "text": f"显卡驱动太旧：{gpu} 当前的驱动最高支持 CUDA {fmt_cuda(driver)}，"
                f"而{what}的 torch（{need_tag}）需要 CUDA {need[0]}，也就是 {driver_min_for(need[0])} 以上的驱动。\n\n"
                f"建议先升级显卡驱动（NVIDIA App 里一键更新，或去官网下载），升级完直接启动就行，不用换 torch。\n\n"
                + ("不想升级的话也可以直接启动：启动器会自动换装兼容的 torch（需要下载约 3GB）。"
                   if can_auto_fix else "不升级驱动的话这次启动会失败。"),
    }


# ------------------------------------------------------------ 换装记录 ----
#
# 驱动太旧时启动器会把 WebUI 默认的 torch 换成驱动带得动的版本。以前换完就不管了：用户照提示
# 升级了驱动，环境里却一直是换装的那一版（Neo 只在没装 torch 时才会装），40 系停在 cu126 上
# 连 SageAttention 都装不了。这里记下「哪个目录被换装成了哪一版」，驱动够新之后提示换回去。
# 只记因为驱动换装的；老架构卡换成 cu126 是永久的，不记。

_FALLBACK_PATH = os.path.join(APP_DIR, "launcher_data", "torch_fallback.json")


def _fb_key(root):
    return os.path.normcase(os.path.abspath(root))


def _fb_load():
    try:
        with open(_FALLBACK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fb_save(data):
    try:
        os.makedirs(os.path.dirname(_FALLBACK_PATH), exist_ok=True)
        with open(_FALLBACK_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def note_fallback(root, torch_spec, wanted_tag):
    """记下 root 的 torch 因为驱动太旧被换装成了 torch_spec（如 2.13.0+cu126）"""
    data, key = _fb_load(), _fb_key(root)
    rec = data.get(key) or {}
    if rec.get("torch") == torch_spec and rec.get("wanted") == wanted_tag:
        return
    data[key] = {"torch": torch_spec, "wanted": wanted_tag, "t": int(time.time())}
    _fb_save(data)


def clear_fallback(root):
    data, key = _fb_load(), _fb_key(root)
    if key in data:
        del data[key]
        _fb_save(data)


def dismiss_fallback(root):
    """用户选了「保持现状」：这条换装记录不再提示"""
    data, key = _fb_load(), _fb_key(root)
    if key in data and not data[key].get("dismissed"):
        data[key]["dismissed"] = True
        _fb_save(data)


def fallback_restorable(root, installed_spec, wanted_tag, driver=None):
    """
    root 里装着的还是当初因为驱动换装的那一版，而现在的驱动和显卡已经带得动 WebUI 默认的
    wanted_tag → True（可以换回去了）。用户自己换过 torch、点过「保持现状」、驱动信息读不到都算 False。
    """
    rec = _fb_load().get(_fb_key(root))
    if not rec or rec.get("dismissed") or not installed_spec or rec.get("torch") != installed_spec:
        return False
    need = tag_cuda(wanted_tag)
    driver = driver if driver is not None else driver_cuda_version()
    if not need or not driver or driver[0] < need[0]:
        return False
    return gpu_tag_mismatch(wanted_tag) is None
