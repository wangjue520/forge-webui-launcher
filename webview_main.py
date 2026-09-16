# -*- coding: utf-8 -*-
"""
Forge WebUI 启动器（pywebview 版）入口。

界面是 web/ 下的 HTML/CSS/JS（Edge WebView2 渲染，Windows 10/11 基本都自带
WebView2 运行时），后端逻辑在 webview_api.py，通过 pywebview 的 js_api 桥接。

运行方式：
    pip install pywebview requests
    python webview_main.py

退出处理：窗口关闭时如果 WebUI 子进程还在运行，先拦住关闭动作、让前端弹
确认框——直接放掉的话 cmd.exe/python.exe 会变成孤儿进程继续占着端口和
显存，下次启动就会遇到 [Errno 10048] 端口被占用。
"""
import os
import sys
import threading

import webview
from webview.dom import DOMEventHandler

from webview_api import LauncherApi

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _bind_dom_events(window, api):
    """绑定拖放事件。

    网页里的 File 对象拿不到磁盘路径（浏览器安全限制），但 pywebview 5 会把
    完整路径放在 Python 侧事件的 dataTransfer.files[i].pywebviewFullPath 里，
    所以拖放在 Python 侧接收、再把路径推回前端。
    """
    def on_dragover(_e):
        # dragover 必须 preventDefault，浏览器才允许 drop
        pass

    def on_drop(e):
        files = ((e or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files
                 if isinstance(f, dict) and f.get("pywebviewFullPath")]
        if paths:
            api.handle_dropped_paths(paths)

    window.dom.document.events.dragover += DOMEventHandler(on_dragover, prevent_default=True)
    window.dom.document.events.drop += DOMEventHandler(on_drop, prevent_default=True)


def main():
    api = LauncherApi()
    window = webview.create_window(
        "Forge WebUI 启动器",
        os.path.join(APP_DIR, "web", "index.html"),
        js_api=api,
        width=1180,
        height=820,
        min_size=(960, 640),
        background_color="#16171c",
    )
    api._set_window(window)

    def on_closing():
        # 部署进行中关窗会把正在装 torch 的进程树孤儿化（下次启动连环卡），
        # 所以和 WebUI 运行中一样走「取消关闭 + 弹确认框」的路径
        if api.webui_running() or api.deploy_running():
            # 让前端弹「WebUI 仍在运行」确认框， veto 本次关闭。
            # 注意：closing 事件跑在 GUI 主线程上，而 request_exit_confirm 里要
            # evaluate_js 同步等 JS 返回——直接在主线程里调会和正在进行的窗口
            # 关闭动作重入死锁（窗口表现为「未响应」），所以扔到后台线程执行。
            threading.Thread(target=api.request_exit_confirm, daemon=True).start()
            return False
        return True

    window.events.closing += on_closing

    webview.start(lambda: _bind_dom_events(window, api))


if __name__ == "__main__":
    sys.exit(main())
