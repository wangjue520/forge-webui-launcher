"""
model_catalog.py — 模型下载页「常用模型一键下载」的目录和选择逻辑

数据在同目录的 model_catalog.json（tools/build_model_catalog.py 联网核对后生成：每个文件的仓库路径、
精确大小、sha256 都查过）。结构：
    模型 → 版本（Anima 的 Aesthetic / Turbo / Base…，互斥，选一个）
         → 组件（主模型 / 文本编码器 / VAE / 加速 LoRA…，必需的锁定勾选）
         → 选项（同一个文件的不同精度 / 量化：bf16、fp8、int8、GGUF Q4…，选一个）

这里只放和界面 / 实例无关的纯逻辑（方便单测）：
  - instance_kind：当前实例是 ComfyUI / Forge Neo / Neo H3 / Forge Classic / A1111
  - option_issue：某个精度在这台电脑 / 这个实例上能不能用（50 系专用、要装 GGUF 插件…）
  - pick_defaults：按显存、内存、显卡架构给每个组件挑默认精度
  - existing_file / partial_size：判断「已经下过」「下了一半会续传」
  - source_urls：一个文件的下载地址（HF 镜像 / 官方 / Civitai，按网络环境排先后）
"""

import json
import os
from urllib.parse import quote

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(APP_DIR, "model_catalog.json")

HF = "https://huggingface.co"
CIVITAI_DOWNLOAD = "https://civitai.com/api/download/models/{vid}"

# 同名文件大小差在这个范围内算「同一个模型」：同一份权重被不同人重新打包时，
# 文件头的元数据 / 键名写法不一样，大小会差几 KB（比如 FLUX.2 VAE 的两份 flux2-vae.safetensors 差 2KB）。
# 真下坏了的文件差得远比这多（.part 没下完不会改成正式文件名）。
SIZE_TOLERANCE = 1024 * 1024

KIND_LABELS = {"comfyui": "ComfyUI", "neo": "Forge Neo", "h3": "Forge Neo H3",
               "classic": "Forge Classic", "a1111": "A1111"}
UI_LABELS = {"comfyui": "ComfyUI", "neo": "Forge Neo", "h3": "Forge Neo H3",
             "classic": "Forge Classic", "a1111": "A1111"}

# 这几种是 RTX 50 系（Blackwell）的硬件格式：别的卡上要么跑不动、要么比 fp8 / int8 还慢，不默认选
BLACKWELL_FMTS = {"nvfp4", "mxfp8", "fp4"}
# 新式量化（ComfyUI / Forge Neo 新版才认），老 Forge Classic / A1111 读不了
NEW_QUANT_FMTS = {"int8", "w4a8", "w6a8", "mxfp8", "nvfp4", "fp4"}
MAIN_ROLES = ("DiffusionModel", "Checkpoint")

_QUALITY = {"fp32": 101, "bf16": 100, "fp16": 100, "fp8": 92, "int8": 92, "mxfp8": 92,
            "w6a8": 86, "nvfp4": 74, "w4a8": 74, "fp4": 72}

_cache = None


class CatalogError(Exception):
    pass


def load(force=False):
    global _cache
    if _cache is None or force:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def find_model(mid):
    return next((m for m in load()["models"] if m["id"] == mid), None)


def default_version(model):
    return next((v for v in model["versions"] if v.get("default")), model["versions"][0])


def find_version(model, vid):
    return next((v for v in model["versions"] if v["id"] == vid), None)


def resolve(mid, vid, cid, oid):
    """(模型, 版本, 组件, 选项)，哪一层对不上都报错"""
    model = find_model(mid)
    if not model:
        raise CatalogError(f"目录里没有这个模型：{mid}")
    ver = find_version(model, vid)
    if not ver:
        raise CatalogError(f"{model['name']} 没有版本 {vid}")
    comp = next((c for c in ver["components"] if c["id"] == cid), None)
    if not comp:
        raise CatalogError(f"{model['name']} {ver['label']} 没有 {cid} 这一项")
    opt = next((o for o in comp["options"] if o["id"] == oid), None)
    if not opt:
        raise CatalogError(f"{comp['label']} 没有 {oid} 这个精度")
    return model, ver, comp, opt


# ---------------------------------------------------------------- 实例 / 硬件

def instance_kind(cfg, is_a1111=None):
    """当前实例的类型：comfyui / h3 / neo / classic / a1111"""
    cfg = cfg or {}
    branch = cfg.get("webui_branch") or ""
    if branch == "comfyui":
        return "comfyui"
    if cfg.get("webui_variant") == "h3":
        return "h3"
    if is_a1111 and is_a1111((cfg.get("webui_root") or "").strip()):
        return "a1111"
    if branch == "classic":
        return "classic"
    return "neo"


def ui_list(model, ver=None):
    return (ver or {}).get("ui") or model.get("ui") or []


def supported(model, ver, kind):
    """这个模型 / 版本在当前实例里能不能用。Neo H3 是 Neo 的分支，Neo 能用的它基本都能用"""
    ui = ui_list(model, ver)
    return kind in ui or (kind == "h3" and "neo" in ui)


def sm_from_name(name):
    """nvidia-smi 太老查不到 compute_cap 时，按显卡型号粗估架构（只用来挑默认精度）"""
    n = (name or "").upper()
    for key, sm in (("RTX 50", 12.0), ("RTX PRO", 12.0), ("B200", 10.0), ("B100", 10.0), ("H100", 9.0),
                    ("H200", 9.0), ("RTX 40", 8.9), ("L40", 8.9), ("L4", 8.9), ("ADA", 8.9), ("RTX 30", 8.6),
                    ("A100", 8.0), ("A10", 8.6), ("A40", 8.6), ("A6000", 8.6), ("RTX 20", 7.5),
                    ("GTX 16", 7.5), ("T4", 7.5), ("TITAN RTX", 7.5), ("V100", 7.0), ("GTX 10", 6.1),
                    ("P40", 6.1), ("P100", 6.0), ("GTX 9", 5.2)):
        if key in n:
            return sm
    return None


def make_ctx(kind, vram_gb=None, ram_gb=None, sm=None, gguf_node=True):
    return {"kind": kind, "vram_gb": vram_gb, "ram_gb": ram_gb, "sm": sm or 0,
            "blackwell": bool(sm and sm >= 10.0), "gguf_node": bool(gguf_node)}


def option_issue(opt, ctx):
    """这个精度在这台电脑 / 这个实例上的问题（空串 = 没问题）。有问题的不会被选成默认，但用户仍可手动选"""
    fmt = opt.get("fmt")
    kind = ctx.get("kind")
    if fmt in NEW_QUANT_FMTS and kind in ("classic", "a1111"):
        return f"{KIND_LABELS.get(kind, kind)} 读不了这种量化"
    if fmt in BLACKWELL_FMTS and not ctx.get("blackwell"):
        return "RTX 50 系专用"
    if fmt == "gguf":
        if kind == "comfyui" and not ctx.get("gguf_node"):
            return "要先装 ComfyUI-GGUF 插件"
        if kind == "a1111":
            return "A1111 读不了 GGUF"
    return ""


def quality(opt):
    if opt.get("fmt") == "gguf":
        q = (opt.get("quant") or "").upper()
        if q.startswith(("BF16", "F16", "F32")):
            return 100
        for tag, val in (("Q8", 90), ("Q6", 82), ("Q5", 76), ("Q4", 68), ("Q3", 54), ("Q2", 40)):
            if tag in q:
                return val
        return 60
    return _QUALITY.get(opt.get("fmt"), 80)


def _score(opt, ctx):
    """越大越好：画质为主，再按显卡架构微调（30 系及以前 int8 有硬件加速、fp8 没有；40 系起反过来）"""
    s = quality(opt)
    fmt, sm = opt.get("fmt"), ctx.get("sm") or 0
    if fmt == "fp8":
        s += 3 if sm >= 8.9 else (1 if not sm else 0)
    elif fmt in ("int8", "w6a8", "w4a8"):
        s += 3 if 0 < sm < 8.9 else (1 if sm else 0)
    elif fmt == "gguf":
        # GGUF 要边算边解量化，同等画质比 safetensors 慢；只有 Forge Neo H3 官方推荐 H3 用 GGUF
        s += 6 if ctx.get("prefer_gguf") else -4
    return s


GB = 1024 ** 3


def main_budget(vram):
    """主模型文件最大多大（GB）：大显存留 1.5GB 给运算；小显存允许略超一点（ComfyUI / Neo 会分块挪进显存）"""
    return vram - 1.5 if vram >= 16 else max(vram * 1.15 - 2, 2.5)


def te_budget(vram):
    """文本编码器只在开头跑一次、跑完就挪出显存，可以比显存稍大"""
    return max(vram * 1.1, 4)


def _pick(options, ctx, budget_gb):
    usable = [o for o in options if not option_issue(o, ctx)] or list(options)
    ranked = sorted(usable, key=lambda o: -_score(o, ctx))
    if budget_gb is None:
        return ranked[0]
    fit = [o for o in ranked if o["size"] <= budget_gb * GB]
    if fit:
        return fit[0]
    return min(usable, key=lambda o: o["size"])


def pick_defaults(ver, ctx, ram_factor=1.0):
    """
    每个组件的默认精度 {组件 id: 选项 id}。
    主模型、文本编码器按显存挑「放得下的最高画质」（main_budget / te_budget），显存查不到时按 12GB 估。
    最后看内存：主模型 + 文本编码器加起来（乘上模型自己的内存系数，H3 这种运算时很吃内存的大一些）
    超过（内存 + 显存）的 70% 时，先降文本编码器、再降主模型，免得一加载就把内存吃满。
    """
    vram = ctx.get("vram_gb") or 12
    picks = {}
    for comp in ver["components"]:
        role = comp.get("role")
        if role in MAIN_ROLES:
            budget = main_budget(vram)
        elif role == "TextEncoder":
            budget = te_budget(vram)
        else:
            budget = None
        picks[comp["id"]] = _pick(comp["options"], ctx, budget)

    ram = ctx.get("ram_gb")
    if ram:
        limit = (ram + vram) * 0.7 * GB
        big = [c for c in ver["components"] if c.get("required") and c.get("role") in MAIN_ROLES + ("TextEncoder",)]
        for _ in range(20):
            total = sum(picks[c["id"]]["size"] for c in big) * (ram_factor or 1.0)
            if total <= limit:
                break
            stepped = False
            # 先降文本编码器，再降主模型；每次降到下一个画质档里最大的那个
            for c in sorted(big, key=lambda c: c.get("role") != "TextEncoder"):
                cur = picks[c["id"]]
                usable = [o for o in c["options"] if not option_issue(o, ctx)]
                smaller = [o for o in usable if o["size"] < cur["size"]]
                if smaller:
                    picks[c["id"]] = max(smaller, key=lambda o: (_score(o, ctx), o["size"]))
                    stepped = True
                    break
            if not stepped:
                break
    return {cid: o["id"] for cid, o in picks.items()}


# ---------------------------------------------------------------- 文件

def existing_file(dirs, filename, size=None, tolerance=SIZE_TOLERANCE):
    """
    在候选目录里找同名文件 → (路径, 大小是否吻合)；没找到 (None, False)。
    size 为 None 时只要存在就算吻合；大小差在 tolerance 以内也算（同一模型的不同打包）。
    """
    for d in dirs:
        if not d:
            continue
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            try:
                got = os.path.getsize(p)
            except OSError:
                continue
            return p, (size is None or abs(got - size) <= tolerance)
    return None, False


def partial_size(dest_dir, filename):
    try:
        return os.path.getsize(os.path.join(dest_dir, filename) + ".part")
    except OSError:
        return 0


def hf_sources(opt):
    return [s for s in opt.get("src") or [] if s.get("hf")]


def source_urls(opt, hf_bases, use_mirror=False):
    """
    [(显示名, 地址, 是否 Civitai)]，按顺序试，前一个出错就换下一个接着续传。
    国内网络：HF 镜像 → HF 官方 → Civitai；其他：HF 官方 → Civitai → HF 镜像。
    同一个文件在不同线路上的内容一样（sha256 在生成目录时核对过），换线路续传没问题。
    """
    hf_urls, civ_urls = [], []
    for s in opt.get("src") or []:
        if s.get("hf"):
            path = quote(s["path"], safe="/")
            for base in hf_bases:
                hf_urls.append((base.split("//", 1)[-1], f"{base}/{s['hf']}/resolve/main/{path}", False))
        elif s.get("civitai"):
            civ_urls.append(("civitai.com", CIVITAI_DOWNLOAD.format(vid=int(s["civitai"])), True))
    if use_mirror or not hf_urls:
        return hf_urls + civ_urls
    return hf_urls[:1] + civ_urls + hf_urls[1:]


def _h3_card_defaults(ver, ram_gb):
    """H3 专用卡片按内存给的默认档位（内存不到 40GB 降到 Q2），换成目录里对应的 GGUF 选项"""
    import h3_models
    want = h3_models.default_quants(ram_gb)
    out = {}
    for comp in ver["components"]:
        q = want.get(comp["id"])
        if q and any(o["id"] == f"gguf-{q}" for o in comp["options"]):
            out[comp["id"]] = f"gguf-{q}"
    return out


def public_view(ctx):
    """
    给界面用的目录：去掉下载地址 / 哈希（界面用不到，少传一点），每个版本带上默认精度、
    每个选项带上在这台电脑上的问题（50 系专用、要装插件…）。
    """
    cat = load()
    models = []
    for m in cat["models"]:
        # Forge Neo H3 上下 H3：照 H3 专用卡片的默认（GGUF，按内存挑 Q4 / Q2）
        h3_here = ctx["kind"] == "h3" and "h3" in (m.get("ui") or [])
        mctx = dict(ctx, prefer_gguf=h3_here)
        vers = []
        for v in m["versions"]:
            defaults = pick_defaults(v, mctx, m.get("ram_factor", 1.0))
            if h3_here:
                defaults.update(_h3_card_defaults(v, ctx.get("ram_gb")))
            comps = []
            for c in v["components"]:
                opts = [{"id": o["id"], "label": o["label"], "fmt": o["fmt"], "quant": o.get("quant"),
                         "file": o["file"], "size": o["size"], "note": o.get("note", ""),
                         "issue": option_issue(o, ctx)} for o in c["options"]]
                comps.append({"id": c["id"], "label": c["label"], "role": c.get("role"),
                              "required": c["required"], "default": c["default"], "desc": c.get("desc", ""),
                              "link": c.get("link"), "options": opts})
            vers.append({"id": v["id"], "label": v["label"], "note": v.get("note", ""), "default": bool(v.get("default")),
                         "supported": supported(m, v, ctx["kind"]),
                         "ui": [UI_LABELS.get(u, u) for u in ui_list(m, v)],
                         "defaults": defaults, "components": comps})
        models.append({"id": m["id"], "name": m["name"], "group": m["group"], "desc": m.get("desc", ""),
                       "tips": m.get("tips", ""), "license": m.get("license", ""), "link": m.get("link", ""),
                       "supported": any(x["supported"] for x in vers), "versions": vers})
    return {"groups": cat["groups"], "models": models, "updated": cat.get("updated", "")}
