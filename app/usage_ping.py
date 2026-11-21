# -*- coding: utf-8 -*-
"""
匿名使用计数：借 GitHub Release 的下载次数数用户，不用自己的服务器。

仓库里有个 tag 为 usage-ping 的 Release，挂两个几字节的小文件：
- user.txt   每台电脑第一次打开启动器时下载一次 → 下载次数 ≈ 用户数
- update.txt 每台电脑每升到一个新版本，第一次打开时下载一次 → 下载次数 ≈ 更新人次

记号存在 %APPDATA%\\WWY\\launcher_usage.json（不在启动器目录里），所以同一台电脑上解压了几份
启动器、删了重装，都只算一个人；同一个版本重复打开不会重复计。下载失败不记号，下次打开再试。

除了下载这两个文件，什么都不发：没有编号、没有路径、没有配置。GitHub 能看到的只有「有人下载了一次」。
配置里 usage_ping 设成 false 就完全关掉。查看次数：python tools/usage_stats.py
"""
import json
import os
import threading
import time

import requests

REPO = "wangjue520/forge-webui-launcher"
TAG = "usage-ping"
USER_URL = f"https://github.com/{REPO}/releases/download/{TAG}/user.txt"
UPDATE_URL = f"https://github.com/{REPO}/releases/download/{TAG}/update.txt"
STARTUP_DELAY = 30   # 秒；别和开屏、检查更新抢网


def _marker_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, "WWY", "launcher_usage.json")


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _candidates(url, cfg):
    try:
        import updater
        return updater._candidate_urls(url, cfg, None)
    except Exception:
        return [url]


def _hit(url, cfg):
    """直连 / 各加速代理挨个试，有一个下成功就算数"""
    for u in _candidates(url, cfg):
        try:
            resp = requests.get(u, headers={"User-Agent": "ForgeLauncher/1.0"}, timeout=15)
            if resp.status_code == 200 and len(resp.content) < 1024:
                return True
        except Exception:
            pass
    return False


def _current_version(cfg):
    try:
        import updater
        v = updater.get_local_info(cfg).get("version") or ""
        return "" if v == "未知" else v
    except Exception:
        return ""


def ping(cfg, marker=None):
    """同步执行一次计数。返回本次实际计上的 {"user": bool, "update": bool}"""
    done = {"user": False, "update": False}
    if (cfg or {}).get("usage_ping") is False:
        return done
    path = marker or _marker_path()
    data = _load(path)
    version = _current_version(cfg)

    if not data.get("user_counted"):
        if _hit(USER_URL, cfg):
            data["user_counted"] = time.strftime("%Y-%m-%d")
            data["version"] = version      # 新用户这次就不再算「更新」
            done["user"] = True
        else:
            return done                    # 连不上就整个下次再来
    elif version and data.get("version") != version:
        if _hit(UPDATE_URL, cfg):
            data["version"] = version
            done["update"] = True

    if done["user"] or done["update"]:
        _save(path, data)
    return done


def start_background(cfg, delay=STARTUP_DELAY):
    """启动器打开后在后台跑一次，失败不打扰用户"""
    def run():
        time.sleep(delay)
        try:
            ping(cfg)
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True, name="usage-ping")
    t.start()
    return t
