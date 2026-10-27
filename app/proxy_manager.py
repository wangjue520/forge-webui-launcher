# -*- coding: utf-8 -*-
"""
手动网络代理（给 Clash / v2rayN 这类本机代理软件用）。

背景：不少人开着代理软件但没开 TUN/增强模式，浏览器能上 HuggingFace，
启动器、pip、git、WebUI/ComfyUI 这些命令行程序却不走代理——WD14 下模型
一直报连不上就是这个原因。以前只能自己在 run_nvidia_gpu.bat 里写
set HTTPS_PROXY=http://127.0.0.1:7890。

做法：在设置里填代理地址后，写进启动器自己进程的环境变量
（HTTP_PROXY / HTTPS_PROXY / NO_PROXY）。启动器自己的联网（requests）、
git / pip 子进程、启动的 WebUI / ComfyUI 都是从这份环境派生的，一处设置全部生效。

NO_PROXY 一定要带上本机地址：WebUI 的 gradio 启动时会请求
http://127.0.0.1:7860/ 自检，走了代理会报 502 起不来；启动器探测端口同理。
这里没开代理也一样要带：httpx 在环境变量里找不到代理时会读 Windows 注册表的
「系统代理」（Clash / v2rayN 开系统代理、或代理软件退出了注册表还开着），
却不认注册表里的「本地地址不走代理」，只认 NO_PROXY，gradio 就报
"When localhost is not accessible, a shareable link must be created"。
所以起 WebUI / ComfyUI 的环境一律经过 with_local_bypass()。
"""
import os
import re
import time

import requests

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")
LOCAL_BYPASS = ["localhost", "127.0.0.1", "::1", "0.0.0.0"]

# 测试代理时访问的地址：正是平时连不上的那几个
TEST_TARGETS = [
    ("HuggingFace", "https://huggingface.co/api/models/SmilingWolf/wd-swinv2-tagger-v3"),
    ("GitHub", "https://github.com/"),
    ("PyPI", "https://pypi.org/simple/pip/"),
]

# 启动器启动时系统里原有的代理变量。关掉代理时恢复成这样，不能直接删——
# 用户可能本来就在系统里设了 HTTP_PROXY
_orig = None
_active = ""    # 当前生效的启动器代理地址，net_guard 拿来判断坏代理是从哪来的


def normalize(text):
    """
    用户输入 → (代理地址, 错误)。
    支持只填端口「7890」、「127.0.0.1:7890」、完整的「http://127.0.0.1:7890」。
    """
    s = (text or "").strip()
    if not s:
        return "", "请填写代理地址或端口"
    if s.isdigit():
        s = "127.0.0.1:" + s
    m = re.match(r"^([a-zA-Z0-9]+)://", s)
    if m:
        scheme = m.group(1).lower()
        if scheme.startswith("socks"):
            return "", ("暂不支持 SOCKS 代理（pip / 部分 Python 库不认）。"
                        "请填代理软件的 HTTP 端口或混合端口，Clash 默认是 7890，v2rayN 默认是 10809")
        if scheme not in ("http", "https"):
            return "", f"不认识的代理类型「{scheme}」，请填 http://地址:端口"
    else:
        s = "http://" + s
    m = re.match(r"^(https?)://([^/:\s]+|\[[0-9a-fA-F:]+\]):(\d{1,5})/?$", s)
    if not m:
        return "", "格式不对，应该像 127.0.0.1:7890 或 http://127.0.0.1:7890"
    port = int(m.group(3))
    if not 0 < port < 65536:
        return "", "端口号要在 1 到 65535 之间"
    return f"{m.group(1)}://{m.group(2)}:{port}", ""


def current(cfg):
    """配置里生效的代理地址；没开返回空串"""
    if not cfg.get("proxy_enabled"):
        return ""
    url, err = normalize(cfg.get("proxy_url", ""))
    return "" if err else url


def _snapshot():
    global _orig
    if _orig is None:
        _orig = {k: os.environ.get(k) for k in _PROXY_KEYS + _NO_PROXY_KEYS}


def _merge_local(base):
    """已有的 NO_PROXY 原样保留，缺的本机地址补在后面"""
    items = []
    for h in [x.strip() for x in (base or "").split(",")] + LOCAL_BYPASS:
        if h and h not in items:
            items.append(h)
    return ",".join(items)


def _no_proxy_value():
    return _merge_local((_orig or {}).get("NO_PROXY") or (_orig or {}).get("no_proxy") or "")


def with_local_bypass(env):
    """给子进程环境（dict）的 NO_PROXY 补上本机地址，不管代理开没开。原地修改并返回 env"""
    keys = [k for k in env if k.upper() == "NO_PROXY"]
    base = ",".join(env[k] for k in keys if env[k])
    for k in keys:
        del env[k]
    value = _merge_local(base)
    env["NO_PROXY"] = value
    if os.name != "nt":
        # Windows 的环境变量名不区分大小写，两套写进去会变成重复的同名变量；其他系统两套都有程序在读
        env["no_proxy"] = value
    return env


def active_url():
    return _active


def apply(cfg):
    """按配置设置/恢复本进程的代理环境变量。返回生效的代理地址（没开为空串）"""
    global _active
    _snapshot()
    url = current(cfg)
    _active = url
    # 先恢复原样（Windows 环境变量名不区分大小写，大小写两套其实是同一个变量）
    for k, v in _orig.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if url:
        for k in _PROXY_KEYS:
            os.environ[k] = url
        no_proxy = _no_proxy_value()
        for k in _NO_PROXY_KEYS:
            os.environ[k] = no_proxy
    try:
        import net_guard
        net_guard.reset()     # 代理改了，之前自动换路的结论作废
    except Exception:
        pass
    return url


def describe(cfg):
    url = current(cfg)
    if url:
        return f"已开启：所有联网走 {url}（本机地址不走代理）"
    return "未开启：按系统网络直连（加速镜像照常生效）"


def test(url, timeout=8):
    """通过指定代理访问几个常用站点。返回 [{name, ok, ms, error}]"""
    proxies = {"http": url, "https": url}
    out = []
    for name, target in TEST_TARGETS:
        t0 = time.monotonic()
        try:
            r = requests.get(target, proxies=proxies, timeout=timeout, stream=True,
                             headers={"User-Agent": "forge-launcher-proxy-test/1.0"})
            r.close()
            ok = r.status_code < 500
            out.append({"name": name, "ok": ok, "ms": int((time.monotonic() - t0) * 1000),
                        "error": "" if ok else f"HTTP {r.status_code}"})
        except requests.exceptions.ProxyError:
            out.append({"name": name, "ok": False, "ms": 0,
                        "error": "连不上代理（代理软件没开，或端口填错了）"})
        except requests.exceptions.ConnectTimeout:
            out.append({"name": name, "ok": False, "ms": 0, "error": "连接超时"})
        except requests.RequestException as e:
            out.append({"name": name, "ok": False, "ms": 0, "error": type(e).__name__})
    return out
