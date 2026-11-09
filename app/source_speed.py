# -*- coding: utf-8 -*-
"""
部署前测速：把每一类下载源都实测一遍，按速度挑最快的。

以前选源靠「出口 IP 在不在国内」：国内一律先走镜像、国外一律走原站。可镜像和
GitHub 加速代理今天快明天慢（实测同一时刻清华 PyPI 500 KB/s、阿里云 2 MB/s、
腾讯云 30 KB/s），开着梯子的国内用户走原站反而最快，写死顺序总有人挨慢。
现在部署开始前花十几秒，同时下载每个源上的同一个真实文件（各下几秒），按实测
速度排序，结果存进 cfg["source_rank"]，mirror_manager 选源时优先看它。

分三类测，三类分开排（各自的瓶颈不一样）：
  github  源码 / 便携 Python、Git / 插件：直连 + 各加速代理，测一个 MinGit 发布包
  pypi    依赖包：官方 + 各国内镜像，测同一个 numpy wheel（老版本，各镜像都同步过）
  torch   torch 本体（最大的一块，约 3GB）：官方 + 各镜像的 torch wheel

同一类里的源同时测（几秒测完），但会互相抢带宽，所以最快的那个如果看起来很慢，
再单独测一次确认，免得误报「所有源都很慢」。
"""
import re
import socket
import threading
import time
from urllib.parse import urljoin

import requests

import mirror_manager as mm

PROBE_SECONDS = 4.0          # 连上以后最多下几秒
CONNECT_TIMEOUT = 5.0        # 连接 + 首字节最多等几秒
PROBE_BYTES = 4 * 1024 * 1024
_CHUNK = 8 * 1024
_UA = "WWY-Launcher-SpeedTest/1.0"

# 测速用的真实文件（都是部署时会下的同类文件）
GITHUB_PROBE_FILE = ("https://github.com/git-for-windows/git/releases/download/"
                     "v2.56.0.windows.4/MinGit-2.56.0.4-64-bit.zip")
PYPI_PROBE_PATH = ("packages/cb/3b/d58c12eafcb298d4e6d0d40216866ab15f59e55d148a5658bb3132311fcf/"
                   "numpy-2.2.6-cp313-cp313-win_amd64.whl")
PYPI_OFFICIAL_FILES = "https://files.pythonhosted.org/"
DEFAULT_TORCH_TAG = "cu128"

GROUP_LABELS = {"github": "GitHub（源码 / 便携环境 / 插件）", "pypi": "PyPI（依赖包）",
                "torch": "PyTorch（torch 本体）"}

# 估算部署时间用的下载量（字节）。按实际部署的日志粗算：
# ComfyUI = 源码 + 便携 Python/Git + 常用节点 ≈ 150MB，依赖 ≈ 700MB，torch 三件套 ≈ 3.3GB
_MB = 1024 * 1024
PLAN_BYTES = {
    "comfyui": {"github": 150 * _MB, "pypi": 700 * _MB, "torch": 3300 * _MB},
    "forge": {"github": 250 * _MB, "pypi": 1000 * _MB, "torch": 3300 * _MB},
    "launcher": {"github": 30 * _MB, "pypi": 80 * _MB},
}

# 预计超过这个时间就算「所有源都很慢」
SLOW_SECONDS = 60 * 60


# ============================================================
# 候选源
# ============================================================

def github_sources(cfg):
    """[(id, 显示名, 测速地址)]；id 为空串 = 直连 github.com"""
    out = [("", "GitHub 直连", GITHUB_PROBE_FILE)]
    if (cfg or {}).get("mirror_mode") != "never":
        for p in mm.GITHUB_PROXIES:
            out.append((p, p.split("//", 1)[-1], f"{p.rstrip('/')}/{GITHUB_PROBE_FILE}"))
    return out


def pypi_files_base(index_url):
    """PyPI 镜像的 simple 索引地址 → 文件根地址（bandersnatch 镜像的 packages/ 路径和官方一致）"""
    u = index_url.rstrip("/")
    if u.endswith("/simple"):
        u = u[: -len("/simple")]
    return u + "/"


def pypi_sources(cfg):
    """[(id, 显示名, 测速地址)]；id 是镜像的 simple 索引地址，空串 = 官方 pypi.org"""
    out = [("", "PyPI 官方", PYPI_OFFICIAL_FILES + PYPI_PROBE_PATH)]
    if (cfg or {}).get("mirror_mode") != "never":
        for name, url in mm.PYPI_MIRRORS:
            out.append((url, name, pypi_files_base(url) + PYPI_PROBE_PATH))
    return out


def torch_sources(cfg):
    """[(id, 显示名, 索引基址)]；id 是不带 cuda tag 的基址"""
    out = [(mm.PYTORCH_OFFICIAL_WHL, "PyTorch 官方", mm.PYTORCH_OFFICIAL_WHL)]
    if (cfg or {}).get("mirror_mode") != "never":
        for b in mm.PYTORCH_MIRROR_BASES:
            out.append((b, b.split("//", 1)[-1].split("/", 1)[0], b))
    return out


_WHEEL_HREF_RE = re.compile(r'href="([^"]+?-win_amd64\.whl)(?:#[^"]*)?"', re.I)


def _torch_probe_url(base, tag, session):
    """从 <base>/<tag>/torch/ 索引里找一个 Windows 的 torch wheel，返回它的地址；找不到返回 None"""
    index = f"{base.rstrip('/')}/{tag}/torch/"
    r = session.get(index, timeout=(CONNECT_TIMEOUT, 8), headers={"User-Agent": _UA})
    if r.status_code != 200:
        raise requests.HTTPError(f"HTTP {r.status_code}")
    hrefs = _WHEEL_HREF_RE.findall(r.text)
    if not hrefs:
        raise requests.HTTPError("索引里没有 Windows 版 torch")
    pick = next((h for h in reversed(hrefs) if "cp313-cp313-" in h), hrefs[-1])
    return urljoin(index, pick)


# ============================================================
# 测速
# ============================================================

def _short_err(e):
    s = str(e)
    for key, text in (("timed out", "超时"), ("Timeout", "超时"), ("NameResolution", "域名解析失败"),
                      ("getaddrinfo", "域名解析失败"), ("Connection refused", "连接被拒绝"),
                      ("ConnectionReset", "连接被重置"), ("SSL", "证书 / TLS 握手失败"),
                      ("ProxyError", "代理连不上"), ("Max retries", "连不上")):
        if key in s:
            return text
    return s[:60] or e.__class__.__name__


def _abort(resp):
    """
    从别的线程打断一个还在读的响应：直接关底层 socket。
    不能用 resp.close()——它要拿读缓冲的锁，而锁正被卡在 read 里的测速线程占着，
    慢源（几百 B/s）一块 8KB 要读几十秒，close 就跟着卡几十秒。
    """
    try:
        conn = getattr(resp.raw, "connection", None) or getattr(resp.raw, "_connection", None)
        sock = getattr(conn, "sock", None)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


def measure_urls(urls, seconds=PROBE_SECONDS, resolve=None, cancelled=None):
    """
    同时下载这些地址各几秒，返回 {url: (字节/秒, 错误说明)}。
    resolve(url, session) 可选：先把 url 换成真正要测的文件地址（torch 要先查索引）。
    慢源的 read 可能一直不返回，所以由主线程按截止时间收尾、强行关连接，不等它。
    """
    state = {u: {"got": 0, "t0": time.monotonic(), "t1": None, "t_end": None, "err": "",
                 "resp": None, "done": False} for u in urls}

    def work(u):
        st = state[u]
        s = requests.Session()
        try:
            target = resolve(u, s) if resolve else u
            r = s.get(target, stream=True, timeout=(CONNECT_TIMEOUT, seconds + 2),
                      headers={"User-Agent": _UA, "Range": f"bytes=0-{PROBE_BYTES - 1}"})
            st["resp"] = r
            if r.status_code not in (200, 206):
                st["err"] = f"HTTP {r.status_code}"
                return
            st["t1"] = time.monotonic()
            for chunk in r.iter_content(_CHUNK):
                st["got"] += len(chunk)
                if st["got"] >= PROBE_BYTES:
                    break
        except Exception as e:
            if not st["got"]:
                st["err"] = _short_err(e)
        finally:
            st["t_end"] = time.monotonic()
            st["done"] = True
            try:
                s.close()
            except Exception:
                pass

    threads = [threading.Thread(target=work, args=(u,), daemon=True, name="speedtest") for u in urls]
    start = time.monotonic()
    for t in threads:
        t.start()
    deadline = start + CONNECT_TIMEOUT + seconds + (8 if resolve else 0)
    while time.monotonic() < deadline:
        if all(st["done"] for st in state.values()):
            break
        if cancelled and cancelled():
            break
        time.sleep(0.1)
    now = time.monotonic()
    out = {}
    for u, st in state.items():
        if not st["done"] and st["resp"] is not None:
            _abort(st["resp"])     # 打断还在慢慢读的连接
        end = st["t_end"] if st["done"] else now
        got = st["got"]
        if got <= 0:
            out[u] = (0.0, st["err"] or ("没有响应" if not st["done"] else "没下到数据"))
            continue
        # 下得够多就按「连上以后」的速度算（大文件拼的是吞吐，不是握手）；下得太少按总耗时算，免得虚高
        t_from = st["t1"] if (st["t1"] and got >= 512 * 1024) else st["t0"]
        out[u] = (got / max(end - t_from, 0.05), "")
    return out


def _rank_group(sources, seconds, resolve=None, cancelled=None):
    urls = [u for _id, _name, u in sources]
    res = measure_urls(urls, seconds, resolve=resolve, cancelled=cancelled)
    items = [{"id": sid, "name": name, "bps": round(res[u][0]), "err": res[u][1]}
             for sid, name, u in sources]
    best = max(items, key=lambda x: x["bps"])
    # 一起测会互相抢带宽：最快的看起来慢，就让它单独再测一次
    if 0 < best["bps"] < slow_bps_hint() and len(items) > 1 and not (cancelled and cancelled()):
        url = next(u for sid, _n, u in sources if sid == best["id"])
        again = measure_urls([url], seconds, resolve=resolve, cancelled=cancelled)[url]
        if again[0] > best["bps"]:
            best["bps"] = round(again[0])
            best["err"] = ""
    items.sort(key=lambda x: -x["bps"])
    return items


def slow_bps_hint():
    """单独复测的门槛：低于 2 MB/s 的「最快」才复测一次"""
    return 2 * _MB


def run(cfg, groups=("github", "pypi", "torch"), torch_tag=None, log=None, cancelled=None,
        seconds=PROBE_SECONDS):
    """
    按组测速，返回 {"time", "torch_tag", "github": [...], "pypi": [...], "torch": [...]}，
    每组按速度从快到慢排好：[{"id", "name", "bps", "err"}]。不写配置，调用方决定存哪。
    """
    log = log or (lambda m: None)
    tag = torch_tag or DEFAULT_TORCH_TAG
    out = {"time": time.time(), "torch_tag": tag}
    for g in groups:
        if cancelled and cancelled():
            break
        if g == "github":
            items = _rank_group(github_sources(cfg), seconds, cancelled=cancelled)
        elif g == "pypi":
            items = _rank_group(pypi_sources(cfg), seconds, cancelled=cancelled)
        elif g == "torch":
            srcs = torch_sources(cfg)
            items = _rank_group(srcs, seconds, cancelled=cancelled,
                                resolve=lambda base, s: _torch_probe_url(base, tag, s))
        else:
            continue
        out[g] = items
        log(f"[测速] {GROUP_LABELS.get(g, g)}：" + "，".join(
            f"{x['name']} {fmt_speed(x['bps'])}" if x["bps"] > 0 else f"{x['name']} 不通（{x['err']}）"
            for x in items))
    return out


# ============================================================
# 判断与显示
# ============================================================

def fmt_speed(bps):
    bps = float(bps or 0)
    if bps >= _MB:
        return f"{bps / _MB:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def fmt_duration(sec):
    if sec == float("inf"):
        return "无法估计（有一类源全部连不上）"
    sec = int(sec)
    if sec < 60:
        return "不到 1 分钟"
    h, m = divmod((sec + 59) // 60, 60)
    return (f"{h} 小时 {m} 分钟" if m else f"{h} 小时") if h else f"{m} 分钟"


def best_bps(rank, group):
    items = (rank or {}).get(group) or []
    return max((x.get("bps") or 0 for x in items), default=0)


def estimate_seconds(rank, plan="comfyui"):
    """按每类最快的源估算下载要多久（只算下载，不算解压 / 安装）"""
    total = 0.0
    for g, size in PLAN_BYTES.get(plan, PLAN_BYTES["comfyui"]).items():
        if g not in (rank or {}):
            continue
        b = best_bps(rank, g)
        if b <= 0:
            return float("inf")
        total += size / b
    return total


def is_slow(rank, plan="comfyui"):
    return estimate_seconds(rank, plan) > SLOW_SECONDS


def summary_lines(rank):
    """每类一行：最快的源和速度"""
    lines = []
    for g in ("github", "pypi", "torch"):
        items = (rank or {}).get(g)
        if not items:
            continue
        top = items[0]
        if top["bps"] > 0:
            lines.append(f"· {GROUP_LABELS[g]}：最快是 {top['name']}，{fmt_speed(top['bps'])}")
        else:
            lines.append(f"· {GROUP_LABELS[g]}：全部连不上")
    return lines


def store(cfg, rank):
    """测速结果写进配置（mirror_manager 选源时读它）。直连 / 官方最快时顺带更新老的检测缓存"""
    cfg["source_rank"] = rank
    for g, key, yes, no in (("pypi", "mirror_detected", "cn", "global"),
                            ("github", "github_mirror_detected", "blocked", "ok")):
        items = rank.get(g) or []
        if items and items[0]["bps"] > 0:
            cfg[key] = no if items[0]["id"] == "" else yes
