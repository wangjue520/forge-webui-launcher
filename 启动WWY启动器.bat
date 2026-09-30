@echo off
rem ============================================================
rem  WWY 启动器 · 入口快捷方式
rem  用法：把这个 bat 放在 forge-webui-launcher 文件夹【旁边】
rem  （同一个目录里），以后双击它即可，不用打开启动器文件夹。
rem  放在文件夹内部也能用（自动找同目录的 start一键启动.bat）。
rem ============================================================
chcp 936 >nul 2>nul
setlocal

rem 情形 1：这个 bat 就在启动器文件夹里
if exist "%~dp0webview_main.py" (
    call "%~dp0start一键启动.bat"
    exit /b
)

rem 情形 2：与启动器文件夹同级（ZIP 解压出来可能带 -main 等后缀）
for /d %%d in ("%~dp0forge-webui-launcher*") do (
    if exist "%%d\webview_main.py" (
        call "%%d\start一键启动.bat"
        exit /b
    )
)

echo.
echo   [x] 没找到启动器本体。
echo       请把这个 bat 放到 forge-webui-launcher 文件夹的旁边（同一级目录）。
echo.
pause
exit /b 1
