# -*- coding: utf-8 -*-
"""
联网兜底：代理（梯子）坏了时自动换路，别让一个失效的端口卡死所有联网。

requests 会自动用三处的代理：启动器设置里的网络代理（写进环境变量）、系统环境变量
HTTPS_PROXY、Windows 系统代理（注册表）。只要其中一个是失效的端口——梯子没开、
换了梯子端口变了、梯子退出时没清系统代理、端口被别的程序占了——所有请求都报
"Unable to connect to proxy / Tunnel connection failed: 404"，用户完全看不懂。

不同梯子的端口各不一样（Clash 7890、Clash Verge Rev 7897、v2rayN 10808/10809、
NekoBox 2080、Hiddify 12334、v2rayA 20171……），所以出错时按这个顺序换路：
  1. 另外几处配置里的代理（比如环境变量是旧的、系统代理是对的）
  2. 本机正在运行的梯子进程监听的端口（按进程名认，自定义端口也能找到）
  3. 常见梯子的默认端口
  4. 直连（TUN / 增强模式下不需要代理，系统代理是残留的）
候选端口先用 CONNECT 试一下，确认真是代理才拿来重发请求。换路成功后记住，
后面的请求直接走新路，日志里写一行说明；全都不通就换成一句看得懂的中文报错。

另一种情况：没配代理、直连国外站点连不上（梯子开着但没开系统代理、或只开了 PAC），
GET 请求也会去找本机的梯子重试一次。

调用方显式传了 proxies（比如设置页的「测试代理」）或关了 trust_env 的，一律不动。
"""
import os
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse

import requests
from requests.utils import get_environ_proxies

# 常见梯子的本机 HTTP / 混合端口（都认 HTTP CONNECT）
KNOWN_PORTS = [
    (7890, "Clash / Mihomo"),
    (7897, "Clash Verge Rev"),
    (10809, "v2rayN"),
    (10808, "v2rayN"),
    (2080, "NekoBox / NekoRay"),
    (12334, "Hiddify"),
    (20171, "v2rayA"),
    (20172, "v2rayA"),
    (1080, "常见代理端口"),
    (8080, "常见代理端口"),
]

# 梯子进程名里会出现的词（小写）。端口改过的也能靠进程找到
LADDER_PROC_WORDS = (
    "clash", "mihomo", "verge", "flclash", "v2ray", "xray", "sing-box", "singbox",
    "nekobox", "nekoray", "hiddify", "karing", "shadowsocks", "ss-local", "trojan",
    "hysteria", "tuic", "naive", "privoxy", "qv2ray", "surge", "shadowrocket",
)

# 直连：proxies 里给空串（不能给 None——requests 合并配置时会删掉 None，
# 跟随重定向时又把环境变量里的坏代理加回来；空串会保留，且被当成「不走代理」）
DIRECT = {"http": "", "https": ""}

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

_lock = threading.Lock()
_routes = {}        # 坏掉的代理地址 / ("direct", 主机) -> 换成的 proxies
_logged = set()
_scan_cache = (0.0, [], None)
_orig_request = None


def _log(msg):
    print(f"[网络] {msg}", flush=True)


def reset():
    """代理设置改了：之前的换路结论作废"""
    global _scan_cache
    with _lock:
        _routes.clear()
        _logged.clear()
        _scan_cache = (0.0, [], None)


# ------------------------------------------------------------
# 代理从哪来
# ------------------------------------------------------------

def _norm_proxy(p):
    if not p:
        return ""
    p = p.strip()
    if "://" not in p:
        p = "http://" + p
    return p.rstrip("/")


def _host_port(proxy):
    u = urlparse(_norm_proxy(proxy))
    return u.hostname or "", u.port or 80


def _registry_proxy():
    """Windows 系统代理（设置 → 网络和 Internet → 代理）。没开返回空串"""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            if not winreg.QueryValueEx(k, "ProxyEnable")[0]:
                return ""
            server = winreg.QueryValueEx(k, "ProxyServer")[0] or ""
    except OSError:
        return ""
    # 「127.0.0.1:7890」或「http=127.0.0.1:7890;https=…;socks=…」
    if "=" in server:
        parts = dict(x.split("=", 1) for x in server.split(";") if "=" in x)
        server = parts.get("https") or parts.get("http") or ""
    return _norm_proxy(server)


def _launcher_proxy():
    try:
        import proxy_manager as pm
        return _norm_proxy(pm.active_url())
    except Exception:
        return ""


def _env_proxy_vars():
    return [_norm_proxy(os.environ.get(k)) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
            if os.environ.get(k)]


def _source(proxy):
    """(来源说明, 怎么改)"""
    p = _norm_proxy(proxy)
    if p and p == _launcher_proxy():
        return "启动器设置里的网络代理", "到启动器「设置 → 网络代理」把端口改成梯子实际的 HTTP / 混合端口，或者关掉"
    if p in _env_proxy_vars():
        return ("系统环境变量里的代理（HTTPS_PROXY / HTTP_PROXY）",
                "到「系统属性 → 环境变量」里改对或删掉 HTTPS_PROXY / HTTP_PROXY，然后重启启动器")
    return ("Windows 系统代理",
            "到 Windows「设置 → 网络和 Internet → 代理」把手动代理的端口改成梯子实际的端口，没在用梯子就关掉")


def _env_proxy_for(url):
    """requests 默认会给这个网址用的代理（环境变量优先，没有再看系统代理），没有返回空串"""
    try:
        proxies = get_environ_proxies(url)
    except Exception:
        return ""
    scheme = urlparse(url).scheme
    return _norm_proxy(proxies.get(scheme) or proxies.get("all"))


# ------------------------------------------------------------
# 探测
# ------------------------------------------------------------

def probe(proxy, host, port=443, timeout=3.0, connect_timeout=None):
    """
    向代理发 CONNECT host:port，看它是不是一个能用的 HTTP 代理。
    返回 (ok, 原因)。不走 TLS，几十毫秒就有结果。
    """
    ph, pp = _host_port(proxy)
    if not ph:
        return False, "代理地址格式不对"
    try:
        s = socket.create_connection((ph, pp), timeout=connect_timeout or timeout)
    except ConnectionRefusedError:
        return False, f"端口 {pp} 没有程序在监听（梯子没开，或端口填错了）"
    except socket.timeout:
        return False, "连代理超时"
    except OSError as e:
        return False, f"连不上代理（{e.strerror or e}）"
    try:
        s.settimeout(timeout)
        s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
        data = b""
        while b"\r\n" not in data and len(data) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        return False, "代理没有响应"
    except OSError as e:
        return False, f"代理断开了连接（{e.strerror or e}）"
    finally:
        try:
            s.close()
        except OSError:
            pass
    m = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", data)
    if not m:
        return False, f"端口 {pp} 上的程序不是 HTTP 代理（可能是 SOCKS 端口，或被别的程序占了）"
    code = int(m.group(1))
    if 200 <= code < 300:
        return True, ""
    if code == 407:
        return False, "代理要求账号密码"
    if code in (502, 503, 504):
        return False, f"代理连不上 {host}（返回 {code}，梯子的节点可能不通）"
    return False, f"端口 {pp} 上的程序不是代理（CONNECT 返回 {code}，端口可能被别的程序占了）"


def _scan():
    """
    (梯子端口 [(端口, 进程名)], 本机所有监听端口 set；查不了为 None)。查一次缓存 30 秒。
    监听端口拿来先筛一遍候选：Windows 上连一个没开的本机端口要两秒才报拒绝，挨个试太慢。
    """
    global _scan_cache
    ts, ladders, listening = _scan_cache
    if time.monotonic() - ts < 30:
        return ladders, listening
    out, listening = [], None
    if os.name == "nt":
        try:
            flags = 0x08000000  # CREATE_NO_WINDOW
            ns = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                timeout=8, creationflags=flags).stdout.decode("mbcs", "replace")
            tl = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                                timeout=8, creationflags=flags).stdout.decode("mbcs", "replace")
            names = {}
            for line in tl.splitlines():
                cols = [c.strip('"') for c in line.split('","')]
                if len(cols) >= 2 and cols[1].isdigit():
                    names[cols[1]] = cols[0]
            listening = set()
            for line in ns.splitlines():
                cols = line.split()
                # TCP  127.0.0.1:7890  0.0.0.0:0  LISTENING  1234（状态列会被本地化，不看它，看远端是 :0）
                if len(cols) < 5 or cols[0].upper() != "TCP" or not cols[2].endswith(":0"):
                    continue
                addr, _, port = cols[1].rpartition(":")
                if addr not in ("127.0.0.1", "0.0.0.0", "[::]", "[::1]") or not port.isdigit():
                    continue
                listening.add(int(port))
                name = names.get(cols[-1], "")
                if any(w in name.lower() for w in LADDER_PROC_WORDS):
                    item = (int(port), name)
                    if item not in out:
                        out.append(item)
        except (OSError, subprocess.SubprocessError, ValueError):
            out, listening = [], None
    known = {p for p, _ in KNOWN_PORTS}
    out.sort(key=lambda x: (x[0] not in known, x[0]))
    _scan_cache = (time.monotonic(), out, listening or None)
    return out, listening or None


def _ladder_ports():
    return _scan()[0]


def _candidates(exclude):
    """[(代理地址, 说明)]，按优先级排好、去重"""
    out, seen = [], set(_norm_proxy(x) for x in exclude if x)

    def add(p, label):
        p = _norm_proxy(p)
        if p and p not in seen:
            seen.add(p)
            out.append((p, label))

    add(_launcher_proxy(), "启动器设置里的代理")
    for p in _env_proxy_vars():
        add(p, "环境变量里的代理")
    add(_registry_proxy(), "Windows 系统代理")
    for port, name in _ladder_ports():
        add(f"http://127.0.0.1:{port}", f"正在运行的 {name}")
    for port, label in KNOWN_PORTS:
        add(f"http://127.0.0.1:{port}", label)
    return out


def _listening_ports():
    return _scan()[1]


def _working_proxies(host, port, exclude):
    found = []
    listening = _listening_ports()
    for p, label in _candidates(exclude):
        ph, pp = _host_port(p)
        local = ph in _LOCAL_HOSTS or ph.startswith("127.")
        if local and listening is not None and pp not in listening:
            continue
        ok, _ = probe(p, host, port, timeout=2.0, connect_timeout=0.5 if local else None)
        if ok:
            found.append((p, label))
    return found


# ------------------------------------------------------------
# 包住 requests
# ------------------------------------------------------------

def _target(url):
    u = urlparse(url)
    host = (u.hostname or "").lower()
    port = u.port or (443 if u.scheme == "https" else 80)
    return host, port


def _send(session, method, url, route, kw):
    kw = dict(kw)
    kw["proxies"] = dict(route)
    return _orig_request(session, method, url, **kw)


def _remember(key, route, msg):
    with _lock:
        _routes[key] = route
        if (key, msg) not in _logged:
            _logged.add((key, msg))
            _log(msg)


def _diagnose(proxy, host, port):
    """坏代理坏在哪。本机端口先查监听表：没开的端口不用再干等两秒"""
    ph, pp = _host_port(proxy)
    listening = _listening_ports()
    if (ph in _LOCAL_HOSTS or ph.startswith("127.")) and listening is not None and pp not in listening:
        return f"端口 {pp} 没有程序在监听（梯子没开，或端口填错了）"
    return probe(proxy, host, port)[1]


def _recover_from_bad_proxy(session, method, url, kw, proxy, err):
    host, port = _target(url)
    reason = _diagnose(proxy, host, port)
    src, fix = _source(proxy)
    reason = reason or str(err)
    with _lock:
        alts = _working_proxies(host, port, exclude=[proxy])
    for p, label in alts:
        try:
            resp = _send(session, method, url, {"http": p, "https": p}, kw)
        except requests.exceptions.RequestException:
            continue
        _remember(proxy, {"http": p, "https": p},
                  f"{src} {proxy} 用不了：{reason}。已自动改走 {p}（{label}）。要根治：{fix}")
        return resp
    try:
        resp = _send(session, method, url, DIRECT, kw)
    except requests.exceptions.RequestException:
        pass
    else:
        _remember(proxy, DIRECT, f"{src} {proxy} 用不了：{reason}。已自动改为直连。要根治：{fix}")
        return resp
    raise requests.exceptions.ProxyError(
        f"{src} {proxy} 用不了：{reason}。"
        f"已经自动试过本机正在运行的梯子、常见梯子端口（Clash / Clash Verge / v2rayN / NekoBox / Hiddify 等）和直连，"
        f"都连不上 {host}。请先打开梯子；如果梯子开着，{fix}",
        request=getattr(err, "request", None), response=getattr(err, "response", None))


def _recover_direct_failure(session, method, url, kw, err):
    host, port = _target(url)
    with _lock:
        alts = _working_proxies(host, port, exclude=[])
    for p, label in alts:
        try:
            resp = _send(session, method, url, {"http": p, "https": p}, kw)
        except requests.exceptions.RequestException:
            continue
        _remember(("direct", host), {"http": p, "https": p},
                  f"直连 {host} 失败，已自动改走本机的梯子 {p}（{label}）")
        return resp
    raise err


def _guarded_request(self, method, url, *args, **kw):
    if args or kw.get("proxies") or self.proxies or not self.trust_env:
        return _orig_request(self, method, url, *args, **kw)
    host, _ = _target(url)
    if not host or host in _LOCAL_HOSTS or host.startswith("127."):
        return _orig_request(self, method, url, **kw)
    proxy = _env_proxy_for(url)
    key = proxy or ("direct", host)
    route = _routes.get(key)
    if route is not None:
        try:
            return _send(self, method, url, route, kw)
        except requests.exceptions.ConnectionError:
            with _lock:   # 记住的路也断了（梯子换了 / 关了），重新来一遍
                _routes.pop(key, None)
    try:
        return _orig_request(self, method, url, **kw)
    except requests.exceptions.ProxyError as e:
        if not proxy:
            raise
        return _recover_from_bad_proxy(self, method, url, kw, proxy, e)
    except requests.exceptions.ConnectionError as e:
        # 走代理时的连接错误是目标站自己的问题，不乱换；直连失败只重试幂等请求
        if proxy or str(method).upper() not in _SAFE_METHODS:
            raise
        return _recover_direct_failure(self, method, url, kw, e)


def install():
    """在启动器进程里给 requests 装上兜底。重复调用无害"""
    global _orig_request
    if _orig_request is not None:
        return
    _orig_request = requests.Session.request
    requests.Session.request = _guarded_request
