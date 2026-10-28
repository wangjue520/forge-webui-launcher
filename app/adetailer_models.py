# -*- coding: utf-8 -*-
"""
替 ADetailer-Neo 备好检测模型，别让 WebUI 卡在「[ADetailer]: Loading Models...」。

ADetailer-Neo（Haoming02，Forge Neo 用的移植版）每次启动都会把缺的 10 个模型直接下载到
models/adetailer，下载地址写死：6 个在 huggingface.co（不认 HF_ENDPOINT，镜像设置对它无效）、
yolov8x-worldv2.pt（140MB）在 GitHub Release、3 个 mediapipe 模型在 storage.googleapis.com。
它用 torch.hub.download_url_to_file 下载，没有超时——国内连接卡在半路，WebUI 就一直停在
Loading Models，还每次留下一个 <文件名>.<uuid>.partial 半截文件（实际反馈里一个 yolov8x 留了 5 个）。

启动器的做法（启动 WebUI 前调用 prepare）：
  1. 清掉 models/adetailer 里的 .partial 半截文件
  2. 缺文件时在后台下载：按镜像设置排好来源（官方 / hf-mirror / GitHub 加速代理 /
     HuggingFace 上和官方 SHA256 一致的副本），有超时、.part 断点续传、下完校验
  3. 这次启动加 --ad-no-huggingface，让 ADetailer-Neo 不自己下载（不再卡住），
     直接用已有的模型；后台下好后，下次启动 WebUI 就能用上
--ad-no-huggingface 是 ADetailer-Neo 在 preload.py 里注册的参数，扩展被禁用时 WebUI 不认识它，
会直接报 unrecognized arguments 起不来，所以只在扩展启用时才加。
"""
import json
import os
import re
import threading
import time

FLAG = "--ad-no-huggingface"

HF = "https://huggingface.co"
HFM = "https://hf-mirror.com"
GHA = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8x-worldv2.pt"
GMP = "https://storage.googleapis.com/mediapipe-models"


def _hf(name):
    return {"official": [(f"{HF}/Bingsu/adetailer/resolve/main/{name}", None)],
            "mirror": [(f"{HFM}/Bingsu/adetailer/resolve/main/{name}", None)], "via": "hf"}


# ADetailer-Neo 的 TO_DOWNLOAD（lib_adetailer/detection/common.py）。size 只用来拦截下到一半就断的文件。
# mediapipe 的后备是 HuggingFace 上和 Google 官方文件 SHA256 一致的副本（2026-10 核对），下完必须校验
FILES = [
    dict(name="face_yolov8n.pt", size=6230011, **_hf("face_yolov8n.pt")),
    dict(name="face_yolov8s.pt", size=22507707, **_hf("face_yolov8s.pt")),
    dict(name="hand_yolov8n.pt", size=6237883, **_hf("hand_yolov8n.pt")),
    dict(name="hand_yolov8s.pt", size=22507643, **_hf("hand_yolov8s.pt")),
    dict(name="person_yolov8n-seg.pt", size=6777003, **_hf("person_yolov8n-seg.pt")),
    dict(name="person_yolov8s-seg.pt", size=23850731, **_hf("person_yolov8s-seg.pt")),
    dict(name="yolov8x-worldv2.pt", size=146355704, via="github",
         official=[(GHA, None)], mirror=[]),   # mirror 用 GitHub 加速代理，运行时从 mirror_manager 取
    dict(name="mediapipe_face_short.tflite", size=229746, via="hf",
         official=[(f"{GMP}/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite", None)],
         mirror=[(f"{HFM}/spaces/VisheshKB/esp32-mediapipe/resolve/main/blaze_face_short_range.tflite",
                  "b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f")]),
    dict(name="mediapipe_face_full.tflite", size=1083786, via="hf",
         official=[(f"{GMP}/face_detector/blaze_face_full_range/float16/latest/blaze_face_full_range.tflite", None)],
         mirror=[]),
    dict(name="face_landmarker.task", size=3758596, via="hf",
         official=[(f"{GMP}/face_landmarker/face_landmarker/float16/latest/face_landmarker.task", None)],
         mirror=[(f"{HFM}/Skulleton12/mediapipe-face-landmarker/resolve/main/face_landmarker.task",
                  "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"),
                 (f"{HFM}/trysem/facelandmarker/resolve/main/face_landmarker.task",
                  "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff")]),
]

# torch.hub 的半截文件：新版是 <文件名>.<32 位十六进制>.partial，老版是 NamedTemporaryFile 的 tmpxxxxxxxx
_PARTIAL_RE = re.compile(r"(\.[0-9a-f]{32}\.partial|^tmp[a-z0-9_]{6,})$", re.IGNORECASE)
_DISABLE_ARGS = ("--disable-all-extensions", "--disable-extra-extensions")

_lock = threading.Lock()
_busy = set()   # 正在后台下载的 models/adetailer 目录


def find_neo(root):
    """启用中的 ADetailer-Neo 扩展目录；没装 / 被禁用返回 None"""
    try:
        with open(os.path.join(root, "config.json"), encoding="utf-8") as f:
            ui = json.load(f)
    except (OSError, ValueError):
        ui = {}
    if str(ui.get("disable_all_extensions", "none")) != "none":
        return None
    disabled = set(ui.get("disabled_extensions") or [])
    ext_root = os.path.join(root, "extensions")
    try:
        names = sorted(os.listdir(ext_root))
    except OSError:
        return None
    for name in names:
        d = os.path.join(ext_root, name)
        if name not in disabled and os.path.isfile(os.path.join(d, "lib_adetailer", "detection", "common.py")):
            return d
    return None


def model_dir(root):
    return os.path.join(root, "models", "adetailer")


def cleanup_partials(d, min_age=120):
    """删掉 torch 留下的半截文件，返回 (个数, 字节数)。刚写过的（min_age 秒内）不碰，可能还在下"""
    n = size = 0
    try:
        entries = list(os.scandir(d))
    except OSError:
        return 0, 0
    now = time.time()
    for e in entries:
        if not e.is_file() or not _PARTIAL_RE.search(e.name):
            continue
        try:
            st = e.stat()
            if now - st.st_mtime < min_age:
                continue
            os.remove(e.path)
            n += 1
            size += st.st_size
        except OSError:
            pass
    return n, size


def missing(d):
    """缺的文件。ADetailer-Neo 只认文件在不在（它下载完才改名），这里同样只看存在与否"""
    return [f for f in FILES if not os.path.isfile(os.path.join(d, f["name"]))]


def sources(f, use_hf_mirror, use_gh_mirror, github_proxies=()):
    """[(url, sha256 或 None)]，按网络环境排好：国内镜像优先或官方优先"""
    mirror = list(f["mirror"])
    if f["via"] == "github":
        mirror = [(f"{p.rstrip('/')}/{u}", sha) for p in github_proxies for u, sha in f["official"]]
        first_mirror = use_gh_mirror
    else:
        first_mirror = use_hf_mirror
    return mirror + f["official"] if first_mirror else f["official"] + mirror


def prepare(root, cfg, log):
    """
    启动 WebUI 前调用。返回这次要追加的启动参数（"" 或 "--ad-no-huggingface"）。
    缺模型时在后台线程下载，不耽误启动。
    """
    extra = cfg.get("extra_args") or ""
    if any(a in extra for a in _DISABLE_ARGS) or not find_neo(root):
        return ""
    d = model_dir(root)
    n, size = cleanup_partials(d)
    if n:
        log(f"[启动器] 清掉了 models\\adetailer 里 {n} 个下到一半的 ADetailer 模型（{size / 1048576:.0f} MB），"
            "是之前启动时 ADetailer 自己下载卡住留下的\n")
    todo = missing(d)
    if not todo:
        return ""
    names = "、".join(f["name"] for f in todo)
    with _lock:
        started = d not in _busy
        if started:
            _busy.add(d)
    if started:
        log(f"[启动器] ADetailer 缺 {len(todo)} 个模型（{names}），启动器在后台下载，下好后下次启动 WebUI 生效\n")
        threading.Thread(target=_download_all, args=(d, todo, dict(cfg), log),
                         name="adetailer-models", daemon=True).start()
    else:
        log(f"[启动器] ADetailer 缺的模型还在后台下载中（{names}）\n")
    if FLAG in extra:
        return ""
    log(f"[启动器] 这次启动加 {FLAG}：ADetailer 不自己下载（它没有超时，网络不好会一直卡在 Loading Models），先用已有的模型\n")
    return FLAG


def _download_all(d, todo, cfg, log):
    try:
        import mirror_manager as mm
        try:
            use_hf = bool(mm.resolve_mode(cfg))
        except Exception:
            use_hf = False
        try:
            use_gh = bool(mm.resolve_github_mode(cfg))
        except Exception:
            use_gh = use_hf
        proxies = list(getattr(mm, "GITHUB_PROXIES", []))
        ok, failed = [], []
        for f in todo:
            if download_one(d, f, sources(f, use_hf, use_gh, proxies), log):
                ok.append(f["name"])
            else:
                failed.append(f["name"])
        if ok:
            log(f"[启动器] ADetailer 模型已下好：{'、'.join(ok)}（下次启动 WebUI 生效）\n")
        if failed:
            log(f"[启动器] ADetailer 模型没下成：{'、'.join(failed)}。下次启动会再试；"
                "这些只在选到对应的检测器时才用得到，其它功能照常\n")
    finally:
        with _lock:
            _busy.discard(d)


def download_one(d, f, srcs, log):
    """按顺序换来源下载一个文件，成功返回 True。.part 留着下次续传"""
    import civitai_downloader as cd
    name, size = f["name"], f["size"]
    for url, sha in srcs:
        host = url.split("/")[2]
        last = [0.0]

        def progress(done, total, _last=last):
            now = time.monotonic()
            if now - _last[0] >= 15 and total:
                _last[0] = now
                log(f"[启动器] ADetailer 模型 {name} 下载中 {done / 1048576:.1f} / {total / 1048576:.1f} MB（{host}）\n")

        info = {"downloadUrl": url, "name": name, "hashes": {"SHA256": sha} if sha else {}}
        try:
            path, hash_ok = cd.download_file(info, d, progress_cb=progress)
        except Exception as e:
            log(f"[启动器] ADetailer 模型 {name} 从 {host} 下载失败：{str(e)[:160]}\n")
            continue
        if hash_ok is False:
            _remove(path)
            log(f"[启动器] ADetailer 模型 {name} 从 {host} 下到的内容和官方不一致，已丢弃\n")
            continue
        if size and os.path.getsize(path) < size * 0.9:
            _remove(path)   # 连接被提前关掉、没报错就收尾的情况
            log(f"[启动器] ADetailer 模型 {name} 从 {host} 下载不完整，已丢弃\n")
            continue
        return True
    return False


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
