#Requires -Version 3.0
# Forge WebUI 启动器 - 便携 Python 自动引导脚本
#
# 用途：start一键启动.bat 在系统里找不到任何可用 Python 时调用本脚本，
# 自动下载 python-build-standalone 的 CPython 3.13 便携构建（uv 同款官方
# 构建，完整标准库 + pip + venv，免安装、可重定位），解压到启动器目录的
# python\ 子目录，之后 bat 用它启动 webview_main.py。
#
# 为什么不写死在 bat 里：bat 内嵌 PowerShell 需要层层转义特殊字符
# （| > & ( ) 等），极难维护；独立 .ps1 可以正常写逻辑、正常Review。
#
# 网络策略与 launcher 的 mirror_manager 保持一致：先探测 github.com 是否
# 可直连，不可直连时所有 HTTP 请求都走加速代理候选列表，原站永远兜底。
param(
    [Parameter(Mandatory = $true)][string]$LauncherDir,
    [Parameter(Mandatory = $true)][string]$ArchivePath
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 与 mirror_manager.GITHUB_PROXIES 保持一致
$proxyPrefixes = @('https://ghfast.top/', 'https://gh-proxy.com/', 'https://github.moeyy.xyz/')
$tagUrl = 'https://raw.githubusercontent.com/astral-sh/python-build-standalone/latest-release/latest-release.json'
$releaseApi = 'https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/'

function Test-GithubDirect {
    try {
        Invoke-WebRequest -Uri 'https://github.com/' -Method Head -UseBasicParsing -TimeoutSec 6 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Get-JsonWithFallback([string]$url, [bool]$useProxy) {
    $candidates = @($url)
    if ($useProxy) {
        $candidates = @()
        foreach ($p in $proxyPrefixes) { $candidates += ($p + $url) }
        $candidates += $url
    }
    $lastErr = $null
    foreach ($u in $candidates) {
        try {
            $resp = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 30
            return ($resp.Content | ConvertFrom-Json)
        }
        catch {
            $lastErr = $_
            Write-Host ("[bootstrap] 请求失败，换下一个地址: " + $u)
        }
    }
    throw $lastErr
}

function Resolve-PythonAssetUrl([bool]$useProxy) {
    $tag = (Get-JsonWithFallback $tagUrl $useProxy).tag
    if (-not $tag) { throw '无法获取 python-build-standalone 最新版本号' }
    Write-Host ("[bootstrap] python-build-standalone 最新版本: " + $tag)
    $release = Get-JsonWithFallback ($releaseApi + $tag) $useProxy
    $asset = $null
    foreach ($a in $release.assets) {
        # 与 portable_env.pick_python_asset 同一套筛选规则：
        # Windows x86_64 / install_only（非 stripped）/ 常规 GIL 构建
        # （freethreaded 的 ABI 是 cp3XXt，torch/numpy 等一大批包没有对应 wheel）
        if ($a.name -match '^cpython-3\.13\.\d+\+.+x86_64-pc-windows-msvc-install_only\.tar\.gz$') {
            $asset = $a
            break
        }
    }
    if (-not $asset) { throw '没有找到匹配 Windows x86_64 的 Python 3.13 便携构建' }
    Write-Host ("[bootstrap] 选中构建: " + $asset.name)
    return $asset.browser_download_url
}

# 优先使用系统自带的工具绝对路径：用户 PATH 里可能装了 Git 之类的软件，
# 它自带的 GNU tar 会把 "C:\..." 当成远程主机名（Cannot connect to C），
# 把 curl 换成别的行为也不一样——系统32目录里的才是我们验证过的行为。
$sysTar = 'C:\Windows\System32\tar.exe'
if (-not (Test-Path $sysTar)) { $sysTar = 'tar.exe' }
$sysCurl = 'C:\Windows\System32\curl.exe'
if (-not (Test-Path $sysCurl)) { $sysCurl = $null }

function Download-File([string]$url, [string]$dest) {
    if (Test-Path $dest) { Remove-Item $dest -Force }
    # 优先 curl.exe（Win10 1803+ 自带，速度快、自带断线重试）；
    # 没有的话退回 Invoke-WebRequest
    if ($sysCurl) {
        & $sysCurl -fL --connect-timeout 15 --retry 2 --retry-delay 3 -o $dest $url
        if ($LASTEXITCODE -ne 0) {
            Remove-Item $dest -Force -ErrorAction SilentlyContinue
            throw ("curl 下载失败 (exit " + $LASTEXITCODE + ")")
        }
    }
    else {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 900 -OutFile $dest
    }
    if (-not (Test-Path $dest)) { throw '下载失败：目标文件不存在' }
}

# ---- 主流程 ----
$useProxy = -not (Test-GithubDirect)
if ($useProxy) {
    Write-Host '[bootstrap] github.com 无法直连，下载将走加速代理（自动逐个尝试）'
}

$url = $null
try {
    $url = Resolve-PythonAssetUrl $useProxy
}
catch {
    # 应急兜底：API 查询全部失败时，尝试一个写死的固定版本。
    # 注意：这个固定链接会随时间失效，只作为「能查到版本但下不动」之外的
    # 最后手段；正常路径永远是上面实时查询出来的地址。
    Write-Host ('[bootstrap] 在线查询版本失败（' + $_.Exception.Message + '），尝试备用固定版本 ...')
    $url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz'
}

$candidates = @($url)
foreach ($p in $proxyPrefixes) { $candidates += ($p + $url) }

$downloaded = $false
foreach ($u in $candidates) {
    try {
        Write-Host ('[bootstrap] 下载: ' + $u)
        Download-File $u $ArchivePath
        $downloaded = $true
        break
    }
    catch {
        Write-Host ('[bootstrap] 下载失败: ' + $_.Exception.Message)
    }
}
if (-not $downloaded) { throw '所有下载地址都失败了，请检查网络/代理后重试' }

Write-Host ('[bootstrap] 解压到 ' + $LauncherDir + ' ...')
& $sysTar -xzf $ArchivePath -C $LauncherDir
if ($LASTEXITCODE -ne 0) { throw ('tar 解压失败 (exit ' + $LASTEXITCODE + ')') }

$py = Join-Path $LauncherDir 'python\python.exe'
if (-not (Test-Path $py)) { throw ('解压完成但没有找到 ' + $py) }

try { Remove-Item $ArchivePath -Force -ErrorAction SilentlyContinue } catch { }
Write-Host ('[bootstrap] 便携 Python 就绪: ' + $py)
