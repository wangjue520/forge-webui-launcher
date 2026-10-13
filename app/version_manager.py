# -*- coding: utf-8 -*-
"""
git 版本管理：WebUI / ComfyUI 本体和插件（Forge 扩展 / ComfyUI 节点）共用一套。

「热更新」的意思是：只在原目录里用 git 换源码，venv / python / 模型 / 输出 /
插件这些 git 不管的文件一律不动，所以更新、换版本、回滚都不用重装环境。

几个踩过的坑都在这里统一处理：
  - 远程地址：老版部署会把 remote.origin.url 设成加速代理地址
    （https://ghfast.top/https://github.com/...），代理一挂更新就失败。
    这里先把地址还原成 GitHub 原地址，联网时再用 url.<代理>.insteadOf
    临时改写，按「代理逐个 → 直连」的顺序重试。
  - 浅克隆：ComfyUI 和节点是 --depth 1 拉的，要列历史版本得先补一段历史；
    完整克隆的仓库绝对不能加 --depth（会把它变成浅克隆）。
  - 本地修改：WebUI 的 webui-user.bat 是仓库里的文件，启动器还会改它的
    COMMANDLINE_ARGS。checkout -f 会把它还原，所以切换前把改过的文件
    备份下来，切换后尝试把改动重新打上；打不上的照样留在备份目录里。
  - 整合包（解压出来的，没有 .git）：可以「接管」——git init 后检出官方
    源码，被覆盖的文件同样先备份。

本模块不直接起进程跑长命令：调用方传 run(program, args, cwd, env, timeout) -> 退出码
（负责流式日志 / 取消 / 超时），短查询用 capture。
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 切换版本后必须保住的用户配置文件（仓库里有，但用户/启动器会改）
WEBUI_KEEP_FILES = ("webui-user.bat", "webui-user.sh")

HISTORY_LIMIT = 15
_GH = "https://github.com/"


# ============================================================
# 地址 / 名字归一
# ============================================================

def normalize_url(url):
    """去掉加速代理前缀、尾部斜杠，统一成 https://github.com/owner/repo.git 这种形式"""
    url = (url or "").strip()
    if not url:
        return ""
    i = url.find(_GH)
    if i > 0:                       # https://代理/https://github.com/... → 原地址
        url = url[i:]
    m = re.match(r"^git@github\.com:(.+)$", url)
    if m:
        url = _GH + m.group(1)
    url = url.rstrip("/")
    if url.startswith(_GH) and not url.endswith(".git"):
        url += ".git"
    return url


def repo_slug(url):
    """owner/repo（小写、去 .git），用来判断两个地址是不是同一个仓库"""
    url = normalize_url(url)
    if not url:
        return ""
    path = re.sub(r"^[a-z]+://[^/]+/", "", url, flags=re.I)
    path = re.sub(r"\.git$", "", path).strip("/")
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else path.lower()


def repo_name(url):
    s = repo_slug(url)
    return s.split("/")[-1] if s else ""


def folder_key(name):
    """文件夹名比较用的键：忽略大小写、停用后缀、GitHub 下载 zip 自带的 -main/-master"""
    n = (name or "").strip().lower()
    if n.endswith(".disabled"):
        n = n[:-len(".disabled")]
    n = re.sub(r"-(main|master)$", "", n)
    return n


# ============================================================
# 不起进程的快速探测（扫插件目录用，几十个插件也是毫秒级）
# ============================================================

def git_dir_of(path):
    """仓库的 .git 目录；.git 是文件（子模块 / worktree）时按 gitdir: 解析"""
    g = os.path.join(path, ".git")
    if os.path.isdir(g):
        return g
    if os.path.isfile(g):
        try:
            with open(g, "r", encoding="utf-8", errors="replace") as f:
                m = re.match(r"gitdir:\s*(.+)", f.read().strip())
            if m:
                d = m.group(1).strip()
                d = d if os.path.isabs(d) else os.path.normpath(os.path.join(path, d))
                if os.path.isdir(d):
                    return d
        except OSError:
            pass
    return None


def read_remote_url(path):
    """直接读 .git/config 拿 origin 地址（没有 origin 就取第一个 remote）"""
    gd = git_dir_of(path)
    if not gd:
        return ""
    try:
        with open(os.path.join(gd, "config"), "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    urls, cur = {}, None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^\[remote\s+"([^"]+)"\]$', s)
        if m:
            cur = m.group(1)
            continue
        if s.startswith("["):
            cur = None
            continue
        if cur:
            m = re.match(r"^url\s*=\s*(.+)$", s)
            if m and cur not in urls:
                urls[cur] = m.group(1).strip()
    if "origin" in urls:
        return urls["origin"]
    return next(iter(urls.values()), "")


def read_head(path):
    """不起 git 进程读当前版本：(分支名或空串, 提交 sha 或空串)。分支为空 = 固定在某个版本"""
    gd = git_dir_of(path)
    if not gd:
        return "", ""
    try:
        with open(os.path.join(gd, "HEAD"), "r", encoding="utf-8", errors="replace") as f:
            head = f.read().strip()
    except OSError:
        return "", ""
    if not head.startswith("ref:"):
        return "", head[:40]
    ref = head[4:].strip()
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    # worktree / 子模块的 refs 可能在 commondir 里
    dirs = [gd]
    try:
        with open(os.path.join(gd, "commondir"), "r", encoding="utf-8") as f:
            dirs.append(os.path.normpath(os.path.join(gd, f.read().strip())))
    except OSError:
        pass
    for d in dirs:
        try:
            with open(os.path.join(d, *ref.split("/")), "r", encoding="utf-8") as f:
                return branch, f.read().strip()[:40]
        except OSError:
            pass
        try:
            with open(os.path.join(d, "packed-refs"), "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.strip().split(" ")
                    if len(parts) == 2 and parts[1] == ref:
                        return branch, parts[0][:40]
        except OSError:
            pass
    return branch, ""


def scan_repos(base_dir):
    """
    列出插件目录下每个子文件夹：[{folder, path, key, disabled, is_git, remote, slug}]
    ComfyUI-Manager 停用节点的方式有两种：改名 xxx.disabled、挪进 .disabled/，都算进来。
    """
    out = []
    if not base_dir or not os.path.isdir(base_dir):
        return out

    def add(parent, name, disabled):
        p = os.path.join(parent, name)
        if not os.path.isdir(p):
            return
        remote = read_remote_url(p)
        branch, sha = read_head(p)
        out.append({"folder": name, "path": p, "key": folder_key(name),
                    "disabled": disabled or name.lower().endswith(".disabled"),
                    "is_git": bool(git_dir_of(p)), "remote": normalize_url(remote),
                    "slug": repo_slug(remote), "branch": branch, "commit": sha,
                    "rel": os.path.relpath(p, base_dir)})

    try:
        names = sorted(os.listdir(base_dir), key=str.lower)
    except OSError:
        return out
    for name in names:
        if name.startswith(".") or name == "__pycache__":
            continue
        add(base_dir, name, False)
    dd = os.path.join(base_dir, ".disabled")
    if os.path.isdir(dd):
        try:
            for name in sorted(os.listdir(dd), key=str.lower):
                add(dd, name, True)
        except OSError:
            pass
    return out


# ============================================================
# git 调用
# ============================================================

def capture(git_exe, repo, args, env=None, timeout=30):
    """跑一条短 git 命令，返回 (退出码, stdout)。超时/起不来返回 (-1, "")"""
    try:
        p = subprocess.run([git_exe, "-C", repo] + list(args), capture_output=True, timeout=timeout,
                           env=env, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        # 只去尾部：status --porcelain 每行开头的空格是有意义的
        return p.returncode, (p.stdout or b"").decode("utf-8", errors="replace").rstrip()
    except (OSError, subprocess.SubprocessError):
        return -1, ""


def net_sources(gh_proxies):
    """联网命令的候选前缀：代理逐个，最后直连（空串）"""
    return [p.rstrip("/") + "/" + _GH for p in (gh_proxies or [])] + [""]


def _net_cfg(prefix, url):
    args = ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60",
            "-c", "credential.interactive=never"]
    if prefix and url.startswith(_GH):
        args = ["-c", f"url.{prefix}.insteadOf={_GH}"] + args
    return args


def _env(env):
    e = dict(env or os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"
    return e


def net_capture(git_exe, repo, args, url, sources, env=None, timeout=45):
    """联网的短查询（ls-remote）：按候选前缀依次试，返回第一个成功的 (0, stdout)"""
    rc, out = -1, ""
    for prefix in sources if url.startswith(_GH) else [""]:
        rc, out = capture(git_exe, repo, _net_cfg(prefix, url) + list(args), _env(env), timeout)
        if rc == 0:
            return rc, out
    return rc, out


def net_run(run, git_exe, repo, args, url, sources, log, env=None, timeout=1200, cancelled=lambda: False):
    """联网的长命令（fetch）：流式日志，按候选前缀依次试"""
    repo = os.path.abspath(repo)
    rc = 1
    cands = sources if url.startswith(_GH) else [""]
    for i, prefix in enumerate(cands):
        if cancelled():
            return 1
        if i:
            log("[版本] 这个地址没连上，换下一个重试 ...\n")
        if prefix:
            log(f"[版本] 经 {prefix.split('/https://')[0]} 加速连接 GitHub ...\n")
        rc = run(git_exe, ["-C", repo] + _net_cfg(prefix, url) + list(args), repo, _env(env), timeout)
        if rc == 0:
            return 0
    return rc


# ============================================================
# 本地状态
# ============================================================

def _fmt_date(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return iso or ""


def dirty_files(git_exe, repo, env=None):
    """被改过的已跟踪文件（未跟踪的文件 checkout 不会动，不用管）"""
    rc, out = capture(git_exe, repo, ["status", "--porcelain", "--untracked-files=no"], env, 60)
    if rc != 0:
        return []
    files = []
    for line in out.splitlines():
        if len(line) > 3:
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(path.strip('"'))
    return files


def local_info(git_exe, repo, env=None, with_dirty=True):
    """
    仓库当前状态。没有 .git → {"is_git": False}
    branch 为空表示「固定在某个版本」（detached HEAD）
    """
    info = {"is_git": False, "path": repo}
    if not repo or not os.path.isdir(repo):
        return info
    if not git_dir_of(repo):
        return info
    info["is_git"] = True
    if not git_exe:
        info["error"] = "没有可用的 git"
        return info
    rc, out = capture(git_exe, repo, ["log", "-1", "--format=%H%x1f%h%x1f%cI%x1f%s"], env)
    if rc != 0 or not out:
        info["error"] = "读取不到当前版本（仓库可能是空的或已损坏）"
        return info
    parts = (out.split("\x1f") + ["", "", "", ""])[:4]
    info.update(commit=parts[0], short=parts[1], date=_fmt_date(parts[2]), subject=parts[3])
    rc, br = capture(git_exe, repo, ["symbolic-ref", "-q", "--short", "HEAD"], env)
    info["branch"] = br if rc == 0 else ""
    rc, tag = capture(git_exe, repo, ["describe", "--tags", "--exact-match", "HEAD"], env)
    info["tag"] = tag if rc == 0 else ""
    if not info["tag"]:
        rc, near = capture(git_exe, repo, ["describe", "--tags", "--abbrev=0", "HEAD"], env)
        info["near_tag"] = near if rc == 0 else ""
    rc, sh = capture(git_exe, repo, ["rev-parse", "--is-shallow-repository"], env)
    info["shallow"] = sh == "true"
    info["remote"] = normalize_url(read_remote_url(repo))
    if info["branch"]:
        rc, up = capture(git_exe, repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], env)
        info["upstream"] = up if rc == 0 else ""
    if with_dirty:
        info["dirty"] = dirty_files(git_exe, repo, env)
    return info


def version_label(info):
    """给人看的版本名：tag > 分支@短哈希"""
    if not info or not info.get("is_git"):
        return "不是 git 仓库"
    if info.get("error"):
        return info["error"]
    if info.get("tag"):
        return info["tag"]
    s = info.get("short", "")
    return f"{info['branch']} @ {s}" if info.get("branch") else f"固定版本 {s}"


# ============================================================
# 远程版本
# ============================================================

def _ver_key(tag):
    nums = [int(x) for x in re.findall(r"\d+", tag)[:4]]
    return nums + [0] * (4 - len(nums))


def remote_refs(git_exe, repo, url, sources, env=None):
    """ls-remote：不下载任何东西，拿到全部 tag 和分支。返回 (ok, {tags, branches, head})"""
    rc, out = net_capture(git_exe, repo, ["ls-remote", "--symref", url, "HEAD", "refs/heads/*", "refs/tags/*"],
                          url, sources, env, 60)
    if rc != 0:
        return False, {"tags": [], "branches": [], "head": ""}
    tags, branches, head = {}, [], ""
    for line in out.splitlines():
        if line.startswith("ref: "):
            m = re.match(r"ref:\s+refs/heads/(\S+)\s+HEAD", line)
            if m:
                head = m.group(1)
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref.startswith("refs/tags/"):
            name = ref[len("refs/tags/"):]
            if name.endswith("^{}"):          # 附注标签：取它指向的提交
                tags[name[:-3]] = sha
            else:
                tags.setdefault(name, sha)
        elif ref.startswith("refs/heads/"):
            branches.append({"name": ref[len("refs/heads/"):], "commit": sha})
    tag_list = [{"name": n, "commit": c} for n, c in tags.items()]
    tag_list.sort(key=lambda t: (_ver_key(t["name"]), t["name"]), reverse=True)
    return True, {"tags": tag_list[:80], "branches": branches, "head": head}


def default_branch(git_exe, repo, url, sources, env=None):
    ok, refs = remote_refs(git_exe, repo, url, sources, env)
    return refs["head"] if ok else ""


def has_commit(git_exe, repo, sha, env=None):
    return capture(git_exe, repo, ["cat-file", "-e", sha + "^{commit}"], env)[0] == 0


def fetch_branch(run, git_exe, repo, url, branch, sources, log, env=None, depth=50, cancelled=lambda: False):
    """把远程分支拉到 refs/remotes/origin/<branch>。浅克隆补到 depth 段历史，完整克隆不加 --depth"""
    shallow = capture(git_exe, repo, ["rev-parse", "--is-shallow-repository"], env)[1] == "true"
    args = ["fetch", "--no-tags", "--prune"]
    if shallow:
        args.append(f"--depth={depth}")
    args += [url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]
    return net_run(run, git_exe, repo, args, url, sources, log, env, 1800, cancelled)


def recent_commits(git_exe, repo, ref, n=40, env=None):
    rc, out = capture(git_exe, repo, ["log", f"-{n}", "--format=%H%x1f%h%x1f%cI%x1f%s", ref], env)
    if rc != 0:
        return []
    res = []
    for line in out.splitlines():
        p = (line.split("\x1f") + ["", "", "", ""])[:4]
        res.append({"commit": p[0], "short": p[1], "date": _fmt_date(p[2]), "subject": p[3]})
    return res


def behind_count(git_exe, repo, ref, env=None):
    """HEAD 落后 ref 多少个提交；HEAD 不在已知历史里（浅克隆）返回 None"""
    rc, out = capture(git_exe, repo, ["rev-list", "--count", f"HEAD..{ref}"], env)
    if rc == 0 and out.isdigit():
        return int(out)
    return None


def check(run, git_exe, repo, url, sources, log, env=None, cancelled=lambda: False):
    """
    检查更新：拉当前分支最新历史 + 列出 tag/分支。
    返回 {ok, branch, behind, latest, commits, tags, branches, error}
    """
    repo = os.path.abspath(repo)
    info = local_info(git_exe, repo, env, with_dirty=False)
    if not info.get("is_git"):
        return {"ok": False, "error": "不是 git 仓库"}
    url = normalize_url(url or info.get("remote"))
    if not url:
        return {"ok": False, "error": "这个仓库没有远程地址，没法检查更新"}
    ok, refs = remote_refs(git_exe, repo, url, sources, env)
    if not ok:
        return {"ok": False, "error": "连不上远程仓库（GitHub 及所有加速地址都失败）"}
    branch = info.get("branch") or ""
    names = {b["name"] for b in refs["branches"]}
    if branch not in names:
        branch = refs["head"] or (refs["branches"][0]["name"] if refs["branches"] else "")
    if not branch:
        return {"ok": False, "error": "远程仓库里没有分支"}
    if fetch_branch(run, git_exe, repo, url, branch, sources, log, env, cancelled=cancelled) != 0:
        return {"ok": False, "error": "拉取远程版本信息失败"}
    ref = f"refs/remotes/origin/{branch}"
    commits = recent_commits(git_exe, repo, ref, 40, env)
    behind = behind_count(git_exe, repo, ref, env)
    head = info.get("commit", "")
    if behind is None:
        idx = next((i for i, c in enumerate(commits) if c["commit"] == head), None)
        behind = idx
    return {"ok": True, "branch": branch, "pinned": not info.get("branch"),
            "behind": behind, "latest": commits[0] if commits else None,
            "commits": commits, "tags": refs["tags"], "branches": refs["branches"],
            "current": head}


# ============================================================
# 切换版本
# ============================================================

def _backup_dir(repo, backup_root):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = backup_root or os.path.join(repo, ".launcher_backup")
    d = os.path.join(root, stamp)
    n = 2
    while os.path.exists(d):
        d = os.path.join(root, f"{stamp}-{n}")
        n += 1
    return d


def _backup_files(repo, files, dest):
    saved = []
    for rel in files:
        src = os.path.join(repo, rel)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest, "files", rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            saved.append(rel)
        except OSError:
            pass
    return saved


def switch(run, git_exe, repo, target, url, sources, log, env=None, keep_files=(), reapply=True,
           backup_root=None, cancelled=lambda: False):
    """
    切换到指定版本。target:
      {"kind": "latest"}                    当前分支（固定版本时为远程默认分支）的最新
      {"kind": "branch", "name": "neo"}     切到某个分支并跟踪它（以后可以一键更新）
      {"kind": "tag", "name": "v0.3.60"}    固定在某个 tag
      {"kind": "commit", "commit": "<sha>"} 固定在某个提交
    返回 {ok, from, to, backup, restored, conflicts, error}
    """
    repo = os.path.abspath(repo)
    res = {"ok": False, "from": "", "to": "", "backup": "", "restored": [], "conflicts": []}
    info = local_info(git_exe, repo, env, with_dirty=False)
    if not info.get("is_git"):
        res["error"] = "不是 git 仓库"
        return res
    res["from"] = info.get("commit", "")
    url = normalize_url(url or info.get("remote"))
    raw_remote = read_remote_url(repo).strip()
    if url and raw_remote != url and repo_slug(raw_remote) in ("", repo_slug(url)):
        # 远程地址是加速代理地址 / 不规范：改回原地址（联网时再临时走代理）
        capture(git_exe, repo, ["config", "remote.origin.url", url], env)
    kind = (target or {}).get("kind", "latest")
    shallow = info.get("shallow")

    # ---- 1. 把目标版本拉到本地 ----
    if kind in ("latest", "branch"):
        branch = target.get("name") if kind == "branch" else info.get("branch")
        if not branch:
            branch = default_branch(git_exe, repo, url, sources, env) if url else ""
        if not branch:
            res["error"] = "不知道要更新哪个分支（连不上远程仓库？）"
            return res
        if not url:
            res["error"] = "仓库没有远程地址"
            return res
        log(f"[版本] 拉取 {branch} 分支的最新代码 ...\n")
        if fetch_branch(run, git_exe, repo, url, branch, sources, log, env, cancelled=cancelled) != 0:
            res["error"] = "拉取失败（网络问题，或者这个分支已经不存在）"
            return res
        rev = f"refs/remotes/origin/{branch}"
        checkout = ["checkout", "-f", "-B", branch, rev]
    elif kind == "tag":
        name = target.get("name", "")
        log(f"[版本] 拉取版本 {name} ...\n")
        args = ["fetch", "--no-tags"] + (["--depth=1"] if shallow else []) + \
            [url, f"+refs/tags/{name}:refs/tags/{name}"]
        if net_run(run, git_exe, repo, args, url, sources, log, env, 1800, cancelled) != 0:
            res["error"] = f"拉取版本 {name} 失败"
            return res
        rev = f"refs/tags/{name}^{{commit}}"
        checkout = ["checkout", "-f", "--detach", rev]
    elif kind == "commit":
        sha = target.get("commit", "")
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
            res["error"] = "提交编号无效"
            return res
        if not has_commit(git_exe, repo, sha, env):
            log(f"[版本] 本地没有提交 {sha[:8]}，从远程拉取 ...\n")
            args = ["fetch", "--no-tags"] + (["--depth=1"] if shallow else []) + [url, sha]
            if not url or net_run(run, git_exe, repo, args, url, sources, log, env, 1800, cancelled) != 0 \
                    or not has_commit(git_exe, repo, sha, env):
                res["error"] = f"拉取提交 {sha[:8]} 失败"
                return res
        rev = sha
        checkout = ["checkout", "-f", "--detach", sha]
    else:
        res["error"] = "未知的切换方式"
        return res
    if cancelled():
        res["error"] = "已取消"
        return res

    rc, new_sha = capture(git_exe, repo, ["rev-parse", rev], env)
    # 固定版本时点「更新」：即使提交相同也要检出，回到跟踪分支的状态
    if rc == 0 and new_sha == res["from"] and (kind in ("tag", "commit") or (kind == "latest" and info.get("branch"))):
        res.update(ok=True, to=new_sha, unchanged=True)
        log("[版本] 已经是这个版本了，不需要切换\n")
        return res

    # ---- 2. 备份本地改过的文件 ----
    dirty = dirty_files(git_exe, repo, env)
    patch = None
    if dirty:
        bdir = _backup_dir(repo, backup_root)
        saved = _backup_files(repo, dirty, bdir)
        try:
            p = subprocess.run([git_exe, "-C", repo, "diff", "--binary", "HEAD"], capture_output=True,
                               timeout=120, env=env, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            if p.returncode == 0 and p.stdout:
                os.makedirs(bdir, exist_ok=True)
                patch = os.path.join(bdir, "changes.patch")
                with open(patch, "wb") as f:
                    f.write(p.stdout)
        except (OSError, subprocess.SubprocessError):
            patch = None
        res["backup"] = bdir
        log(f"[版本] 有 {len(dirty)} 个文件被改过，已备份到 {bdir}\n")
        for rel in saved[:20]:
            log(f"         {rel}\n")
        if len(saved) > 20:
            log(f"         …… 共 {len(saved)} 个\n")

    # ---- 3. 检出 ----
    log("[版本] 切换源码 ...\n")
    rc = run(git_exe, ["-C", repo] + checkout, repo, _env(env), 600)
    if rc != 0:
        res["error"] = f"检出失败（退出码 {rc}）。如果是文件被占用，请先关闭正在运行的程序再试"
        return res
    if kind in ("latest", "branch"):
        capture(git_exe, repo, ["branch", f"--set-upstream-to=origin/{branch}"], env)
    res["to"] = capture(git_exe, repo, ["rev-parse", "HEAD"], env)[1]

    # ---- 4. 把本地修改打回去 ----
    if dirty:
        applied = False
        if reapply and patch:
            rc, _ = capture(git_exe, repo, ["apply", "--whitespace=nowarn", patch], env, 120)
            applied = rc == 0
            if applied:
                res["restored"] = list(dirty)
                log("[版本] 本地修改已自动合并到新版本\n")
        if not applied:
            keep = {k.lower() for k in keep_files}
            for rel in dirty:
                src = os.path.join(res["backup"], "files", rel)
                if rel.replace("\\", "/").lower() in keep and os.path.isfile(src):
                    try:
                        shutil.copy2(src, os.path.join(repo, rel))
                        res["restored"].append(rel)
                    except OSError:
                        res["conflicts"].append(rel)
                else:
                    res["conflicts"].append(rel)
            if res["restored"]:
                log(f"[版本] 已恢复你的配置文件：{'、'.join(res['restored'])}\n")
            if res["conflicts"] and reapply:
                log(f"[版本] 有 {len(res['conflicts'])} 个改过的文件和新版本冲突，已换成新版本的内容；"
                    f"你原来的修改在备份目录里：{res['backup']}\n")
    res["ok"] = True
    return res


def adopt(run, git_exe, repo, url, branch, sources, log, env=None, keep_files=(), cancelled=lambda: False):
    """
    接管没有 .git 的目录（整合包解压的）：git init → 拉官方分支 → 检出。
    检出时会覆盖与官方不一致的源码文件，覆盖前全部备份；未跟踪的
    venv / 模型 / 插件 / 输出不受影响。
    """
    repo = os.path.abspath(repo)
    url = normalize_url(url)
    if git_dir_of(repo):
        return {"ok": False, "error": "这个目录已经是 git 仓库了"}
    for args in (["init"], ["config", "remote.origin.url", url],
                 ["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"]):
        if capture(git_exe, repo, args, env)[0] != 0:
            return {"ok": False, "error": "git 初始化失败"}
    capture(git_exe, repo, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], env)
    log(f"[版本] 拉取官方 {branch} 分支 ...\n")
    args = ["fetch", "--no-tags", "--depth=50", url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]
    if net_run(run, git_exe, repo, args, url, sources, log, env, 1800, cancelled) != 0:
        shutil.rmtree(os.path.join(repo, ".git"), ignore_errors=True)   # 半截的 .git 会让下次误判成 git 仓库
        return {"ok": False, "error": "拉取官方源码失败（网络问题）"}
    # 先只动索引不动文件，就能算出哪些文件跟官方不一样，备份完再真正检出
    capture(git_exe, repo, ["reset", "-q", f"refs/remotes/origin/{branch}"], env, 300)
    res = switch(run, git_exe, repo, {"kind": "branch", "name": branch}, url, sources, log, env,
                 keep_files=keep_files, reapply=False, cancelled=cancelled)
    return res


# ============================================================
# 版本历史（一键回滚用）
# ============================================================

def _hist_key(repo):
    return os.path.normcase(os.path.abspath(repo))


def load_history(path, repo):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get(_hist_key(repo)) or [])
    except (OSError, ValueError, AttributeError):
        return []


def record_history(path, repo, commit, label):
    """记下切换前的版本（最新的在前），同一个提交只留一条"""
    if not commit:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    key = _hist_key(repo)
    items = [h for h in (data.get(key) or []) if h.get("commit") != commit]
    items.insert(0, {"commit": commit, "label": label, "time": time.strftime("%Y-%m-%d %H:%M")})
    data[key] = items[:HISTORY_LIMIT]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass
