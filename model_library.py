# -*- coding: utf-8 -*-
"""
模型库操作（V3）。目前实现：拖拽上传（把拖进模型管理页的文件复制到当前分类）。

设计要点（见 V3 开发手册「模型管理 · 拖拽上传」）：
  - 复制而不是移动：用户原文件不动
  - 先写 .part 临时文件，完成后再改名：取消 / 失败不会留下半截模型
  - 同名同大小 → 视为同一个模型，跳过；同名不同大小 → 自动改名 name_1，
    因为 WebUI 按文件名调用 LoRA，绝不能覆盖或静默替换
  - 同目录下的同名附属文件（预览图、.civitai.info 等）自动带上，改名时一起改
  - 读 safetensors 头判断是不是 LoRA，放错分类时由界面提醒

纯标准库实现，不依赖界面；进度通过回调报告。
"""
import os
import shutil
import time

import safetensors_meta as sm

# 可以上传的模型文件（和模型管理页列表用的扩展名保持一致，另加 .sft）
MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx", ".gguf", ".sft")

# 模型旁边的附属文件：stem + 后缀。顺序有讲究——长后缀在前，
# 免得 "x.preview.png" 被当成 "x.preview" 的 ".png"
SIDECAR_SUFFIXES = (".civitai.info", ".preview.png", ".preview.jpg", ".preview.jpeg",
                    ".preview.webp", ".json", ".png", ".jpg", ".jpeg", ".webp")

CHUNK = 8 * 1024 * 1024


class Cancelled(Exception):
    pass


def _is_model(path):
    return path.lower().endswith(MODEL_EXTS)


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def _sidecar_suffix(name, model_stems):
    """name 是否是 model_stems 里某个模型的附属文件；是则返回 (stem, suffix)"""
    low = name.lower()
    for suf in SIDECAR_SUFFIXES:
        if low.endswith(suf):
            stem = name[: len(name) - len(suf)]
            if stem in model_stems:
                return stem, name[len(stem):]
    return None


def _find_sidecars(model_src):
    """模型所在目录里的同名附属文件 → [(源路径, 后缀)]"""
    d, stem = os.path.dirname(model_src), _stem(model_src)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not n.startswith(stem) or n == os.path.basename(model_src):
            continue
        hit = _sidecar_suffix(n, {stem})
        if hit:
            full = os.path.join(d, n)
            if os.path.isfile(full):
                out.append((full, hit[1]))
    return out


_FULL_MARKERS = ("model.diffusion_model.", "diffusion_model.", "double_blocks.",
                 "single_blocks.", "cond_stage_model.", "conditioner.")


def detect_kind(path):
    """
    'lora' / 'full'（大模型）/ 'other'（VAE、嵌入、放大模型等）/ None（读不出来）。
    只有 lora 和 full 用来判断「放错分类」——VAE 和嵌入的键名跟大模型有重叠，
    宁可不提醒也不要误报。
    """
    if not path.lower().endswith((".safetensors", ".sft")):
        return None
    head = sm.read_safetensors_header(path)
    if not head:
        return None
    meta, keys = head
    kind, _arch, _note = sm.guess_architecture(meta, keys)
    if kind == "LoRA":
        return "lora"
    if any(k.startswith(_FULL_MARKERS) for k in keys):
        return "full"
    return "other"


def _collect(paths):
    """
    把拖进来的路径展开成 [(源路径, 目标相对路径)] 和被忽略的文件名列表。
    拖进文件夹时保留文件夹本身这一层：拖「角色」→ 目标/角色/xxx.safetensors
    """
    items, ignored, seen = [], [], set()

    def add(src, rel):
        key = os.path.normcase(os.path.abspath(src))
        if key not in seen:
            seen.add(key)
            items.append((src, rel))

    loose = [p for p in paths if isinstance(p, str) and os.path.isfile(p)]
    loose_models = {_stem(p) for p in loose if _is_model(p)}
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            continue
        if os.path.isdir(p):
            base = os.path.dirname(os.path.abspath(p))
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    if _is_model(fn):
                        add(full, os.path.relpath(full, base))
        elif os.path.isfile(p):
            name = os.path.basename(p)
            if _is_model(p):
                add(p, name)
            elif _sidecar_suffix(name, loose_models):
                pass  # 附属文件：跟着模型走，不单列
            else:
                ignored.append(name)
    return items, ignored


def _free_name(dest, taken=()):
    """dest 已被占用时找 name_1 / name_2 …（磁盘上已有的和本次计划里已占的都要避开）"""
    root, ext = os.path.splitext(dest)
    i = 1
    while True:
        cand = f"{root}_{i}{ext}"
        if not (os.path.exists(cand) or os.path.exists(cand + ".part")
                or os.path.normcase(cand) in taken):
            return cand
        i += 1


def _same_size_twin(dest, size):
    """dest 或它的改名版本 name_1、name_2… 里有大小完全一样的，就当作同一个文件"""
    root, ext = os.path.splitext(dest)
    cands = [dest] + [f"{root}_{i}{ext}" for i in range(1, 50)]
    for c in cands:
        try:
            if os.path.getsize(c) == size:
                return c
        except OSError:
            if c != dest:
                break          # 编号断了就不用再往后找
    return None


def plan_import(target_dir, paths, progress=None, detect=True):
    """
    只读不写：算出这次上传会做什么，给界面确认用。
    progress(i, n, name)：每处理一个文件回调一次（读文件头判断类型要时间）；
    detect=False 时不读文件头（kind 全为 None），开始复制前重算计划用。
    返回 {items:[{src, dest, rel, size, action, kind}], ignored:[...],
          counts:{lora, full}, total_bytes, free_bytes}
    action: copy / rename（目标同名但内容不同，改名复制）/ skip（已存在同一个文件）
    """
    raw, ignored = _collect(paths)
    items, counts, total = [], {"lora": 0, "full": 0}, 0
    taken = set()
    n = len(raw)
    for idx, (src, rel) in enumerate(raw, 1):
        if progress:
            progress(idx, n, os.path.basename(src))
        try:
            size = os.path.getsize(src)
        except OSError:
            ignored.append(os.path.basename(src))
            continue
        dest = os.path.join(target_dir, rel)
        action = "copy"
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest)):
            action = "skip"          # 就是从这个文件夹里拖出来又拖回来
        elif os.path.exists(dest) or os.path.normcase(dest) in taken:
            twin = _same_size_twin(dest, size)
            if twin:
                dest, action = twin, "skip"   # 以前已经传过（可能被改过名），不再复制一份
            else:
                dest = _free_name(dest, taken)
                action = "rename"
        taken.add(os.path.normcase(dest))
        kind = detect_kind(src) if detect else None
        if kind in counts:
            counts[kind] += 1
        if action != "skip":
            total += size
        items.append({"src": src, "dest": dest, "rel": os.path.relpath(dest, target_dir),
                      "size": size, "action": action, "kind": kind})
    try:
        os.makedirs(target_dir, exist_ok=True)
        free = shutil.disk_usage(target_dir).free
    except OSError:
        free = None
    return {"items": items, "ignored": ignored, "counts": counts,
            "total_bytes": total, "free_bytes": free}


def _copy_file(src, dest, on_bytes, cancelled):
    """分块复制到 dest.part，完成后改名为 dest；取消/失败时删掉 .part"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                if cancelled():
                    raise Cancelled()
                buf = fi.read(CHUNK)
                if not buf:
                    break
                fo.write(buf)
                on_bytes(len(buf))
        try:
            shutil.copystat(src, part)   # 保留修改时间，模型列表按时间排序时不乱
        except OSError:
            pass
        os.replace(part, dest)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def run_import(plan, progress=None, cancelled=lambda: False):
    """
    按 plan_import 的结果执行复制。progress(info) 在字节推进时被调用（已节流）。
    返回 {copied, renamed, skipped, failed:[(name, err)], cancelled, last_dest}
    """
    todo = [it for it in plan["items"] if it["action"] != "skip"]
    total = sum(it["size"] for it in todo) or 1
    done = 0
    res = {"copied": 0, "renamed": 0, "skipped": len(plan["items"]) - len(todo),
           "failed": [], "cancelled": False, "last_dest": "", "dests": []}
    last_emit = [0.0]

    def report(i, name, force=False):
        now = time.monotonic()
        if progress and (force or now - last_emit[0] >= 0.15):
            last_emit[0] = now
            progress({"i": i, "n": len(todo), "name": name,
                      "done": done, "total": total,
                      "pct": round(done * 100.0 / total, 1)})

    for i, it in enumerate(todo, 1):
        name = os.path.basename(it["dest"])
        report(i, name, force=True)

        def on_bytes(n, _i=i, _name=name):
            nonlocal done
            done += n
            report(_i, _name)
        try:
            _copy_file(it["src"], it["dest"], on_bytes, cancelled)
        except Cancelled:
            res["cancelled"] = True
            break
        except OSError as e:
            res["failed"].append((name, str(e)))
            continue
        # 附属文件跟着走；模型被改名时附属文件用新名字
        new_stem = _stem(it["dest"])
        for sc_src, suffix in _find_sidecars(it["src"]):
            sc_dest = os.path.join(os.path.dirname(it["dest"]), new_stem + suffix)
            if os.path.exists(sc_dest):
                continue
            try:
                shutil.copy2(sc_src, sc_dest)
            except OSError:
                pass
        res["copied"] += 1
        if it["action"] == "rename":
            res["renamed"] += 1
        res["last_dest"] = it["dest"]
        res["dests"].append(it["dest"])
    report(len(todo), "", force=True)
    return res


# ============================================================
# 共享模型库（V3 阶段 4）
# ============================================================
import hashlib
import json
import uuid

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "launcher_data")
JOURNAL_DIR = os.path.join(DATA_DIR, "moves")
TRASH_DIR = os.path.join(DATA_DIR, "trash")

# 模型库分类：(键, 显示名, 是否 LoRA, WebUI 下的候选目录, ComfyUI 下的目录, WebUI 参数候选)
# 注意同一种模型在两边内部的目录名不一样（共享库目录名沿用 ComfyUI 风格）：
#   WebUI/Neo 用 models/Stable-diffusion、models/Lora、models/VAE、models/ESRGAN、
#   models/hypernetworks、models/text_encoder 这种各有各的写法；
#   ComfyUI 统一用 models/checkpoints、models/loras、models/vae、
#   models/upscale_models、models/hypernetworks、models/text_encoders 全小写复数。
# WebUI 候选目录按顺序取第一个存在的（Forge Neo 把 embeddings 挪进了 models/）。
# WebUI 参数候选按优先顺序排列，forge_library_args 取目标实际支持的第一个：
#   Neo 的 --ckpt-dirs/--lora-dirs/--vae-dirs/--text-encoder-dirs/--controlnet-dirs
#   是 action="append" 的叠加式参数（实例自己的目录仍然保留，优先用它们）；
#   Classic 只有 --ckpt-dir 这类替换式单数参数。候选末尾是读不到源码时的兜底，
#   None 表示「没把握就不传」。
LIBRARY_CATEGORIES = [
    ("checkpoints", "Checkpoint 大模型", False, ("models/Stable-diffusion", "models/checkpoints"), "models/checkpoints", ("--ckpt-dirs", "--ckpt-dir")),
    ("loras", "LoRA", True, ("models/Lora",), "models/loras", ("--lora-dirs", "--lora-dir")),
    ("vae", "VAE", False, ("models/VAE",), "models/vae", ("--vae-dirs", "--vae-dir")),
    ("embeddings", "Embedding 嵌入", False, ("models/embeddings", "embeddings"), "models/embeddings", ("--embeddings-dir",)),
    ("controlnet", "ControlNet", False, ("models/ControlNet",), "models/controlnet", ("--controlnet-dirs", "--controlnet-dir")),
    ("upscale_models", "放大模型", False, ("models/ESRGAN",), "models/upscale_models", ("--esrgan-models-path",)),
    ("hypernetworks", "Hypernetwork", False, ("models/hypernetworks",), "models/hypernetworks", ("--hypernetwork-dir",)),
    ("diffusion_models", "扩散模型 (UNet / DiT)", False, ("models/diffusion_models",), "models/diffusion_models", ("--ckpt-dirs", None)),
    # Neo 支持 --text-encoder-dirs（Classic 没有，扫描时会被自动跳过）
    ("text_encoders", "文本编码器", False, ("models/text_encoder",), "models/text_encoders", ("--text-encoder-dirs",)),
    ("clip_vision", "CLIP Vision", False, (), "models/clip_vision", ()),
]


def library_categories(lib_path):
    """模型库的分类列表（模型管理页用），目录不存在也列出来（上传时会自动建）"""
    return [{"key": k, "label": label, "is_lora": is_lora, "path": os.path.join(lib_path, k)}
            for k, label, is_lora, _f, _c, _a in LIBRARY_CATEGORIES]


# image_meta_core / civitai_downloader 里的「角色」→ 模型库分类键
ROLE_TO_LIBRARY_KEY = {
    "Checkpoint": "checkpoints", "LoRA": "loras", "Embedding": "embeddings", "VAE": "vae",
    "ControlNet": "controlnet", "Upscaler": "upscale_models", "TextEncoder": "text_encoders",
    "Hypernetwork": "hypernetworks",
}

# LIBRARY_CATEGORIES 里没列出、但下载器会给出的旧式目录名
_EXTRA_FOLDER_KEYS = {
    "models/hypernetwork": "hypernetworks", "models/unet": "diffusion_models",
    "models/clip": "text_encoders", "models/lycoris": "loras",
}


def library_key_for_folder(rel_folder):
    """实例里的相对目录（任意分支写法：models/Lora、models/loras、embeddings…）→ 模型库分类键；认不出返回 None"""
    r = str(rel_folder or "").replace("\\", "/").strip("/").lower()
    if not r:
        return None
    for k, _l, _i, frels, crel, _a in LIBRARY_CATEGORIES:
        if r == k or r == crel.lower() or r in (x.lower() for x in frels):
            return k
    return _EXTRA_FOLDER_KEYS.get(r)


def instance_model_dirs(cfg, comfy_layout):
    """实例自己的模型目录 {分类键: 绝对路径}（只返回真实存在的）"""
    root = (cfg.get("webui_root") or "").strip()
    out = {}
    if not root or not os.path.isdir(root):
        return out
    if cfg.get("webui_branch") == "comfyui":
        base = comfy_layout(root)[0] or root
        for k, _l, _i, _f, crel, _a in LIBRARY_CATEGORIES:
            d = os.path.join(base, crel.replace("/", os.sep))
            if os.path.isdir(d):
                out[k] = d
        # 老版 ComfyUI 的 unet / clip 目录
        for k, alt in (("diffusion_models", "models/unet"), ("text_encoders", "models/clip")):
            d = os.path.join(base, alt.replace("/", os.sep))
            if k not in out and os.path.isdir(d):
                out[k] = d
    else:
        for k, _l, _i, frels, _c, _a in LIBRARY_CATEGORIES:
            for rel in frels:
                d = os.path.join(root, rel.replace("/", os.sep))
                if os.path.isdir(d):
                    out[k] = d
                    break
    return out


def comfy_yaml_text(lib_path):
    """给 ComfyUI 的 extra_model_paths 配置（启动器自己生成一份，通过参数传入，不动用户原有的 yaml）"""
    base = lib_path.replace("\\", "/")
    lines = ["# 由启动器生成：共享模型库。改这里没用，每次启动会重新生成。",
             "wwy_launcher_library:",
             f'    base_path: "{base}"',
             "    is_default: true"]
    for k, *_ in LIBRARY_CATEGORIES:
        lines.append(f"    {k}: {k}")
    lines.append("    unet: diffusion_models")
    lines.append("    clip: text_encoders")
    return "\n".join(lines) + "\n"


_FLAG_CACHE = {}


def _scan_supported_flags(webui_root):
    """从目标 WebUI 源码里收集实际支持的命令行参数（--xxx 形式）。
    读不到源码返回 None（调用方按旧行为兜底）。按 webui_root 缓存。

    踩过的坑：参数定义不止在 modules/cmd_args.py——Neo 把 --controlnet-dir /
    --controlnet-dirs 放在 modules_forge/shared.py，--lora-dir 在
    extensions-builtin/sd_forge_lora/preload.py（扩展 preload 机制）。
    只扫 cmd_args.py + launch.py 会把这些误判成"不支持"而跳过，对应分类的
    模型在 WebUI 里就整个消失了（合并后实例自带目录已空，等于模型全丢）。"""
    if webui_root in _FLAG_CACHE:
        return _FLAG_CACHE[webui_root]
    import glob
    import re
    rels = ["modules/cmd_args.py", "launch.py", "modules_forge/shared.py"]
    rels += sorted(glob.glob(os.path.join("extensions-builtin", "*", "preload.py"),
                             root_dir=webui_root) if os.path.isdir(webui_root) else [])
    rels += sorted(glob.glob(os.path.join("extensions", "*", "preload.py"),
                             root_dir=webui_root) if os.path.isdir(webui_root) else [])
    text_parts = []
    for rel in rels:
        try:
            with open(os.path.join(webui_root, rel), "r", encoding="utf-8", errors="replace") as f:
                text_parts.append(f.read())
        except OSError:
            continue
    flags = set(re.findall(r"""["'](--[a-z0-9-]+)["']""", "\n".join(text_parts))) if text_parts else None
    _FLAG_CACHE[webui_root] = flags
    return flags


def forge_library_args(lib_path, webui_root=""):
    """WebUI 用的模型目录参数，返回 (参数字符串, 被跳过的分类键列表)。

    webui_root 传入时会先扫描目标 WebUI 的参数定义，每个分类只传它真正
    支持的候选参数（见 LIBRARY_CATEGORIES 注释）。读不到源码时按各分类的
    兜底参数传（保持旧行为）；兜底是 None 的分类宁可不传。"""
    supported = _scan_supported_flags(webui_root) if webui_root else None
    args, skipped = [], []
    for k, _l, _i, _f, _c, candidates in LIBRARY_CATEGORIES:
        if not candidates:
            continue
        flag = None
        if supported is not None:
            for f in candidates:
                if f and f in supported:
                    flag = f
                    break
        else:
            flag = candidates[-1]     # 兜底：旧行为用的参数，None = 不传
        if flag is None:
            skipped.append(k)
            continue
        args.append(f'{flag} "{os.path.join(lib_path, k)}"')
    return " ".join(args), skipped


def quick_hash(path):
    """大小 + 头尾各 1MB 的哈希：判重够用，几 GB 的模型也是毫秒级"""
    h = hashlib.sha1()
    size = os.path.getsize(path)
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            f.seek(-1024 * 1024, os.SEEK_END)
            h.update(f.read(1024 * 1024))
    return h.hexdigest()


def _walk_models(d):
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames[:] = [x for x in dirnames if not x.startswith(".")]
        for fn in filenames:
            if _is_model(fn):
                yield os.path.join(dirpath, fn)


def _same_volume(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b if os.path.exists(b) else os.path.dirname(b)).st_dev
    except OSError:
        return False


def plan_merge(instances, lib_path, comfy_layout):
    """
    把各实例已有的模型合并进模型库的计划（只读）。
    instances: [(iid, name, cfg)]
    返回 {moves:[{src,dst,size,iid,dup}], total_bytes, dup_bytes, cross_bytes, count}
      dup=True 表示模型库里已有同一个文件（按大小+头尾哈希判断），源文件进回收站而不是再搬一份
    """
    moves, seen = [], {}
    lib_norm = os.path.normcase(os.path.abspath(lib_path))
    # 模型库里已有的文件先登记，用来判重
    for k, *_ in LIBRARY_CATEGORIES:
        d = os.path.join(lib_path, k)
        if os.path.isdir(d):
            for p in _walk_models(d):
                try:
                    seen.setdefault((k, os.path.getsize(p)), []).append((p, p))
                except OSError:
                    pass
    total = dup_bytes = cross = 0
    planned_dst = set()
    for iid, name, cfg in instances:
        for k, d in instance_model_dirs(cfg, comfy_layout).items():
            if os.path.normcase(os.path.abspath(d)).startswith(lib_norm):
                continue    # 已经指向模型库（之前合并过）
            for src in _walk_models(d):
                try:
                    size = os.path.getsize(src)
                except OSError:
                    continue
                rel = os.path.relpath(src, d)
                dup_of = None
                for hash_path, final_path in seen.get((k, size), []):
                    try:
                        if quick_hash(hash_path) == quick_hash(src):
                            dup_of = final_path     # 合并完成后那一份所在的位置
                            break
                    except OSError:
                        continue
                if dup_of:
                    moves.append({"src": src, "dst": dup_of, "size": size, "iid": iid,
                                  "cat": k, "dup": True})
                    dup_bytes += size
                    continue
                dst = os.path.join(lib_path, k, rel)
                if os.path.exists(dst) or os.path.normcase(dst) in planned_dst:
                    dst = _free_name(dst, planned_dst)
                planned_dst.add(os.path.normcase(dst))
                seen.setdefault((k, size), []).append((src, dst))   # 后面的实例遇到同一文件时判重
                moves.append({"src": src, "dst": dst, "size": size, "iid": iid, "cat": k, "dup": False})
                total += size
                if not _same_volume(src, lib_path):
                    cross += size
    return {"moves": moves, "total_bytes": total, "dup_bytes": dup_bytes,
            "cross_bytes": cross, "count": len(moves)}


# ---------------------------------------------------------- 回收站 / 移动日志 ----

def send_to_trash(path):
    """删除一律进回收站（Windows）；其他系统挪进启动器自己的 trash 目录"""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                            ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
            FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
            op = SHFILEOPSTRUCTW()
            op.wFunc = FO_DELETE
            op.pFrom = os.path.abspath(path) + "\0\0"
            op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
            rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            return rc == 0 and not os.path.exists(path)
        except Exception:
            return False
    try:
        os.makedirs(TRASH_DIR, exist_ok=True)
        dst = os.path.join(TRASH_DIR, f"{int(time.time())}_{os.path.basename(path)}")
        shutil.move(path, dst)
        return True
    except OSError:
        return False


class MoveJournal:
    """一批文件移动的记录，用于撤销。存成 launcher_data/moves/<批次>.json"""

    def __init__(self, kind, title, root=""):
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.data = {"id": self.id, "kind": kind, "title": title, "time": time.time(), "root": root,
                     "moves": [], "trashed": [], "workflows": [], "undone": False}

    def moved(self, src, dst):
        self.data["moves"].append([src, dst])

    def trashed(self, src):
        self.data["trashed"].append(src)

    def save(self):
        with open(os.path.join(JOURNAL_DIR, self.id + ".json"), "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)


def list_journals(kind=None):
    out = []
    if not os.path.isdir(JOURNAL_DIR):
        return out
    for fn in sorted(os.listdir(JOURNAL_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOURNAL_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if kind and d.get("kind") != kind:
            continue
        out.append(d)
    return out


def undo_journal(jid):
    """撤销一批移动：倒序把文件搬回原处、恢复被改写的工作流。进回收站的文件需要用户自己去回收站还原。"""
    path = os.path.join(JOURNAL_DIR, jid + ".json")
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if d.get("undone"):
        return {"restored": 0, "failed": [], "trashed": 0, "already": True}
    restored, failed = 0, []
    for src, dst in reversed(d.get("moves") or []):
        if not os.path.exists(dst):
            failed.append((os.path.basename(dst), "文件已不在整理后的位置"))
            continue
        if os.path.exists(src):
            failed.append((os.path.basename(src), "原位置已有同名文件"))
            continue
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dst, src)
            restored += 1
        except OSError as e:
            failed.append((os.path.basename(dst), str(e)))
    for wf, backup in d.get("workflows") or []:
        try:
            if os.path.exists(backup):
                shutil.copy2(backup, wf)
        except OSError as e:
            failed.append((os.path.basename(wf), str(e)))
    _remove_empty_dirs({os.path.dirname(dst) for _s, dst in d.get("moves") or []}, d.get("root"))
    d["undone"] = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return {"restored": restored, "failed": failed, "trashed": len(d.get("trashed") or [])}


def _remove_empty_dirs(dirs, stop):
    """删掉整理后空出来的文件夹，只删 stop 目录「里面」的，绝不往上删到 stop 本身及以外"""
    stop_n = os.path.normcase(os.path.abspath(stop)) if stop else ""
    for d in sorted(dirs, key=len, reverse=True):
        try:
            while d and os.path.isdir(d) and not os.listdir(d):
                dn = os.path.normcase(os.path.abspath(d))
                if not stop_n or dn == stop_n or not dn.startswith(stop_n + os.sep):
                    break
                os.rmdir(d)
                d = os.path.dirname(d)
        except OSError:
            pass


def move_with_sidecars(src, dst, journal):
    """移动模型及同名附属文件；目标已存在时抛 FileExistsError"""
    if os.path.exists(dst):
        raise FileExistsError(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    sidecars = _find_sidecars(src)
    shutil.move(src, dst)
    journal.moved(src, dst)
    new_stem = _stem(dst)
    for sc, suffix in sidecars:
        sd = os.path.join(os.path.dirname(dst), new_stem + suffix)
        if os.path.exists(sd):
            continue
        try:
            shutil.move(sc, sd)
            journal.moved(sc, sd)
        except OSError:
            pass


def run_merge(plan, lib_path, progress=None, cancelled=lambda: False):
    """执行合并计划。返回 {moved, trashed, failed, cancelled, journal}"""
    j = MoveJournal("merge", "合并各实例模型到模型库", root=lib_path)
    res = {"moved": 0, "trashed": 0, "failed": [], "cancelled": False, "journal": j.id}
    n = len(plan["moves"])
    for i, mv in enumerate(plan["moves"], 1):
        if cancelled():
            res["cancelled"] = True
            break
        if progress:
            progress({"i": i, "n": n, "name": os.path.basename(mv["src"])})
        try:
            if mv["dup"]:
                sidecars = _find_sidecars(mv["src"])
                if send_to_trash(mv["src"]):
                    j.trashed(mv["src"])
                    res["trashed"] += 1
                    # 附属文件：库里那份没有就搬过去，有就一起进回收站
                    for sc, suffix in sidecars:
                        sd = os.path.join(os.path.dirname(mv["dst"]), _stem(mv["dst"]) + suffix)
                        if not os.path.exists(sd):
                            shutil.move(sc, sd)
                            j.moved(sc, sd)
                        else:
                            send_to_trash(sc)
                else:
                    res["failed"].append((os.path.basename(mv["src"]), "无法放进回收站（可能被占用）"))
            else:
                move_with_sidecars(mv["src"], mv["dst"], j)
                res["moved"] += 1
        except OSError as e:
            res["failed"].append((os.path.basename(mv["src"]), str(e)))
    j.save()
    return res


# ============================================================
# LoRA 自动整理
# ============================================================

_BASE_ALIASES = [
    (("illustrious",), "Illustrious"),
    (("noobai", "noob ai"), "NoobAI"),
    (("pony",), "Pony"),
    (("sdxl",), "SDXL"),
    (("sd 1.5", "sd1.5", "sd 1.4", "sd1.4"), "SD1.5"),
    (("sd 2", "sd2"), "SD2"),
    (("sd 3", "sd3"), "SD3"),
    (("flux",), "Flux"),
    (("anima",), "Anima"),
    (("wan video", "wan 2", "wan2", "wan"), "Wan"),
    (("hunyuan",), "Hunyuan"),
    (("qwen",), "Qwen"),
    (("lumina",), "Lumina"),
    (("chroma",), "Chroma"),
    (("hidream",), "HiDream"),
    (("kolors",), "Kolors"),
]

# Civitai 模型标签 → 类型文件夹
_TYPE_BY_TAG = [
    (("character", "celebrity"), "角色"),
    (("style",), "画风"),
    (("clothing",), "服装"),
    (("poses", "pose", "action"), "姿势动作"),
    (("concept",), "概念"),
    (("background", "buildings"), "场景"),
    (("tool",), "工具"),
]

_BAD_PATH_CHARS = '<>:"/\\|?*'


def _clean_folder(name):
    name = "".join("_" if c in _BAD_PATH_CHARS else c for c in str(name)).strip(" .")
    return name[:40] or "未分类"


def normalize_base(base, arch_hint=""):
    s = (base or "").strip()
    low = s.lower()
    for keys, label in _BASE_ALIASES:
        if any(k in low for k in keys):
            return label
    if s:
        return _clean_folder(s)
    hint = (arch_hint or "").lower()
    if "sdxl" in hint:
        return "SDXL"
    if "sd1.5" in hint:
        return "SD1.5"
    if "flux" in hint:
        return "Flux"
    return ""


def classify_type(tags):
    tl = [str(t).lower() for t in (tags or [])]
    for keys, label in _TYPE_BY_TAG:
        if any(t in keys for t in tl):
            return label
    return "其他" if tl else ""


def organize_subdir(info, template, instance_name="", arch_hint=""):
    """按模板算出目标子文件夹，信息不够时返回 '未分类'"""
    base = normalize_base((info or {}).get("baseModel"), arch_hint)
    typ = classify_type((info or {}).get("tags"))
    parts = []
    for key in (template or "base/type").split("/"):
        if key == "base":
            parts.append(base or "未知底模")
        elif key == "type":
            parts.append(typ or "未分类")
        elif key == "instance":
            parts.append(_clean_folder(instance_name or "实例"))
    if not base and not typ:
        return "未分类"
    return os.path.join(*[_clean_folder(p) for p in parts])


def organize_candidates(lora_root, include_sub, only=None):
    """要参与整理的 LoRA：根目录下的 + 「未分类」里的；勾了 include_sub 则包括所有子文件夹"""
    if only:
        return [p for p in only if os.path.isfile(p)]
    files = []
    for p in _walk_models(lora_root):
        rel = os.path.relpath(p, lora_root)
        if rel.count(os.sep) == 0 or rel.split(os.sep)[0] == "未分类" or include_sub:
            files.append(p)
    return files


def plan_organize(lora_root, template, include_sub, read_info, arch_of, instance_name="",
                  only=None):
    """
    LoRA 整理计划（只读）。
    read_info(path) → sidecar 信息字典；arch_of(path) → 架构提示文本
    only: 只整理这些文件（上传/下载后自动整理用）
    返回 [{src, dst, rel_from, rel_to, sub}]
    """
    plans, taken = [], set()
    if not os.path.isdir(lora_root):
        return plans
    for src in organize_candidates(lora_root, include_sub, only):
        info = read_info(src) or {}
        sub = organize_subdir(info, template, instance_name, arch_of(src) if not info.get("baseModel") else "")
        dst = os.path.join(lora_root, sub, os.path.basename(src))
        if os.path.normcase(os.path.dirname(src)) == os.path.normcase(os.path.dirname(dst)):
            continue    # 已经在该在的位置
        if os.path.exists(dst) or os.path.normcase(dst) in taken:
            dst = _free_name(dst, taken)
        taken.add(os.path.normcase(dst))
        plans.append({"src": src, "dst": dst, "sub": sub,
                      "rel_from": os.path.relpath(src, lora_root),
                      "rel_to": os.path.relpath(dst, lora_root)})
    return plans


def fix_comfy_workflows(workflow_dirs, rel_map, journal):
    """
    ComfyUI 已保存的工作流里 LoRA 是按「相对 loras 目录的路径」引用的，
    文件被整理进子文件夹后旧工作流会找不到。这里把 JSON 里精确等于旧路径的字符串换成新路径
    （正斜杠、反斜杠两种写法都处理），改之前备份，撤销时还原。
    返回修改过的工作流文件数。
    """
    if not rel_map:
        return 0
    pairs = []
    for old, new in rel_map.items():
        # 本机分隔符在前：旧路径没有子文件夹时两种写法相同，以本机写法为准
        for sep_old, sep_new in ((os.sep, os.sep), ("/" if os.sep == "\\" else "\\",) * 2):
            o = old.replace(os.sep, sep_old)
            n = new.replace(os.sep, sep_new)
            pairs.append((json.dumps(o, ensure_ascii=False), json.dumps(n, ensure_ascii=False)))
    backup_root = os.path.join(DATA_DIR, "workflow_backups", journal.id)
    changed = 0
    for d in workflow_dirs:
        if not os.path.isdir(d):
            continue
        for dirpath, _dn, filenames in os.walk(d):
            for fn in filenames:
                if not fn.lower().endswith(".json"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        text = f.read()
                except (OSError, UnicodeDecodeError):
                    continue
                new_text = text
                for o, n in pairs:
                    if o in new_text:
                        new_text = new_text.replace(o, n)
                if new_text == text:
                    continue
                try:
                    bk = os.path.join(backup_root, f"{changed}_{fn}")
                    os.makedirs(backup_root, exist_ok=True)
                    shutil.copy2(p, bk)
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(new_text)
                    journal.data["workflows"].append([p, bk])
                    changed += 1
                except OSError:
                    continue
    return changed


def run_organize(plans, lora_root, workflow_dirs=(), progress=None, title="LoRA 自动整理"):
    """执行 LoRA 整理计划。返回 {moved, failed, workflows, journal}"""
    j = MoveJournal("organize", title, root=lora_root)
    res = {"moved": 0, "failed": [], "workflows": 0, "journal": j.id}
    rel_map = {}
    for i, pl in enumerate(plans, 1):
        if progress:
            progress({"i": i, "n": len(plans), "name": os.path.basename(pl["src"])})
        try:
            move_with_sidecars(pl["src"], pl["dst"], j)
            res["moved"] += 1
            rel_map[pl["rel_from"]] = pl["rel_to"]
        except OSError as e:
            res["failed"].append((os.path.basename(pl["src"]), str(e)))
    try:
        res["workflows"] = fix_comfy_workflows(workflow_dirs, rel_map, j)
    except Exception:
        pass
    _remove_empty_dirs({os.path.dirname(pl["src"]) for pl in plans}
                       - {os.path.dirname(pl["dst"]) for pl in plans}, lora_root)
    j.save()
    return res


def comfy_workflow_dirs(comfy_dir):
    """ComfyUI 保存工作流的位置：user/<用户>/workflows"""
    out = []
    user = os.path.join(comfy_dir, "user")
    if os.path.isdir(user):
        for name in os.listdir(user):
            d = os.path.join(user, name, "workflows")
            if os.path.isdir(d):
                out.append(d)
    return out
