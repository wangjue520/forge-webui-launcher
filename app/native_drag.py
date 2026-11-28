# -*- coding: utf-8 -*-
"""
把文件从启动器窗口「拖出去」（输出管理：拖到桌面 / 文件夹 / 聊天软件 = 复制一份原图）。

网页里的拖动（HTML5 DownloadURL）在 Chrome 里能直接存成文件，但在启动器的 WebView2 里会变成一次
「下载」，被 pywebview 拦掉，只弹出一个空的下载面板。所以改成原生拖动：网页在 dragstart 时取消
浏览器自己的拖动，调这里；这里在窗口的界面线程上调 WinForms 的 Form.DoDragDrop，交给 Windows
一份文件列表（CF_HDROP），和从资源管理器里拖文件完全一样。鼠标此时还按着，松开在哪儿就复制到哪儿。
（Electron 的 webContents.startDrag 也是这个做法。）
只在 Windows 的 pywebview 窗口里可用；拿不到原生窗口时返回失败，界面提示改用「复制图片」。
"""
import os
import threading

_LOCK = threading.Lock()
_BUSY = [False]


def _form_of(window):
    form = getattr(window, "native", None)
    return form if form is not None and hasattr(form, "BeginInvoke") and hasattr(form, "DoDragDrop") else None


def start(window, paths):
    """开始拖动这些文件。立即返回（拖动在界面线程上进行，松开鼠标才结束）"""
    paths = [os.path.abspath(p) for p in paths or [] if p and os.path.isfile(p)]
    if not paths:
        return {"ok": False, "error": "文件已经不在了"}
    form = _form_of(window)
    if form is None:
        return {"ok": False, "error": "这个窗口不支持直接拖出文件，请用「复制图片」再粘贴"}
    with _LOCK:
        if _BUSY[0]:
            return {"ok": False, "error": "上一次拖动还没结束"}
        _BUSY[0] = True
    try:
        import clr  # noqa: F401  pythonnet：pywebview 的 Windows 窗口本身就是它建的
        clr.AddReference("System")
        clr.AddReference("System.Windows.Forms")
        import System.Windows.Forms as WF
        from System import Func, Type
        from System.Collections.Specialized import StringCollection
        files = StringCollection()
        for p in paths:
            files.Add(p)
        data = WF.DataObject()
        data.SetFileDropList(files)
    except Exception as e:
        _BUSY[0] = False
        return {"ok": False, "error": f"拖出失败：{e}"}

    def run():
        try:
            form.DoDragDrop(data, WF.DragDropEffects.Copy)
        except Exception as e:      # 界面线程上的异常不能抛出去，打印即可
            print(f"[拖出] DoDragDrop 出错: {e}")
        finally:
            _BUSY[0] = False

    try:
        form.BeginInvoke(Func[Type](run))
    except Exception as e:
        _BUSY[0] = False
        return {"ok": False, "error": f"拖出失败：{e}"}
    return {"ok": True, "count": len(paths)}
