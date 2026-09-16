@echo off
cd /d "%~dp0"
title Forge WebUI 启动器
setlocal enabledelayedexpansion

rem ===================================================================
rem  启动入口：自己把环境准备好，用户只需要双击这一个文件。
rem
rem  做的事按顺序是：找 Python -> 查版本 -> 缺依赖就装 -> 启动。
rem  依赖齐全时这些检查加起来不到一秒，不会拖慢日常启动。
rem
rem  找不到 Python 也不会卡死：自动下载一个免安装的便携版 Python 3.13
rem  （python-build-standalone，uv 同款官方构建）到本启动器的 python\
rem  目录，全程不需要用户手动装任何东西。
rem ===================================================================

set "REQ=requirements.txt"
set "PY="

rem ---------- 1. 找一个能用的 Python ----------
rem 优先用启动器自带的便携版（如果做过便携打包/之前自动下载过）
if exist "%~dp0python\python.exe" (
    rem 引号要在这里就包进变量：便携版路径可能带空格，
    rem 后面所有 %PY% 的展开都得自带引号才不会被拆成两截
    set PY="%~dp0python\python.exe"
    goto :got_python
)

rem 其次用 py 启动器。它比直接叫 python 可靠：Windows 自带一个
rem "python" 假命令，敲下去只会打开应用商店，而 py 不受这个影响。
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    goto :got_python
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto :got_python
)

rem ---------- 2. 系统里一个 Python 都没有：自动下载便携版 ----------
echo.
echo   ============================================================
echo    没有检测到 Python —— 正在自动准备便携版 Python
echo   ============================================================
echo.
call :bootstrap_python
if errorlevel 1 goto :end_fail
set "PY=%~dp0python\python.exe"

:got_python
rem ---------- 3. 版本必须 >= 3.10；太旧就改用便携版 ----------
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto :check_deps

echo.
echo   [!] 检测到的 Python 版本低于 3.10，运行不了这个启动器。
%PY% -c "import sys; print('       当前版本: Python %%d.%%d' %% sys.version_info[:2])"
echo.
echo       将自动改用便携版 Python 3.13（放在启动器目录里，不影响系统里已装的旧版）。
echo.
call :bootstrap_python
if errorlevel 1 goto :end_fail
set "PY=%~dp0python\python.exe"

:check_deps
rem ---------- 4. 依赖齐了就直接开 ----------
%PY% -c "import webview, requests" >nul 2>nul
if not errorlevel 1 goto :launch

rem ---------- 5. 缺依赖：自动装 ----------
echo.
echo   首次运行，正在准备运行环境（只需要这一次）...
echo   要装的东西很小，一般十几秒就好。
echo.

rem pip 有可能没随 Python 一起装上，先确保它在
%PY% -m pip --version >nul 2>nul
if errorlevel 1 (
    echo   [1/2] 正在准备 pip ...
    %PY% -m ensurepip --default-pip >nul 2>nul
)

rem 镜像顺序跟启动器内部保持一致：清华 -> 阿里 -> 官方源。
rem 每个源都试一遍，而不是失败就放弃 —— 镜像站随时可能抽风，
rem 而官方源在国内又经常连不上，只有挨个试才稳。
call :try_install "https://pypi.tuna.tsinghua.edu.cn/simple" "清华镜像"
if not errorlevel 1 goto :launch

call :try_install "https://mirrors.aliyun.com/pypi/simple" "阿里云镜像"
if not errorlevel 1 goto :launch

echo   正在尝试官方源 ...
%PY% -m pip install -r "%REQ%" --disable-pip-version-check --retries 2
if not errorlevel 1 goto :launch

echo.
echo   ============================================================
echo    依赖安装失败
echo   ============================================================
echo.
echo    所有下载源都试过了，多半是网络问题。可以这样排查：
echo.
echo      1. 如果你在用加速器/代理，先确认它是开着的，然后重试
echo      2. 检查一下杀毒软件有没有拦住 pip
echo      3. 手动执行下面这行，看看具体报什么错：
echo.
echo         %PY% -m pip install -r requirements.txt
echo.
goto :end_fail

:try_install
echo   正在通过 %~2 安装 ...
%PY% -m pip install -r "%REQ%" -i %1 --disable-pip-version-check --retries 1 --timeout 20
if errorlevel 1 (
    echo   %~2 没成功，换下一个源重试 ...
    exit /b 1
)
echo   [√] 依赖安装完成
echo.
exit /b 0

rem ===================================================================
rem  自动下载便携版 Python（bootstrap_python.ps1 干重活，这里只做调度和报错）
rem ===================================================================
:bootstrap_python

where powershell >nul 2>nul
if errorlevel 1 (
    echo   [x] 找不到 PowerShell（Windows 10/11 系统自带）。
    echo       请手动安装 Python 3.10+（勾选 Add Python to PATH）：
    echo       https://www.python.org/downloads/
    exit /b 1
)
where tar >nul 2>nul
if errorlevel 1 (
    echo   [x] 找不到 tar 解压工具（Windows 10 1803+ / Windows 11 自带）。
    echo       请手动安装 Python 3.10+（勾选 Add Python to PATH）：
    echo       https://www.python.org/downloads/
    exit /b 1
)

echo   将从 python-build-standalone（uv 同款官方 CPython 构建）下载
echo   一个免安装、不改系统的 Python 3.13 到：
echo       %~dp0python
echo   文件约 30MB，下载完会自动解压，全程无需手动操作。
echo   （网络走的是和启动器相同的加速策略，github 连不上会自动换代理）
echo.

rem %~dp0 末尾自带反斜杠，直接包引号会变成 \" 把引号转义掉、参数粘连，
rem 标准写法是 %~dp0.（末尾补一个点），PowerShell 才能正确收到两个参数
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap_python.ps1" -LauncherDir "%~dp0." -ArchivePath "%TEMP%\forge_launcher_python.tar.gz"
if errorlevel 1 (
    echo.
    echo   [x] 便携 Python 下载/解压失败（上面应有详细原因）。
    echo       多半是网络问题：挂好代理/加速器后重新双击本文件即可；
    echo       也可以手动安装 Python 3.10+（勾选 Add Python to PATH）：
    echo       https://www.python.org/downloads/
    exit /b 1
)
if not exist "%~dp0python\python.exe" (
    echo   [x] 解压后没有找到 %~dp0python\python.exe，请重试。
    exit /b 1
)
echo.
echo   [√] 便携 Python 已就绪
echo.
exit /b 0

rem ---------- 6. 启动 ----------
:launch
%PY% webview_main.py
if errorlevel 1 (
    echo.
    echo   ============================================================
    echo    启动器异常退出
    echo   ============================================================
    echo.
    echo    如果上面显示的是窗口相关的错误（白屏、WebView2 之类），
    echo    多半是缺少 Edge WebView2 运行时。装一下微软官方的就好：
    echo        https://developer.microsoft.com/microsoft-edge/webview2/
    echo.
    echo    其他错误请把上面的红字截图反馈。
    echo.
    pause
)
exit /b 0

:end_fail
echo.
pause
exit /b 1
