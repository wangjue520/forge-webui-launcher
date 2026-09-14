# -*- coding: utf-8 -*-
"""
便携版环境部署模块。

目标：让"环境部署"页可以做到完全不依赖用户系统里是否装了 Python / Git，
自动下载真正独立、可重定位的运行环境，解压成跟"秋叶整合包"一样的目录
结构（<root>/python/python.exe、<root>/git/cmd/git.exe），这样后续
config_manager.detect_bundled_python / detect_bundled_git 能直接识别到。

Python 来源：astral-sh/python-build-standalone
    这不是官方 python.org 的"embeddable"精简包（那个阉割了 venv/ensurepip，
    没法用来创建虚拟环境），而是真正完整、自包含、可重定位的 CPython 构建——
    uv (astral 的包管理器) 底层用的就是这同一套构建产物，包含完整标准库、
    pip、venv 模块，解压即用。

Git 来源：git-for-windows 官方发布的 PortableGit（自解压 7z 包），
    这是 Git for Windows 项目自己提供的便携版本，不是第三方转包的。

两者都通过 GitHub Releases API 实时查询最新版本和下载链接，不写死某个
具体版本号/文件名（那样过一段时间必然失效）。
"""
import os
import re
import subprocess
import tarfile
import tempfile

import requests

GITHUB_API = "https://api.github.com"
PBS_REPO = "astral-sh/python-build-standalone"
GIT_REPO = "git-for-windows/git"

# 分支对应推荐部署的 Python 大版本前缀。
# Classic 分支历史上锁定在 3.10.x；Neo 分支在启动时会打印它测试的具体版本
# （目前是 3.13.x），这里跟随 Neo 自己声明的版本诉求。
#
# 踩过的坑：界面上"环境部署"页的分支下拉框后来改成了 classic/neo2 两个
# key（neo2 = 新版参数体系的 Neo），但这张表当时没有同步更新，还只认
# "neo" 这个旧 key。结果是部署 Neo 时 .get(branch_key, "3.10") 查不到
# "neo2"，静默 fallback 到 3.10——装出来的 Python 版本比 Neo 最新
# requirements.txt 要求的低了两个大版本（比如 numpy 2.3.x 需要
# Python 3.11+），装依赖时一堆包报 "Requires-Python >=3.11/3.12/3.13"、
# 目标包版本"找不到匹配的发行版"，表面上看像是网络或镜像问题，实际上
# 是从一开始就选错了 Python 版本。
# "neo" 这个旧 key 继续保留，兼容可能还存着旧配置的用户。
PYTHON_VERSION_BY_BRANCH = {
    "classic": "3.10",
    "neo": "3.13",
    "neo2": "3.13",
}


class PortableEnvError(Exception):
    pass


_GH_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ForgeLauncher/1.0"}


def _github_candidates(url, cfg=None, log_cb=None):
    """
    与 download_file 同一套镜像策略：GitHub 相关地址在 resolve_github_mode
    判定需要加速时，把各加速代理地址放前面、原站兜底；否则只返回原站。
    纯本地调用（cfg=None）时保持原行为。
    """
    urls = [url]
    if cfg is not None:
        try:
            import mirror_manager as mm
            if mm.resolve_github_mode(cfg, log_cb):
                mirrored = [mm.github_url(url, True, i) for i in range(len(mm.GITHUB_PROXIES))]
                urls = [u for u in mirrored if u != url] + [url]
        except Exception:
            pass
    return urls


def _get_json(url, cfg=None, log_cb=None, timeout=30):
    """
    带镜像兜底的 JSON GET。踩过的坑：这两个 GitHub 查询接口
    （raw.githubusercontent.com / api.github.com）以前都是裸连，而
    download_file 那套下载逻辑明明已经会按检测结果走加速代理——结果是
    「下文件能走镜像、查版本号却裸连」，国内把 GitHub 墙了的网络环境里，
    部署直接死在第一步「查询最新版本」，跟没做镜像加速一样。
    现在查询和下载走同一套候选地址策略，全部失败才报错。
    """
    urls = _github_candidates(url, cfg, log_cb)
    last_error = None
    for i, u in enumerate(urls):
        try:
            if log_cb and i > 0:
                log_cb(f"[网络] 上一个地址失败，改用: {u.split('/')[2]}")
            resp = requests.get(u, headers=_GH_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = e
            continue
    raise PortableEnvError(f"查询失败（已尝试 {len(urls)} 个地址）: {last_error}")


def get_latest_pbs_tag(cfg=None, log_cb=None):
    """获取 python-build-standalone 最新 release 的 tag（形如 '20260610'）"""
    data = _get_json(
        "https://raw.githubusercontent.com/astral-sh/python-build-standalone/latest-release/latest-release.json",
        cfg, log_cb,
    )
    tag = data.get("tag") if isinstance(data, dict) else None
    if not tag:
        raise PortableEnvError("无法获取 python-build-standalone 最新版本号")
    return tag


def list_release_assets(repo, tag=None, cfg=None, log_cb=None):
    """tag=None 表示查询 'latest' release"""
    if tag:
        url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    else:
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    data = _get_json(url, cfg, log_cb)
    return [{"name": a["name"], "url": a["browser_download_url"]} for a in data.get("assets", [])]


def pick_python_asset(assets, version_prefix):
    """
    在 assets 里找 Windows x86_64、install_only（非stripped）、优先 .tar.gz
    （避免额外依赖 zstd 解压库）的 CPython 构建。
    """
    candidates = []
    for a in assets:
        name = a["name"]
        if not name.startswith(f"cpython-{version_prefix}"):
            continue
        if "x86_64-pc-windows-msvc" not in name:
            continue
        if "install_only" not in name or "install_only_stripped" in name:
            continue
        # 排除自由线程（no-GIL / free-threading）实验构建：文件名带 "freethreaded"，
        # ABI 是 cp3XXt，torch 有对应 wheel 但 numpy 等一大批包没有，pip 会退回
        # 源码编译然后因为没有 C 编译器而失败。必须用常规 GIL 构建。
        if "freethreaded" in name:
            continue
        candidates.append(a)

    if not candidates:
        return None

    # 优先 .tar.gz（tarfile 标准库原生支持gzip），没有的话退而求其次用 .tar.zst
    for a in candidates:
        if a["name"].endswith(".tar.gz"):
            return a
    for a in candidates:
        if a["name"].endswith(".tar.zst"):
            return a
    return candidates[0]


def pick_git_asset(assets):
    """在 git-for-windows 的 release assets 里找 64位 PortableGit 自解压包"""
    pattern = re.compile(r"^PortableGit-[\d.]+-64-bit\.7z\.exe$")
    for a in assets:
        if pattern.match(a["name"]):
            return a
    return None


def download_file(url, dest_path, progress_cb=None, cancel_flag=None, cfg=None, log_cb=None):
    """
    下载文件。cfg 传入时会根据镜像设置尝试 GitHub 加速代理，
    代理失败自动换下一个，全部失败则回退原始地址。
    """
    urls = [url]
    if cfg is not None:
        try:
            import mirror_manager as mm
            if mm.resolve_github_mode(cfg, log_cb):
                mirrored = [mm.github_url(url, True, i) for i in range(len(mm.GITHUB_PROXIES))]
                urls = [u for u in mirrored if u != url] + [url]
        except Exception:
            pass

    last_error = None
    for i, u in enumerate(urls):
        try:
            if log_cb and i > 0:
                log_cb(f"[网络] 上一个地址失败，改用: {u.split('/')[2]}")
            return _download_once(u, dest_path, progress_cb, cancel_flag, log_cb=log_cb)
        except PortableEnvError as e:
            # 用户主动取消：直接向上抛。其余 PortableEnvError（重试耗尽等）
            # 记下来，换下一个候选地址整个重新试。
            if "已取消" in str(e):
                raise
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue
    raise PortableEnvError(f"下载失败（已尝试 {len(urls)} 个地址）: {last_error}")


# 传输中途连接被重置/中断，值得对同一个地址原地重试（通常是网络抖动，
# 换地址反而更慢）；连不上/403/404 这类不会重试成功的错误不在此列。
_TRANSIENT_EXCEPTIONS = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    ConnectionResetError,
)

_MAX_RESUME_ATTEMPTS = 5


def _download_once(url, dest_path, progress_cb=None, cancel_flag=None, log_cb=None):
    """
    下载单个 URL，支持断点续传：传输中途连接被重置时，不是整个重新下载，
    而是用 HTTP Range 头从已下载的字节数继续接着要，最多重试
    _MAX_RESUME_ATTEMPTS 次。大文件（如 torch 相关的几百MB~几个GB 的
    便携环境包）在不稳定网络下中途断线是常态，之前的逻辑一断线就整个
    地址判失败换下一个，既慢又容易把所有候选地址都耗尽。
    """
    tmp_path = dest_path + ".part"
    downloaded = 0
    if os.path.exists(tmp_path):
        downloaded = os.path.getsize(tmp_path)

    total = 0
    attempt = 0
    while True:
        headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
        try:
            with requests.get(url, stream=True, timeout=60, headers=headers) as resp:
                if downloaded and resp.status_code == 200:
                    # 服务器不支持 Range，忽略了续传请求，返回了完整内容——
                    # 已下载的部分作废，从头开始写
                    downloaded = 0
                elif downloaded and resp.status_code not in (200, 206):
                    resp.raise_for_status()
                else:
                    resp.raise_for_status()

                content_range_total = None
                cr = resp.headers.get("content-range")
                if cr and "/" in cr:
                    try:
                        content_range_total = int(cr.rsplit("/", 1)[1])
                    except ValueError:
                        pass
                total = content_range_total or (downloaded + int(resp.headers.get("content-length", 0)))

                mode = "ab" if downloaded and resp.status_code == 206 else "wb"
                if mode == "wb":
                    downloaded = 0
                with open(tmp_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if cancel_flag and cancel_flag():
                            raise PortableEnvError("下载已取消")
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)
            break  # 正常下载完成
        except PortableEnvError:
            raise  # 用户主动取消，不重试
        except _TRANSIENT_EXCEPTIONS as e:
            attempt += 1
            downloaded = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
            if attempt >= _MAX_RESUME_ATTEMPTS:
                raise PortableEnvError(
                    f"下载中断且重试 {attempt} 次后仍失败（已下载 {downloaded} 字节）: {e}")
            if log_cb:
                log_cb(f"[网络] 连接中断，从 {downloaded/1024/1024:.1f} MB 处断点续传"
                       f"（第 {attempt}/{_MAX_RESUME_ATTEMPTS} 次重试）...")

    os.replace(tmp_path, dest_path)
    return dest_path


def extract_python_tar(archive_path, root_dir, log_cb=None):
    """
    python-build-standalone 的 install_only 归档顶层就是一个 "python/" 目录，
    直接解压到 WebUI 根目录下，得到 <root>/python/python.exe，
    正好符合 config_manager.detect_bundled_python 的检测约定。
    """
    mode = "r:gz" if archive_path.endswith(".tar.gz") else "r:*"
    if log_cb:
        log_cb(f"正在解压 Python 到 {root_dir} ...")
    with tarfile.open(archive_path, mode) as tf:
        # Python 3.12 起对 extractall 不加过滤发弃用警告、3.14 起默认策略
        # 变更（PEP 706）。显式指定 "data"（拒绝绝对路径和 .. 跳转，防篡改
        # 归档）；老版本 Python 没有 filter 参数，回退默认行为。
        try:
            tf.extractall(root_dir, filter="data")
        except TypeError:
            tf.extractall(root_dir)
    python_exe = os.path.join(root_dir, "python", "python.exe")
    if not os.path.exists(python_exe):
        raise PortableEnvError(f"解压完成但没找到 {python_exe}，归档内部目录结构可能变化了")
    return python_exe


def extract_portable_git(archive_path, root_dir, log_cb=None):
    """
    PortableGit-*.7z.exe 是 7-Zip 自解压包，支持命令行静默解压参数：
    -y（全部确认） -o路径（无空格，直接跟在 -o 后面）。
    解压到 <root>/git，得到 <root>/git/cmd/git.exe。
    """
    target_dir = os.path.join(root_dir, "git")
    os.makedirs(target_dir, exist_ok=True)
    if log_cb:
        log_cb(f"正在解压 Git 到 {target_dir} ...")
    result = subprocess.run(
        [archive_path, "-y", f"-o{target_dir}"],
        capture_output=True, text=True, timeout=300,
    )
    git_exe = os.path.join(target_dir, "cmd", "git.exe")
    if result.returncode != 0 or not os.path.exists(git_exe):
        raise PortableEnvError(
            f"Git 解压失败 (退出码 {result.returncode})：{result.stdout}\n{result.stderr}"
        )
    return git_exe


# ============================================================
# venv 诊断 + hashlib 兼容性补丁
#
# 踩过的坑：venv 一旦创建，就把"当初是用哪个 Python 建的"记录死在
# venv/pyvenv.cfg 的 home= 字段里。之后不管启动时把 PYTHON 环境变量
# 指向哪个解释器，只要 venv 文件夹还在，webui.bat 检测到 venv 已存在
# 就直接复用，完全不理会新的 PYTHON 变量——这也是为什么"改分支/换便携版
# Python"之后问题依旧的真正原因：用户以为换了 Python，实际上 venv 还在
# 用建立时那个（可能是系统里另装的、版本对不上的）解释器。
# ============================================================

def read_venv_pyvenv_cfg(venv_dir):
    """解析 venv 的 pyvenv.cfg，返回 dict；venv 不存在或没这个文件返回 None"""
    cfg_path = os.path.join(venv_dir, "pyvenv.cfg")
    if not os.path.exists(cfg_path):
        return None
    result = {}
    with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    return result


def get_venv_python_info(venv_dir):
    """
    返回 (version_str, home_path) 或 None（venv 不存在/读不出版本）。
    version_str 形如 '3.10.11'，home_path 是当初创建这个 venv 用的
    Python 解释器所在目录（不是 python.exe 本身）。
    """
    cfg = read_venv_pyvenv_cfg(venv_dir)
    if not cfg:
        return None
    version = cfg.get("version") or cfg.get("version_info")
    home = cfg.get("home")
    return (version, home)


def check_venv_version_mismatch(venv_dir, required_major_minor):
    """
    检查现有 venv 的 Python 版本是否满足 required_major_minor（如 "3.13"）。
    返回 (has_mismatch: bool, detail: str)。venv 不存在时 has_mismatch=False
    （表示"没有历史包袱"，可以放心新建）。
    """
    info = get_venv_python_info(venv_dir)
    if info is None:
        return False, "该目录下还没有 venv，全新创建不存在版本冲突问题"

    version, home = info
    if not version:
        return False, "venv 存在但读不出版本号（可能是非标准 venv），无法判断"

    major_minor = ".".join(version.split(".")[:2])
    if major_minor != required_major_minor:
        return True, (
            f"检测到现有 venv 是用 Python {version} 创建的（来自 {home}），"
            f"跟当前分支要求的 {required_major_minor}.x 不一致。\n"
            f"venv 一旦建好就认死了创建时的 Python，之后改 PYTHON 环境变量也没用，"
            f"必须删掉 venv 文件夹重建才能真正切换版本。"
        )
    return False, f"venv 版本 {version} 与要求的 {required_major_minor}.x 一致，没有问题"


HASHLIB_FILE_DIGEST_PATCH = '''# 由 Forge WebUI 启动器自动写入
# 给低于 Python 3.11 的环境补上 hashlib.file_digest 方法。
# Forge/Neo 在计算模型 SHA-256 哈希时会调用这个 3.11+ 才有的方法，
# 老版本 Python 缺这个方法会在加载新模型时崩溃报
# "AttributeError: module 'hashlib' has no attribute 'file_digest'"。
# 这里按官方 3.11+ 的实现原样补上，不影响已经有这个方法的新版本 Python。
import hashlib as _hashlib

if not hasattr(_hashlib, "file_digest"):
    def _file_digest(fileobj, digest, *, _bufsize=2 ** 18):
        digestobj = _hashlib.new(digest) if isinstance(digest, str) else digest()
        buf = bytearray(_bufsize)
        view = memoryview(buf)
        while True:
            size = fileobj.readinto(buf)
            if size == 0:
                break
            digestobj.update(view[:size])
        return digestobj

    _hashlib.file_digest = _file_digest
'''


def _find_site_packages_dir(venv_dir):
    """兼容 Windows(venv/Lib/site-packages) 和 posix(venv/lib/pythonX.Y/site-packages) 两种布局"""
    win_path = os.path.join(venv_dir, "Lib", "site-packages")
    if os.path.isdir(win_path):
        return win_path

    lib_dir = os.path.join(venv_dir, "lib")
    if os.path.isdir(lib_dir):
        for name in os.listdir(lib_dir):
            candidate = os.path.join(lib_dir, name, "site-packages")
            if os.path.isdir(candidate):
                return candidate
    return None


def write_hashlib_patch(venv_dir, log_cb=None):
    """
    往 venv 的 site-packages 写入 sitecustomize.py 补丁（Python 解释器每次
    启动都会自动加载这个文件，是官方支持的机制，不需要改 Forge 源码）。
    作为部署完成后的保险步骤，不管这次用的是新版还是旧版 Python 都写一份，
    反正补丁本身检测到已有 file_digest 就什么都不做，无害。
    """
    target_dir = _find_site_packages_dir(venv_dir)
    if not target_dir:
        if log_cb:
            log_cb("未找到 venv 的 site-packages 目录，跳过写入 hashlib 兼容性补丁")
        return False

    patch_path = os.path.join(target_dir, "sitecustomize.py")
    with open(patch_path, "w", encoding="utf-8") as f:
        f.write(HASHLIB_FILE_DIGEST_PATCH)
    if log_cb:
        log_cb(f"已写入 hashlib 兼容性补丁: {patch_path}")
    return True
