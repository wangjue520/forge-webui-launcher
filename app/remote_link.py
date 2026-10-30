# -*- coding: utf-8 -*-
"""
局域网 / 广域网联动：一台电脑当「主机」，另一台电脑上的启动器输入主机的 24 位密钥后，
可以像操作本机一样操作主机——管理 LoRA、让主机下载模型、把输出下载到本机、
启动 / 停止主机上的 WebUI / ComfyUI，并在本机浏览器里直接用主机的 WebUI。

整体只用标准库，主机只开一个 TCP 端口（默认 7899），局域网发现另用一个 UDP 端口（7898）：

  POST /rpc      调主机启动器的接口（和本机界面调的是同一套 LauncherApi 方法）
  GET  /events   长轮询拿主机推给界面的事件（日志、进度、状态）
  GET  /file     下载主机上的输出 / 模型文件（只能取输出索引里的文件和模型目录里的文件）
  PUT  /upload   把本机文件传到主机的收件箱（模型上传先传到这里再导入）
  GET  /tunnel   回 200 之后这条连接变成到主机 127.0.0.1:端口 的原始 TCP 管道——
                 远程启动器在本机开一个端口转发过去，WebUI / ComfyUI 的 WebSocket 都能直接用。
                 只允许转发到主机自己在接口结果里告诉过对方的端口（实例地址、输出文件服务）

安全：密钥本身从不在网络上传。每个请求带 X-WWY-Auth: 时间戳:随机数:签名，
签名 = HMAC-SHA256(密钥, 方法 + 路径 + 时间戳 + 随机数 + 请求体的 SHA256)，
主机拒绝 5 分钟以外的时间戳和用过的随机数（防重放）。传输内容本身不加密，
公网使用建议走 Tailscale / ZeroTier 这类虚拟局域网，或至少只在需要时打开。
"""
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import socket
import socketserver
import threading
import time
import urllib.parse

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根目录（代码在 app/ 子目录里）
STATE_PATH = os.path.join(APP_DIR, "launcher_data", "remote_link.json")
INBOX_DIR = os.path.join(APP_DIR, "launcher_data", "link_inbox")

DEFAULT_PORT = 7899
DISCOVERY_PORT = 7898
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉了容易看错的 0 O 1 I
KEY_LEN = 24
CLOCK_SKEW = 300           # 允许两台电脑的时钟差几秒
PROTO = 1                  # 联动协议版本，改了不兼容的地方再加
AUTH_HEADER = "X-WWY-Auth"

# 接口结果 / 事件里指向主机本地的地址：远程那边要换成本机转发端口
_LOCAL_URL_RE = re.compile(r"(https?|wss?)://(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])(?::(\d+))?",
                           re.IGNORECASE)


# ============================================================
# 密钥与签名
# ============================================================
def new_key():
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LEN))


def format_key(key):
    """ABCD-EFGH-… 每 4 位一组，方便念给别人 / 抄"""
    return "-".join(key[i:i + 4] for i in range(0, len(key), 4))


def normalize_key(text):
    """用户输入的密钥 → 24 位标准形式；格式不对返回 None。大小写、空格、横杠都无所谓"""
    s = re.sub(r"[^A-Za-z0-9]", "", str(text or "")).upper()
    if len(s) != KEY_LEN or any(c not in KEY_ALPHABET for c in s):
        return None
    return s


def key_id(key):
    """局域网广播里用的密钥指纹：只能拿来认「是不是这台主机」，反推不出密钥"""
    return hashlib.sha256(b"wwy-link-id:" + key.encode()).hexdigest()[:16]


def _sign(key, method, path, ts, nonce, body_hash):
    msg = "\n".join((method.upper(), path, str(ts), nonce, body_hash)).encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def body_digest(body):
    return hashlib.sha256(body or b"").hexdigest()


def make_auth(key, method, path, body=b"", offset=0.0, unsigned_body=False):
    ts = int(time.time() + offset)
    nonce = secrets.token_hex(8)
    bh = "UNSIGNED" if unsigned_body else body_digest(body)
    return f"{ts}:{nonce}:{_sign(key, method, path, ts, nonce, bh)}"


class _NonceCache:
    """用过的随机数记 CLOCK_SKEW*2 秒，同一个请求原样重放会被拒"""

    def __init__(self):
        self._seen = {}
        self._lock = threading.Lock()

    def add(self, nonce):
        now = time.time()
        with self._lock:
            if len(self._seen) > 20000:
                self._seen = {n: t for n, t in self._seen.items() if now - t < CLOCK_SKEW * 2}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


def check_auth(key, header, method, path, body_hash, nonces):
    """返回 (是否通过, 原因)：原因 clock = 时间对不上（对方据此校准时差）"""
    try:
        ts_s, nonce, sig = (header or "").split(":", 2)
        ts = int(ts_s)
    except ValueError:
        return False, "auth"
    if not key or not nonce or len(nonce) > 64:
        return False, "auth"
    if abs(time.time() - ts) > CLOCK_SKEW:
        return False, "clock"
    want = _sign(key, method, path, ts, nonce, body_hash)
    if not hmac.compare_digest(want, sig):
        return False, "auth"
    if not nonces.add(nonce):
        return False, "replay"
    return True, ""


# ============================================================
# 地址改写：主机的 http://127.0.0.1:7860 → 远程本机的 http://127.0.0.1:转发端口
# ============================================================
def local_url_ports(obj, out=None):
    """接口结果 / 事件里出现的主机本地端口（主机据此决定允许转发哪些端口）"""
    out = set() if out is None else out
    if isinstance(obj, str):
        for m in _LOCAL_URL_RE.finditer(obj):
            if m.group(2):
                out.add(int(m.group(2)))
            elif m.group(1).lower() in ("http", "ws"):
                out.add(80)
    elif isinstance(obj, dict):
        for v in obj.values():
            local_url_ports(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            local_url_ports(v, out)
    return out


def rewrite_local_urls(obj, port_map):
    """把字符串里的主机本地地址换成 port_map[端口] 对应的本机转发地址"""
    if isinstance(obj, str):
        if "://" not in obj:
            return obj

        def sub(m):
            port = int(m.group(2)) if m.group(2) else 80
            local = port_map(port)
            return f"{m.group(1)}://127.0.0.1:{local}" if local else m.group(0)
        return _LOCAL_URL_RE.sub(sub, obj)
    if isinstance(obj, dict):
        return {k: rewrite_local_urls(v, port_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rewrite_local_urls(v, port_map) for v in obj]
    return obj


def parse_addr(text, default_port=DEFAULT_PORT):
    """「192.168.1.5」「192.168.1.5:7899」「http://xx.com:7899/」「[::1]:7899」→ (host, port)"""
    s = str(text or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    try:
        u = urllib.parse.urlsplit(s)
        host, port = u.hostname, u.port or default_port
    except ValueError:
        return None
    if not host:
        return None
    return host, int(port)


def lan_ipv4s():
    """本机的局域网 IPv4（界面上告诉用户「远程填这个地址」）"""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    try:   # 默认出口那块网卡排第一（多网卡 / 虚拟网卡时最可能是对的那个）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips = [ip] + [x for x in ips if x != ip]
    except OSError:
        pass
    return ips


def _broadcast_targets():
    """广播地址：全局广播 + 每块网卡按 /24 猜的子网广播（Windows 的 255.255.255.255 只走一块网卡）"""
    out = ["255.255.255.255"]
    for ip in lan_ipv4s():
        try:
            net = ipaddress.ip_network(ip + "/24", strict=False)
            b = str(net.broadcast_address)
            if b not in out:
                out.append(b)
        except ValueError:
            pass
    return out


# ============================================================
# 状态文件
# ============================================================
def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("host", {})
            st.setdefault("peers", [])
            return st
    except (OSError, ValueError):
        pass
    return {"host": {}, "peers": []}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# ============================================================
# 主机端
# ============================================================
class _EventRing:
    """主机推给界面的事件留一份给远程长轮询；只留最近一段，断线太久的从最新处接上"""

    def __init__(self, size=4000):
        self.epoch = secrets.token_hex(4)    # 主机重启后换一个，远程据此知道序号重置了
        self._buf = []
        self._size = size
        self._seq = 0
        self._cv = threading.Condition()

    def push(self, evt):
        with self._cv:
            self._seq += 1
            self._buf.append((self._seq, evt))
            if len(self._buf) > self._size:
                del self._buf[: len(self._buf) - self._size]
            self._cv.notify_all()

    def since(self, seq, wait):
        deadline = time.time() + wait
        with self._cv:
            while True:
                if seq > self._seq:      # 对方的序号比我们大 = 主机重启过，从现在开始
                    seq = self._seq
                evts = [(s, e) for s, e in self._buf if s > seq]
                left = deadline - time.time()
                if evts or left <= 0:
                    return self._seq, [e for _s, e in evts]
                self._cv.wait(left)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def handle_error(self, request, client_address):
        # 远程掉线、浏览器关标签页时连接被掐断是常事，别往日志里刷整段 traceback
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, OSError)):
            super().handle_error(request, client_address)


class LinkHost:
    """
    dispatch(method, args) -> 结果：由 webview_api 提供，负责白名单和真正调用。
    file_resolver(query) -> 文件路径或 None：/file 的取文件规则（只给输出和模型）。
    """

    def __init__(self, dispatch, file_resolver, log=print):
        self.dispatch = dispatch
        self.file_resolver = file_resolver
        self.log = log
        self.key = ""
        self.port = 0
        self.ring = _EventRing()
        self.nonces = _NonceCache()
        self.allowed_ports = set()
        self.clients = {}              # 远程 IP -> {"name", "last"}
        self._srv = None
        self._udp = None
        self._lock = threading.Lock()
        self.error = ""

    # ---- 生命周期 ----
    @property
    def running(self):
        return self._srv is not None

    def start(self, key, port):
        self.stop()
        self.key, self.port = key, int(port)
        host = self

        class Handler(_Handler):
            link = host
        try:
            srv = _Server(("0.0.0.0", self.port), Handler)
        except OSError as e:
            self.error = f"端口 {self.port} 打不开（{e.strerror or e}），可能被别的程序占用了，换一个端口试试"
            return False
        self._srv = srv
        self.error = ""
        threading.Thread(target=srv.serve_forever, daemon=True, name="link-host").start()
        self._start_discovery()
        return True

    def stop(self):
        srv, self._srv = self._srv, None
        if srv:
            try:
                srv.shutdown()
                srv.server_close()
            except OSError:
                pass
        udp, self._udp = self._udp, None
        if udp:
            try:
                udp.close()
            except OSError:
                pass

    def _start_discovery(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", DISCOVERY_PORT))
        except OSError:
            return      # 发现端口被占也不影响：远程手动填地址照样能连
        self._udp = s

        def loop():
            name = socket.gethostname()
            while self._udp is s:
                try:
                    data, addr = s.recvfrom(2048)
                    msg = json.loads(data.decode("utf-8", "replace"))
                except (OSError, ValueError):
                    if self._udp is not s:
                        return
                    continue
                if not isinstance(msg, dict) or msg.get("wwy") != "find":
                    continue
                if msg.get("kid") != key_id(self.key):
                    continue
                reply = {"wwy": "here", "kid": msg.get("kid"), "port": self.port, "name": name}
                try:
                    s.sendto(json.dumps(reply).encode(), addr)
                except OSError:
                    pass
        threading.Thread(target=loop, daemon=True, name="link-discovery").start()

    # ---- 给界面的事件 ----
    def publish(self, evt):
        if self._srv is not None:
            self.ring.push(evt)

    def note_ports(self, obj):
        ports = local_url_ports(obj)
        if ports:
            with self._lock:
                self.allowed_ports |= ports

    def touch_client(self, ip, name=""):
        c = self.clients.setdefault(ip, {"name": "", "last": 0})
        c["last"] = time.time()
        if name:
            c["name"] = name

    def online_clients(self, within=60):
        now = time.time()
        return [{"ip": ip, "name": c["name"], "ago": int(now - c["last"])}
                for ip, c in self.clients.items() if now - c["last"] <= within]


def _jsonable(obj):
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class _Handler(http.server.BaseHTTPRequestHandler):
    link = None          # LinkHost，由 LinkHost.start 里的子类注入
    protocol_version = "HTTP/1.1"
    server_version = "WWYLink/1"
    _fail_times = {}     # IP -> 最近一次签名失败的时间：失败了就慢一点回，挡暴力猜

    def log_message(self, *_a):
        pass

    # ---- 通用 ----
    def _send_json(self, code, obj, extra=None):
        body = _jsonable(obj)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-WWY-Time", str(int(time.time())))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, body_hash):
        ok, why = check_auth(self.link.key, self.headers.get(AUTH_HEADER), self.command,
                             self.path, body_hash, self.link.nonces)
        if ok:
            self.link.touch_client(self.client_address[0],
                                   urllib.parse.unquote(self.headers.get("X-WWY-Name", ""))[:64])
            return True
        ip = self.client_address[0]
        last = self._fail_times.get(ip, 0)
        self._fail_times[ip] = time.time()
        if why != "clock" and time.time() - last < 5:
            time.sleep(1.5)
        msg = {"clock": "两台电脑的时间相差太多", "replay": "重复的请求"}.get(why, "密钥不对")
        self._send_json(401, {"ok": False, "reason": why, "error": msg})
        return False

    def _read_body(self, limit=64 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > limit:
            raise ValueError("请求太大")
        return self.rfile.read(n) if n else b""

    def _query(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))

    def _route(self):
        return urllib.parse.urlsplit(self.path).path

    # ---- POST /rpc ----
    def do_POST(self):
        try:
            body = self._read_body()
        except ValueError:
            self._send_json(413, {"ok": False, "error": "请求太大"})
            return
        if not self._auth(body_digest(body)):
            return
        if self._route() != "/rpc":
            self._send_json(404, {"ok": False, "error": "没有这个接口"})
            return
        try:
            req = json.loads(body.decode("utf-8"))
            method, args = req.get("method"), req.get("args") or []
            if not isinstance(method, str) or not isinstance(args, list):
                raise ValueError
        except (ValueError, AttributeError):
            self._send_json(400, {"ok": False, "error": "请求格式不对"})
            return
        try:
            result = self.link.dispatch(method, args)
            self.link.note_ports(result)
            self._send_json(200, {"ok": True, "result": result})
        except PermissionError as e:
            self._send_json(403, {"ok": False, "error": str(e)})
        except Exception as e:     # 接口内部出错：原样告诉远程，界面上照常弹提示
            self._send_json(200, {"ok": False, "error": f"主机执行 {method} 出错：{e}"})

    # ---- GET /events /file /tunnel ----
    def do_GET(self):
        if not self._auth(body_digest(b"")):
            return
        route, q = self._route(), self._query()
        if route == "/events":
            return self._events(q)
        if route == "/file":
            return self._file(q)
        if route == "/tunnel":
            return self._tunnel(q)
        self._send_json(404, {"ok": False, "error": "没有这个接口"})

    def _events(self, q):
        try:
            since = int(q.get("since", "0"))
        except ValueError:
            since = 0
        if q.get("epoch") != self.link.ring.epoch:
            since = self.link.ring.since(10 ** 12, 0)[0]   # 第一次 / 主机重启过：不补历史，从现在开始
            self._send_json(200, {"ok": True, "epoch": self.link.ring.epoch, "seq": since, "events": []})
            return
        seq, evts = self.link.ring.since(since, min(25.0, float(q.get("wait", "25") or 25)))
        self.link.note_ports(evts)
        self._send_json(200, {"ok": True, "epoch": self.link.ring.epoch, "seq": seq, "events": evts})

    def _file(self, q):
        try:
            path = self.link.file_resolver(q)
        except PermissionError as e:
            self._send_json(403, {"ok": False, "error": str(e)})
            return
        if not path or not os.path.isfile(path):
            self._send_json(404, {"ok": False, "error": "文件不存在"})
            return
        size = os.path.getsize(path)
        start = 0
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes=") and rng[6:].split("-")[0].strip().isdigit():
            start = min(int(rng[6:].split("-")[0]), size)
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size - start))
        self.send_header("X-WWY-Size", str(size))
        self.send_header("X-WWY-Name", urllib.parse.quote(os.path.basename(path)))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                shutil.copyfileobj(f, self.wfile, 1024 * 512)
        except (OSError, ConnectionError):
            self.close_connection = True

    def _tunnel(self, q):
        try:
            port = int(q.get("port", ""))
        except ValueError:
            port = 0
        if port not in self.link.allowed_ports:
            self._send_json(403, {"ok": False, "error": f"主机没有开放端口 {port}"})
            return
        try:
            up = socket.create_connection(("127.0.0.1", port), timeout=10)
        except OSError as e:
            self._send_json(502, {"ok": False, "error": f"主机上的 {port} 端口连不上：{e}"})
            return
        up.settimeout(None)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("X-WWY-Tunnel", "1")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        conn = self.connection
        conn.settimeout(None)

        def down():   # 主机端口 → 远程
            try:
                while True:
                    buf = up.recv(65536)
                    if not buf:
                        break
                    conn.sendall(buf)
            except OSError:
                pass
            finally:
                _shut(conn)
        t = threading.Thread(target=down, daemon=True, name="link-tunnel-down")
        t.start()
        try:          # 远程 → 主机端口（rfile 是带缓冲的，用 read1 别漏掉已读进缓冲的字节）
            while True:
                buf = self.rfile.read1(65536)
                if not buf:
                    break
                up.sendall(buf)
        except (OSError, ValueError):
            pass
        finally:
            _shut(up)
        t.join(timeout=30)
        try:
            up.close()
        except OSError:
            pass

    # ---- PUT /upload ----
    def do_PUT(self):
        if not self._auth("UNSIGNED"):
            return
        q = self._query()
        if self._route() != "/upload":
            self._send_json(404, {"ok": False, "error": "没有这个接口"})
            return
        batch = re.sub(r"[^A-Za-z0-9_-]", "", q.get("batch", ""))[:40]
        name = os.path.basename(q.get("name", "").replace("\\", "/")).strip()
        if not batch or not name or name in (".", ".."):
            self._send_json(400, {"ok": False, "error": "缺少文件名"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        d = os.path.join(INBOX_DIR, batch)
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, name)
        part = dest + ".uploading"
        try:
            with open(part, "wb") as f:
                left = n
                while left > 0:
                    buf = self.rfile.read(min(1024 * 512, left))
                    if not buf:
                        break
                    f.write(buf)
                    left -= len(buf)
            if left:
                raise OSError("连接中途断开")
            os.replace(part, dest)
        except OSError as e:
            try:
                os.remove(part)
            except OSError:
                pass
            self.close_connection = True
            self._send_json(500, {"ok": False, "error": f"主机保存失败：{e}"})
            return
        self._send_json(200, {"ok": True, "path": dest})


def _shut(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def clean_inbox(max_age=86400):
    """收件箱里一天前没导入完的残留（上传到一半远程掉线等）删掉"""
    if not os.path.isdir(INBOX_DIR):
        return
    now = time.time()
    for n in os.listdir(INBOX_DIR):
        p = os.path.join(INBOX_DIR, n)
        try:
            if now - os.path.getmtime(p) > max_age:
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass


# ============================================================
# 远程端
# ============================================================
class LinkError(Exception):
    pass


def discover(key, timeout=1.5):
    """局域网广播找拿着这把密钥的主机：[(ip, port, 主机名)]"""
    kid = key_id(key)
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.3)
    except OSError:
        return found
    msg = json.dumps({"wwy": "find", "kid": kid}).encode()
    deadline = time.time() + timeout
    next_send = 0
    try:
        while time.time() < deadline:
            if time.time() >= next_send:      # 丢包很常见，隔一会儿再喊一遍
                for b in _broadcast_targets():
                    try:
                        s.sendto(msg, (b, DISCOVERY_PORT))
                    except OSError:
                        pass
                next_send = time.time() + 0.5
            try:
                data, addr = s.recvfrom(2048)
                r = json.loads(data.decode("utf-8", "replace"))
            except (OSError, ValueError):
                continue
            if isinstance(r, dict) and r.get("wwy") == "here" and r.get("kid") == kid:
                item = (addr[0], int(r.get("port") or DEFAULT_PORT), str(r.get("name") or addr[0]))
                if item not in found:
                    found.append(item)
                    if len(found) >= 1 and time.time() + 0.3 < deadline:
                        deadline = time.time() + 0.3     # 找到了再等一小会儿收齐多网卡的回应
    finally:
        s.close()
    return found


class Peer:
    """一台主机的连接信息 + 发请求"""

    def __init__(self, addr, port, key, name="", pid=""):
        self.addr, self.port, self.key = addr, int(port), key
        self.name = name or addr
        self.pid = pid or secrets.token_hex(4)
        self.offset = 0.0         # 主机时间 - 本机时间（时钟不准时自动校准）
        self.client_name = socket.gethostname()

    def to_json(self):
        return {"id": self.pid, "name": self.name, "addr": self.addr, "port": self.port, "key": self.key}

    @classmethod
    def from_json(cls, d):
        return cls(d["addr"], d.get("port") or DEFAULT_PORT, d["key"], d.get("name", ""), d.get("id", ""))

    def _conn(self, timeout):
        return http.client.HTTPConnection(self.addr, self.port, timeout=timeout)

    def request(self, method, path, body=b"", timeout=60, headers=None, unsigned=False, _retry=True):
        """发一个签过名的请求，返回 (状态码, 响应头 dict, HTTPResponse 已读完的 body)"""
        conn = self._conn(timeout)
        h = {AUTH_HEADER: make_auth(self.key, method, path, body, self.offset, unsigned),
             "X-WWY-Name": urllib.parse.quote(self.client_name)}
        if headers:
            h.update(headers)
        try:
            conn.request(method, path, body=body if body else None, headers=h)
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise LinkError(f"连不上主机 {self.addr}:{self.port}（{_net_reason(e)}）")
        conn.close()
        if resp.status == 401 and _retry and _is_clock(data):
            self._sync_clock(resp)
            return self.request(method, path, body, timeout, headers, unsigned, _retry=False)
        return resp.status, dict(resp.getheaders()), data

    def _sync_clock(self, resp):
        try:
            self.offset = float(resp.getheader("X-WWY-Time")) - time.time()
        except (TypeError, ValueError):
            pass

    def rpc(self, method, args=(), timeout=300):
        body = json.dumps({"method": method, "args": list(args)}, ensure_ascii=False).encode("utf-8")
        status, _h, data = self.request("POST", "/rpc", body, timeout=timeout,
                                        headers={"Content-Type": "application/json"})
        try:
            r = json.loads(data.decode("utf-8"))
        except ValueError:
            raise LinkError(f"主机回应看不懂（HTTP {status}）")
        if status == 401:
            raise LinkError("主机拒绝了：" + (r.get("error") or "密钥不对"))
        if not r.get("ok"):
            raise LinkError(r.get("error") or f"主机出错（HTTP {status}）")
        return r.get("result")

    def hello(self, timeout=4):
        return self.rpc("link_hello", [], timeout=timeout)

    def events(self, since, epoch, wait=25):
        path = f"/events?since={since}&epoch={urllib.parse.quote(epoch or '')}&wait={wait}"
        status, _h, data = self.request("GET", path, timeout=wait + 15)
        if status != 200:
            raise LinkError(f"取事件失败（HTTP {status}）")
        return json.loads(data.decode("utf-8"))

    def download(self, query, dest, progress=None, cancelled=lambda: False):
        """GET /file?… 存到 dest（先写 .part，支持断点续传）"""
        path = "/file?" + urllib.parse.urlencode(query)
        part = dest + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        conn = self._conn(60)
        h = {AUTH_HEADER: make_auth(self.key, "GET", path, b"", self.offset)}
        if have:
            h["Range"] = f"bytes={have}-"
        try:
            conn.request("GET", path, headers=h)
            resp = conn.getresponse()
            if resp.status == 401 and _is_clock(resp.read()):
                conn.close()
                self._sync_clock(resp)
                return self.download(query, dest, progress, cancelled)
            if resp.status not in (200, 206):
                try:
                    err = json.loads(resp.read().decode("utf-8")).get("error")
                except ValueError:
                    err = ""
                raise LinkError(err or f"下载失败（HTTP {resp.status}）")
            if resp.status == 200:
                have = 0
            total = int(resp.getheader("X-WWY-Size") or 0) or None
            with open(part, "ab" if have else "wb") as f:
                done = have
                while True:
                    if cancelled():
                        raise LinkError("已取消")
                    buf = resp.read(1024 * 512)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if progress:
                        progress(done, total)
            if total and done < total:
                raise LinkError("连接中途断开，再点一次会接着下")
        except (OSError, http.client.HTTPException) as e:
            raise LinkError(f"下载中断（{_net_reason(e)}），再点一次会接着下")
        finally:
            conn.close()
        os.replace(part, dest)
        return dest

    def upload(self, local_path, batch, progress=None, cancelled=lambda: False):
        """PUT 到主机收件箱，返回主机上的路径"""
        name = os.path.basename(local_path)
        path = "/upload?" + urllib.parse.urlencode({"batch": batch, "name": name})
        size = os.path.getsize(local_path)
        conn = self._conn(120)
        try:
            conn.putrequest("PUT", path)
            conn.putheader(AUTH_HEADER, make_auth(self.key, "PUT", path, b"", self.offset, unsigned_body=True))
            conn.putheader("Content-Length", str(size))
            conn.putheader("Content-Type", "application/octet-stream")
            conn.endheaders()
            done = 0
            with open(local_path, "rb") as f:
                while True:
                    if cancelled():
                        raise LinkError("已取消")
                    buf = f.read(1024 * 512)
                    if not buf:
                        break
                    conn.send(buf)
                    done += len(buf)
                    if progress:
                        progress(done, size)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status == 401 and _is_clock(data):
                conn.close()
                self._sync_clock(resp)
                return self.upload(local_path, batch, progress, cancelled)
            r = json.loads(data.decode("utf-8"))
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise LinkError(f"上传 {name} 失败（{_net_reason(e)}）")
        finally:
            conn.close()
        if not r.get("ok"):
            raise LinkError(r.get("error") or "上传失败")
        return r["path"]

    def open_tunnel(self, port, timeout=10):
        """连到主机、发 /tunnel 请求，200 之后返回这条 socket（已经是到主机端口的管道）"""
        path = f"/tunnel?port={int(port)}"
        s = socket.create_connection((self.addr, self.port), timeout=timeout)
        req = (f"GET {path} HTTP/1.1\r\nHost: {self.addr}:{self.port}\r\n"
               f"{AUTH_HEADER}: {make_auth(self.key, 'GET', path, b'', self.offset)}\r\n\r\n")
        s.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            buf = s.recv(4096)
            if not buf:
                s.close()
                raise LinkError("主机断开了隧道连接")
            head += buf
            if len(head) > 65536:
                s.close()
                raise LinkError("主机回应异常")
        hdr, _, rest = head.partition(b"\r\n\r\n")
        first = hdr.split(b"\r\n", 1)[0]
        if b" 200 " not in first + b" ":
            s.close()
            raise LinkError("主机拒绝转发端口 " + str(port) + "：" + first.decode("latin-1"))
        s.settimeout(None)
        return s, rest


def _is_clock(data):
    try:
        return json.loads(data.decode("utf-8")).get("reason") == "clock"
    except (ValueError, AttributeError):
        return False


def _net_reason(e):
    if isinstance(e, socket.timeout):
        return "超时"
    if isinstance(e, ConnectionRefusedError):
        return "对方没开联动，或端口 / 防火墙没放行"
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


class Forwarder:
    """本机 127.0.0.1:随机端口 → 主机 127.0.0.1:port。每个进来的连接开一条隧道"""

    def __init__(self, peer, remote_port):
        self.peer = peer
        self.remote_port = remote_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.local_port = self.sock.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept, daemon=True, name=f"link-fwd-{remote_port}").start()

    def _accept(self):
        while not self.closed:
            try:
                c, _a = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True, name="link-fwd-conn").start()

    def _serve(self, c):
        try:
            up, rest = self.peer.open_tunnel(self.remote_port)
        except (OSError, LinkError) as e:
            msg = f"联动隧道连不上主机：{e}".encode("utf-8")
            try:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n"
                          b"Content-Length: " + str(len(msg)).encode() + b"\r\nConnection: close\r\n\r\n" + msg)
            except OSError:
                pass
            c.close()
            return
        if rest:
            try:
                c.sendall(rest)
            except OSError:
                pass
        _pipe(c, up)

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def _pipe(a, b):
    def one(src, dst):
        try:
            while True:
                buf = src.recv(65536)
                if not buf:
                    break
                dst.sendall(buf)
        except OSError:
            pass
        finally:
            _shut(dst)
    t = threading.Thread(target=one, args=(b, a), daemon=True, name="link-pipe")
    t.start()
    one(a, b)
    t.join(timeout=30)
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


class LinkClient:
    """远程模式：当前在控制哪台主机、事件泵、端口转发表"""

    def __init__(self, deliver, on_status):
        self.deliver = deliver          # 把主机事件交给本机界面
        self.on_status = on_status      # (online: bool, text) 连接状态变了
        self.peer = None
        self._forwards = {}
        self._fwd_lock = threading.Lock()
        self._gen = 0
        self.online = False

    @property
    def active(self):
        return self.peer is not None

    def enter(self, peer):
        self.leave()
        self.peer = peer
        self._gen += 1
        gen = self._gen
        threading.Thread(target=self._pump, args=(peer, gen), daemon=True, name="link-events").start()

    def leave(self):
        # 端口转发不关：远程模式里打开的 WebUI 标签页回到本机界面后还要能接着用
        self.peer = None
        self._gen += 1
        self.online = False

    def drop_peer(self, pid):
        """删掉一台主机时才关它的端口转发"""
        with self._fwd_lock:
            for k in [k for k in self._forwards if k[0] == pid]:
                self._forwards.pop(k).close()

    def forward_port(self, port, peer=None):
        peer = peer or self.peer
        if peer is None:
            return None
        key = (peer.pid, port)
        with self._fwd_lock:
            f = self._forwards.get(key)
            if f is None or f.closed:
                try:
                    f = Forwarder(peer, port)
                except OSError:
                    return None
                self._forwards[key] = f
            return f.local_port

    def rewrite(self, obj, peer=None):
        peer = peer or self.peer
        return rewrite_local_urls(obj, lambda p: self.forward_port(p, peer))

    def call(self, method, args, timeout=300):
        peer = self.peer
        if peer is None:
            raise LinkError("没有在控制远程主机")
        return self.rewrite(peer.rpc(method, args, timeout=timeout), peer)

    def _pump(self, peer, gen):
        seq, epoch, fails = 0, "", 0
        while self._gen == gen:
            try:
                r = peer.events(seq, epoch)
                if not self.online:
                    self.online = True
                    self.on_status(True, "")
                fails = 0
                if r.get("epoch") != epoch and epoch:
                    self.deliver({"scope": "link", "type": "host_restarted"})
                epoch, seq = r.get("epoch", ""), int(r.get("seq") or 0)
                if self._gen != gen:
                    return
                for evt in r.get("events") or []:
                    if isinstance(evt, dict):
                        self.deliver(self.rewrite(evt, peer))
            except (LinkError, ValueError, OSError) as e:
                fails += 1
                if self.online or fails == 1:
                    self.online = False
                    self.on_status(False, str(e))
                time.sleep(min(10, 1.5 * fails))
