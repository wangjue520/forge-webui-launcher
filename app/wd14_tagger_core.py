# -*- coding: utf-8 -*-
"""
WD14 反推标签核心逻辑，直接用 onnxruntime 跑 SmilingWolf 系列模型，
不依赖任何 webui 扩展，跟 Forge 是 Classic 还是 Neo 完全无关——
这就是它能绕开"Neo 装不了 WD14 插件"这个问题的原因。

模型来源：SmilingWolf 在 HuggingFace 上发布的 wd-*-tagger-v3 系列，
每个仓库都包含 model.onnx + selected_tags.csv 两个文件，直接匿名
HTTP 下载即可，不需要装 huggingface_hub 这个额外的库。

selected_tags.csv 的列结构（这是这一系列模型通用、社区广泛验证过的格式）：
    tag_id, name, category, count
category 含义：9 = 评分(rating), 0 = 常规标签(general), 4 = 角色(character)
"""
import csv
import io
import os
import time

import requests

# numpy / PIL 故意不放在顶层 import——这个模块的 is_model_cached /
# download_model / model_cache_dir 这几个函数在启动器主进程里也会被调用，
# 只需要 requests 就够了。numpy/PIL/onnxruntime 只有真正做图像预处理和推理
# 时才需要，全部挪到用到的地方再 import，这样主进程 import 这个模块本身
# 不会连带装/加载 numpy 等依赖，跟隔离 venv 的边界更干净。

HF_BASE = "https://huggingface.co"

# 提供两个规格不同的模型可选：一个偏快偏小，一个偏准偏大
MODEL_CATALOG = {
    "wd-vit-tagger-v3": {
        "label": "wd-vit-tagger-v3（默认，速度快，~380MB）",
        "repo": "SmilingWolf/wd-vit-tagger-v3",
    },
    "wd-eva02-large-tagger-v3": {
        "label": "wd-eva02-large-tagger-v3（精度更高，体积更大，速度更慢）",
        "repo": "SmilingWolf/wd-eva02-large-tagger-v3",
    },
}

# 这几个标签本身就带下划线含义（颜文字等），替换下划线为空格时要跳过，
# 是社区里 WD14 类工具通用的一份小豁免表
_KAOMOJI_TAGS = {
    "0_0", "(o)_(o)", "+_+", "+_-", "._.", "<o>_<o>", "<|>_<|>",
    "=_=", ">_<", "3_3", "6_9", ">_o", "@_@", "^_^", "o_o", "u_u",
    "x_x", "|_|", "||_||",
}


class WD14TaggerError(Exception):
    pass


def model_cache_dir(base_dir, model_key):
    return os.path.join(base_dir, "wd_tagger_models", model_key)


def is_model_cached(base_dir, model_key):
    d = model_cache_dir(base_dir, model_key)
    return os.path.exists(os.path.join(d, "model.onnx")) and os.path.exists(os.path.join(d, "selected_tags.csv"))


# ============================================================
# 模型下载
# ============================================================

# HuggingFace 的下载端点候选，按顺序尝试。
#
# 这一块是被实际反馈逼出来的：原来只有「国内就用 hf-mirror，否则直连官方」
# 这一条路，一旦判错（比如挂了代理但代理不走 HF，或者 hf-mirror 当天抽风）
# 就直接失败，用户看到的只有一句干巴巴的超时。
#
# 现在改成「所有端点依次试一遍」，跟网络环境探测结果无关 —— 探测只决定
# 谁排前面。反正失败一个就换下一个，多试几次的代价远小于卡住不动。
_HF_ENDPOINTS = [
    ("https://hf-mirror.com", "hf-mirror 国内镜像"),
    ("https://huggingface.co", "HuggingFace 官方"),
]

# 单个端点内部的重试次数。大文件（eva02-large 的 onnx 有 1.2GB）在国内
# 网络下中途断流很常见，配合断点续传重试几次通常就能拿下。
_RETRY_PER_ENDPOINT = 3
_CONNECT_TIMEOUT = 15
_READ_TIMEOUT = 60


def _endpoint_order(cfg, log_cb=None):
    """决定端点尝试顺序：国内网络把镜像排前面，否则官方优先。"""
    order = list(_HF_ENDPOINTS)
    use_mirror = True   # 探测不出来时默认镜像优先——国内用户占绝大多数
    if cfg is not None:
        try:
            import mirror_manager as mm
            use_mirror = mm.resolve_mode(cfg, log_cb)
        except Exception:
            pass
    if not use_mirror:
        order.reverse()
    return order


def _download_one(url, dest, progress_cb=None, log_cb=None, cancel_flag=None, filename=""):
    """
    带断点续传的单文件下载。已有 .part 就从断点继续，服务端不支持 Range
    （返回 200 而不是 206）就老老实实从头来。
    """
    tmp = dest + ".part"
    done = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    headers = {"User-Agent": "forge-launcher-wd14/1.0"}
    if done:
        headers["Range"] = f"bytes={done}-"
        if log_cb:
            log_cb(f"  从 {done // 1048576} MB 处续传 ...")

    with requests.get(url, stream=True, headers=headers,
                      timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)) as resp:
        if done and resp.status_code == 200:
            # 服务端不认 Range，之前下的那截作废
            done = 0
            mode = "wb"
        elif done and resp.status_code == 206:
            mode = "ab"
        else:
            done = 0
            mode = "wb"
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0)) + done
        with open(tmp, mode) as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if cancel_flag and cancel_flag():
                    raise WD14TaggerError("下载已取消")
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(filename, done, total)

    if total and os.path.getsize(tmp) < total:
        raise IOError(f"文件不完整（{os.path.getsize(tmp)}/{total} 字节）")
    os.replace(tmp, dest)


def download_model(base_dir, model_key, progress_cb=None, log_cb=None, cancel_flag=None, cfg=None):
    if model_key not in MODEL_CATALOG:
        raise WD14TaggerError(f"未知的模型: {model_key}")
    repo = MODEL_CATALOG[model_key]["repo"]
    target_dir = model_cache_dir(base_dir, model_key)
    os.makedirs(target_dir, exist_ok=True)

    endpoints = _endpoint_order(cfg, log_cb)
    failures = []

    for filename in ("selected_tags.csv", "model.onnx"):
        dest = os.path.join(target_dir, filename)
        if os.path.exists(dest):
            continue
        if log_cb:
            log_cb(f"正在下载 {filename} ...")

        ok = False
        for base, label in endpoints:
            url = f"{base}/{repo}/resolve/main/{filename}"
            for attempt in range(1, _RETRY_PER_ENDPOINT + 1):
                if cancel_flag and cancel_flag():
                    raise WD14TaggerError("下载已取消")
                try:
                    if log_cb and (attempt > 1 or label != endpoints[0][1]):
                        log_cb(f"  尝试 {label}（第 {attempt} 次）...")
                    _download_one(url, dest, progress_cb, log_cb, cancel_flag, filename)
                    ok = True
                    break
                except WD14TaggerError:
                    raise                      # 取消，直接往外抛
                except Exception as e:
                    msg = f"{label}: {type(e).__name__}: {str(e)[:120]}"
                    failures.append(msg)
                    if log_cb:
                        log_cb(f"  失败 —— {msg}")
                    if attempt < _RETRY_PER_ENDPOINT:
                        time.sleep(2 * attempt)   # 简单退避，别把对面打死
            if ok:
                break

        if not ok:
            raise WD14TaggerError(_download_help_text(model_key, repo, filename, failures))

    return target_dir


def _download_help_text(model_key, repo, filename, failures):
    """下载彻底失败时给一段能照着做的中文说明，而不是甩个英文异常。"""
    tail = "\n".join("  · " + f for f in failures[-6:])
    return (
        f"模型文件 {filename} 下载失败，所有下载源都试过了。\n\n"
        f"失败记录：\n{tail}\n\n"
        "可以这样解决：\n"
        "1) 如果你在用代理/加速器，先确认它对 huggingface.co 生效，然后重试；\n"
        "2) 到「高级选项」页把网络模式改成「总是使用国内镜像」再重试；\n"
        "3) 实在下不动就手动下载 —— 用浏览器打开下面任一地址，\n"
        f"   把 model.onnx 和 selected_tags.csv 两个文件下到同一个文件夹里，\n"
        f"   再回到本页点「手动导入模型」选中那个文件夹即可：\n"
        f"   https://hf-mirror.com/{repo}/tree/main\n"
        f"   https://huggingface.co/{repo}/tree/main\n"
        f"   （国内一般第一个能开，用迅雷之类的下载工具拖 model.onnx 会快很多）"
    )


def model_repo_urls(model_key):
    """给界面上「手动下载」按钮用的仓库页面地址。"""
    repo = MODEL_CATALOG.get(model_key, {}).get("repo", "")
    if not repo:
        return []
    return [f"https://hf-mirror.com/{repo}/tree/main",
            f"https://huggingface.co/{repo}/tree/main"]


def import_model_files(base_dir, model_key, src_dir):
    """
    把用户自己下好的模型文件导入缓存目录。

    只认 model.onnx + selected_tags.csv 这两个名字，但允许它们躺在
    src_dir 的任意一层子目录里 —— 浏览器下载下来经常带一层同名文件夹，
    让用户自己去翻是没必要的麻烦。
    """
    import shutil

    if model_key not in MODEL_CATALOG:
        raise WD14TaggerError(f"未知的模型: {model_key}")
    if not src_dir or not os.path.isdir(src_dir):
        raise WD14TaggerError("选择的文件夹不存在")

    wanted = {"model.onnx": None, "selected_tags.csv": None}
    for dirpath, _dirs, files in os.walk(src_dir):
        for fn in files:
            low = fn.lower()
            if low in wanted and wanted[low] is None:
                wanted[low] = os.path.join(dirpath, fn)
        if all(wanted.values()):
            break

    missing = [k for k, v in wanted.items() if v is None]
    if missing:
        raise WD14TaggerError(
            "选中的文件夹里缺少：" + "、".join(missing) +
            "\n\n请确认这两个文件都已经下载完成，并且放在同一个文件夹里。")

    # 体积明显不对的话多半是下了个 HTML 错误页或者没下完
    if os.path.getsize(wanted["model.onnx"]) < 1024 * 1024:
        raise WD14TaggerError("model.onnx 体积异常偏小，多半没下完或下到了错误页面，请重新下载。")

    target_dir = model_cache_dir(base_dir, model_key)
    os.makedirs(target_dir, exist_ok=True)
    for name, src in wanted.items():
        dst = os.path.join(target_dir, name)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
    return target_dir


def load_labels(model_dir):
    csv_path = os.path.join(model_dir, "selected_tags.csv")
    tag_names = []
    rating_indexes, general_indexes, character_indexes = [], [], []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            tag_names.append(row["name"])
            category = int(row["category"])
            if category == 9:
                rating_indexes.append(i)
            elif category == 4:
                character_indexes.append(i)
            else:
                general_indexes.append(i)
    return tag_names, rating_indexes, general_indexes, character_indexes


def prepare_image(pil_image, target_size):
    """
    标准 WD14 系列预处理：转 RGB 贴白底 -> BGR -> 填充成正方形(白边) -> 缩放 -> float32
    """
    import numpy as np
    from PIL import Image

    image = pil_image.convert("RGBA")
    canvas = Image.new("RGBA", image.size, (255, 255, 255))
    canvas.paste(image, mask=image)
    image = canvas.convert("RGB")

    arr = np.asarray(image)
    arr = arr[:, :, ::-1]  # RGB -> BGR

    h, w = arr.shape[:2]
    size = max(h, w)
    pad_y, pad_x = size - h, size - w
    top, left = pad_y // 2, pad_x // 2
    arr = np.pad(
        arr,
        ((top, pad_y - top), (left, pad_x - left), (0, 0)),
        mode="constant",
        constant_values=255,
    )

    resized = Image.fromarray(arr).resize((target_size, target_size), Image.BICUBIC)
    arr = np.asarray(resized).astype(np.float32)
    return np.expand_dims(arr, 0)


class WD14Tagger:
    """加载一次模型后可反复调用 tag_image()，避免每张图都重新加载 onnx 模型"""

    def __init__(self, model_dir):
        import onnxruntime as ort
        self.session = ort.InferenceSession(model_dir_onnx_path(model_dir), providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        shape = self.session.get_inputs()[0].shape
        # shape 通常是 [None, H, W, 3] 或固定值；找不到就退回常见默认值 448
        dims = [d for d in shape if isinstance(d, int) and d > 1]
        self.target_size = dims[0] if dims else 448
        self.tag_names, self.rating_idx, self.general_idx, self.character_idx = load_labels(model_dir)

    def tag_image(self, pil_image, general_thresh=0.35, character_thresh=0.85, include_rating=False):
        batch = prepare_image(pil_image, self.target_size)
        probs = self.session.run([self.output_name], {self.input_name: batch})[0][0]

        result = {}
        if include_rating and self.rating_idx:
            best_i = max(self.rating_idx, key=lambda i: probs[i])
            result["rating"] = (self.tag_names[best_i], float(probs[best_i]))

        general = [(self.tag_names[i], float(probs[i])) for i in self.general_idx if probs[i] > general_thresh]
        general.sort(key=lambda x: x[1], reverse=True)

        character = [(self.tag_names[i], float(probs[i])) for i in self.character_idx if probs[i] > character_thresh]
        character.sort(key=lambda x: x[1], reverse=True)

        def _clean(name):
            return name if name in _KAOMOJI_TAGS else name.replace("_", " ")

        ordered_tags = [_clean(n) for n, _ in character] + [_clean(n) for n, _ in general]
        result["tags"] = ordered_tags
        result["tag_string"] = ", ".join(ordered_tags)
        result["general"] = general
        result["character"] = character
        return result


def model_dir_onnx_path(model_dir):
    return os.path.join(model_dir, "model.onnx")
