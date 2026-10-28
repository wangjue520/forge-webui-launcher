# -*- coding: utf-8 -*-
"""
让 .NET Framework 放行界面组件（pythonnet 的 Python.Runtime.dll）。

现象：界面打不开，日志里是 "Failed to resolve Python.Runtime.Loader.Initialize"。
clr_loader 的发行版把真实异常吞了，实际是 .NET 报 0x80131515（FileLoadException /
NotSupportedException）：它把 Python.Runtime.dll 当成「来自网络」的程序集，拒绝加载。
文件带下载标记时是这样；有用户的文件上查不到标记、照样被这么判（区域判定还受系统
设置影响），只解除标记不够。

解法是微软给这个错误的标准做法：在进程 exe 旁边放 <exe>.config，打开
loadFromRemoteSources。CLR 在第一次 import clr 时才启动、才读这个文件，所以启动器
自己在 import webview 之前写上就当场生效；bat 的 :check_clr 也会先跑一遍本文件。

注意 .venv\\Scripts\\python.exe 只是个转发器，真正跑的是基础 Python（启动器的 python\\
目录），配置要放在基础 Python 的 python.exe / pythonw.exe 旁边，放在 .venv 里没用。
"""
import os
import sys

CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<!-- WWY 启动器自动生成：允许 .NET Framework 加载被判为「来自网络」的程序集，
     否则 pythonnet 报 Failed to resolve Python.Runtime.Loader.Initialize（0x80131515）。见 app/dotnet_config.py -->
<configuration>
  <runtime>
    <loadFromRemoteSources enabled="true" />
  </runtime>
</configuration>
"""


def _process_exe():
    """当前进程真正的 exe（venv 转发器拉起的基础 python.exe / pythonw.exe）"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
            return buf.value
    except Exception:
        pass
    return getattr(sys, "_base_executable", "") or sys.executable


def ensure():
    """给基础 Python 的 python.exe / pythonw.exe 写好 .config。返回写入的文件列表；失败静默"""
    if os.name != "nt":
        return []
    exe = _process_exe()
    base = os.path.dirname(exe)
    names = {os.path.basename(exe).lower(), "python.exe", "pythonw.exe"}
    written = []
    for name in sorted(names):
        if not os.path.isfile(os.path.join(base, name)):
            continue
        cfg = os.path.join(base, name + ".config")
        try:
            if os.path.isfile(cfg):
                with open(cfg, encoding="utf-8", errors="replace") as f:
                    old = f.read()
                if "loadFromRemoteSources" in old:
                    continue
                os.replace(cfg, cfg + ".bak")   # 原来就有、内容不一样的留个备份
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(CONFIG)
            written.append(cfg)
        except OSError:
            pass   # 没权限（比如装在 Program Files 的系统 Python）就算了，bat 的诊断会给出指引
    return written


if __name__ == "__main__":
    for p in ensure():
        print(p)
