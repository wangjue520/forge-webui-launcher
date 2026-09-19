# -*- coding: utf-8 -*-
"""
Civitai 模型下载模块。

支持输入：
  - 模型页面网址   https://civitai.com/models/12345/some-name
  - 带版本号的网址 https://civitai.com/models/12345?modelVersionId=67890
  - 版本详情网址   https://civitai.com/models/12345/some-name?modelVersionId=67890
  - API 直链       https://civitai.com/api/download/models/67890
  - 纯数字 ID（当作 modelId 处理）

功能：
  1. 调用 Civitai 公开 API 获取模型/版本信息
  2. 根据 model.type 自动判断应存放的子文件夹
  3. 下载模型文件（带进度回调），校验 SHA256（若 API 提供了哈希）
  4. 保存 .civitai.info 元数据文件 和 预览图，约定与常见的 Civitai Helper 插件一致
"""
import os
import re
import json
import hashlib
import requests
from urllib.parse import urlparse

DEFAULT_API_HOST = "civitai.com"
API_BASE_TEMPLATE = "https://{host}/api/v1"


# Civitai 官方文档中的模型类型 -> Forge/A1111 默认模型文件夹的映射
# 注意：LyCORIS/LoCon 在不同版本的 webui 中位置可能不同（有的版本和 LORA 共用
# models/Lora 文件夹，有的独立成 models/LyCORIS），如果你的 webui 是后者，
# 请在下载完成后自行把文件移动过去，或者在 UI 里手动修改目标文件夹。
MODEL_TYPE_FOLDER_MAP = {
    "Checkpoint": "models/Stable-diffusion",
    "LORA": "models/Lora",
    "LoCon": "models/Lora",
    "DoRA": "models/Lora",
    "TextualInversion": "embeddings",
    "Hypernetwork": "models/hypernetwork",
    "AestheticGradient": "models/aesthetic_embeddings",
    "Controlnet": "models/ControlNet",
    "VAE": "models/VAE",
    "Upscaler": "models/ESRGAN",
    "MotionModule": "models/motion_module",
    "Poses": "models/Poses",
    "Wildcards": "models/wildcards",
    "Workflows": "models/Workflows",
    "Other": "models/Other",
}

DEFAULT_FOLDER = "models/Other"


class CivitaiError(Exception):
    pass


def parse_input(text):
    """
    从用户输入中解析出 modelId 和/或 modelVersionId，以及来源域名（host）。
    不写死 civitai.com —— 像 civitai.red 这类镜像/反代站点用的是同一套
    模型ID体系和一样的 URL 结构，所以只按路径/参数模式匹配，不限定域名。
    """
    text = text.strip()
    result = {"model_id": None, "version_id": None, "host": None}

    if "://" in text:
        try:
            result["host"] = urlparse(text).netloc or None
        except Exception:
            result["host"] = None

    m = re.search(r"/api/download/models/(\d+)", text)
    if m:
        result["version_id"] = m.group(1)
        return result

    m = re.search(r"/api/v1/model-versions/(\d+)", text)
    if m:
        result["version_id"] = m.group(1)
        return result

    m = re.search(r"/api/v1/models/(\d+)", text)
    if m:
        result["model_id"] = m.group(1)

    m = re.search(r"/models/(\d+)", text)
    if m:
        result["model_id"] = m.group(1)

    m = re.search(r"modelVersionId=(\d+)", text)
    if m:
        result["version_id"] = m.group(1)

    if not result["model_id"] and not result["version_id"] and text.isdigit():
        result["model_id"] = text

    if not result["model_id"] and not result["version_id"]:
        raise CivitaiError("无法从输入中识别出模型ID或版本ID，请确认粘贴的是完整的模型网址")

    return result


def _headers(api_key, url=None):
    """基础请求头。传 url 时按主机白名单决定是否附带 API Key：
    Authorization 只发给 civitai 自己的域名——API 返回的图片/下载地址可能
    指向站外主机，Bearer token 跟着过去就泄露了。"""
    headers = {"User-Agent": "ForgeLauncher/1.0"}
    if api_key and (url is None or _is_civitai_host(url)):
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _is_civitai_host(url):
    try:
        host = (url.split("//", 1)[1] if "//" in url else url).split("/", 1)[0].lower()
    except IndexError:
        return False
    return host == "civitai.com" or host.endswith(".civitai.com")


def fetch_version_info(parsed, api_key=None):
    """
    返回统一格式的版本信息 dict。
    优先使用输入网址所在的域名（例如 civitai.red 这类镜像站，假设其 /api/v1
    结构和官方一致）；如果该域名请求失败，自动回退到官方 civitai.com API
    重新尝试一次（因为模型ID/版本ID在镜像站和官方通常是同一套编号）。
    """
    hosts_to_try = []
    if parsed.get("host") and parsed["host"] != DEFAULT_API_HOST:
        hosts_to_try.append(parsed["host"])
    hosts_to_try.append(DEFAULT_API_HOST)

    last_err = None
    for host in hosts_to_try:
        try:
            return _fetch_version_info_from_host(parsed, host, api_key)
        except CivitaiError as e:
            last_err = e
            continue
        except requests.exceptions.RequestException as e:
            last_err = CivitaiError(f"连接 {host} 失败: {e}")
            continue
    raise last_err if last_err else CivitaiError("获取模型信息失败")


def _normalize_version_payload(data, model_name, model_type, model_id):
    """
    把 Civitai API 返回的一个"版本对象"（不管是来自 /model-versions/{id}、
    /models/{id} 里取出的第一个版本、还是 /model-versions/by-hash/{hash}——
    这三个接口返回的版本对象结构是一致的）统一整理成本模块内部通用的
    dict 格式，供 fetch_version_info / fetch_version_info_by_hash 共用，
    避免同一段解析逻辑抄两遍。
    """
    files = []
    for f in data.get("files", []):
        files.append({
            "name": f.get("name"),
            "sizeKB": f.get("sizeKB", 0),
            "downloadUrl": f.get("downloadUrl"),
            "primary": f.get("primary", False),
            "type": f.get("type", ""),
            "hashes": f.get("hashes", {}),
        })
    # 若没有明确标记 primary，默认第一个为 primary
    if files and not any(f["primary"] for f in files):
        files[0]["primary"] = True

    images = []
    for img in data.get("images", [])[:3]:
        u = img.get("url")
        if u:
            images.append(u)

    return {
        "model_name": model_name,
        "model_type": model_type,
        "version_name": data.get("name", ""),
        "version_id": data.get("id"),
        "model_id": model_id,
        "base_model": data.get("baseModel", ""),
        "files": files,
        "images": images,
        "raw": data,
    }


def _fetch_version_info_from_host(parsed, host, api_key):
    api_base = API_BASE_TEMPLATE.format(host=host)
    headers = _headers(api_key)

    if parsed.get("version_id"):
        url = f"{api_base}/model-versions/{parsed['version_id']}"
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 401:
            raise CivitaiError("需要 API Key 才能访问该资源（可能是需要登录/受限内容），请在设置里填写 API Key")
        if resp.status_code == 404:
            raise CivitaiError(f"在 {host} 上找不到该模型版本")
        resp.raise_for_status()
        data = resp.json()
        model = data.get("model", {})
        model_name = model.get("name", "未知模型")
        model_type = model.get("type", "Other")
        model_id = data.get("modelId")
    else:
        url = f"{api_base}/models/{parsed['model_id']}"
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 401:
            raise CivitaiError("需要 API Key 才能访问该资源（可能是需要登录/受限内容），请在设置里填写 API Key")
        if resp.status_code == 404:
            raise CivitaiError(f"在 {host} 上找不到该模型")
        resp.raise_for_status()
        mdata = resp.json()
        model_name = mdata.get("name", "未知模型")
        model_type = mdata.get("type", "Other")
        model_id = mdata.get("id")
        versions = mdata.get("modelVersions", [])
        if not versions:
            raise CivitaiError("该模型没有可用的版本")
        data = versions[0]  # 最新版本

    return _normalize_version_payload(data, model_name, model_type, model_id)


def fetch_version_info_by_hash(hash_value, api_key=None, host=None):
    """
    通过文件哈希（AutoV1/AutoV2/SHA256/CRC32/Blake3 均可，A1111/Forge 生成
    参数里存的 "Model hash"/"Lora hashes" 默认就是 AutoV2 十位十六进制值）
    直接查这是 Civitai 上的哪个模型版本——这是"从图片反查模型"这个功能的
    核心：图片元数据里的哈希就是模型文件内容的指纹，比按文件名猜准得多
    （文件名用户经常自己改，哈希不会变）。

    返回值跟 fetch_version_info 完全一样的 dict 格式，找不到匹配时抛
    CivitaiError（这是正常情况——很多本地模型压根没上传过 Civitai，
    调用方应该把这个当成"查不到"而不是网络错误处理）。
    """
    host = host or DEFAULT_API_HOST
    api_base = API_BASE_TEMPLATE.format(host=host)
    headers = _headers(api_key)
    url = f"{api_base}/model-versions/by-hash/{hash_value}"
    try:
        resp = requests.get(url, headers=headers, timeout=20)
    except requests.exceptions.RequestException as e:
        raise CivitaiError(f"连接 {host} 失败: {e}")
    if resp.status_code == 404:
        raise CivitaiError(f"在 Civitai 上找不到哈希 {hash_value} 对应的模型（可能是本地专属/未公开发布的模型）")
    if resp.status_code == 401:
        raise CivitaiError("需要 API Key 才能访问该资源，请在设置里填写 API Key")
    resp.raise_for_status()
    data = resp.json()
    model = data.get("model", {})
    return _normalize_version_payload(
        data, model.get("name", "未知模型"), model.get("type", "Other"), data.get("modelId"),
    )


def search_models_by_name(query, api_key=None, limit=6, host=None):
    """
    按名字模糊搜索 Civitai——只在"没有哈希可查"（典型场景是 ComfyUI 图，
    元数据里通常不存哈希）时作为退路使用。

    这条路径不保证精确匹配：Civitai 上同名/近似名称的模型并不少见，
    模糊搜索排第一的结果不一定就是原图实际用的那个。跟按哈希查询
    （fetch_version_info_by_hash，哈希是文件内容的指纹，基本不会认错）
    完全不是一个可信等级——调用方必须让用户从候选列表里手动确认要下载
    哪一个，绝不能自动挑第一个结果就下载，这一点由 UI 层负责保证，这里
    只负责如实返回候选列表。
    """
    host = host or DEFAULT_API_HOST
    api_base = API_BASE_TEMPLATE.format(host=host)
    headers = _headers(api_key)
    try:
        resp = requests.get(f"{api_base}/models", headers=headers,
                             params={"query": query, "limit": limit}, timeout=20)
    except requests.exceptions.RequestException as e:
        raise CivitaiError(f"连接 {host} 失败: {e}")
    if resp.status_code == 401:
        raise CivitaiError("需要 API Key 才能访问该资源，请在设置里填写 API Key")
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("items", []):
        versions = item.get("modelVersions") or []
        if not versions:
            continue
        results.append(_normalize_version_payload(
            versions[0], item.get("name", "未知模型"), item.get("type", "Other"), item.get("id"),
        ))
    return results


def guess_folder(model_type):
    return MODEL_TYPE_FOLDER_MAP.get(model_type, DEFAULT_FOLDER)


def download_file(file_info, dest_dir, api_key=None, progress_cb=None, cancel_flag=None,
                  extra_headers=None):
    """
    下载单个文件到 dest_dir，支持断点续传：
    中断/取消后残留的是 {文件名}.part，下次下载自动从断点继续
    （向服务器发 Range 请求，服务器支持就追加，不支持就重头下）。

    哈希校验失败不会把损坏文件放到最终路径——会改名成 {文件名}.broken
    保留现场（免得用户拿着一个损坏的 6GB checkpoint 反复排查"为什么加载报错"），
    并以 hash_ok=False 返回；确认损坏后重下或手动删除即可。

    progress_cb(downloaded_bytes, total_bytes) 会被周期性调用。
    cancel_flag 是一个 callable，返回 True 时中止下载（.part 保留，可续传）。
    extra_headers：额外请求头（liblib 用它带 usertoken 登录凭证）。
    返回 (最终保存路径或 .broken 路径, hash_ok)。
    """
    os.makedirs(dest_dir, exist_ok=True)
    url = file_info["downloadUrl"]
    # 文件名只取单层 basename：API/镜像返回的名字若带路径分隔符或 ..，
    # 直接 join 会写出下载目录（目录穿越覆盖任意文件）
    filename = os.path.basename(str(file_info["name"]).replace("\\", "/")).strip()
    if not filename or filename in (".", ".."):
        raise CivitaiError(f"服务器返回了非法的文件名: {file_info.get('name')!r}")
    final_path = os.path.join(dest_dir, filename)
    tmp_path = final_path + ".part"

    headers = _headers(api_key, url)
    if extra_headers:
        headers.update(extra_headers)
    resume_from = 0
    if os.path.exists(tmp_path):
        resume_from = os.path.getsize(tmp_path)

    resp_ctx = None
    for attempt in range(2):  # 第一次带 Range 续传；服务器拒绝（416）就重头下
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
        else:
            headers.pop("Range", None)
        resp_ctx = requests.get(url, headers=headers, stream=True, timeout=60,
                                allow_redirects=True)
        if resp_ctx.status_code == 416 and resume_from > 0:
            # 服务端认为范围无效（文件已变/不支持续传），删掉残块重新下
            resp_ctx.close()
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            resume_from = 0
            continue
        break

    with resp_ctx as resp:
        if resp.status_code == 401:
            resp.close()
            raise CivitaiError("下载被拒绝（401）。该文件可能需要登录 Civitai 账号获取的 API Key 才能下载")
        resp.raise_for_status()
        # liblib 的下载端点出错时返回的是 200 + JSON 错误体（如"用户未登录"），
        # 不拦一下的话会把这段 JSON 当成模型文件写进 .part，最后变成莫名其妙的
        # 「哈希校验失败」。
        if "json" in (resp.headers.get("content-type") or "").lower():
            # 流式只读前 4KB：resp.content 会把整个响应体先读进内存，
            # 异常服务器持续输出时可能耗尽内存
            body = b""
            for chunk in resp.iter_content(chunk_size=4096):
                body = chunk[:4096]
                break
            resp.close()
            msg = None
            try:
                msg = json.loads(body.decode("utf-8", "ignore")).get("msg")
            except Exception:
                pass
            raise CivitaiError(msg or "服务器返回了错误信息而不是文件（可能需要登录）")
        resumed = resp.status_code == 206 and resume_from > 0
        downloaded = resume_from if resumed else 0
        total = downloaded + (int(resp.headers.get("content-length", 0))
                              or max(0, file_info.get("sizeKB", 0) * 1024 - downloaded))
        sha256 = hashlib.sha256()
        if resumed:
            # 续传：先把本地已有部分喂进哈希，再追加新内容
            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024 * 4)
                    if not chunk:
                        break
                    sha256.update(chunk)
            if progress_cb:
                progress_cb(downloaded, total)
        with open(tmp_path, "ab" if resumed else "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if cancel_flag and cancel_flag():
                    # .part 保留——下次继续，这就是断点续传的意义
                    raise CivitaiError(f"下载已取消（已下载 {downloaded / 1048576:.0f} MB，"
                                       "再次下载会从中断处继续）")
                if not chunk:
                    continue
                f.write(chunk)
                sha256.update(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)

    expected_hash = (file_info.get("hashes") or {}).get("SHA256")
    hash_ok = None
    if expected_hash:
        hash_ok = sha256.hexdigest().lower() == expected_hash.lower()

    if hash_ok is False:
        # 校验失败：不污染最终路径，改名 .broken 保留现场
        broken_path = final_path + ".broken"
        if os.path.exists(broken_path):
            os.remove(broken_path)
        os.replace(tmp_path, broken_path)
        return broken_path, False

    os.replace(tmp_path, final_path)
    return final_path, hash_ok


def save_sidecar_metadata(final_path, version_info, file_info):
    """保存 .civitai.info 元数据 json，格式参考常见 Civitai Helper 类插件的约定"""
    info_path = os.path.splitext(final_path)[0] + ".civitai.info"
    raw = version_info.get("raw") or {}
    trained = raw.get("trainedWords") or []
    if isinstance(trained, str):
        trained = [trained]
    payload = {
        "modelId": version_info.get("model_id"),
        "modelName": version_info.get("model_name"),
        "modelType": version_info.get("model_type"),
        "versionId": version_info.get("version_id"),
        "versionName": version_info.get("version_name"),
        "baseModel": version_info.get("base_model"),
        "fileName": file_info.get("name"),
        "hashes": file_info.get("hashes"),
        # 多组触发词（Civitai trainedWords 本身就是列表，每组一个字符串）
        "trainedWords": [str(w) for w in trained if str(w).strip()],
        "trainedWordsSource": "civitai" if trained else "",
    }
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return info_path


def download_preview_image(final_path, version_info, api_key=None):
    """下载第一张预览图，保存为 {文件名}.preview.png"""
    images = version_info.get("images") or []
    if not images:
        return None
    preview_path = os.path.splitext(final_path)[0] + ".preview.png"
    headers = _headers(api_key, images[0])
    try:
        resp = requests.get(images[0], headers=headers, timeout=30)
        resp.raise_for_status()
        with open(preview_path, "wb") as f:
            f.write(resp.content)
        return preview_path
    except Exception:
        return None
