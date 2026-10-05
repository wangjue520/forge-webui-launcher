# -*- coding: utf-8 -*-
"""
首次启动时在桌面和启动器文件夹里各建一个带图标的快捷方式（图标 assets/wwy_launcher_icon.ico）。

.bat 自己没法带图标（Windows 只给 .lnk / .exe 显示自定义图标），所以建一个指向
「启动WWY启动器.bat」的 .lnk，图标设在快捷方式上；实际创建由 create_shortcuts.ps1 完成。

「建过了」按 电脑 + 用户 + 启动器所在目录 记录在 launcher_data/shortcuts.json：
  - 文件夹整个打包发给别人：别人的电脑对不上 → 在他那边重新建
  - 启动器挪了位置：旧快捷方式指向的路径已失效 → 重新建
  - 用户自己删了桌面图标：记录对得上 → 不会再自作主张建回来
以前用的是 launcher_data/desktop_shortcut.flag，那个文件会跟着打包一起发出去，
导致收到的人永远不会自动建快捷方式。
"""
import json
import os
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MARKER = os.path.join(APP_DIR, "launcher_data", "shortcuts.json")
SCRIPT = os.path.join(APP_DIR, "create_shortcuts.ps1")
ICON = os.path.join(APP_DIR, "assets", "wwy_launcher_icon.ico")
DESKTOP_NAME = "WWY 启动器.lnk"
FOLDER_NAME = "启动WWY启动器.lnk"

_NO_WINDOW = 0x08000000


def _key():
    import config_manager as cm
    return {"machine": cm.machine_id(),
            "user": os.environ.get("USERNAME", ""),
            "dir": os.path.normcase(APP_DIR)}


def _desktop_dir():
    """真实桌面路径（被 OneDrive 接管或改过位置的桌面也认）"""
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        # CSIDL_DESKTOPDIRECTORY = 0x10
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def ensure_shortcuts(log=None, force=False):
    """需要时建快捷方式。返回建好的快捷方式路径列表（没建返回空列表）。任何失败都静默。"""
    if os.name != "nt" or not (os.path.isfile(SCRIPT) and os.path.isfile(ICON)):
        return []
    key = _key()
    if not force:
        try:
            with open(MARKER, "r", encoding="utf-8") as f:
                if json.load(f) == key:
                    return []
        except (OSError, ValueError):
            pass
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT],
                       cwd=APP_DIR, capture_output=True, timeout=60, creationflags=_NO_WINDOW)
    except Exception as e:
        if log:
            log(f"创建快捷方式失败: {e}")
        return []
    made = [p for p in (os.path.join(APP_DIR, FOLDER_NAME), os.path.join(_desktop_dir(), DESKTOP_NAME))
            if os.path.isfile(p)]
    if made:
        try:
            os.makedirs(os.path.dirname(MARKER), exist_ok=True)
            with open(MARKER, "w", encoding="utf-8") as f:
                json.dump(key, f)
        except OSError:
            pass
    return made
