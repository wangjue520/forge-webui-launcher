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

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根目录（代码在 app/ 子目录里）
LOG_PATH = os.path.join(APP_DIR, "launcher_data", "launcher.log")

# 由 bat 用 pythonw.exe 拉起时没有控制台（不再挂着一个黑框）：
# print / 报错输出改写进 launcher_data/launcher.log，出问题时还能翻
WINDOWLESS = sys.stdout is None or os.path.basename(sys.executable).lower() == "pythonw.exe"
if WINDOWLESS:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        _log_file = open(LOG_PATH, "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stderr = _log_file
    except OSError:
        _devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stderr = _devnull

try:   # 上次 ZIP 更新时被界面占用、暂存起来的文件，趁界面还没打开先换上（必须在导入其它模块之前）
    import updater
    updater.apply_pending(print)
except Exception:
    pass

import webview
from webview.dom import DOMEventHandler

import config_manager as cm
from webview_api import LauncherApi

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


def _no_console_for_children():
    """
    没有控制台时，子进程（git / pip / python …）只要没带 CREATE_NO_WINDOW，Windows 就会
    给它新开一个黑框。大部分调用已经带了，这里给漏掉的统一补上；显式要求
    新控制台 / 脱离控制台的调用保持原样。GUI 程序（explorer 等）会忽略这个标志。
    """
    if os.name != "nt" or not WINDOWLESS:
        return
    import subprocess
    CREATE_NEW_CONSOLE, DETACHED_PROCESS, CREATE_NO_WINDOW = 0x10, 0x08, 0x08000000
    orig_init = subprocess.Popen.__init__

    def patched_init(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) or 0
        if not flags & (CREATE_NEW_CONSOLE | DETACHED_PROCESS):
            kwargs["creationflags"] = flags | CREATE_NO_WINDOW
        orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched_init


def _ignore_stale_js_callbacks():
    """
    页面刷新（换主题 / 切实例会 reload）时，刷新前发出的 js_api 调用（比如训练器卡片
    每 5 秒一次的 trainer_status）在 Python 侧跑完回传结果，可 JS 侧的回调表已经随旧
    页面没了，pywebview 的后台线程就抛 JavascriptException「… is not a function」，
    打一大段红字。结果本来就没人要了，这种直接忽略；其他线程异常照常输出。
    """
    default_hook = threading.excepthook

    def hook(args):
        if (args.exc_type is not None and args.exc_type.__name__ == "JavascriptException"
                and "_returnValuesCallbacks" in str(args.exc_value)):
            return
        default_hook(args)

    threading.excepthook = hook


def _show_fatal_error(text):
    """没有控制台时启动失败，弹个系统对话框告诉用户，而不是窗口一闪就没了。"""
    if os.name != "nt" or not WINDOWLESS:
        return
    try:
        import ctypes
        msg = (text.strip()[-1500:] + "\n\n"
               "如果是窗口相关的错误（白屏、WebView2 之类），多半是缺少 Edge WebView2 运行时：\n"
               "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
               f"完整日志：{LOG_PATH}\n"
               "也可以在命令行里运行「启动WWY启动器.bat debug」，带控制台启动查看报错。")
        ctypes.windll.user32.MessageBoxW(None, msg, "WWY 启动器异常退出", 0x10)
    except Exception:
        pass


def _quiet_windows_error_dialogs():
    """
    关掉 Windows 的系统报错弹窗（「无法定位程序输入点 …于动态链接库 …上」这类）。
    子进程会继承这个设置：WebUI / pip / 环境检查里加载到不配套的 DLL（比如 ROCm 版
    torch 配上 CUDA 版 torchvision 的 _C.pyd）时不再弹一个要人点「确定」的窗口把进程
    卡住，而是照常报 ImportError，错误进运行日志，启动器能识别并给出修复提示。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        SEM_FAILCRITICALERRORS, SEM_NOGPFAULTERRORBOX, SEM_NOOPENFILEERRORBOX = 0x0001, 0x0002, 0x8000
        k32 = ctypes.windll.kernel32
        k32.SetErrorMode(k32.GetErrorMode() | SEM_FAILCRITICALERRORS
                         | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX)
    except Exception:
        pass


def main():
    _quiet_windows_error_dialogs()
    _no_console_for_children()
    _ignore_stale_js_callbacks()
    try:   # 从旧版本（代码平铺在根目录）升级上来时，清掉根目录残留的旧代码
        import updater
        updater.cleanup_legacy_layout()
    except Exception:
        pass
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
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        print(tb, flush=True)
        _show_fatal_error(tb)
        sys.exit(1)
