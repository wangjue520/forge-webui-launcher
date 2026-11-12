"""
WWY 启动器桥接扩展（WebUI / Forge 版，由启动器自动放进 extensions，启动器更新时一起更新）

启动器的图片信息页、输出管理器点「发送到 WebUI」时，把那张图的生成参数（infotext）交给这里；
网页那边（javascript/wwy_launcher_bridge.js）一直挂着一个长轮询，拿到后切到文生图、
把参数放进提示词框、按一下 WebUI 自己的 ↙ 粘贴按钮——跟「图片信息 → 发送到文生图」完全一样，
所以 ADetailer / ControlNet / 高清修复这些插件参数，只要插件支持粘贴就都会套上。

· 不加任何界面、不改任何设置；接口：
    POST /wwy_bridge/send     启动器交参数（网页没打开就先存着，网页一打开自动套用，2 分钟过期）
    GET  /wwy_bridge/poll     网页长轮询取参数
    POST /wwy_bridge/ack      网页回执；GET /wwy_bridge/ack/{id} 启动器查回执
    GET  /wwy_bridge/status   版本、有几个网页在等
· 不想要：把文件夹改名成 wwy_launcher_bridge.disabled（启动器不会再放回来），或在扩展页里停用
"""
import asyncio
import time

VERSION = 1
PENDING_TTL = 120          # 秒：存着没人取就作废，免得隔天打开网页突然被套上一份旧参数
POLL_WAIT = 25             # 秒：长轮询一次最多挂这么久

_state = {"item": None, "ts": 0.0, "waiting": 0, "last_poll": 0.0}
_acks = {}


def put(item, now=None):
    _state["item"], _state["ts"] = item, (now or time.time())


def take(now=None):
    item, ts = _state["item"], _state["ts"]
    if not item:
        return None
    _state["item"] = None
    if (now or time.time()) - ts > PENDING_TTL:
        return None
    return item


def clients(now=None):
    """现在有几个网页在等（长轮询挂着，或者 3 秒内刚来过）"""
    if _state["waiting"] > 0:
        return _state["waiting"]
    return 1 if (now or time.time()) - _state["last_poll"] < 3 else 0


def store_ack(data):
    cid = str((data or {}).get("id") or "")
    if not cid:
        return
    _acks[cid] = {"ok": bool(data.get("ok")), "text": str(data.get("text") or "")}
    while len(_acks) > 100:
        _acks.pop(next(iter(_acks)))


def _on_app_started(_demo, app):
    from fastapi import Request

    @app.post("/wwy_bridge/send")
    async def _send(request: Request):
        data = await request.json()
        if not isinstance(data, dict) or not data.get("id") or not str(data.get("infotext") or "").strip():
            return {"ok": False, "error": "格式不对"}
        n = clients()
        put({"id": str(data["id"]), "infotext": str(data["infotext"]),
             "tab": "img2img" if data.get("tab") == "img2img" else "txt2img",
             "title": str(data.get("title") or "")})
        return {"ok": True, "clients": n, "queued": n == 0, "version": VERSION}

    @app.get("/wwy_bridge/poll")
    async def _poll():
        _state["waiting"] += 1
        try:
            t0 = time.time()
            while time.time() - t0 < POLL_WAIT:
                item = take()
                if item:
                    return {"ok": True, "item": item}
                await asyncio.sleep(0.25)
            return {"ok": True, "item": None}
        finally:
            _state["waiting"] -= 1
            _state["last_poll"] = time.time()

    @app.post("/wwy_bridge/ack")
    async def _ack(request: Request):
        store_ack(await request.json())
        return {"ok": True}

    @app.get("/wwy_bridge/ack/{cid}")
    async def _get_ack(cid: str):
        return {"ok": True, "ack": _acks.get(cid)}

    @app.get("/wwy_bridge/status")
    async def _status():
        return {"ok": True, "version": VERSION, "clients": clients()}


try:
    from modules import script_callbacks
    script_callbacks.on_app_started(_on_app_started)
except ImportError:          # 不在 WebUI 里（单测）
    pass
except Exception as e:       # 接口变了：只是发不过来，不影响 WebUI
    print(f"[WWY启动器] 桥接扩展没能注册接口：{e}", flush=True)
