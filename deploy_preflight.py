# -*- coding: utf-8 -*-
"""
部署预检模块：在下载几个 GB 的东西之前，把能在 3 秒内查出来的问题先查出来。
全部检查都是只读的，任何一步失败都不影响主流程（预检是锦上添花）。
"""
import os
import re
import shutil
import subprocess
import tempfile

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 部署需要的磁盘空间：Forge 本体+venv+torch 等依赖约 10GB，再加模型和缓存余量
_DISK_ERROR_GB = 8      # 低于这个直接不让开始
_DISK_WARN_GB = 25      # 低于这个给提醒
_TEMP_WARN_GB = 2       # TEMP 盘低于这个提醒（部署时 TEMP 会自动重定向到目标盘）

# 常见国产杀软/安全软件的进程名（用于事前预警"可能弹拦截框"）
_AV_PROCESSES = {
    "360tray.exe": "360 安全卫士",
    "360sd.exe": "360 杀毒",
    "qqpctray.exe": "腾讯电脑管家",
    "hipstray.exe": "火绒安全",
    "usysdiag.exe": "火绒安全",
}


def _free_gb(path):
    """path 所在盘的剩余空间（GB），拿不到返回 None"""
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return None


def detect_nvidia():
    """
    探测 NVIDIA 显卡。返回 (name, driver_version) 或 None。
    优先 nvidia-smi（驱动装了才有）；找不到不代表一定没有 N 卡（驱动没装），
    调用方注意措辞。
    """
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    if not os.path.exists(exe):
        return None
    try:
        r = subprocess.run(
            [exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, timeout=10, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            return None
        line = (r.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
        if not line or not line[0].strip():
            return None
        parts = [p.strip() for p in line[0].split(",")]
        return (parts[0], parts[1] if len(parts) > 1 else "")
    except Exception:
        return None


def detect_antivirus():
    """返回正在运行的第三方杀软名称列表（用于事前预警）。失败返回空列表。"""
    try:
        r = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                           capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        out = (r.stdout or b"").decode("gbk", errors="replace").lower()
        found = []
        for proc, name in _AV_PROCESSES.items():
            if proc in out and name not in found:
                found.append(name)
        return found
    except Exception:
        return []


def clock_skew_seconds():
    """
    系统时钟偏差（秒）：跟 HTTP 响应头里的 Date 对比。偏差太大会让全网
    TLS 证书校验失败（证书"尚未生效/已过期"）。取不到返回 None。
    """
    import email.utils
    import time
    import requests
    for url in ("https://pypi.org", "https://www.baidu.com"):
        try:
            resp = requests.head(url, timeout=4)
            date = resp.headers.get("Date")
            if not date:
                continue
            server_ts = email.utils.parsedate_to_datetime(date).timestamp()
            return abs(time.time() - server_ts)
        except Exception:
            continue
    return None


def precheck_issues(target):
    """
    返回预检问题列表：[{"level": "error"/"warn", "id": ..., "text": ...}]
    与 webview_api._api_deploy_precheck 的 issues 结构一致。
    """
    issues = []

    free = _free_gb(target)
    if free is not None:
        if free < _DISK_ERROR_GB:
            issues.append({
                "level": "error", "id": "disk_space",
                "text": f"目标盘剩余空间只有 {free:.1f} GB。Forge 本体 + 依赖约需 10 GB，"
                        f"再加大模型建议至少 25 GB。请清理磁盘或换一个盘。"})
        elif free < _DISK_WARN_GB:
            issues.append({
                "level": "warn", "id": "disk_space",
                "text": f"目标盘剩余空间 {free:.1f} GB，勉强够装 Forge 本体，"
                        f"但放不下几个大模型（每个 2~7 GB）。建议预留 25 GB 以上。"})

    temp_free = _free_gb(tempfile.gettempdir())
    if temp_free is not None and temp_free < _TEMP_WARN_GB:
        issues.append({
            "level": "warn", "id": "temp_space",
            "text": f"系统临时目录（TEMP）所在盘只剩 {temp_free:.1f} GB，依赖安装时可能爆满。"
                    f"部署时启动器会自动把临时目录改到安装盘，但建议还是清理一下系统盘。"})

    if "onedrive" in target.lower():
        issues.append({
            "level": "warn", "id": "onedrive",
            "text": "安装目录在 OneDrive 同步目录里。OneDrive 会把文件变成云端占位符、"
                    "锁文件、把几 GB 的依赖往云上同步，部署和运行都会出怪问题。"
                    "强烈建议换到本地普通目录（例如 D:\\forge）。"})

    gpu = detect_nvidia()
    if gpu is None:
        issues.append({
            "level": "warn", "id": "no_nvidia",
            "text": "没有检测到 NVIDIA 显卡（或没装驱动）。Forge 将只能用 CPU 出图，"
                    "速度非常慢（每张图几分钟到几十分钟）。如果你有 N 卡，"
                    "请先到 NVIDIA 官网安装显卡驱动。"})
    elif gpu[1]:
        # cu130 需要较新的驱动（CUDA 13 对应 R580+）；给提醒不拦截
        m = re.match(r"(\d+)", gpu[1])
        if m and int(m.group(1)) < 580:
            issues.append({
                "level": "warn", "id": "old_driver",
                "text": f"当前显卡驱动版本 {gpu[1]}，可能不满足新版 PyTorch（cu130）的要求"
                        f"（建议 R580 以上）。如果部署后启动报 CUDA driver 相关错误，"
                        f"请先升级显卡驱动：https://www.nvidia.cn/drivers/"})

    avs = detect_antivirus()
    if avs:
        issues.append({
            "level": "warn", "id": "antivirus",
            "text": f"检测到正在运行的安全软件：{'、'.join(avs)}。部署要下载并解压几百兆的"
                    f" Python/Git，如果它们弹出拦截/查杀提示，请选择「允许」或「信任」；"
                    f"如果文件被误删，请把安装目录加入白名单后重新部署。"})

    return issues
