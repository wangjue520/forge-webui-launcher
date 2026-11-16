# -*- coding: utf-8 -*-
"""
整合包配方（.txt）：把一个装好的 WebUI / ComfyUI / LoRA 训练器记成一个文本文件，
别人拿它在启动器里一键部署出一样的环境（模型除外，底模可选带上下载方式）。

查不到来源的插件（没有 git 地址、提交没推到远程、本地改过代码）没法只靠文本还原，
制作时把它们的文件一起打进压缩包：<名字>.zip = 整合包.txt + plugins/<文件夹>/…；
部署时 .txt 和 .zip 都能用（只给 .txt 时这些插件列为「缺失」）。

这个文件只放格式和纯函数（解析 / 生成 / 比较 pip 版本 / 识别官方节点库安装的节点），
联网、跑 git / pip 的流程在 pack_manager.py。

格式（人能看懂、也能手改）：
    # 注释
    [整合包]
    key = value
    [插件]
    文件夹 | key=value | key=value | 标记
    [pip]
    name==version
    [模型]
    分类/文件名 | size=… | sha256=… | civitai=… | liblib=… | hf=…
"""
import json
import os
import re
import time
import zipfile

FORMAT_VERSION = 1
RECIPE_NAME = "整合包.txt"          # 压缩包里配方文件的名字
PLUGIN_DIR = "plugins"               # 压缩包里放插件文件的目录

# 程序类型（= 部署页下拉框的 key）
KIND_LABELS = {
    "neo2": "Forge Neo", "neo2h3": "Forge Neo H3 视频版", "classic": "Forge Classic",
    "comfyui": "ComfyUI", "lora_trainer": "WWY LoRA 训练器",
}

# 这些包跟着显卡 / CUDA 走，不按 PyPI 版本号对齐（单独处理 torch）
TORCH_FAMILY = ("torch", "torchvision", "torchaudio")
PIP_SKIP = {"pip"}   # 对齐时不动 pip 自己

# 底模的分类（模型库分类键）；训练器的模型用 trainer:<相对目录>
BASE_MODEL_KEYS = ("checkpoints", "diffusion_models", "text_encoders", "vae")
MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin", ".sft")

SECTION_HEAD = "整合包"
SECTION_MAIN = "本体"
SECTION_PLUGINS = "插件"
SECTION_ENGINES = "训练引擎"
SECTION_MODELS = "模型"
PIP_PREFIX = "pip"          # [pip] 主环境；[pip:envs/diffsynth] 训练器的可选引擎环境


class RecipeError(Exception):
    pass


# ---------------------------------------------------------------- 名字 / 版本号

def norm_pkg(name):
    """pip 包名规范化（PEP 503）：大小写、_ . - 都算一样"""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


def torch_tag(version):
    """'2.8.0+cu128' → 'cu128'；ROCm / CPU 版返回对应的 tag，没有本地版本号返回 ''"""
    m = re.search(r"\+([a-z]+[0-9.]*)", version or "")
    return m.group(1) if m else ""


def base_version(version):
    return (version or "").split("+", 1)[0]


def safe_folder(name):
    """插件文件夹名进压缩包 / 落盘前做一次检查：不能带路径分隔符、不能是 . / .."""
    n = (name or "").strip()
    if not n or n in (".", "..") or any(c in n for c in '/\\:*?"<>|') or n.startswith("."):
        return ""
    return n


# ---------------------------------------------------------------- pip

def parse_freeze(text):
    """
    `pip freeze --all` 的输出 → {规范名: 原样那一行}。
    跳过可编辑安装（-e，来自本地源码，比如训练引擎自己）和本地文件安装（@ file:），
    这两种在别的电脑上装不出来；git+ / https 直链安装原样保留。
    返回 (packages, skipped)；skipped 是跳过的原始行，写进配方的注释里方便排查。
    """
    pkgs, skipped = {}, []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-e ") or line.startswith("--editable"):
            skipped.append(line)
            continue
        if " @ " in line:
            name, url = line.split(" @ ", 1)
            if url.strip().lower().startswith("file:"):
                # 本地的 .whl（启动器从自己的缓存装 torch 就是这样）：文件名里有版本号，按 name==版本 记，别的电脑从源里装
                ver = _wheel_version(url)
                if ver:
                    pkgs[norm_pkg(name)] = f"{name.strip()}=={ver}"
                else:
                    skipped.append(line)
                continue
            pkgs[norm_pkg(name)] = f"{name.strip()} @ {url.strip()}"
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*(\S+)$", line)
        if m:
            pkgs[norm_pkg(m.group(1))] = f"{m.group(1)}=={m.group(2)}"
        else:
            skipped.append(line)
    return pkgs, skipped


def _wheel_version(url):
    """file:///…/torch-2.13.0%2Bcu130-cp313-cp313-win_amd64.whl#sha256=… → '2.13.0+cu130'；不是 .whl 返回 None"""
    from urllib.parse import unquote
    path = unquote(url.split("#", 1)[0].split("?", 1)[0])
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    m = re.match(r"^[A-Za-z0-9_.]+-([0-9][^-]*)-.+\.whl$", base)
    return m.group(1) if m else None


def spec_version(spec):
    """'numpy==1.26.4' → '1.26.4'；直链安装返回 None"""
    return spec.split("==", 1)[1].strip() if "==" in spec and " @ " not in spec else None


def pip_plan(recorded, installed):
    """
    按配方对齐 pip 包：要装的行（版本不同 / 没装），torch 系不在这里管。
    recorded / installed 都是 parse_freeze 的结果。本机多出来的包不卸载（可能是别的插件要用的）。
    """
    todo = []
    for key, spec in recorded.items():
        if key in PIP_SKIP or key in TORCH_FAMILY:
            continue
        have = installed.get(key)
        if have is None:
            todo.append(spec)
            continue
        want_v, have_v = spec_version(spec), spec_version(have)
        if want_v is not None:
            if have_v is None or base_version(have_v) != base_version(want_v) or \
                    (torch_tag(want_v) and have_v != want_v):
                todo.append(spec)
        elif have.split(" @ ", 1)[-1] != spec.split(" @ ", 1)[-1]:
            todo.append(spec)
    return sorted(todo, key=str.lower)


def torch_specs(recorded):
    """配方里的 torch 三件套 → {'torch': '2.8.0+cu128', …}（只取 == 写法）"""
    out = {}
    for name in TORCH_FAMILY:
        spec = recorded.get(name)
        v = spec_version(spec) if spec else None
        if v:
            out[name] = v
    return out


# ---------------------------------------------------------------- ComfyUI 官方节点库

def read_cnr(folder):
    """
    ComfyUI-Manager 从官方节点库（registry.comfy.org，Manager 里叫 CNR）装的节点：
    没有 .git，但有 .tracking 和 pyproject.toml（[project] name / version）。
    认出来返回 (节点 id, 版本)，否则 None。
    """
    if not os.path.isfile(os.path.join(folder, ".tracking")):
        return None
    try:
        with open(os.path.join(folder, "pyproject.toml"), "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"^\[project\](.*?)(?=^\[|\Z)", text, re.S | re.M)
    if not m:
        return None
    body = m.group(1)
    name = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', body, re.M)
    ver = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', body, re.M)
    if not name or not ver:
        return None
    return name.group(1).strip().lower(), ver.group(1).strip()


def cnr_install_api(node_id, version):
    return f"https://api.comfy.org/nodes/{node_id}/install?version={version}"


# ---------------------------------------------------------------- 配方 <-> 文本

def new_recipe(kind, name=""):
    return {"head": {"format": str(FORMAT_VERSION), "kind": kind, "name": name or KIND_LABELS.get(kind, kind),
                     "created": time.strftime("%Y-%m-%d %H:%M")},
            "main": {}, "plugins": [], "engines": [], "pip": {}, "pip_skipped": {}, "models": []}


_HEAD_KEYS = [("format", "格式"), ("kind", "程序"), ("name", "名称"), ("label", "程序名"), ("created", "制作时间"),
              ("launcher", "启动器版本"), ("gpu", "显卡"), ("python", "Python"), ("torch", "torch"),
              ("torch_variant", "训练器 PyTorch")]
_MAIN_KEYS = [("url", "仓库"), ("branch", "分支"), ("commit", "提交"), ("label", "版本"), ("zip", "压缩包"),
              ("note", "说明")]   # 说明 = 本地提交：这个提交没推到远程，部署时装分支最新版
_HEAD_BY_CN = {cn: k for k, cn in _HEAD_KEYS}
_MAIN_BY_CN = {cn: k for k, cn in _MAIN_KEYS}

# 插件一行里的字段：git / commit / branch / cnr / version / zip / overlay / file；标记：停用
_PLUGIN_FIELDS = ("git", "commit", "branch", "cnr", "version", "zip", "overlay", "file", "note")
_MODEL_FIELDS = ("size", "sha256", "civitai", "civitai_url", "liblib", "hf", "name")


def _fmt_fields(item, fields):
    parts = []
    for k in fields:
        v = item.get(k)
        if v not in (None, "", False):
            parts.append(f"{k}={str(v).replace('|', '/').strip()}")
    return parts


def dump(recipe):
    """配方 dict → 文本"""
    h = recipe.get("head") or {}
    kind = h.get("kind", "")
    lines = [
        "# WWY 启动器整合包配方",
        "# 用法：WWY 启动器 →「环境部署」→「整合包」→「部署整合包…」，选这个文件（或同名的 .zip）。",
        "# 会部署一样版本的程序、一样的插件 / 节点（钉在同一个提交）和一样版本的 pip 包；模型只按下面 [模型] 里列的下载。",
        "# 可以手改：# 开头是注释；插件一行一个，删掉某行就不装它。",
        "",
        f"[{SECTION_HEAD}]",
    ]
    for k, cn in _HEAD_KEYS:
        v = h.get(k)
        if k == "label" and not v:
            v = KIND_LABELS.get(kind, "")
        if v not in (None, ""):
            lines.append(f"{cn} = {v}")
    m = recipe.get("main") or {}
    if m:
        lines += ["", f"[{SECTION_MAIN}]"]
        for k, cn in _MAIN_KEYS:
            if m.get(k):
                lines.append(f"{cn} = {m[k]}")
    plugins = recipe.get("plugins") or []
    if plugins or kind != "lora_trainer":
        lines += ["", f"[{SECTION_PLUGINS}]",
                  "# 文件夹 | git=地址 | commit=提交 | branch=分支            git 仓库，钉在这个提交",
                  "# 文件夹 | cnr=节点 id | version=版本                     ComfyUI 官方节点库（Manager 装的）",
                  "# 文件夹 | zip=plugins/文件夹                             来源查不到，文件在同名压缩包里",
                  "# 文件夹 | git=… | commit=… | overlay=plugins/文件夹       本地改过的文件在压缩包里，装好后覆盖上去",
                  "# 行尾的「停用」= 原来是停用状态；note= 是说明"]
        for p in plugins:
            folder = p.get("folder", "")
            parts = [folder] + _fmt_fields(p, _PLUGIN_FIELDS)
            if p.get("disabled"):
                parts.append("停用")
            lines.append(" | ".join(parts))
    engines = recipe.get("engines") or []
    if engines:
        lines += ["", f"[{SECTION_ENGINES}]", "# 名字 | git=地址 | commit=提交 | extra=1（可选引擎，独立环境 envs/名字）"]
        for e in engines:
            parts = [e.get("name", "")] + _fmt_fields(e, ("git", "commit", "branch", "extra"))
            lines.append(" | ".join(parts))
    for env_name, pkgs in (recipe.get("pip") or {}).items():
        sect = PIP_PREFIX if not env_name else f"{PIP_PREFIX}:{env_name}"
        lines += ["", f"[{sect}]"]
        for s in (recipe.get("pip_skipped") or {}).get(env_name, []):
            lines.append(f"# 跳过（本地安装，别的电脑装不出来）：{s}")
        lines += sorted(pkgs.values(), key=str.lower)
    models = recipe.get("models") or []
    if models:
        lines += ["", f"[{SECTION_MODELS}]",
                  "# 分类/文件名 | size=字节 | sha256=… | civitai=版本 id | liblib=模型/版本 | hf=仓库:路径",
                  "# 部署时选从哪儿下（C 站 / 常用模型目录 / LiblibAI），哪个都没有的只能自己放进对应文件夹"]
        for md in models:
            parts = [f"{md.get('key', '')}/{md.get('name', '')}"] + _fmt_fields(md, _MODEL_FIELDS[:-1])
            lines.append(" | ".join(parts))
    return "\n".join(lines) + "\n"


def _split_row(line):
    cells = [c.strip() for c in line.split("|")]
    first, rest = cells[0], cells[1:]
    fields, flags = {}, []
    for c in rest:
        if not c:
            continue
        if "=" in c:
            k, v = c.split("=", 1)
            fields[k.strip()] = v.strip()
        else:
            flags.append(c)
    return first, fields, flags


def parse(text):
    """文本 → 配方 dict。认不出的行跳过（手改出错不至于整个打不开），缺关键字段抛 RecipeError"""
    r = {"head": {}, "main": {}, "plugins": [], "engines": [], "pip": {}, "pip_skipped": {}, "models": []}
    sect = None
    for raw in (text or "").lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            sect = m.group(1).strip()
            if sect == PIP_PREFIX or sect.startswith(PIP_PREFIX + ":"):
                r["pip"].setdefault(sect[len(PIP_PREFIX) + 1:].strip() if ":" in sect else "", {})
            continue
        if sect in (SECTION_HEAD, SECTION_MAIN):
            if "=" not in line:
                continue
            k, v = (s.strip() for s in line.split("=", 1))
            if sect == SECTION_HEAD:
                r["head"][_HEAD_BY_CN.get(k, k)] = v
            else:
                r["main"][_MAIN_BY_CN.get(k, k)] = v
        elif sect == SECTION_PLUGINS:
            folder, fields, flags = _split_row(line)
            folder = safe_folder(folder)
            if not folder:
                continue
            p = {"folder": folder, **{k: v for k, v in fields.items() if k in _PLUGIN_FIELDS}}
            if "停用" in flags or "disabled" in flags:
                p["disabled"] = True
            r["plugins"].append(p)
        elif sect == SECTION_ENGINES:
            name, fields, _ = _split_row(line)
            if safe_folder(name):
                r["engines"].append({"name": name, **{k: v for k, v in fields.items()
                                                      if k in ("git", "commit", "branch", "extra")}})
        elif sect == SECTION_MODELS:
            first, fields, _ = _split_row(line)
            key, _, name = first.replace("\\", "/").partition("/")
            name = name.strip().strip("/")
            if not key or not name or any(safe_folder(s) != s for s in name.split("/")):
                continue
            md = {"key": key.strip(), "name": name}
            for k in _MODEL_FIELDS:
                if k in fields:
                    md[k] = fields[k]
            try:
                md["size"] = int(md.get("size") or 0)
            except ValueError:
                md["size"] = 0
            r["models"].append(md)
        elif sect is not None and (sect == PIP_PREFIX or sect.startswith(PIP_PREFIX + ":")):
            env_name = sect[len(PIP_PREFIX) + 1:].strip() if ":" in sect else ""
            pkgs, _ = parse_freeze(line)
            r["pip"].setdefault(env_name, {}).update(pkgs)
    kind = r["head"].get("kind", "")
    if kind not in KIND_LABELS:
        raise RecipeError("这不是启动器的整合包配方（缺少「程序 = …」，或者程序类型不认识："
                          f"{kind or '空'}）")
    try:
        if int(r["head"].get("format") or 1) > FORMAT_VERSION:
            raise RecipeError("这个整合包是新版启动器做的，请先把启动器更新到最新版再部署")
    except ValueError:
        pass
    return r


# ---------------------------------------------------------------- 压缩包

def read_source(path):
    """
    打开 .txt 或 .zip → (配方 dict, 压缩包路径或 None)。
    .zip 里找 整合包.txt（找不到就用压缩包里唯一的 .txt）。
    """
    if not path or not os.path.isfile(path):
        raise RecipeError("文件不存在")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            cand = [n for n in names if n.replace("\\", "/").rsplit("/", 1)[-1] == RECIPE_NAME]
            if not cand:
                cand = [n for n in names if n.lower().endswith(".txt") and "/" not in n.strip("/")]
            if not cand:
                raise RecipeError(f"压缩包里没有找到 {RECIPE_NAME}")
            text = z.read(cand[0]).decode("utf-8-sig", errors="replace")
        return parse(text), path
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return parse(raw.decode(enc)), None
        except UnicodeDecodeError:
            continue
    raise RecipeError("文件编码认不出来（应该是 UTF-8 文本）")


def zip_members(zpath, prefix):
    """压缩包里 prefix/ 下面的条目（不含目录本身）"""
    pre = prefix.strip("/") + "/"
    with zipfile.ZipFile(zpath) as z:
        return [n for n in z.namelist() if n.replace("\\", "/").startswith(pre)]


def extract_prefix(zpath, prefix, dest):
    """
    把压缩包里 prefix/ 下的内容解到 dest（prefix 是单个文件时解成 dest 这个文件）。
    防越界：条目解出来必须还在 dest 里。返回解出的文件数。
    """
    prefix = prefix.replace("\\", "/").strip("/")
    n = 0
    with zipfile.ZipFile(zpath) as z:
        names = [i for i in z.infolist() if i.filename.replace("\\", "/").rstrip("/") == prefix
                 or i.filename.replace("\\", "/").startswith(prefix + "/")]
        if len(names) == 1 and names[0].filename.replace("\\", "/") == prefix:   # 单文件插件（xxx.py）
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with z.open(names[0]) as src, open(dest, "wb") as out:
                out.write(src.read())
            return 1
        base = os.path.abspath(dest)
        for info in names:
            rel = info.filename.replace("\\", "/")[len(prefix):].lstrip("/")
            if not rel:
                continue
            target = os.path.abspath(os.path.join(base, *rel.split("/")))
            if not (target == base or target.startswith(base + os.sep)):
                continue
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
            n += 1
    return n


_ZIP_SKIP_DIRS = {".git", "__pycache__", ".cache", "node_modules", ".venv", "venv"}


def add_tree(z, src, arc, files=None, max_file=None):
    """
    把 src（文件夹或单个文件）写进压缩包的 arc 下面。files 给了就只放这些相对路径（本地改动覆盖包）。
    跳过 .git / __pycache__ 这类；max_file 超过这个大小的单个文件不放（通常是模型，记在返回值里）。
    返回 (放进去的文件数, 总字节, 跳过的大文件 [(相对路径, 大小)])
    """
    count, total, big = 0, 0, []
    if os.path.isfile(src):
        z.write(src, arc)
        return 1, os.path.getsize(src), []
    if files is not None:
        walk = [(os.path.join(src, f), f.replace("\\", "/")) for f in files]
    else:
        walk = []
        for dirpath, dirnames, filenames in os.walk(src, followlinks=True):
            dirnames[:] = [d for d in dirnames if d not in _ZIP_SKIP_DIRS]
            for fn in filenames:
                if fn.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(dirpath, fn)
                walk.append((full, os.path.relpath(full, src).replace("\\", "/")))
    for full, rel in walk:
        if not os.path.isfile(full):
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if max_file and size > max_file:
            big.append((rel, size))
            continue
        z.write(full, f"{arc}/{rel}")
        count += 1
        total += size
    return count, total, big


def summary(recipe, zpath=None):
    """给部署前的确认框：各类插件数、pip 包数、模型列表、压缩包里缺了哪些文件"""
    have = set()
    if zpath:
        try:
            with zipfile.ZipFile(zpath) as z:
                for n in z.namelist():
                    parts = n.replace("\\", "/").split("/")
                    if len(parts) >= 2 and parts[0] == PLUGIN_DIR:
                        have.add(parts[1])
        except (OSError, zipfile.BadZipFile):
            pass
    counts = {"git": 0, "cnr": 0, "zip": 0, "missing": 0}
    missing = []
    for p in recipe.get("plugins") or []:
        if p.get("zip"):
            if os.path.basename(p["zip"].rstrip("/")) in have:
                counts["zip"] += 1
            else:
                counts["missing"] += 1
                missing.append(p["folder"])
        elif p.get("cnr"):
            counts["cnr"] += 1
        elif p.get("git"):
            counts["git"] += 1
            if p.get("overlay") and os.path.basename(p["overlay"].rstrip("/")) not in have:
                missing.append(p["folder"] + "（本地改动）")
        else:
            counts["missing"] += 1
            missing.append(p["folder"])
    h = recipe.get("head") or {}
    return {"kind": h.get("kind"), "label": h.get("label") or KIND_LABELS.get(h.get("kind"), ""),
            "name": h.get("name", ""), "created": h.get("created", ""), "gpu": h.get("gpu", ""),
            "python": h.get("python", ""), "torch": h.get("torch", ""),
            "main": recipe.get("main") or {}, "plugins": counts, "missing": missing,
            "engines": [e.get("name") for e in recipe.get("engines") or []],
            "pip": {k or "主环境": len(v) for k, v in (recipe.get("pip") or {}).items()},
            "models": recipe.get("models") or [], "zip": bool(zpath)}


def to_json(recipe):
    return json.dumps(recipe, ensure_ascii=False)
