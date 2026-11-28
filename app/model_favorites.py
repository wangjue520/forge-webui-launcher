# -*- coding: utf-8 -*-
"""
模型管理的收藏夹：只记录「哪些模型在哪个收藏夹」，从不移动、复制模型文件（模型动辄几 GB）。
存在 launcher_data/model_collections.json：
  {"next": 3, "collections": [{"id": 1, "name": "常用画风", "created": 时间, "items": ["D:\\models\\loras\\a.safetensors", ...]}]}
路径按 Windows 不区分大小写比较；文件被删 / 改名后记录还在，列表里只是对不上（count_alive 不算它）。
"""
import json
import os
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(APP_DIR, "launcher_data", "model_collections.json")
_LOCK = threading.RLock()


def _key(p):
    return os.path.normcase(os.path.abspath(str(p or "")))


def _load():
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("collections"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"next": 1, "collections": []}


def _save(data):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def _find(data, cid):
    for c in data["collections"]:
        if c.get("id") == int(cid):
            return c
    raise ValueError("收藏夹不存在")


def _check_name(data, name, skip=None):
    name = str(name or "").strip()[:40]
    if not name:
        raise ValueError("名字不能为空")
    if any(c.get("name") == name and c.get("id") != skip for c in data["collections"]):
        raise ValueError("已有同名收藏夹")
    return name


def listing():
    """[{id, name, count, alive}] + {规范化路径: [收藏夹 id]}（界面给每个模型标星、按收藏夹筛选用）"""
    with _LOCK:
        data = _load()
    cols, member = [], {}
    for c in data["collections"]:
        items = c.get("items") or []
        cols.append({"id": c["id"], "name": c["name"], "count": len(items),
                     "alive": sum(1 for p in items if os.path.isfile(p))})
        for p in items:
            member.setdefault(_key(p), []).append(c["id"])
    return {"collections": cols, "member": member}


def create(name):
    with _LOCK:
        data = _load()
        name = _check_name(data, name)
        cid = int(data.get("next") or 1)
        data["next"] = cid + 1
        data["collections"].append({"id": cid, "name": name, "created": time.time(), "items": []})
        _save(data)
    return cid


def rename(cid, name):
    with _LOCK:
        data = _load()
        c = _find(data, cid)
        c["name"] = _check_name(data, name, skip=c["id"])
        _save(data)


def delete(cid):
    with _LOCK:
        data = _load()
        _find(data, cid)
        data["collections"] = [c for c in data["collections"] if c.get("id") != int(cid)]
        _save(data)


def set_items(cid, paths, on):
    """加入 / 移出。只改记录，不碰文件"""
    with _LOCK:
        data = _load()
        c = _find(data, cid)
        items = list(c.get("items") or [])
        keys = {_key(p) for p in items}
        for p in paths or []:
            k = _key(p)
            if on and k not in keys:
                items.append(os.path.abspath(str(p)))
                keys.add(k)
            elif not on and k in keys:
                items = [x for x in items if _key(x) != k]
                keys.discard(k)
        c["items"] = items
        _save(data)
