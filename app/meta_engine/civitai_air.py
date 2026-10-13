"""
civitai_air.py - Civitai AIR (Asset Identification Record) 资源提取器

从提示词/工作流/参数文本中扫描:
1. "Civitai resources: [{...}]" 标记后的 JSON 数组 (ComfyUI 前端 / 部分插件写法), 元素形如
   {"air": "urn:air:sd1:lora:civitai:123456@7890", "modelName": "...", "versionName": "...",
    "weight": 0.8}
2. 裸 AIR URN: urn:air:<baseModel>:<type>:civitai:<model_id>@<version_id>

产出结构化列表 (挂到 ImageMetadata.civitai_resources):
  {"model_name", "version_name", "weight", "air", "model_id", "version_id",
   "model_type", "civitai_url"}
按 (air / model_name+version_name) 去重。所有输入畸形均静默返回空/跳过, 不抛异常。
"""
import json
import re
from typing import Any, Dict, List, Optional, Set

# urn:air:<base>:<type>:civitai:<model_id>@<version_id>
_AIR_URN_RE = re.compile(
    r"urn:air:([A-Za-z0-9_\-]+):([A-Za-z0-9_\-]+):civitai:(\d+)@(\d+)", re.IGNORECASE)
# 字符串级廉价预检标记 (任一命中才做正则/JSON 解析)
_MARKER_TEXT = "Civitai resources:"
_MARKER_URN = "urn:air:"


def text_has_marker(text: Any) -> bool:
    """廉价预检: 字符串中是否可能出现 AIR 资源 (C 级子串扫描)"""
    return isinstance(text, str) and (_MARKER_URN in text or _MARKER_TEXT in text)


def _civitai_url(model_id: int, version_id: int) -> str:
    return f"https://civitai.com/models/{model_id}?modelVersionId={version_id}"


def _build_resource(air: str, model_id: int, version_id: int, model_type: str,
                    model_name: Optional[str] = None,
                    version_name: Optional[str] = None,
                    weight: Optional[float] = None) -> Dict[str, Any]:
    return {
        "model_name": (model_name or "").strip() or f"Civitai model {model_id}",
        "version_name": (version_name or "").strip() or None,
        "weight": weight,
        "air": air,
        "model_id": model_id,
        "version_id": version_id,
        "model_type": (model_type or "").strip().lower() or None,
        "civitai_url": _civitai_url(model_id, version_id),
    }


def _dedup_key(res: Dict[str, Any]) -> tuple:
    if res.get("air"):
        return ("air", res["air"].lower())
    return ("nv", res.get("model_name", ""), res.get("version_name") or "")


def _parse_resource_item(item: Any, seen: Set[tuple], out: List[Dict[str, Any]]) -> None:
    """解析 "Civitai resources:" 数组内的单个元素 dict"""
    if not isinstance(item, dict):
        return
    air = str(item.get("air", "") or "").strip()
    match = _AIR_URN_RE.search(air)
    if not match:
        return
    base, model_type, mid_s, vid_s = match.groups()
    try:
        model_id, version_id = int(mid_s), int(vid_s)
    except ValueError:
        return
    weight = None
    if item.get("weight") is not None:
        try:
            weight = float(item["weight"])
        except (TypeError, ValueError):
            weight = None
    res = _build_resource(air, model_id, version_id, model_type,
                          model_name=item.get("modelName"),
                          version_name=item.get("versionName"),
                          weight=weight)
    key = _dedup_key(res)
    if key not in seen:
        seen.add(key)
        out.append(res)


def _parse_marker_arrays(text: str, seen: Set[tuple], out: List[Dict[str, Any]]) -> None:
    """解析文本中所有 "Civitai resources: [...]" 标记后的 JSON 数组"""
    start = 0
    while True:
        marker_index = text.find(_MARKER_TEXT, start)
        if marker_index < 0:
            return
        start = marker_index + len(_MARKER_TEXT)
        bracket = text.find("[", start)
        if bracket < 0:
            return
        try:
            parsed, end = json.JSONDecoder().raw_decode(text[bracket:])
        except (ValueError, TypeError):
            continue  # 数组残缺: 跳过该标记, 继续找下一个
        if isinstance(parsed, list):
            for item in parsed:
                _parse_resource_item(item, seen, out)
        start = bracket + max(end, 1)


def _parse_bare_urns(text: str, seen: Set[tuple], out: List[Dict[str, Any]]) -> None:
    """扫描文本中的裸 AIR URN (无 modelName 元数据时以 "Civitai model <id>" 兜底命名)"""
    for match in _AIR_URN_RE.finditer(text):
        base, model_type, mid_s, vid_s = match.groups()
        try:
            model_id, version_id = int(mid_s), int(vid_s)
        except ValueError:
            continue
        res = _build_resource(match.group(0), model_id, version_id, model_type)
        key = _dedup_key(res)
        if key not in seen:
            seen.add(key)
            out.append(res)


def _walk(obj: Any, seen: Set[tuple], out: List[Dict[str, Any]], depth: int = 0) -> None:
    """递归遍历 JSON 结构, 只对含标记的字符串做解析 (其余为 C 级子串检查, 开销极低)"""
    if depth > 32:
        return
    if isinstance(obj, dict):
        for value in obj.values():
            _walk(value, seen, out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _walk(value, seen, out, depth + 1)
    elif isinstance(obj, str):
        if not (_MARKER_URN in obj or _MARKER_TEXT in obj):
            return
        if _MARKER_TEXT in obj:
            _parse_marker_arrays(obj, seen, out)
        if _MARKER_URN in obj:
            _parse_bare_urns(obj, seen, out)


def scan_json(prompt_json: Any = None, workflow_json: Any = None) -> List[Dict[str, Any]]:
    """扫描 ComfyUI prompt / workflow JSON (dict/str 均可), 返回去重后的资源列表"""
    out: List[Dict[str, Any]] = []
    seen: Set[tuple] = set()
    try:
        for data in (prompt_json, workflow_json):
            if data is None:
                continue
            _walk(data, seen, out)
    except Exception:
        pass
    return out


def extract_from_text(text: Optional[str]) -> List[Dict[str, Any]]:
    """扫描纯文本 (WebUI parameters / 隐写载荷 / NAI Comment 等), 返回去重后的资源列表"""
    out: List[Dict[str, Any]] = []
    seen: Set[tuple] = set()
    try:
        if text and (_MARKER_URN in text or _MARKER_TEXT in text):
            _parse_marker_arrays(text, seen, out)
            _parse_bare_urns(text, seen, out)
    except Exception:
        pass
    return out


class CivitaiAIR:
    """Civitai AIR 提取器门面 (模块级函数的类形式, 便于从 core 包统一导出)"""

    text_has_marker = staticmethod(text_has_marker)
    scan_json = staticmethod(scan_json)
    extract_from_text = staticmethod(extract_from_text)
