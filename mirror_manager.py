# -*- coding: utf-8 -*-
"""
国内网络加速模块。

背景：这个启动器几乎所有下载都指向国外——便携 Python/Git 和源码、插件在
GitHub，torch 等依赖在 pypi.org 和 download.pytorch.org，WD14 模型在
HuggingFace。国内直连这些站点普遍慢且容易断（实测 torch 那个 2.4GB 的包
会中途 Connection timed out，靠 pip 自动续传才救回来）。

设计原则：
1) 自动检测网络环境，不强迫用户理解"我该不该开镜像"——检测一次结果缓存
   在配置里，之后不再重复探测。
2) 镜像只是"优先尝试"，失败一律回退原站。镜像站是第三方服务，随时可能
   关停或限速，写死依赖单一镜像是不负责任的，所以每个环节都保留原站兜底。
3) 用户可以在界面上强制指定"自动/总是用镜像/总是用原站"，覆盖自动检测。

检测方法：优先查出口 IP 的归属地（国内 -> 镜像，国外 -> 原站），
IP 查询接口全部失败时退回"并发请求国内/国外站点比延迟"的实测方案。
挂代理的用户出口 IP 显示为代理所在国，判定为国外、直连原站——
这正是想要的行为（代理下走原站通常更快）。
"""
import concurrent.futures
import re
import time

import requests

# ---- IP 归属地探测（首选信号）----
# 免 key、体量小、国内外都有节点的接口，任意一个通就行
_COUNTRY_ENDPOINTS = [
    "https://ipapi.co/country/",
    "http://ip-api.com/line/?fields=countryCode",
    "https://ipinfo.io/country",
]
_COUNTRY_TIMEOUT = 5

# ---- 探测用的目标 ----
# 选择原则：体量小、响应快、本身就是我们后续真正要用的服务
_PROBE_CN = "https://pypi.tuna.tsinghua.edu.cn/simple/"
_PROBE_GLOBAL = "https://pypi.org/simple/"
_PROBE_TIMEOUT = 6

# ---- 镜像配置 ----
# PyPI 镜像。清华源是国内高校镜像里最稳的之一，阿里云作为备选。
PYPI_MIRRORS = [
    ("清华大学", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple"),
    ("腾讯云", "https://mirrors.cloud.tencent.com/pypi/simple"),
]

# PyTorch 的 cu1xx wheel 不在普通 PyPI 里，需要专门的镜像路径。
# 清华镜像把 download.pytorch.org 的 whl 目录整个同步了过来。
PYTORCH_MIRROR_BASE = "https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels"

# HuggingFace 镜像：hf-mirror.com 是国内广泛使用的 HF 反向代理，
# 路径结构跟官方完全一致，只需替换域名。
HF_MIRROR = "https://hf-mirror.com"

# GitHub 加速代理：这类服务变动频繁，所以准备多个候选依次尝试。
# 用法是把原始 URL 拼在代理域名后面。
GITHUB_PROXIES = [
    "https://ghfast.top",
    "https://gh-proxy.com",
    "https://github.moeyy.xyz",
]


def _probe(url):
    """返回响应耗时（秒），不可达返回 None"""
    start = time.time()
    try:
        requests.head(url, timeout=_PROBE_TIMEOUT, allow_redirects=True)
        return time.time() - start
    except requests.RequestException:
        return None


def detect_country_code():
    """查询出口 IP 的国家代码（如 "CN"/"US"），所有接口都失败返回 None"""
    for url in _COUNTRY_ENDPOINTS:
        try:
            r = requests.get(url, timeout=_COUNTRY_TIMEOUT)
            if r.ok:
                code = r.text.strip().upper()
                if re.fullmatch(r"[A-Z]{2}", code):
                    return code
        except requests.RequestException:
            continue
    return None


def detect_network_environment():
    """
    返回 "cn" 或 "global"。

    首选：查出口 IP 归属地——CN 判国内，其他一律判国外（挂了代理的国内
    用户出口 IP 在境外，此时直连原站本来就更快，判国外是正确行为）。
    IP 查询接口全部失败时退回延迟实测兜底：
      - 国外不可达、国内可达            -> cn（典型的国内裸连环境）
      - 两边都可达，但国内明显更快      -> cn
      - 国外可达且不慢                  -> global
      - 两边都不可达                    -> global（网络本身有问题，
                                          用原站至少行为可预期）
    """
    country = detect_country_code()
    if country:
        return "cn" if country == "CN" else "global"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_cn = pool.submit(_probe, _PROBE_CN)
        f_global = pool.submit(_probe, _PROBE_GLOBAL)
        t_cn, t_global = f_cn.result(), f_global.result()

    if t_global is None:
        return "cn" if t_cn is not None else "global"
    if t_cn is None:
        return "global"
    # 两边都通：国内快出一倍以上才认为值得走镜像，避免在代理环境下
    # 多绕一层反而更慢
    return "cn" if t_cn * 2 < t_global else "global"


def resolve_mode(cfg, log_cb=None):
    """
    根据配置决定本次要不要用镜像，返回 True/False。

    cfg["mirror_mode"]: "auto"（默认）/ "always" / "never"
    自动检测的结果会缓存进 cfg["mirror_detected"]，避免每次操作都探测网络。

    注意：这个探测比的是"清华 PyPI vs pypi.org 哪个快"，反映的是通用网络
    质量，不代表 GitHub 本身是否可达——国内很多网络环境是选择性限制
    GitHub（DNS 污染/端口连接超时），同时 pypi.org 这类站点完全正常，
    会被这个通用探测误判成"不需要镜像"。凡是涉及 github.com 的地址，
    请用下面的 resolve_github_mode，不要用这个。
    """
    mode = cfg.get("mirror_mode", "auto")
    if mode == "always":
        return True
    if mode == "never":
        return False

    cached = cfg.get("mirror_detected")
    if cached in ("cn", "global"):
        return cached == "cn"

    if log_cb:
        log_cb("[网络] 正在检测网络环境以决定是否使用国内镜像 ...")
    result = detect_network_environment()
    cfg["mirror_detected"] = result
    if log_cb:
        log_cb("[网络] 检测结果: " + ("国内网络，后续下载优先使用镜像加速"
                                      if result == "cn" else
                                      "可直连国外站点，使用原始源"))
    return result == "cn"


# GitHub 专用探测：不复用上面 PyPI 的通用判断，直接实测能不能连上
# github.com。这是踩过的坑——国内很多网络对 GitHub 的限制比对其他站点
# 更狠（有时是选择性 DNS 污染/端口层面连接超时），通用探测会漏判。
_PROBE_GITHUB = "https://github.com/"
_GITHUB_PROBE_TIMEOUT = 8


def resolve_github_mode(cfg, log_cb=None):
    """
    专门决定"访问 github.com 相关地址（clone/下载 release）要不要走镜像"。
    cfg["mirror_mode"] 的 always/never 依然优先生效；auto 模式下单独实测
    github.com 是否可达，结果缓存进 cfg["github_mirror_detected"]，
    跟 mirror_detected（PyPI 那个）是两套独立缓存，互不影响。
    """
    mode = cfg.get("mirror_mode", "auto")
    if mode == "always":
        return True
    if mode == "never":
        return False

    cached = cfg.get("github_mirror_detected")
    if cached in ("blocked", "ok"):
        return cached == "blocked"

    if log_cb:
        log_cb("[网络] 正在检测 GitHub 是否可直连 ...")
    reachable = _probe(_PROBE_GITHUB) is not None
    # 上面这次探测用的默认超时（_PROBE_TIMEOUT，6秒）比实际连接超时短，
    # 探测报"不可达"这里就直接采信；报"可达"时再用更长的超时确认一次，
    # 避免临界情况下把慢连接误判成完全不通
    if reachable:
        start = time.time()
        try:
            requests.head(_PROBE_GITHUB, timeout=_GITHUB_PROBE_TIMEOUT)
        except requests.RequestException:
            reachable = False

    result = "ok" if reachable else "blocked"
    cfg["github_mirror_detected"] = result
    if log_cb:
        log_cb("[网络] GitHub 检测结果: " + ("可直连" if reachable else "无法直连，改用加速代理"))
    return result == "blocked"


# ============================================================
# 各环节的 URL 重写
# ============================================================

def pip_index_args(use_mirror, mirror_index=0):
    """
    返回追加给 pip 的参数列表。
    mirror_index 用于失败后换下一个镜像重试。
    """
    if not use_mirror:
        return []
    if mirror_index >= len(PYPI_MIRRORS):
        return []
    _name, url = PYPI_MIRRORS[mirror_index]
    host = url.split("//", 1)[1].split("/", 1)[0]
    return ["-i", url, "--trusted-host", host]


def github_url(url, use_mirror, proxy_index=0):
    """
    把 GitHub 下载/克隆地址换成加速代理地址。
    proxy_index 用于失败后换下一个代理重试；超出范围则返回原始地址。
    覆盖 github.com / raw.githubusercontent.com / codeload.github.com /
    api.github.com——部署流程查版本号走的就是 api.github.com，漏了它会导致
    「下载走镜像、查询却裸连」的半截子加速。
    """
    if not use_mirror or proxy_index >= len(GITHUB_PROXIES):
        return url
    if not url.startswith("https://github.com/") and \
       not url.startswith("https://raw.githubusercontent.com/") and \
       not url.startswith("https://codeload.github.com/") and \
       not url.startswith("https://api.github.com/"):
        return url
    return f"{GITHUB_PROXIES[proxy_index]}/{url}"


def huggingface_url(url, use_mirror):
    """把 huggingface.co 换成国内镜像域名"""
    if not use_mirror:
        return url
    return url.replace("https://huggingface.co", HF_MIRROR)


def torch_index_url(use_mirror, cuda_tag):
    """
    torch 的 cu1xx wheel 索引地址。
    cuda_tag 形如 "cu121" / "cu126"，跟 Forge 自己请求的版本保持一致。
    """
    if not use_mirror:
        return f"https://download.pytorch.org/whl/{cuda_tag}"
    return f"{PYTORCH_MIRROR_BASE}/{cuda_tag}"


def pip_env_overrides(use_mirror, cuda_tag=None):
    """
    生成注入给 webui.bat 子进程的环境变量，让 Forge 内部自己调用的 pip
    也走镜像——这是最关键的一环：torch 那 2.4GB 是 Forge 的 launch.py
    自己装的，不经过我们的代码，只能通过环境变量影响它。

    PIP_INDEX_URL / PIP_EXTRA_INDEX_URL 是 pip 官方支持的环境变量，
    优先级低于命令行参数，所以不会覆盖 Forge 显式指定的 --extra-index-url，
    但能改变默认索引，大部分包因此走镜像。
    """
    if not use_mirror:
        return {}
    _name, index = PYPI_MIRRORS[0]
    host = index.split("//", 1)[1].split("/", 1)[0]
    env = {
        "PIP_INDEX_URL": index,
        "PIP_TRUSTED_HOST": host,
        # 断点续传和超时放宽，2GB 级别的包在国内网络下很容易触发默认超时
        "PIP_RETRIES": "5",
        "PIP_TIMEOUT": "60",
    }
    if cuda_tag:
        env["PIP_EXTRA_INDEX_URL"] = torch_index_url(True, cuda_tag)
    return env
