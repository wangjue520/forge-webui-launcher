"""
plugin_registry.py — 插件管理页的「联网找插件 / 注册表版节点」那部分（只用标准库，联网函数由调用方注入）

ComfyUI：
  ComfyUI-Manager 现在默认从 Comfy 注册表（api.comfy.org）装节点：下的是 zip，解压进 custom_nodes/<节点 id>，
  没有 .git，旁边一个 .tracking 记着解压出来的文件清单，版本号在 pyproject.toml 里。
  以前启动器只认 git 装的插件，这类节点显示「没法管理版本」。这里补上：认出注册表版、查新版本、
  按版本号换版本（和 Manager 一样整包替换，用户自己放进去的文件保留）、从注册表搜索安装新节点。
WebUI（Forge / A1111）：
  扩展没有注册表，用 AUTOMATIC1111 官方维护的扩展索引（WebUI「扩展 → 可下载」读的就是它）搜索，装的时候 git clone。
"""
import io
import json
import os
import re
import shutil
import time
import zipfile

REGISTRY = "https://api.comfy.org"
FORGE_INDEX_URL = ("https://raw.githubusercontent.com/AUTOMATIC1111/"
                   "stable-diffusion-webui-extensions/master/index.json")
TRACKING = ".tracking"


# ============================================================
# 认出注册表版节点
# ============================================================

def _read_pyproject(path):
    p = os.path.join(path, "pyproject.toml")
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        import tomllib
        return tomllib.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        pass
    # 解析不了（老 Python / 文件写得不规范）：只抠 [project] 下的 name / version
    text = raw.decode("utf-8", errors="replace")
    m = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not m:
        return None
    sec = m.group(1)
    out = {}
    for key in ("name", "version"):
        k = re.search(r'^\s*%s\s*=\s*["\']([^"\']+)["\']' % key, sec, re.M)
        if k:
            out[key] = k.group(1)
    return {"project": out, "tool": {"comfy": {}} if "[tool.comfy]" in text else {}}


def read_cnr(path, is_git=False):
    """
    注册表版节点 → {"id", "version"}；不是就返回 None。
    判断：不是 git 仓库，且有 .tracking（Manager 装的）或 pyproject.toml 里有 [tool.comfy]（注册表发布配置）。
    """
    if is_git or not os.path.isdir(path):
        return None
    data = _read_pyproject(path)
    if not data:
        return None
    proj = data.get("project") or {}
    tracked = os.path.isfile(os.path.join(path, TRACKING))
    if not tracked and "comfy" not in (data.get("tool") or {}):
        return None
    nid = str(proj.get("name") or "").strip().lower()
    if not nid:
        return None
    return {"id": nid, "version": str(proj.get("version") or "").strip(), "tracked": tracked}


def version_key(v):
    """「1.10.2」比「1.9.0」新：按数字段比较；带字母的段放后面比字符串"""
    parts = []
    for seg in re.split(r"[.\-+]", str(v or "")):
        parts.append((0, int(seg), "") if seg.isdigit() else (1, 0, seg))
    return parts


def is_newer(latest, current):
    if not latest:
        return False
    if not current:
        return True
    return version_key(latest) > version_key(current)


# ============================================================
# 注册表接口（get：url → 解析好的 JSON，失败抛异常）
# ============================================================

def _q(s):
    from urllib.parse import quote
    return quote(str(s), safe="")


def registry_node(get, node_id):
    return get(f"{REGISTRY}/nodes/{_q(node_id)}")


def registry_versions(get, node_id):
    """[{version, date, url, deprecated, changelog}]，新的在前"""
    data = get(f"{REGISTRY}/nodes/{_q(node_id)}/versions")
    out = []
    for v in data or []:
        if not isinstance(v, dict) or not v.get("version"):
            continue
        out.append({"version": str(v["version"]), "date": str(v.get("createdAt") or "")[:10],
                    "url": v.get("downloadUrl") or "", "deprecated": bool(v.get("deprecated")),
                    "changelog": str(v.get("changelog") or "")[:300]})
    out.sort(key=lambda x: version_key(x["version"]), reverse=True)
    return out


def registry_search(get, query, limit=30):
    data = get(f"{REGISTRY}/nodes/search?search={_q(query)}&limit={int(limit)}")
    out = []
    for n in (data or {}).get("nodes") or []:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        lv = n.get("latest_version") or {}
        out.append({"id": n["id"], "name": n.get("name") or n["id"],
                    "desc": str(n.get("description") or "")[:400],
                    "url": n.get("repository") or "", "downloads": n.get("downloads") or 0,
                    "stars": n.get("github_stars") or 0, "version": lv.get("version") or "",
                    "author": ((n.get("publisher") or {}).get("name") or "")})
    # 注册表自己的排序很怪（搜 gguf 第一个不是 ComfyUI-GGUF）：名字里带关键词的排前面，再按下载量
    q = str(query or "").strip().lower()
    out.sort(key=lambda n: (0 if q and (q in n["id"].lower() or q in n["name"].lower()) else 1, -int(n["downloads"] or 0)))
    return out


def registry_download_url(get, node_id, version=None):
    """某个版本（缺省最新）的 zip 地址 → (url, version)"""
    vs = registry_versions(get, node_id)
    if not vs:
        raise RuntimeError("注册表里没有这个节点的可下载版本")
    if version:
        hit = next((v for v in vs if v["version"] == version), None)
        if not hit:
            raise RuntimeError(f"注册表里没有 {version} 这个版本")
    else:
        hit = next((v for v in vs if not v["deprecated"]), vs[0])
    if not hit["url"]:
        raise RuntimeError(f"{hit['version']} 没有下载地址")
    return hit["url"], hit["version"]


# ============================================================
# 解压安装 / 换版本（和 Manager 的做法一样：整包替换 + .tracking）
# ============================================================

def _safe_members(zf):
    """zip 里的文件，过滤掉绝对路径和 ..（防止解压到插件目录外面）"""
    out = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or not name or name.startswith("/") or ".." in name.split("/") or ":" in name:
            continue
        out.append((info, name))
    return out


def _tracked(path):
    try:
        with open(os.path.join(path, TRACKING), "r", encoding="utf-8") as f:
            return {ln.strip().replace("\\", "/") for ln in f if ln.strip()}
    except OSError:
        return None


def install_zip(zip_bytes, dest, backup_dir=None, log=lambda m: None):
    """
    把注册表的 zip 装到 dest（custom_nodes/<id>）。dest 已经有旧版本时：
    旧文件夹整个挪到 backup_dir（想回去可以挪回来），解压新版，再把旧版里「不是 zip 带来的」文件
    （.tracking 里没有的：用户自己放的配置、模型、缓存）拷回来。返回解压的文件数。
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    members = _safe_members(zf)
    if not members:
        raise RuntimeError("下载的压缩包是空的")
    parent = os.path.dirname(dest.rstrip("\\/"))
    stage = os.path.join(parent, f".wwy-stage-{os.path.basename(dest)}-{int(time.time())}")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    try:
        for info, name in members:
            target = os.path.join(stage, *name.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
        with open(os.path.join(stage, TRACKING), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(n for _i, n in members) + "\n")

        if os.path.isdir(dest):
            old_tracked = _tracked(dest)
            new_names = {n for _i, n in members}
            kept = 0
            for root, dirs, files in os.walk(dest):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, dest).replace("\\", "/")
                    if rel == TRACKING or rel in new_names:
                        continue
                    if old_tracked is not None and rel in old_tracked:
                        continue      # 旧版本自带、新版本删掉了的文件：不留
                    if old_tracked is None:
                        continue      # 没有清单（不是 Manager 装的）：分不清哪些是用户的，只换不留
                    tgt = os.path.join(stage, *rel.split("/"))
                    os.makedirs(os.path.dirname(tgt), exist_ok=True)
                    shutil.copy2(full, tgt)
                    kept += 1
            if kept:
                log(f"保留了 {kept} 个你自己放进去的文件（配置 / 模型等）\n")
            if backup_dir:
                os.makedirs(backup_dir, exist_ok=True)
                shutil.move(dest, os.path.join(backup_dir, os.path.basename(dest)))
                log(f"旧版本已移到 {backup_dir}\n")
            else:
                shutil.rmtree(dest)
        os.rename(stage, dest)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return len(members)


# ============================================================
# WebUI 扩展索引
# ============================================================

def parse_forge_index(data):
    out = []
    for e in (data or {}).get("extensions") or []:
        if not isinstance(e, dict) or not e.get("url"):
            continue
        out.append({"name": e.get("name") or e["url"].rstrip("/").rsplit("/", 1)[-1],
                    "url": e["url"], "desc": str(e.get("description") or "")[:400],
                    "tags": [str(t) for t in (e.get("tags") or [])],
                    "added": str(e.get("added") or "")})
    return out


def load_forge_index(cache_path, fetch, max_age=86400):
    """读缓存的扩展索引，过期了联网刷新（刷新失败就用旧的）"""
    cached = None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        pass
    if cached and time.time() - float(cached.get("at") or 0) < max_age:
        return cached.get("items") or []
    try:
        items = parse_forge_index(fetch(FORGE_INDEX_URL))
    except Exception:
        items = []
    if items:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"at": time.time(), "items": items}, f, ensure_ascii=False)
        except OSError:
            pass
        return items
    if cached:
        return cached.get("items") or []
    raise RuntimeError("扩展索引下载失败（GitHub 直连和加速地址都没连上）")


def search_items(items, query, limit=40):
    """名字命中排前面，其次是说明 / 标签 / 地址"""
    q = (query or "").strip().lower()
    if not q:
        return items[:limit]
    words = q.split()
    scored = []
    for it in items:
        name = it["name"].lower()
        hay = " ".join([name, it.get("desc", "").lower(), " ".join(it.get("tags") or []).lower(),
                        it.get("url", "").lower()])
        if not all(w in hay for w in words):
            continue
        score = (0 if name == q else 1 if name.startswith(q) else 2 if q in name else 3)
        scored.append((score, it["name"].lower(), it))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [it for _s, _n, it in scored[:limit]]


GIT_URL_RE = re.compile(r"^(https?://[^\s]+|git@[^\s]+:[^\s]+)$", re.I)


def normalize_git_url(text):
    """用户粘贴的地址 → 能 clone 的地址。GitHub 网页地址（/tree/main 之类）截到仓库根"""
    t = (text or "").strip().strip('"').strip()
    if not GIT_URL_RE.match(t):
        return None
    m = re.match(r"^(https?://(?:www\.)?github\.com/[^/\s]+/[^/\s#?]+)", t, re.I)
    if m:
        t = m.group(1)
        if t.lower().endswith(".git"):
            t = t[:-4]
    return t.rstrip("/")


def repo_folder(url):
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.lower().endswith(".git"):
        name = name[:-4]
    name = re.sub(r'[<>:"|?*\\/]', "_", name).strip(". ")
    return name or "plugin"
