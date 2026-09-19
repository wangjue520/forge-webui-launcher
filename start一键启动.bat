@echo off
cd /d "%~dp0"
title Forge WebUI 启动器
rem 不开 enabledelayedexpansion：路径里如果带 ! 会被吞掉，这个脚本也用不到它
setlocal

rem ===================================================================
rem  启动入口：自己把环境准备好，用户只需要双击这一个文件。
rem
rem  所有依赖都装进启动器目录下的 .venv 虚拟环境，绝不碰系统 Python。
rem  原因：uv 装的 Python、Linux 发行版式的 Python 会被标记为
rem  "externally-managed"（PEP 668），直接往全局 pip install 会被拒绝。
rem
rem  顺序：.venv 可用且依赖齐 -> 直接启动（日常启动走这里，不到一秒）
rem        否则：便携 Python（没有就先自动下载）-> 建 .venv -> 装依赖 -> 启动
rem
rem  不挑系统里装的 Python（版本/来源千奇百怪，是"打不开"的最大来源）：
rem  统一自动下载便携版 Python 3.13（python-build-standalone，uv 同款官方
rem  构建）到本启动器的 python\ 目录，任何电脑上环境完全一致。
rem  装依赖时按 IP 归属地自动选择镜像：国内走国内镜像，国外走官方源。
rem ===================================================================

set "REQ=%~dp0requirements.txt"
set "VENV=%~dp0.venv"
rem 引号直接包进变量：路径可能带空格，后面每次展开都得自带引号
set VPY="%~dp0.venv\Scripts\python.exe"
set "PIPLOG=%TEMP%\forge_launcher_pip.log"
set "PY="
set "REBUILT="

if not exist "%REQ%" (
    echo.
    echo   [x] 找不到 requirements.txt，启动器文件不完整，请重新解压一份。
    goto :end_fail
)

rem ---------- 0. 快速通道：.venv 已经能用 ----------
if exist %VPY% (
    rem 基础 Python 被卸载/移动后，venv 里的 python.exe 会失效，这里顺便检查
    %VPY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   [!] 现有的 .venv 虚拟环境已失效，将自动重建 ...
        rmdir /s /q "%VENV%" >nul 2>nul
    ) else (
        %VPY% -c "import webview, requests" >nul 2>nul
        if not errorlevel 1 goto :launch
        goto :install_deps
    )
)

:find_python
rem ---------- 1. 统一使用便携 Python：任何电脑上表现都一样 ----------
rem 不挑系统里装的 Python——版本太旧的、uv 托管被 PEP 668 锁死的、
rem 微软商店占位 stub、被杀软动过的……千奇百怪，是"打不开一键启动"
rem 反馈的最大来源。永远用启动器目录 python\ 下的便携版（3.13），
rem 没有就先自动下载；下载失败才退回系统 Python 兜底。
if exist "%~dp0python\python.exe" (
    set PY="%~dp0python\python.exe"
    goto :got_python
)

echo.
echo   正在准备便携版 Python（只需下载一次，约 30MB）...
call :bootstrap_python
if not errorlevel 1 (
    set PY="%~dp0python\python.exe"
    goto :got_python
)

rem ---------- 2. 便携版下载失败：退回系统 Python 兜底 ----------
rem 注意顺序：py 启动器优先于 python 命令——Windows 自带一个 "python"
rem 假命令，敲下去只会打开应用商店，而 py 不受这个影响。
echo.
echo   [!] 便携 Python 下载失败，改用系统里已安装的 Python 试试 ...
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

echo   [x] 系统里也没有可用的 Python，无法继续。
goto :end_fail

:got_python
rem ---------- 3. 版本必须 >= 3.10；太旧就改用便携版 ----------
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto :make_venv

echo.
echo   [!] 检测到的 Python 版本低于 3.10，运行不了这个启动器。
%PY% -c "import sys; print('       当前版本: Python %%d.%%d' %% sys.version_info[:2])"
echo.
echo       将自动改用便携版 Python 3.13（放在启动器目录里，不影响系统里已装的旧版）。
echo.
call :bootstrap_python
if errorlevel 1 goto :end_fail
set PY="%~dp0python\python.exe"

:make_venv
rem ---------- 4. 创建项目内虚拟环境 ----------
echo.
echo   首次运行，正在准备运行环境（只需要这一次）...
echo.
echo   [1/3] 正在创建虚拟环境 .venv ...
rem 用标准库 venv 而不是 uv venv：标准库建出来的环境自带 pip
%PY% -m venv "%VENV%"
if errorlevel 1 goto :venv_fail
if not exist %VPY% goto :venv_fail

:install_deps
rem ---------- 5. 确保 .venv 里有 pip ----------
rem 用 uv venv 建的环境默认不带 pip，这里补上；补不上就整个重建
%VPY% -m pip --version >nul 2>nul
if not errorlevel 1 goto :pip_ready

echo   [2/3] 正在准备 pip ...
%VPY% -m ensurepip --upgrade >nul 2>nul
%VPY% -m pip --version >nul 2>nul
if not errorlevel 1 goto :pip_ready

rem ensurepip 也救不回来：删掉 .venv 用标准库 venv 重建一次（只重建一次，防止死循环）
if defined REBUILT goto :venv_fail
set "REBUILT=1"
echo   [!] 现有 .venv 里装不上 pip，删掉重建 ...
rmdir /s /q "%VENV%" >nul 2>nul
goto :find_python

:pip_ready
rem ---------- 6. 装依赖 ----------
echo   [3/3] 正在安装依赖（一般十几秒，安装日志失败时才会显示）...
echo.

rem 按 IP 归属地决定镜像顺序：国内 -> 镜像优先；国外 -> 官方源优先。
rem 检测不到时按国内处理（用户大多数在国内，且官方源始终排在最后兜底）。
rem :try_install 返回值：0 成功；1 疑似网络/镜像问题，换下一个源；
rem                      2 本地环境问题，换源也没用，直接停下
set "REGION="
call :detect_region

if /i "%REGION%"=="CN" goto :order_cn
if not "%REGION%"=="" goto :order_global

:order_cn
call :try_install "-i https://pypi.tuna.tsinghua.edu.cn/simple" "清华镜像"
if errorlevel 2 goto :local_fail
if not errorlevel 1 goto :deps_ok
call :try_install "-i https://mirrors.aliyun.com/pypi/simple" "阿里云镜像"
if errorlevel 2 goto :local_fail
if not errorlevel 1 goto :deps_ok
call :try_install "" "官方源"
if errorlevel 2 goto :local_fail
if not errorlevel 1 goto :deps_ok
goto :all_failed

:order_global
call :try_install "" "官方源"
if errorlevel 2 goto :local_fail
if not errorlevel 1 goto :deps_ok
call :try_install "-i https://pypi.tuna.tsinghua.edu.cn/simple" "清华镜像"
if errorlevel 2 goto :local_fail
if not errorlevel 1 goto :deps_ok
call :try_install "-i https://mirrors.aliyun.com/pypi/simple" "阿里云镜像"
if errorlevel 2 goto :local_fail
if not errorlevel 1 goto :deps_ok

:all_failed
echo.
echo   ============================================================
echo    依赖安装失败
echo   ============================================================
echo.
echo    最后一次的安装日志：
echo   ------------------------------------------------------------
type "%PIPLOG%"
echo   ------------------------------------------------------------
echo.
echo    所有下载源都试过了，多半是网络问题。可以这样排查：
echo.
echo      1. 如果你在用加速器/代理，先确认它是开着的，然后重试
echo      2. 检查一下杀毒软件有没有拦住 pip
echo      3. 把日志里的报错截图发给作者
echo.
goto :end_fail

rem ===================================================================
rem  :detect_region —— 查询出口 IP 的国家代码写入 %REGION%（查不到留空）
rem ===================================================================
:detect_region
for /f "delims=" %%r in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0detect_region.ps1"') do set "REGION=%%r"
if /i "%REGION%"=="CN" (
    echo   检测到国内网络环境，安装依赖优先走国内镜像 ...
) else (
    if "%REGION%"=="" (
        echo   未能检测网络归属地，按国内处理（镜像优先，官方源兜底）...
    ) else (
        echo   检测到海外网络环境（%REGION%），安装依赖优先走官方源 ...
    )
)
exit /b 0

:deps_ok
rem 装完再实际 import 一次，防止"装成功了但其实没装进这个环境"
%VPY% -c "import webview, requests" >nul 2>nul
if not errorlevel 1 goto :launch
echo.
echo   [x] 依赖显示已安装，但 .venv 里仍然无法导入 webview / requests。
echo       请删除启动器目录下的 .venv 文件夹后重新双击本文件。
goto :end_fail

:local_fail
echo.
echo   ============================================================
echo    依赖安装失败（本地环境问题，不是网络问题）
echo   ============================================================
echo.
echo    安装日志：
echo   ------------------------------------------------------------
type "%PIPLOG%"
echo   ------------------------------------------------------------
echo.
echo    这种错误换下载源也没用，所以没有继续重试。可以这样处理：
echo.
echo      1. 删除启动器目录下的 .venv 文件夹，然后重新双击本文件
echo      2. 还不行的话，请把上面的日志截图反馈
echo.
goto :end_fail

:venv_fail
echo.
echo   [x] 创建虚拟环境失败。
echo       当前使用的 Python：%PY%
echo       可以删除启动器目录下的 .venv 文件夹后重试；
echo       也可以删掉 python 文件夹（如果有），让启动器改用便携版 Python。
goto :end_fail

rem ===================================================================
rem  :try_install "索引参数" "显示名"
rem ===================================================================
:try_install
echo   正在通过 %~2 安装 ...
%VPY% -m pip install -r "%REQ%" %~1 --disable-pip-version-check --retries 1 --timeout 20 > "%PIPLOG%" 2>&1
if errorlevel 1 goto :try_install_failed
echo   [√] 依赖安装完成
echo.
exit /b 0

:try_install_failed
rem 这些是本地错误，换源重试纯属浪费时间
findstr /i /c:"externally-managed-environment" /c:"No module named pip" /c:"Permission denied" /c:"拒绝访问" /c:"No space left" "%PIPLOG%" >nul 2>nul
if not errorlevel 1 exit /b 2
echo   %~2 没成功，换下一个源重试 ...
exit /b 1

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

rem ---------- 7. 启动（永远用 .venv 里的 Python） ----------
:launch
%VPY% webview_main.py
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
