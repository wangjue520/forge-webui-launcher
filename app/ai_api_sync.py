# -*- coding: utf-8 -*-
"""
报错诊断的 AI 接口配置（服务商、Key、模型、接口地址）和 WWY LoRA 训练器自动同步。

两边共用一个文件 %APPDATA%\\WWY\\ai_api.json：{"values": {diag_llm_*}, "updated": 时间, "by": "launcher" / "lora_trainer"}。
启动器在配置里记着上次同步时共用文件内容的哈希（diag_llm_shared_seen）：
  - 启动器这边的值和上次同步的不一样 → 用户在启动器里改过 → 写进共用文件
  - 共用文件和上次同步的不一样 → 训练器改过 → 拿过来
这样启动器开着时，它内存里的旧值也不会把训练器刚改的盖回去（反过来训练器那边也是同一套做法，见训练器 app/aiconf.py）。
"""
import hashlib
import json
import os
import sys
import time

FIELDS = ("diag_llm_provider", "diag_llm_api_key", "diag_llm_base_url", "diag_llm_model", "diag_llm_format")
SEEN_KEY = "diag_llm_shared_seen"


_REAL_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launcher_config.json")


def _under_test():
    argv0 = os.path.basename(sys.argv[0]) if sys.argv else ""
    return "unittest" in argv0 or "pytest" in argv0 or "pytest" in sys.modules


def _enabled():
    """只在真正的启动器配置上同步：单元测试把 CONFIG_PATH 换成临时文件、拿假 Key 测时，别写进用户真实的共用文件。"""
    if os.environ.get("WWY_AI_API_FILE"):
        return True
    try:
        import config_manager as cm
        return os.path.normcase(os.path.abspath(cm.CONFIG_PATH)) == os.path.normcase(_REAL_CONFIG) and not _under_test()
    except Exception:
        return False


def shared_path():
    if os.environ.get("WWY_AI_API_FILE"):   # 测试用
        return os.environ["WWY_AI_API_FILE"]
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "WWY", "ai_api.json")


def _vals(d):
    d = d or {}
    out = {k: str(d.get(k) or "").strip() for k in FIELDS}
    out["diag_llm_provider"] = out["diag_llm_provider"] or "deepseek"
    out["diag_llm_format"] = out["diag_llm_format"] or "openai"
    return out


def _hash(v):
    return hashlib.sha1(json.dumps(v, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def read_shared(path=None):
    try:
        with open(path or shared_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("values"), dict):
        return None
    return {"values": _vals(d["values"]), "updated": float(d.get("updated") or 0), "by": d.get("by") or ""}


def write_shared(values, path=None):
    p = path or shared_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"values": _vals(values), "updated": time.time(), "by": "launcher"}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def sync(cfg, path=None, local_mtime=0.0):
    """
    启动器配置 cfg（原地修改）和共用文件对一下。返回 True = cfg 里的诊断设置被换成了训练器那边的（调用方要存盘）。
    local_mtime：第一次同步（还没记过哈希）时两边都有值又不一样，按启动器配置文件的修改时间和共用文件比谁新。
    出错（没权限写 AppData 之类）不影响启动器本身，返回 False。
    """
    if not _enabled():
        return False
    try:
        local = _vals(cfg)
        seen = cfg.get(SEEN_KEY) or ""
        sh = read_shared(path)
        local_changed = _hash(local) != seen
        shared_changed = bool(sh) and _hash(sh["values"]) != seen
        if sh and _hash(sh["values"]) == _hash(local):
            if seen != _hash(local):
                cfg[SEEN_KEY] = _hash(local)
            return False
        if not seen and sh:
            # 第一次：谁新用谁（启动器没填 Key 时直接用共用文件的）
            take_shared = not local["diag_llm_api_key"] or sh["updated"] >= (local_mtime or 0)
            local_changed, shared_changed = not take_shared, take_shared
        if local_changed and (local["diag_llm_api_key"] or sh):
            write_shared(local, path)
            cfg[SEEN_KEY] = _hash(local)
            return False
        if shared_changed:
            cfg.update(sh["values"])
            cfg[SEEN_KEY] = _hash(sh["values"])
            return True
    except Exception:
        pass
    return False
