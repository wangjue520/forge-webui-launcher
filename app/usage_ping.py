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
    """直连 / 各加速代理挨个试，有一个下成功就算数。

    只有「根本没连上」（连接失败 / 连接超时）才换下一个地址；已经连上但读超时、返回异常的，
    请求可能已经到了 GitHub、被计过一次，再换地址就会重复计，所以本次到此为止，下次打开再说。
    """
    for u in _candidates(url, cfg):
        try:
            resp = requests.get(u, headers={"User-Agent": "ForgeLauncher/1.0"}, timeout=(6, 20))
        except requests.ConnectionError:   # 含连接超时、代理 / SSL 握手失败：请求还没发出去
            continue
        except Exception:                  # 读超时等：请求可能已经到了 GitHub
            return False
        return resp.status_code == 200 and len(resp.content) < 1024
    return False


def _version_key(v):
    """'3.1.032' → (3, 1, 32)；认不出来返回 None"""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return None


LOCK_STALE = 300   # 秒；锁文件比这旧说明上次那个进程中途没了


def _acquire_lock(path):
    """同时开了两个启动器时只让一个去计数；锁文件建不出来（目录写不进去）也不计——
    记号存不下的话，每次打开都会被当成新用户重复计"""
    lock = path + ".lock"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return None
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return lock
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) < LOCK_STALE:
                    return None
                os.remove(lock)
            except OSError:
                return None
        except OSError:
            return None
    return None


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
    lock = _acquire_lock(path)
    if not lock:
        return done
    try:
        data = _load(path)
        version = _current_version(cfg)
        key = _version_key(version)

        if not data.get("user_counted"):
            if not _save(path, data):          # 先确认记号写得进去，写不进去就不计
                return done
            if _hit(USER_URL, cfg):
                data["user_counted"] = time.strftime("%Y-%m-%d")
                data["version"] = version      # 新用户这次就不再算「更新」
                done["user"] = True
        elif key and key > (_version_key(data.get("version")) or ()):
            # 只在比记过的最高版本还新时才算更新：同一台电脑上 git 版、zip 版启动器
            # 版本号差一位，轮流打开不会来回计
            if _hit(UPDATE_URL, cfg):
                data["version"] = version
                done["update"] = True

        if done["user"] or done["update"]:
            _save(path, data)
        return done
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


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
