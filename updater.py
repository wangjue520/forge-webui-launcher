# -*- coding: utf-8 -*-
"""
启动器自更新模块。

目标：用户点一下「立即更新」就把启动器升级到 GitHub 仓库的最新代码，
不用再去重新下载整个压缩包。两条路径自动选择：

1) 目录里有 .git（用户是 git clone 来的）：走 git fetch + 快进合并，
   只下载变化的提交，流量最小。GitHub 不可直连时自动换加速代理拉取。
2) 没有 .git（用户是下载 ZIP 的）：下载仓库最新 main 分支的 zip，
   解压后覆盖到启动器目录——但跳过用户私有/运行期生成的内容
   （launcher_config.json、便携 python/、wd14_venv/ 等，见 PRESERVE_TOP）。

版本号规则：BASE_VERSION + 提交数（如 2.0.42），每次更新自动累加、
写进项目根目录的 version.json。有 .git 时直接按 git 提交数实时算，
永远准确；纯 ZIP 用户以 version.json 为准（每次更新时重写）。
提交数从 GitHub API 分页响应的 Link 头里取（per_page=1 时最后一页
页码就是总提交数），加速代理可能不透传该头，拿不到就本地计数 +1，
保证版本号单调递增。

维护者打包前可执行 `python updater.py --write-version` 把当前 git
版本写进 version.json，让 ZIP 用户拿到的就是最新版本号。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime

import requests

APP_DIR = os.path.dirname(os.path.abspath(__file__))

REPO = "wangjue520/forge-webui-launcher"
BRANCH = "main"
REPO_URL = f"https://github.com/{REPO}.git"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
COMMITS_API = f"https://api.github.com/repos/{REPO}/commits?sha={BRANCH}&per_page=1"

BASE_VERSION = "2.0"
VERSION_FILE = os.path.join(APP_DIR, "version.json")

# 覆盖更新时要跳过的顶层名字：用户私有配置、自动下载的运行环境、缓存。
# 这些都是启动器运行期生成/下载的，覆盖掉等于把用户环境重置了。
PRESERVE_TOP = {
    ".git", ".venv", "python", "git", "venv", "wd14_venv", "wd_tagger_models", "tmp",
    "clipboard_inbox",
    "launcher_config.json", "model_hash_cache.json", "version.json",
}

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_GH_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ForgeLauncher/1.0"}


class UpdateError(Exception):
    pass


# ============================================================
# 本地版本
# ============================================================

def _find_git_exe(cfg=None):
    """自定义路径优先，其次系统 PATH。找不到返回 None（走 ZIP 覆盖路径）。"""
    custom = ((cfg or {}).get("custom_git_path") or "").strip().strip('"')
    if custom and os.path.exists(custom):
        return custom
    return shutil.which("git")


def _git(git_exe, *args, timeout=30):
    # 不用 text=True：Windows 中文系统按 GBK 解码，git 输出里的 UTF-8 字符
    # （提交信息、文件名）会直接炸 UnicodeDecodeError
    try:
        r = subprocess.run([git_exe, "-C", APP_DIR, *args],
                           capture_output=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        # 统一成 UpdateError：调用方按它换下一个源/代理重试，
        # 否则一次超时就会跳过整个镜像兜底链
        raise UpdateError(f"git {' '.join(args[:2])} 超时（{timeout} 秒）")
    out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    if r.returncode != 0:
        raise UpdateError(err or out or "git 命令失败")
    return out


def _git_local_info(git_exe):
    count = int(_git(git_exe, "rev-list", "--count", "HEAD"))
    sha = _git(git_exe, "rev-parse", "HEAD")
    return {
        "version": f"{BASE_VERSION}.{count}",
        "commit": sha[:7],
        "sha": sha,
        "count": count,
        "source": "git",
    }


def _json_local_info():
    if not os.path.exists(VERSION_FILE):
        return None
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "version": data.get("version") or "未知",
            "commit": (data.get("commit") or "")[:7],
            "sha": data.get("commit") or "",
            "count": int(data.get("count") or 0),
            "source": "version.json",
        }
    except Exception:
        return None


def get_local_info(cfg=None):
    """
    当前版本信息：{"version","commit","sha","count","source"}。
    有 .git 且 git 可用时以 git 实时提交数为准；version.json 更新（比如
    上次是用 ZIP 覆盖更新的）则以 version.json 为准。两边都拿不到说明是
    老版本启动器（还没有版本文件），version 显示「未知」，检查更新时
    一律视为可更新。
    """
    git_info = None
    if os.path.isdir(os.path.join(APP_DIR, ".git")):
        git_exe = _find_git_exe(cfg)
        if git_exe:
            try:
                git_info = _git_local_info(git_exe)
            except Exception:
                git_info = None
    json_info = _json_local_info()
    if git_info and json_info:
        return json_info if json_info["count"] > git_info["count"] else git_info
    return git_info or json_info or {
        "version": "未知", "commit": "", "sha": "", "count": 0, "source": "unknown",
    }


def write_version_file(version, commit, count):
    data = {
        "version": version,
        "commit": commit,
        "count": int(count or 0),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


# ============================================================
# 远端版本（GitHub API，带加速代理兜底）
# ============================================================

def _candidate_urls(url, cfg, log_cb):
    urls = [url]
    try:
        import mirror_manager as mm
        if mm.resolve_github_mode(cfg or {}, log_cb):
            mirrored = [mm.github_url(url, True, i) for i in range(len(mm.GITHUB_PROXIES))]
            urls = [u for u in mirrored if u != url] + [url]
    except Exception:
        pass
    return urls


def get_remote_info(cfg=None, log_cb=None):
    """
    远端 main 分支最新提交：{"sha","short","message","date","count"}。
    count 来自分页 Link 头（per_page=1 时 last 页码=总提交数），
    代理不透传该头时为 None，调用方自行兜底。
    """
    import re
    urls = _candidate_urls(COMMITS_API, cfg, log_cb)
    last_error = None
    for i, u in enumerate(urls):
        try:
            if log_cb and i > 0:
                log_cb(f"[网络] 上一个地址失败，改用: {u.split('/')[2]}")
            resp = requests.get(u, headers=_GH_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                raise UpdateError("接口返回内容异常（不是提交列表）")
            item = data[0]
            count = None
            link = resp.headers.get("Link") or ""
            m = re.search(r'[?&]page=(\d+)>\s*;\s*rel="last"', link)
            if m:
                count = int(m.group(1))
            commit = item.get("commit") or {}
            return {
                "sha": item.get("sha") or "",
                "short": (item.get("sha") or "")[:7],
                "message": (commit.get("message") or "").splitlines()[0][:80],
                "date": ((commit.get("committer") or {}).get("date") or "")[:10],
                "count": count,
            }
        except Exception as e:
            last_error = e
            continue
    raise UpdateError(f"查询最新版本失败（已尝试 {len(urls)} 个地址）: {last_error}")


def check_update(cfg=None, log_cb=None):
    """对比本地与远端，返回给前端展示用的完整结果。"""
    local = get_local_info(cfg)
    remote = get_remote_info(cfg, log_cb)
    remote_version = (f"{BASE_VERSION}.{remote['count']}" if remote.get("count")
                      else f"{BASE_VERSION}+{remote['short']}")
    has_update = True
    if local["sha"] and remote["sha"]:
        has_update = not remote["sha"].startswith(local["sha"])
    return {
        "has_update": has_update,
        "current": {"version": local["version"], "commit": local["commit"]},
        "remote": {"version": remote_version, "commit": remote["short"],
                   "message": remote["message"], "date": remote["date"]},
    }


# ============================================================
# 执行更新
# ============================================================

def _update_via_git(git_exe, cfg, log_cb):
    """git fetch + 快进合并。GitHub 被墙时依次换加速代理地址直接 fetch。"""
    sources = ["origin"]
    try:
        import mirror_manager as mm
        if mm.resolve_github_mode(cfg or {}, log_cb):
            sources = [mm.github_url(REPO_URL, True, i)
                       for i in range(len(mm.GITHUB_PROXIES))] + ["origin"]
    except Exception:
        pass

    last_error = None
    for i, src in enumerate(sources):
        try:
            if log_cb:
                log_cb(f"[更新] 正在拉取最新代码（{src if src == 'origin' else src.split('/')[2]}）...")
            _git(git_exe, "fetch", src, BRANCH, timeout=600)
            break
        except UpdateError as e:
            last_error = e
            if log_cb and i + 1 < len(sources):
                log_cb("[更新] 拉取失败，换下一个地址重试 ...")
    else:
        raise UpdateError(f"拉取最新代码失败: {last_error}")

    try:
        _git(git_exe, "merge", "--ff-only", "FETCH_HEAD")
    except UpdateError:
        # 本地改过我方代码导致无法快进。launcher_config.json 是 gitignore 的，
        # reset --hard 不会动它，丢的只是对启动器源码的手动修改。
        if log_cb:
            log_cb("[更新] 本地源码有手动修改，无法快进合并，将强制对齐到远端版本"
                   "（你的配置和下载的环境不受影响）")
        _git(git_exe, "reset", "--hard", "FETCH_HEAD")


def _overlay_copy(src_root, log_cb):
    """把解压出来的仓库内容覆盖到启动器目录，跳过 PRESERVE_TOP 和缓存目录。"""
    copied = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        parts = [] if rel == "." else rel.split(os.sep)
        # 目录级剪枝：保留名单、__pycache__、web/_preview 整个不碰
        dirnames[:] = [
            d for d in dirnames
            if d not in PRESERVE_TOP and d != "__pycache__"
            and not (parts == ["web"] and d == "_preview")
        ]
        if parts and parts[0] in PRESERVE_TOP:
            continue
        dest_dir = os.path.join(APP_DIR, rel) if rel != "." else APP_DIR
        os.makedirs(dest_dir, exist_ok=True)
        for name in filenames:
            if not parts and name in PRESERVE_TOP:
                continue
            shutil.copy2(os.path.join(dirpath, name), os.path.join(dest_dir, name))
            copied += 1
    if log_cb:
        log_cb(f"[更新] 已覆盖 {copied} 个文件（配置、便携环境、模型缓存均保留）")
    return copied


def _update_via_zip(cfg, log_cb, progress_cb, cancel_flag):
    """无 .git 的 ZIP 用户：下载 main 分支快照，覆盖式更新。"""
    import portable_env as pe
    remote = get_remote_info(cfg, log_cb)

    tmp_dir = tempfile.mkdtemp(prefix="forge_launcher_update_")
    try:
        zip_path = os.path.join(tmp_dir, "update.zip")
        if log_cb:
            log_cb("[更新] 正在下载最新代码包 ...")
        pe.download_file(ZIP_URL, zip_path, progress_cb=progress_cb,
                         cancel_flag=cancel_flag, cfg=cfg, log_cb=log_cb)

        extract_dir = os.path.join(tmp_dir, "extract")
        if log_cb:
            log_cb("[更新] 下载完成，正在覆盖旧文件 ...")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
        roots = [d for d in os.listdir(extract_dir)
                 if os.path.isdir(os.path.join(extract_dir, d))]
        if len(roots) != 1:
            raise UpdateError("代码包内部结构异常，已中止")
        _overlay_copy(os.path.join(extract_dir, roots[0]), log_cb)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return remote


_UPDATE_LOCK = threading.Lock()


def perform_update(cfg=None, log_cb=None, progress_cb=None, cancel_flag=None):
    """
    一键更新主入口。返回 {"version","commit","count"}（新版本信息）。
    成功后自动把新版本号写进项目里的 version.json。
    注意：覆盖的是源码文件，正在运行的启动器进程不受影响，重启后生效。
    同一时刻只允许一个更新事务（两个更新交错覆盖源码必然出坏文件）。
    """
    if not _UPDATE_LOCK.acquire(blocking=False):
        raise UpdateError("已有更新任务在进行中，请等它结束")
    try:
        return _perform_update_locked(cfg, log_cb, progress_cb, cancel_flag)
    finally:
        _UPDATE_LOCK.release()


def _perform_update_locked(cfg, log_cb, progress_cb, cancel_flag):
    local = get_local_info(cfg)
    git_exe = _find_git_exe(cfg)
    use_git = git_exe and os.path.isdir(os.path.join(APP_DIR, ".git"))

    if use_git:
        _update_via_git(git_exe, cfg, log_cb)
        new_info = get_local_info(cfg)  # 重新按 git 算，count 已是最新
    else:
        if log_cb:
            log_cb("[更新] 未检测到 git 仓库，使用整包覆盖更新（配置和环境会保留）...")
        remote = _update_via_zip(cfg, log_cb, progress_cb, cancel_flag)
        count = remote.get("count") or (local["count"] + 1)
        new_info = {
            "version": f"{BASE_VERSION}.{count}",
            "commit": remote["short"],
            "sha": remote["sha"],
            "count": count,
        }

    write_version_file(new_info["version"], new_info.get("sha") or new_info["commit"],
                       new_info["count"])
    if log_cb:
        log_cb(f"[更新] 完成！新版本 V{new_info['version']}（{new_info['commit']}），"
               f"重启启动器后生效。")
    return {"version": new_info["version"], "commit": new_info["commit"],
            "count": new_info["count"]}


# ============================================================
# 维护者工具：python updater.py --write-version
# 打包发布前执行一次，把当前 git 版本写进 version.json，
# 这样下载 ZIP 的用户拿到的初始版本号就是准确的。
# ============================================================

if __name__ == "__main__":
    if "--write-version" in sys.argv:
        info = get_local_info()
        if info["source"] == "unknown":
            print("当前目录没有 .git 也没有 version.json，无法确定版本")
            sys.exit(1)
        write_version_file(info["version"], info["sha"] or info["commit"], info["count"])
        print(f"已写入 version.json: V{info['version']} ({info['commit']})")
    else:
        info = get_local_info()
        print(f"当前版本: V{info['version']} ({info['commit']}) [来源: {info['source']}]")
        try:
            result = check_update()
            mark = "有更新" if result["has_update"] else "已是最新"
            print(f"最新版本: V{result['remote']['version']} ({result['remote']['commit']})"
                  f" {result['remote']['date']} —— {mark}")
        except Exception as e:
            print(f"检查更新失败: {e}")
