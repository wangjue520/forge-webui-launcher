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

import config_manager as cm
from webview_api import LauncherApi

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(APP_DIR, "assets", "wwy_launcher_icon.ico")


def _patch_http_server_backlog():
    """
    pywebview 用 wsgiref 起内置 HTTP 服务器给界面供文件，wsgiref 默认监听队列
    （backlog）只有 5。页面加载时浏览器同时发起十几个 js/css 请求，系统一忙
    （杀软扫描等）accept 不过来，多余的连接直接被拒——表现为随机几个页面
    模块没加载上：按钮没反应、功能缺失，重启又好了（用户反馈"第一次打不开
    第二次就好"的根源之一）。这里把 backlog 提到 128 并开 daemon_threads，
    其余行为与 pywebview 自带的 ThreadedAdapter 完全一致。
    结构对不上（pywebview 改版）就静默退回默认实现，绝不影响启动。
    """
    try:
        import bottle
        from socketserver import ThreadingMixIn
        from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
        import webview.http as wh

        class BigBacklogAdapter(bottle.ServerAdapter):
            def run(self, handler):
                if self.quiet:
                    class QuietHandler(WSGIRequestHandler):
                        def log_request(*args, **_):
                            pass
                    self.options["handler_class"] = QuietHandler

                class ThreadAdapter(ThreadingMixIn, WSGIServer):
                    daemon_threads = True
                    request_queue_size = 128

                server = make_server(self.host, self.port, handler,
                                     server_class=ThreadAdapter, **self.options)
                server.serve_forever()

        wh.ThreadedAdapter = BigBacklogAdapter
    except Exception:
        pass


def _bind_dom_events(window, api):
    """拖放：拿到拖入文件的完整路径。

    网页里的 File 对象拿不到磁盘路径（浏览器安全限制）。WebView2 下 pywebview 的做法是：
    JS 调 chrome.webview.postMessageWithAdditionalObjects("FilesDropped", files)，
    原生侧把每个文件的完整路径存进 webview.dom._dnd_state["paths"]。

    以前这里用 pywebview 的 DOMEventHandler 监听 dragover / drop，但它会把整个事件对象
    序列化后传给 Python——事件里的 currentTarget 是 document，会把【整棵 DOM 树】连同
    所有属性一起序列化（实测约 2MB、300ms 一次），而 dragover 在拖动时每秒触发十几次，
    界面直接卡死，drop 也经常传不过来。

    现在拖放完全由前端自己处理（web/js/core.js 的 bindFileDrop），只发文件名过来，
    Python 侧（LauncherApi.drop_files）按文件名从 _dnd_state 里取完整路径。
    这里只需要告诉 pywebview「有人在收拖放路径」（num_listeners > 0 它才会存）。
    """
    try:
        from webview.dom import _dnd_state
    except ImportError:
        try:
            from webview.util import _dnd_state
        except ImportError:
            _dnd_state = None
    if isinstance(_dnd_state, dict) and "paths" in _dnd_state:
        _dnd_state["num_listeners"] = _dnd_state.get("num_listeners", 0) + 1
        api._dnd_state = _dnd_state
        return

    # 兜底（pywebview 内部结构变了）：退回旧做法，只监听 drop，不再监听 dragover
    def on_drop(e):
        files = ((e or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files
                 if isinstance(f, dict) and f.get("pywebviewFullPath")]
        if paths:
            api.handle_dropped_paths(paths)

    window.dom.document.events.drop += DOMEventHandler(on_drop, prevent_default=True)


def main():
    _patch_http_server_backlog()
    api = LauncherApi()
    cm.write_theme_file(api.cfg)   # 开屏动画在后端应答前就要读风格文件
    window = webview.create_window(
        "WWY 启动器",
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
        # 所以和实例运行中一样走「取消关闭 + 弹确认框」的路径
        if api.webui_running() or api.deploy_running():
            # 让前端弹「实例正在运行」确认框， veto 本次关闭。
            # 注意：closing 事件跑在 GUI 主线程上，而 request_exit_confirm 里要
            # evaluate_js 同步等 JS 返回——直接在主线程里调会和正在进行的窗口
            # 关闭动作重入死锁（窗口表现为「未响应」），所以扔到后台线程执行。
            threading.Thread(target=api.request_exit_confirm, daemon=True).start()
            return False
        return True

    window.events.closing += on_closing

    webview.start(lambda: _bind_dom_events(window, api),
                  icon=ICON_PATH if os.path.isfile(ICON_PATH) else None)


if __name__ == "__main__":
    sys.exit(main())
