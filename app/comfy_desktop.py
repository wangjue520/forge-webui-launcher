# -*- coding: utf-8 -*-
"""
ComfyUI 桌面版（新版 Comfy Desktop，能装多个实例）的模型目录。

新版桌面版每个实例装在 <数据根>\\ComfyUI-Installs\\<名字>\\（ComfyUI\\main.py + standalone-env\\python.exe），
模型默认放共享库 <数据根>\\ComfyUI-Shared\\models（所有实例共用），实例自己的 ComfyUI\\models 只是兜底。
数据根默认是 %LOCALAPPDATA%\\Comfy-Desktop（老版本在用户目录，装在别的盘时是 <盘>\\Comfy-Desktop）。

桌面版的设置在 %APPDATA%\\Comfy Desktop\\：
  settings.json       modelsDirs = 共享模型目录列表（「设置 → 存储」里加的）
  installations.json  每个实例一条：installPath / useSharedModels / modelDirs / modelDirsPrimary
桌面版启动实例时自己生成 extra_model_paths 传给 ComfyUI。启动器把实例根目录当模型目录的话，
下载 / 整理的模型都落进 installs 里：桌面版的下载和别的实例都看不到，桌面版启动时也会把共享库排在前面。
所以对桌面版实例：下载、模型管理用它的默认模型目录，用启动器启动时照桌面版的样子把共享目录挂上去。
默认目录的规则照抄桌面版源码 src/main/lib/models.ts 的 resolveLauncherModelDirs。
"""
import json
import os

# 桌面版 extra_model_paths 里挂的模型类型（src/main/lib/models.ts 的 MODEL_FOLDER_TYPES）
MODEL_FOLDER_TYPES = (
    "checkpoints", "classifiers", "clip_vision", "configs", "controlnet", "diffusers",
    "diffusion_models", "embeddings", "gligen", "hypernetworks", "latent_upscale_models",
    "loras", "model_patches", "audio_encoders", "photomaker", "style_models", "text_encoders",
    "upscale_models", "background_removal", "frame_interpolation", "geometry_estimation",
    "optical_flow", "detection", "vae", "vae_approx",
)


def _appdata_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, "Comfy Desktop")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _records():
    data = _read_json(os.path.join(_appdata_dir(), "installations.json"))
    if isinstance(data, dict):
        data = data.get("installations") or data.get("records") or []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _looks_like_install(d):
    return os.path.isdir(os.path.join(d, "standalone-env")) and \
        os.path.isfile(os.path.join(d, "ComfyUI", "main.py"))


def find_install(root):
    """
    root（实例目录，或者里面的 ComfyUI 目录）是桌面版实例时返回 (实例目录, 桌面版里的记录 dict)，
    否则 (None, None)。记录可能是空 dict（目录结构像，但桌面版的 installations.json 里没登记）。
    """
    if not root:
        return None, None
    root = os.path.abspath(root)
    cands = [root, os.path.dirname(root)]
    want = {_norm(c) for c in cands}
    for r in _records():
        ip = r.get("installPath")
        if isinstance(ip, str) and ip.strip() and _norm(ip) in want:
            return os.path.abspath(ip), r
    for c in cands:
        if _looks_like_install(c):
            return c, {}
    return None, None


def _default_shared(install_path):
    """settings.json 读不到时，按桌面版的默认位置找共享模型目录（只要存在的）"""
    out = []
    parent = os.path.dirname(install_path)
    if os.path.basename(parent).lower() == "comfyui-installs":
        out.append(os.path.join(os.path.dirname(parent), "ComfyUI-Shared", "models"))
    la = os.environ.get("LOCALAPPDATA")
    if la:
        out.append(os.path.join(la, "Comfy-Desktop", "ComfyUI-Shared", "models"))
    out.append(os.path.join(os.path.expanduser("~"), "ComfyUI-Shared", "models"))
    return [d for d in out if os.path.isdir(d)][:1]


def model_dirs(root):
    """
    桌面版实例 → (默认放模型的目录, 要挂给 ComfyUI 的模型目录列表)；不是桌面版 → (None, [])。
    默认目录是 None 表示这个实例关了共享模型、模型就放它自己的 ComfyUI\\models（启动器原来的做法）。
    """
    ip, rec = find_install(root)
    if not ip:
        return None, []
    rec = rec or {}
    settings = _read_json(os.path.join(_appdata_dir(), "settings.json")) or {}
    shared = settings.get("modelsDirs") if isinstance(settings, dict) else None
    if isinstance(shared, list) and any(isinstance(d, str) and d.strip() for d in shared):
        shared = [d for d in shared if isinstance(d, str) and d.strip()]
    else:
        shared = _default_shared(ip)
    own = _norm(os.path.join(ip, "ComfyUI", "models"))
    use_shared = rec.get("useSharedModels") is not False
    shared = [os.path.abspath(d) for d in shared if _norm(d) != own] if use_shared else []
    inst = [os.path.abspath(d) for d in (rec.get("modelDirs") or [])
            if isinstance(d, str) and d.strip() and _norm(d) != own]
    dirs = []
    for d in shared + inst:
        if not any(_norm(d) == _norm(x) for x in dirs):
            dirs.append(d)
    prim = rec.get("modelDirsPrimary")
    if isinstance(prim, str) and prim.strip() and _norm(prim) == own:
        primary = None
    elif isinstance(prim, str) and prim.strip() and any(_norm(prim) == _norm(d) for d in dirs):
        primary = os.path.abspath(prim)
    else:
        primary = shared[0] if shared else None
    return primary, dirs


def category_dir(models_dir, comfy_rel):
    """ComfyUI 的 models/xxx 相对路径 → 桌面版模型目录里的对应文件夹"""
    rel = comfy_rel.replace("\\", "/")
    if rel.lower().startswith("models/"):
        rel = rel[len("models/"):]
    return os.path.join(models_dir, *rel.split("/"))


# 桌面版另外会挂的东西（src/main/lib/models.ts）——启动器以前只挂上面那 25 个分类，
# 共享目录里的 unet / clip / t2i_adapter 和自定义节点自己的模型文件夹（ipadapter、ultralytics…）
# 用桌面版启动看得到、用启动器启动就看不到：
LEGACY_ALIASES = ("clip", "unet")                      # 老目录名，ComfyUI 会并进 text_encoders / diffusion_models
SECONDARY_DIRS = {"controlnet": ("t2i_adapter",)}      # 同一个类型多读一个文件夹
KNOWN_MODEL_FOLDERS = set(MODEL_FOLDER_TYPES) | {"clip", "unet", "t2i_adapter"}
NON_MODEL_FOLDERS = {"custom_nodes", "user", "input", "output", "temp", "models", ".venv", ".snapshots",
                     ".git", "__pycache__", ".desktop2-downloads"}


def extra_folders(dirs):
    """模型目录里上面清单之外的文件夹名（自定义节点的模型类型），排好序；照桌面版 discoverExtraFoldersFromSharedDirs"""
    seen = set()
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name not in KNOWN_MODEL_FOLDERS and name not in NON_MODEL_FOLDERS \
                    and os.path.isdir(os.path.join(d, name)):
                seen.add(name)
    return sorted(seen)


def own_models_dir(root):
    """桌面版实例自己的 ComfyUI\\models（不是桌面版返回 ""）"""
    ip, _rec = find_install(root)
    return os.path.join(ip, "ComfyUI", "models") if ip else ""


def search_roots(root):
    """
    桌面版实例实际会读的全部模型目录 [(目录, 来源)]，来源是 "own"（实例自带的 ComfyUI\\models）或
    "shared"（挂上去的共享 / 额外目录）。照桌面版 resolveInstallModelSearchPaths：自带的 models 永远在读，
    共享目录是另外挂的——所以模型不管塞在 installs 还是 shared，ComfyUI 都加载得到。不是桌面版返回 []。
    """
    own = own_models_dir(root)
    if not own:
        return []
    out = [(own, "own")]
    for d in model_dirs(root)[1]:
        if not any(_norm(d) == _norm(x) for x, _s in out):
            out.append((d, "shared"))
    return out


def _yq(s):
    """YAML 单引号字符串（文件夹名可能带空格、冒号）"""
    return "'" + s.replace("'", "''") + "'"


def yaml_text(dirs, primary, extra=None):
    """
    照桌面版 buildYaml 生成 extra_model_paths：每个模型目录一段，默认目录标 is_default。
    extra：清单之外要挂的文件夹名，不给就到 dirs 里现找（extra_folders）。
    """
    extra = extra_folders(dirs) if extra is None else list(extra)
    lines = ["# 由启动器生成：ComfyUI 桌面版的共享模型目录（照桌面版自己的设置）。改这里没用，每次启动会重新生成。"]
    for i, d in enumerate(dirs):
        base = d.replace("\\", "/").replace('"', '\\"')
        lines.append(f"wwy_comfy_desktop_{i}:")
        lines.append(f'    base_path: "{base}"')
        if primary and _norm(d) == _norm(primary):
            lines.append("    is_default: true")
        for name in MODEL_FOLDER_TYPES:
            if name in SECONDARY_DIRS:
                lines.append(f"    {name}: |-")
                lines += [f"        {sub}/" for sub in (name,) + SECONDARY_DIRS[name]]
            else:
                lines.append(f"    {name}: {name}/")
        for name in extra:
            lines.append(f"    {_yq(name)}: {_yq(name + '/')}")
        for name in LEGACY_ALIASES:
            lines.append(f"    {name}: {name}/")
    return "\n".join(lines) + "\n"
