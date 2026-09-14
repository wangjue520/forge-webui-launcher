# -*- coding: utf-8 -*-
"""
liblib（哩布哩布）模型信息查询。

liblib 没有公开的「按文件哈希查模型」官方 API，这里用的是它网页前端自用的
两个内部接口（无需登录，直接请求即可）：

  - GET  https://www.liblib.art/api/www/model-version/hash/{sha256}
        按文件 SHA256 查模型版本。查不到时返回 {"code":0, "msg":"hash值无法找到模型", "data":null}

  - POST https://www.liblib.art/api/www/model/getByUuid/{modelUuid}
        按模型 UUID 拿完整信息（版本列表、触发词、底模等），用于「粘贴链接绑定」。

接口没有文档、随时可能变，所以解析写得比较保守：字段缺失就当查不到/部分可用，
绝不让异常直接砸到调用方（调用方把 Exception 当"liblib 不可用"忽略即可）。
"""
import re
import json
import requests

API_BASE = "https://www.liblib.art/api/www"
PAGE_BASE = "https://www.liblib.art/modelinfo/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Content-Type": "application/json",
    "Referer": "https://www.liblib.art/",
}


class LiblibError(Exception):
    pass


def parse_url(text):
    """
    从 liblib 链接里解析模型 UUID 和版本 UUID。
    支持：
      https://www.liblib.art/modelinfo/{modelUuid}
      https://www.liblib.art/modelinfo/{modelUuid}?versionUuid={versionUuid}
      （www.liblib.ai 旧域名也认）
    """
    text = (text or "").strip()
    m = re.search(r"/modelinfo/([0-9a-f]{32})", text, re.I)
    if not m:
        raise LiblibError("无法从输入中识别 liblib 模型链接，"
                          "应形如 https://www.liblib.art/modelinfo/xxxx")
    v = re.search(r"[?&]versionUuid=([0-9a-f]{32})", text, re.I)
    return {"model_uuid": m.group(1).lower(),
            "version_uuid": v.group(1).lower() if v else None}


def page_url(model_uuid, version_uuid=None):
    u = PAGE_BASE + model_uuid
    if version_uuid:
        u += f"?versionUuid={version_uuid}"
    return u


def extract_trigger_words(version):
    """
    从版本对象里提取触发词。triggerWord 在站点数据里可能是：
    字符串列表 / 逗号分隔字符串 / None。统一返回「多组触发词」的列表，
    每组一个字符串（保留组内逗号分隔的原样，因为有些模型一组要一起用）。
    """
    tw = version.get("triggerWord")
    if not tw:
        return []
    if isinstance(tw, str):
        tw = [tw]
    if not isinstance(tw, list):
        return []
    return [str(w).strip() for w in tw if str(w or "").strip()]


def _base_type_of(version):
    # 只认字符串型的 baseTypeName；baseType 是数值编码（如 19=FLUX），
    # 没有对照表，直接显示数字比显示"未知"更费解
    bt = version.get("baseTypeName")
    return bt if isinstance(bt, str) and bt.strip() else ""


def query_by_hash(sha256_hex):
    """
    按文件 SHA256 查 liblib。命中返回统一格式 dict，未命中返回 None。
    网络/解析异常直接抛给调用方（调用方按"liblib 不可用"降级）。
    """
    sha = (sha256_hex or "").strip().lower()
    if not sha:
        return None
    resp = requests.get(f"{API_BASE}/model-version/hash/{sha}",
                        headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data")
    if not data or not isinstance(data, dict):
        return None

    # 命中的返回结构没有文档，按网页端版本对象的常见字段防御式提取
    model_uuid = (data.get("modelUuid") or data.get("modelId") or "")
    version_uuid = (data.get("uuid") or data.get("versionUuid") or "")
    model_name = (data.get("modelName") or data.get("model", {}).get("name")
                  if isinstance(data.get("model"), dict) else data.get("modelName")) or ""
    return {
        "source": "liblib",
        "model_uuid": str(model_uuid) if model_uuid else "",
        "version_uuid": str(version_uuid) if version_uuid else "",
        "model_name": model_name or data.get("name", "") or "",
        "version_name": data.get("name", "") or data.get("versionName", "") or "",
        "base_model": _base_type_of(data),
        "trigger_words": extract_trigger_words(data),
        "page_url": page_url(str(model_uuid), str(version_uuid)) if model_uuid else "",
    }


def fetch_model(model_uuid, version_uuid=None):
    """
    按模型 UUID 拉完整信息（粘贴链接绑定用）。返回统一格式 dict，
    trigger_words 取指定版本（未指定则第一个版本）。
    """
    resp = requests.post(f"{API_BASE}/model/getByUuid/{model_uuid}",
                         headers=_HEADERS, json={}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise LiblibError(f"liblib 返回错误: {payload.get('msg') or payload.get('code')}")
    data = payload["data"]
    versions = data.get("versions") or []
    chosen = None
    if version_uuid:
        for v in versions:
            if (v.get("uuid") or "").lower() == version_uuid.lower():
                chosen = v
                break
    if chosen is None and versions:
        chosen = versions[0]
    chosen = chosen or {}
    vuuid = chosen.get("uuid") or version_uuid or ""
    return {
        "source": "liblib",
        "model_uuid": model_uuid,
        "version_uuid": vuuid,
        "model_name": data.get("name", "") or "",
        "version_name": chosen.get("name", "") or "",
        "base_model": _base_type_of(chosen),
        "trigger_words": extract_trigger_words(chosen),
        "page_url": page_url(model_uuid, vuuid),
    }


# ============================================================
# 模型下载页支持（获取信息 + 登录后下载）
# ============================================================

def _attachment_of(version):
    att = version.get("attachment")
    return att if isinstance(att, dict) else {}


def _version_entry(version):
    """把 getByUuid 返回里的一个版本对象整理成统一条目。"""
    att = _attachment_of(version)
    try:
        size = int(att.get("modelSourceSize") or 0)
    except (TypeError, ValueError):
        size = 0
    vip_used = bool(version.get("vipUsed"))
    exclusive = bool(version.get("exclusive"))
    return {
        "version_uuid": version.get("uuid") or "",
        "version_name": version.get("name") or "",
        "base_model": _base_type_of(version),
        "trigger_words": extract_trigger_words(version),
        "file_name": att.get("modelSourceName") or "",
        "file_size": size,                       # 字节
        "sha256": (att.get("modelSourceHash") or "").strip().lower(),
        # 会员/独家模型经常不给 modelSource（None），给了也可能仍被服务端拒
        "download_url": att.get("modelSource") or "",
        "vip_used": vip_used,
        "exclusive": exclusive,
    }


def fetch_download_info(model_uuid, version_uuid=None):
    """
    给模型下载页用：拉完整模型信息 + 每个版本的附件（文件名/大小/SHA256/下载地址）。
    version_uuid 指定时把对应版本排在最前并标记 chosen。
    """
    resp = requests.post(f"{API_BASE}/model/getByUuid/{model_uuid}",
                         headers=_HEADERS, json={}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise LiblibError(f"liblib 返回错误: {payload.get('msg') or payload.get('code')}")
    data = payload["data"]
    versions = [_version_entry(v) for v in (data.get("versions") or [])
                if isinstance(v, dict)]
    if not versions:
        raise LiblibError("该模型下没有任何版本")
    chosen = 0
    if version_uuid:
        for i, v in enumerate(versions):
            if (v["version_uuid"] or "").lower() == version_uuid.lower():
                chosen = i
                break
    return {
        "source": "liblib",
        "model_uuid": model_uuid,
        "model_name": data.get("name") or "",
        "versions": versions,
        "chosen": chosen,
        "page_url": page_url(model_uuid, versions[chosen]["version_uuid"]),
    }


def guess_folder_by_size(size_bytes, file_name=""):
    """
    liblib 的接口里 LoRA 和大模型的类型编码是一样的（modelType=5 两边都在用），
    只能靠文件大小猜：>= 1.5GB 按大模型，否则按 LoRA。猜错无所谓——
    下载页的保存位置可以手动改。
    返回 (相对文件夹, 类型显示名)。
    """
    name = (file_name or "").lower()
    if "vae" in name:
        return "models/VAE", "VAE（按文件名猜测）"
    if size_bytes >= 1536 * 1024 * 1024:
        return "models/Stable-diffusion", "Checkpoint 大模型（按文件大小猜测）"
    return "models/Lora", "LoRA（按文件大小猜测）"


def download_headers(token):
    """
    liblib 下载要求的登录凭证。网页端是 axios 拦截器统一加 token 头，
    cookie 里的 usertoken 也认，两个都给上最稳。
    """
    h = dict(_HEADERS)
    h.pop("Content-Type", None)  # GET 下载不带 JSON content-type
    if token:
        h["token"] = token
        h["Cookie"] = f"usertoken={token}"
    return h


def fetch_user_info(token):
    """验证 usertoken 是否有效。有效返回用户昵称，无效抛 LiblibError。"""
    token = (token or "").strip()
    if not token:
        raise LiblibError("token 为空")
    resp = requests.post(f"{API_BASE}/user/getUserInfo", json={},
                         headers={**_HEADERS, "token": token}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise LiblibError("usertoken 无效或已过期（liblib 返回：未登录），"
                          "请重新从浏览器 Cookie 里复制最新的值")
    data = payload["data"]
    return data.get("nickname") or data.get("name") or data.get("uuid") or ""
