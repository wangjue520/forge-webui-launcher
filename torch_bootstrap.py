# -*- coding: utf-8 -*-
"""
torch 外置预下载器。

为什么需要它：Forge 的 launch.py 会用 pip 自己装 torch（2GB+），pip 的下载器
没有断点续传——下到 90% 断线就整个重来，国内网络下这是部署失败的重灾区。
这个模块在跑 webui.bat 之前，由启动器先把 torch/torchvision 的 wheel 用支持
断点续传的下载器（portable_env.download_file，Range 续传）拉到本地，
校验 sha256（索引页自带的 #sha256= 摘要），再本地 pip install 进目标环境。
Forge 启动时检测到 torch 已安装（is_installed）就会跳过自己的安装步骤。

任何一步失败都只是"预下载没成功"，返回 False 让 Forge 自己装
（启动器已注入 TORCH_INDEX_URL 镜像，那个路径依然可用），绝不阻断部署。
"""
import os
import re
import subprocess
import sys

import requests

import portable_env as pe

# 索引页里 wheel 链接形如：
#   <a href=".../torch-2.13.0%2Bcu130-cp311-cp311-win_amd64.whl#sha256=...">
_WHEEL_LINK_RE = re.compile(
    r'href="([^"#]*?/({name}-[^"#]+?-{pytag}[^"#]*?win_amd64\.whl))(?:#sha256=([0-9a-f]{64}))?"',
    re.IGNORECASE)

_DOWNLOAD_TIMEOUT = 30  # 索引页请求超时


def _log(log, msg):
    log(f"[torch 预下载] {msg}\n")


def _parse_torch_spec(root_dir):
    """
    从 <root>/modules/launch_utils.py 提取 Forge 要的 torch 规格：
    返回 [(name, version), ...] 和官方索引的 cuda tag；解析失败返回 (None, None)。
    例：([('torch', '2.13.0+cu130'), ('torchvision', '0.28.0+cu130')], 'cu130')
    """
    path = os.path.join(root_dir, "modules", "launch_utils.py")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None, None
    m = re.search(r'''TORCH_INDEX_URL["']\s*,\s*["']([^"']+)["']''', text)
    if not m:
        return None, None
    official_index = m.group(1).strip()
    if not official_index.startswith("https://download.pytorch.org/whl/"):
        return None, None
    tag = official_index.rsplit("/", 1)[-1].strip()
    if not re.match(r"^(cu[0-9]+|cpu)$", tag):
        return None, None
    # 只取第一个（主）TORCH_COMMAND 默认值里的包规格；
    # nunchaku 等分支的备选规格不考虑——那种情况让用户自己走 TORCH_COMMAND
    m = re.search(r'''TORCH_COMMAND["']\s*,\s*f?["']pip install ([^"']+)["']''', text)
    if not m:
        return None, None
    specs = []
    for tok in m.group(1).split():
        pm = re.match(r"^([A-Za-z0-9_-]+)==([A-Za-z0-9.+!_]+)$", tok)
        if pm:
            specs.append((pm.group(1).lower(), pm.group(2)))
    if not specs:
        return None, None
    return specs, tag


def _python_tag(python_exe, log):
    """目标解释器的 wheel tag（如 cp311）和版本；拿不到返回 None"""
    try:
        r = subprocess.run(
            [python_exe, "-c",
             "import sys; print('cp%d%d' % sys.version_info[:2]); print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, timeout=30, creationflags=0x08000000 if os.name == "nt" else 0)
        lines = (r.stdout or b"").decode("utf-8", errors="replace").splitlines()
        if r.returncode == 0 and len(lines) >= 2:
            return lines[0].strip(), lines[1].strip()
    except Exception:
        pass
    return None, None


def _installed_version(python_exe, package):
    try:
        r = subprocess.run(
            [python_exe, "-m", "pip", "show", package],
            capture_output=True, timeout=60,
            env={**os.environ, "PIP_CONFIG_FILE": os.devnull},
            creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode != 0:
            return None
        out = (r.stdout or b"").decode("utf-8", errors="replace")
        m = re.search(r"^Version:\s*(\S+)", out, re.MULTILINE)
        return m.group(1) if m else None
    except Exception:
        return None


def _resolve_wheel(index_base, name, version, pytag, log):
    """
    在 {index_base}/{name}/ 索引页里找精确版本+平台+pytag 的 wheel，
    返回 (url, sha256_or_None)；找不到返回 (None, None)。
    """
    from urllib.parse import urljoin
    page_url = f"{index_base}/{name}/"
    resp = requests.get(page_url, timeout=_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    pattern = _WHEEL_LINK_RE.pattern.replace("{name}", re.escape(name)).replace(
        "{pytag}", re.escape(pytag))
    for href, fname, sha in re.findall(pattern, resp.text):
        # 文件名里的版本号必须精确匹配（torch-2.13.0+cu130 或 %2B 编码）
        ver_norm = version.replace("+", "%2b").lower()
        if (f"-{version}-" not in fname.lower()
                and ver_norm not in fname.lower()):
            continue
        return urljoin(page_url, href), (sha or None)
    return None, None


def _pip_install_local(python_exe, wheel_paths, cfg, log):
    """把下载好的 wheel 本地装进目标环境。torch 的依赖（filelock/sympy 等）
    从 pip 索引补，走镜像。"""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PIP_") and k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_RETRIES"] = "5"
    env["PIP_TIMEOUT"] = "60"
    try:
        import mirror_manager as mm
        if mm.resolve_mode(cfg):
            env["PIP_INDEX_URL"] = mm.PYPI_MIRRORS[0][1]
    except Exception:
        pass
    r = subprocess.run(
        [python_exe, "-m", "pip", "install", *wheel_paths],
        capture_output=True, timeout=1800, env=env,
        creationflags=0x08000000 if os.name == "nt" else 0)
    out = (r.stdout or b"").decode("utf-8", errors="replace")
    tail = (r.stderr or b"").decode("utf-8", errors="replace")
    if r.returncode != 0:
        _log(log, f"本地安装失败（退出码 {r.returncode}）：\n{tail[-800:] or out[-800:]}")
        return False
    return True


def ensure_torch(root_dir, cfg, log, progress=None, cancel_event=None):
    """
    部署主流程调用：在 webui.bat 之前把 torch 装好。返回 True=已就绪
    （Forge 会跳过自己的安装），False=没装上（Forge 自己装，不阻断部署）。
    """
    try:
        return _ensure_torch(root_dir, cfg, log, progress, cancel_event)
    except Exception as e:
        _log(log, f"预下载出现异常（不影响后续，Forge 会自行安装）: {e}")
        return False


def _ensure_torch(root_dir, cfg, log, progress, cancel_event):
    if os.name != "nt":
        return False  # 目前只处理 Windows 的 wheel

    specs, tag = _parse_torch_spec(root_dir)
    if not specs:
        _log(log, "无法从 launch_utils.py 解析 torch 版本规格，跳过预下载")
        return False

    # 目标解释器：venv 存在用 venv（继续/重建场景），否则便携 Python
    # （VENV_DIR=- 场景，依赖直接装进便携 Python）
    venv_py = os.path.join(root_dir, "venv", "Scripts", "python.exe")
    portable_py = os.path.join(root_dir, "python", "python.exe")
    if os.path.exists(venv_py):
        python_exe = venv_py
    elif os.path.exists(portable_py):
        python_exe = portable_py
    else:
        _log(log, "没有找到可用的目标 Python，跳过预下载")
        return False

    # 已经装对了就跳过（部署可重入）
    want = {n: v for n, v in specs}
    if all(_installed_version(python_exe, n) == v for n, v in want.items()):
        _log(log, "torch/torchvision 已是目标版本，跳过")
        return True

    pytag, pyver = _python_tag(python_exe, log)
    if not pytag:
        _log(log, "无法确定目标 Python 版本，跳过预下载")
        return False

    try:
        import mirror_manager as mm
        use_mirror = mm.resolve_mode(cfg)
    except Exception:
        use_mirror = False
    official = f"https://download.pytorch.org/whl/{tag}"
    if use_mirror:
        bases = [f"{b}/{tag}" for b in mm.PYTORCH_MIRROR_BASES] + [official]
    else:
        bases = [official] + [f"{b}/{tag}" for b in mm.PYTORCH_MIRROR_BASES]

    cache_dir = os.path.join(root_dir, ".launcher_cache", "wheels")
    os.makedirs(cache_dir, exist_ok=True)

    cancel = (lambda: cancel_event.is_set()) if cancel_event is not None else None

    for base in bases:
        try:
            _log(log, f"候选索引: {base}")
            downloads = []
            for name, version in specs:
                wheel_url, sha = _resolve_wheel(base, name, version, pytag, log)
                if not wheel_url:
                    raise pe.PortableEnvError(
                        f"索引里找不到 {name}=={version} 的 {pytag}/win_amd64 wheel")
                downloads.append((name, version, wheel_url, sha))
            _log(log, "索引解析完成，开始下载 "
                      + "、".join(f"{n}=={v}" for n, v, _, _ in downloads))
            paths = []
            for name, version, wheel_url, sha in downloads:
                fname = wheel_url.rsplit("/", 1)[-1]
                # URL 里的 %2B 解码回 +，避免文件名和包名对不上
                fname = fname.replace("%2B", "+").replace("%2b", "+")
                dest = os.path.join(cache_dir, fname)
                if not (os.path.exists(dest) and _sha256_ok(dest, sha)):
                    _log(log, f"下载 {fname} ...")
                    pe.download_file(wheel_url, dest, progress_cb=progress,
                                     cancel_flag=cancel, cfg=None, log_cb=log)
                    if not _sha256_ok(dest, sha):
                        # 校验失败：连 .part 一起删掉，否则换源重试时会从
                        # 上一个源的损坏断点继续追加，永远修不好
                        for p in (dest, dest + ".part"):
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                        raise pe.PortableEnvError(f"{fname} 下载后 sha256 校验不符")
                else:
                    _log(log, f"{fname} 已有缓存且校验通过，跳过下载")
                paths.append(dest)
            _log(log, "下载完成，本地安装到目标环境 ...")
            if _pip_install_local(python_exe, paths, cfg, log):
                ok = all(_installed_version(python_exe, n) == v for n, v in want.items())
                if ok:
                    _log(log, "torch 环境预装完成 ✓（Forge 启动时将跳过自己的安装步骤）")
                    return True
                _log(log, "安装后版本核验不通过，放弃预装结果")
        except pe.PortableEnvError as e:
            if "已取消" in str(e):
                raise
            _log(log, f"该候选失败: {e}，换下一个候选")
        except Exception as e:
            _log(log, f"该候选失败: {e}，换下一个候选")
    _log(log, "所有候选都未成功，将由 Forge 自行安装（已注入镜像索引兜底）")
    return False


def _sha256_ok(path, expected):
    if not expected:
        return True  # 索引页没带摘要时只能不验（官方页和主流镜像都带）
    if not os.path.exists(path):
        return False
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected.lower()
