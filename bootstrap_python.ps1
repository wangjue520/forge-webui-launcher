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
# 网络安全（重要）：下载会经过 ghfast.top / gh-proxy.com / github.moeyy.xyz
# 这类第三方 GitHub 加速代理，它们按设计就是中间人——有能力返回任何内容。
# 所以本脚本对每一个下载物都做来源校验，校验不通过一律中止（fail-closed）：
#   1. 资产清单与哈希取自 release 官方附带的 SHA256SUMS 文件（优先）或
#      GitHub API 的 digest 字段（兜底），两者不一致即中止（元数据被篡改）
#   2. 归档下载完成后、解压/执行之前，强制比对 sha256，不符直接中止，
#      并且自动换下一个候选地址重新下载
#   3. 拿不到任何哈希基准时拒绝继续——宁可装不了，也不执行来源不可证的
#      二进制
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

# 与 portable_env.pick_python_asset 同一套筛选规则：
# Windows x86_64 / install_only（非 stripped）/ 常规 GIL 构建
# （freethreaded 的 ABI 是 cp3XXt，torch/numpy 等一大批包没有对应 wheel）
# 注意：资产名里真正的分隔符是 '-' 而不是 '_'
$assetNamePattern = '^cpython-3\.13\.\d+\+.+x86_64-pc-windows-msvc-install_only\.tar\.gz$'

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

function Resolve-PythonAsset([bool]$useProxy) {
    $tag = (Get-JsonWithFallback $tagUrl $useProxy).tag
    if (-not $tag) { throw '无法获取 python-build-standalone 最新版本号' }
    Write-Host ("[bootstrap] python-build-standalone 最新版本: " + $tag)
    $release = Get-JsonWithFallback ($releaseApi + $tag) $useProxy
    $asset = $null
    foreach ($a in $release.assets) {
        if ($a.name -match $assetNamePattern) { $asset = $a; break }
    }
    if (-not $asset) { throw '没有找到匹配 Windows x86_64 的 Python 3.13 便携构建' }
    Write-Host ("[bootstrap] 选中构建: " + $asset.name)
    return @{
        Tag      = $tag
        Name     = $asset.name
        Url      = $asset.browser_download_url
        Digest   = ($asset.digest -replace '^sha256:', '')
        SumsUrl  = ($release.assets | Where-Object { $_.name -eq 'SHA256SUMS' } | Select-Object -First 1).browser_download_url
    }
}

function Get-ExpectedHash($asset, [bool]$useProxy) {
    <#
        取哈希校验基准，优先级：release 官方 SHA256SUMS 清单 > API digest。
        两者同时拿到但不一致 => 元数据链路被篡改 => 中止（fail-closed）。
        都拿不到 => 中止。宁可装不了，也不执行来源不可证的二进制。
    #>
    $sumsHash = $null
    if ($asset.SumsUrl) {
        $candidates = @($asset.SumsUrl)
        if ($useProxy) {
            $candidates = @()
            foreach ($p in $proxyPrefixes) { $candidates += ($p + $asset.SumsUrl) }
            $candidates += $asset.SumsUrl
        }
        foreach ($u in $candidates) {
            try {
                $text = (Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 60).Content
                foreach ($line in ($text -split "`n")) {
                    $parts = $line.Trim() -split '\s+'
                    if ($parts.Count -eq 2 -and $parts[0] -match '^[0-9a-fA-F]{64}$' -and $parts[1] -eq $asset.Name) {
                        $sumsHash = $parts[0].ToLower()
                        break
                    }
                }
                if ($sumsHash) {
                    Write-Host '[bootstrap] 已取得官方 SHA256SUMS 校验基准'
                    break
                }
            }
            catch {
                Write-Host ("[bootstrap] SHA256SUMS 获取失败，换地址: " + $u)
            }
        }
    }
    $apiHash = $null
    if ($asset.Digest -and $asset.Digest -match '^[0-9a-fA-F]{64}$') { $apiHash = $asset.Digest.ToLower() }
    if ($sumsHash -and $apiHash -and $sumsHash -ne $apiHash) {
        throw 'SHA256SUMS 与 GitHub API 摘要不一致，元数据可能被篡改，已中止'
    }
    $hash = $sumsHash
    if (-not $hash) { $hash = $apiHash }
    if (-not $hash) { throw '无法获取任何哈希校验基准（SHA256SUMS 与 API digest 均不可用），已中止' }
    return $hash
}

function Test-FileHash([string]$path, [string]$expected) {
    $actual = (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToLower()
    return ($actual -eq $expected.ToLower())
}

# ---- 主流程 ----
$useProxy = -not (Test-GithubDirect)
if ($useProxy) {
    Write-Host '[bootstrap] github.com 无法直连，下载将走加速代理（逐个字节校验，防篡改）'
}

$asset = $null
try {
    $asset = Resolve-PythonAsset $useProxy
}
catch {
    # 应急兜底：API 查询全部失败时，尝试一个写死的固定版本。
    # 注意：这个固定链接会随时间失效，只作为最后手段；哈希基准仍然照常校验。
    Write-Host ('[bootstrap] 在线查询版本失败（' + $_.Exception.Message + '），尝试备用固定版本 ...')
    $fixedTag = '20260901'
    $fixedName = 'cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz'
    $asset = @{
        Tag     = $fixedTag
        Name    = $fixedName
        Url     = 'https://github.com/astral-sh/python-build-standalone/releases/download/' + $fixedTag + '/' + $fixedName
        Digest  = '9bcc038a0bf180612ed56dec93d4977d035e80b8d9320ef51a38c287baf134b7'
        SumsUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/' + $fixedTag + '/SHA256SUMS'
    }
}

$expectedHash = Get-ExpectedHash $asset $useProxy
Write-Host ('[bootstrap] 期望 sha256: ' + $expectedHash)

$candidates = @($asset.Url)
foreach ($p in $proxyPrefixes) { $candidates += ($p + $asset.Url) }

$downloaded = $false
$lastErr = $null
foreach ($u in $candidates) {
    try {
        Write-Host ('[bootstrap] 下载: ' + $u)
        Download-File $u $ArchivePath
        if (Test-FileHash $ArchivePath $expectedHash) {
            $downloaded = $true
            break
        }
        $actualHash = (Get-FileHash -Path $ArchivePath -Algorithm SHA256).Hash.ToLower()
        $lastErr = ('校验失败！期望 ' + $expectedHash + '，实际 ' + $actualHash + '——该地址返回的内容被篡改或损坏，换下一个地址')
        Write-Host ('[bootstrap] ' + $lastErr)
    }
    catch {
        $lastErr = $_.Exception.Message
        Write-Host ('[bootstrap] 下载失败: ' + $lastErr)
    }
}
if (-not $downloaded) {
    Remove-Item $ArchivePath -Force -ErrorAction SilentlyContinue
    throw ('所有候选地址都失败或校验不通过。最后错误: ' + $lastErr)
}

Write-Host ('[bootstrap] 校验通过，解压到 ' + $LauncherDir + ' ...')
& $sysTar -xzf $ArchivePath -C $LauncherDir
if ($LASTEXITCODE -ne 0) { throw ('tar 解压失败 (exit ' + $LASTEXITCODE + ')') }

$py = Join-Path $LauncherDir 'python\python.exe'
if (-not (Test-Path $py)) { throw ('解压完成但没有找到 ' + $py) }

try { Remove-Item $ArchivePath -Force -ErrorAction SilentlyContinue } catch { }
Write-Host ('[bootstrap] 便携 Python 就绪: ' + $py)
