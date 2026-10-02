# create_shortcuts.ps1 —— 给启动器的 bat 生成带图标的快捷方式
#
# 背景：.bat 批处理文件本身无法携带图标（Windows 只给 .exe / .lnk 显示自定义
# 图标），所以"给 bat 加图标"的正确做法是在 bat 旁边放一个指向它的 .lnk
# 快捷方式，图标设在快捷方式上。双击快捷方式与双击 bat 完全等效。
#
# 用法：
#   powershell -File create_shortcuts.ps1              # bat 旁边 + 桌面都建
#   powershell -File create_shortcuts.ps1 -DesktopOnly # 只在桌面建一个
#
# 重复运行是安全的（覆盖同名快捷方式），不会报错中断启动流程。
param(
    [switch]$DesktopOnly
)

$ErrorActionPreference = "SilentlyContinue"

$LauncherDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$IconPath    = Join-Path $LauncherDir "wwy_launcher_icon.ico"
if (-not (Test-Path $IconPath)) { exit 0 }   # 没有图标就什么都不做

$EntryBat = Join-Path $LauncherDir "启动WWY启动器.bat"
$StartBat = Join-Path $LauncherDir "start一键启动.bat"

$ws = New-Object -ComObject WScript.Shell

function New-LauncherShortcut {
    param([string]$LnkPath, [string]$TargetBat, [string]$WorkDir)
    if (-not (Test-Path $TargetBat)) { return }
    $s = $ws.CreateShortcut($LnkPath)
    $s.TargetPath       = $TargetBat
    $s.WorkingDirectory = $WorkDir
    $s.IconLocation     = "$IconPath,0"
    $s.Description      = "WWY 启动器"
    $s.WindowStyle      = 1
    $s.Save()
}

if (-not $DesktopOnly) {
    # bat 旁边各放一个同名快捷方式，在文件夹里直接显示图标
    New-LauncherShortcut (Join-Path $LauncherDir "启动WWY启动器.lnk") $EntryBat $LauncherDir
    New-LauncherShortcut (Join-Path $LauncherDir "start一键启动.lnk") $StartBat $LauncherDir

    # 入口 bat 有一份习惯放在启动器文件夹【旁边】（上一层目录），那边也补上
    $ParentEntry = Join-Path (Split-Path -Parent $LauncherDir) "启动WWY启动器.bat"
    if (Test-Path $ParentEntry) {
        New-LauncherShortcut (Join-Path (Split-Path -Parent $LauncherDir) "启动WWY启动器.lnk") `
            $ParentEntry (Split-Path -Parent $LauncherDir)
    }
}

# 桌面：指向入口 bat（它自己会找到 start一键启动.bat）
$Desktop = [Environment]::GetFolderPath("Desktop")
New-LauncherShortcut (Join-Path $Desktop "WWY 启动器.lnk") $EntryBat $LauncherDir

exit 0
