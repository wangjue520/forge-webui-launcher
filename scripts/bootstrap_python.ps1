#Requires -Version 3.0
# 【注意】本文件必须保存为「UTF-8 带 BOM」：Windows 自带的 PowerShell 5.1 读到没有 BOM 的脚本
# 会按系统编码（中文系统 GBK）解析，中文变乱码、引号被吃掉，整个脚本语法错误，首次启动直接失败。
# Forge WebUI 启动器 - 便携 Python 自动引导脚本
#
# 用途：启动WWY启动器.bat 在系统里找不到任何可用 Python 时调用本脚本，
# 自动下载 python-build-standalone 的 CPython 3.13 便携构建（uv 同款官方
# 构建，完整标准库 + pip + venv，免安装、可重定位），解压到启动器目录的
# python\ 子目录，之后 bat 用它启动 webview_main.py。
#
# 为什么不写死在 bat 里：bat 内嵌 PowerShell 需要层层转义特殊字符
# （| > & ( ) 等），极难维护；独立 .ps1 可以正常写逻辑、正常Review。
#
# 网络安全（重要）：下载会经过 ghfast.top / gh-proxy.com / gh-proxy.org
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
# IWR 的进度条会把下载拖慢 1-2 个数量级，静默掉
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 与 mirror_manager.GITHUB_PROXIES 保持一致（tests/test_portable_download.py 会核对）
$proxyPrefixes = @('https://ghfast.top/', 'https://gh-proxy.com/', 'https://ghproxy.net/', 'https://gh.llkk.cc/', 'https://gh-proxy.org/')
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
    # 直连优先，但直连失败一定要再试代理：Test-GithubDirect 测的是 github.com，
    # 而这里请求的是 raw.githubusercontent.com / api.github.com——国内常见
    # github.com 能通、raw/api 被墙，此时若不走代理就只能落到会过期的固定版本兜底。
    # 安全性不受影响：下载物最终都要过 sha256 校验（fail-closed）。
    $candidates = @($url)
    if ($useProxy) {
        $candidates = @()
        foreach ($p in $proxyPrefixes) { $candidates += ($p + $url) }
        $candidates += $url
    }
    else {
        foreach ($p in $proxyPrefixes) { $candidates += ($p + $url) }
    }
    $lastErr = $null
    foreach ($u in $candidates) {
        try {
            $resp = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 12
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

function Measure-Sources([string[]]$urls) {
    # 国内的加速代理时好时坏，今天最快的明天可能只剩 100 B/s，写死顺序靠不住。
    # 所以开始下载前同时测一遍所有下载源（各下几秒），按实测速度排序，总共只花几秒。
    # 返回 Good（能用的，从快到慢）和 Bad（连不上或几乎不动的，第一轮直接跳过）。
    if (-not $sysCurl) { return @{ Good = $urls; Bad = @() } }
    $probes = @()
    foreach ($u in $urls) {
        $out = [IO.Path]::GetTempFileName()
        $err = [IO.Path]::GetTempFileName()
        $curlArgs = '-sL --connect-timeout 5 --max-time 6 -r 0-2097151 -o NUL -w "%{http_code} %{speed_download}" "' + $u + '"'
        $proc = Start-Process -FilePath $sysCurl -ArgumentList $curlArgs -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $probes += @{ Url = $u; Proc = $proc; Out = $out; Err = $err }
    }
    $results = @()
    foreach ($p in $probes) {
        if (-not $p.Proc.WaitForExit(9000)) {
            try { $p.Proc.Kill() } catch { }
            $p.Proc.WaitForExit(2000) | Out-Null
        }
        $speed = 0.0
        $parts = ("" + (Get-Content -Path $p.Out -Raw -ErrorAction SilentlyContinue)).Trim() -split '\s+'
        if ($parts.Count -eq 2 -and ($parts[0] -eq '200' -or $parts[0] -eq '206')) {
            [double]::TryParse($parts[1], [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$speed) | Out-Null
        }
        Remove-Item $p.Out, $p.Err -Force -ErrorAction SilentlyContinue
        Write-Host ('[bootstrap] 测速 ' + ('{0,8:N0}' -f ($speed / 1024)) + ' KB/s  ' + $p.Url.Split('/')[2])
        $results += New-Object PSObject -Property @{ Url = $p.Url; Speed = $speed }
    }
    $sorted = @($results | Sort-Object -Property Speed -Descending)
    return @{
        Good = @($sorted | Where-Object { $_.Speed -ge 10240 } | ForEach-Object { $_.Url })
        Bad  = @($sorted | Where-Object { $_.Speed -lt 10240 } | ForEach-Object { $_.Url })
    }
}

function Download-File([string]$url, [string]$dest, [int]$minBps, [int]$stallSec, [bool]$restartIfNoRange) {
    # 优先 curl.exe（Win10 1803+ 自带，速度快）；没有的话退回 Invoke-WebRequest
    if (-not $sysCurl) {
        if (Test-Path $dest) { Remove-Item $dest -Force }
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 900 -OutFile $dest
        if (-not (Test-Path $dest)) { throw '下载失败：目标文件不存在' }
        return
    }
    # 这里只防「连上了但几乎不动」：只设连接超时的话，100 多 B/s 的连接永远不算失败，
    # 会一直耗下去（用户实测 44MB 显示要 4 天）。速度持续低于下限就放弃，换下一个源。
    # -C - 接着 $dest 已有的部分下，换源也不用从头来；不同源拼出来的文件最后照样过 sha256。
    # 不用 --retry：换源和重试都由外层循环管，这里多试只会在坏的源上多耗时间。
    $resume = @('-C', '-')
    while ($true) {
        $code = & $sysCurl -fL --connect-timeout 10 --speed-limit $minBps --speed-time $stallSec @resume -w '%{http_code}' -o $dest $url
        $exit = $LASTEXITCODE
        if ($exit -eq 0) { break }
        if ($exit -eq 28) {
            throw ('速度低于 ' + [int]($minBps / 1024) + ' KB/s 或连接超时，换下一个源（已下载的部分保留）')
        }
        if ($code -eq '416') {
            # 已有的部分比完整文件还大，肯定是坏的
            Remove-Item $dest -Force -ErrorAction SilentlyContinue
            throw '已下载的部分已损坏，删除后重新下载'
        }
        if ($exit -eq 33 -and $restartIfNoRange -and $resume.Count -gt 0) {
            # 这个源不支持续传：前面几轮都不行时，才值得丢掉已下载的部分从头下
            Remove-Item $dest -Force -ErrorAction SilentlyContinue
            $resume = @()
            continue
        }
        throw ('curl 下载失败 (exit ' + $exit + ')')
    }
    if (-not (Test-Path $dest)) { throw '下载失败：目标文件不存在' }
}

function Get-WebText([string]$url) {
    # 服务器按 application/octet-stream 返回时（SHA256SUMS 就是），PowerShell 5.1 的 .Content
    # 是字节数组而不是字符串，直接拿去 -split 永远匹配不上。之前因此每次都退回 GitHub API，
    # API 在国内常被墙或限流，查不到就只能用写死的旧版本。
    $content = (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 20).Content
    if ($content -is [byte[]]) { return [Text.Encoding]::UTF8.GetString($content) }
    return $content
}

function Get-TextWithFallback([string]$url, [bool]$useProxy) {
    # 与 Get-JsonWithFallback 同一套候选策略，但返回纯文本（SHA256SUMS 用）
    $candidates = @($url)
    if ($useProxy) {
        $candidates = @()
        foreach ($p in $proxyPrefixes) { $candidates += ($p + $url) }
        $candidates += $url
    }
    else {
        foreach ($p in $proxyPrefixes) { $candidates += ($p + $url) }
    }
    $lastErr = $null
    foreach ($u in $candidates) {
        try {
            return (Get-WebText $u)
        }
        catch {
            $lastErr = $_
            Write-Host ("[bootstrap] 请求失败，换下一个地址: " + $u)
        }
    }
    throw $lastErr
}

function Resolve-PythonAsset([bool]$useProxy) {
    $tag = (Get-JsonWithFallback $tagUrl $useProxy).tag
    if (-not $tag) { throw '无法获取 python-build-standalone 最新版本号' }
    Write-Host ("[bootstrap] python-build-standalone 最新版本: " + $tag)
    # 优先走可预测的 SHA256SUMS 直链：releases/download 不吃
    # api.github.com 的 60 次/小时未认证限额（共享出口 IP 极易超限），
    # 而且文件名清单就在清单内容里，连列资产的 API 调用都省了
    $sumsUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/' + $tag + '/SHA256SUMS'
    try {
        $sumsText = Get-TextWithFallback $sumsUrl $useProxy
        foreach ($line in ($sumsText -split "`n")) {
            $parts = $line.Trim() -split '\s+'
            if ($parts.Count -eq 2 -and $parts[1] -match $assetNamePattern) {
                Write-Host ("[bootstrap] 选中构建: " + $parts[1])
                return @{
                    Tag     = $tag
                    Name    = $parts[1]
                    Url     = 'https://github.com/astral-sh/python-build-standalone/releases/download/' + $tag + '/' + $parts[1]
                    Digest  = ''
                    SumsUrl = $sumsUrl
                }
            }
        }
        throw 'SHA256SUMS 里没有匹配 Windows x86_64 的 Python 3.13 便携构建'
    }
    catch {
        Write-Host ('[bootstrap] SHA256SUMS 直链不可用（' + $_.Exception.Message + '），退回 GitHub API 查询 ...')
    }
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
        # 与 Get-JsonWithFallback 同理：直连失败也要试代理，不能仅凭
        # github.com 的探测结果放弃（objects.githubusercontent.com 独立被墙很常见）
        $candidates = @($asset.SumsUrl)
        if ($useProxy) {
            $candidates = @()
            foreach ($p in $proxyPrefixes) { $candidates += ($p + $asset.SumsUrl) }
            $candidates += $asset.SumsUrl
        }
        else {
            foreach ($p in $proxyPrefixes) { $candidates += ($p + $asset.SumsUrl) }
        }
        foreach ($u in $candidates) {
            try {
                $text = Get-WebText $u
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

Write-Host '[bootstrap] 正在测试各下载源的速度 ...'
$sources = Measure-Sources $candidates

# 未完成的文件名带上哈希：上次中断留下的同一版本接着下，别的版本的残留不会被接上去
$partPath = $ArchivePath + '.' + $expectedHash.Substring(0, 12) + '.part'
$downloaded = $false
$lastErr = $null
# 三轮逐步放宽速度下限。第一轮只用测速能用的源，慢的立刻换；全都慢说明就是网络差，
# 放宽下限后留在最快的源上慢慢下，总比下不完强。已下载的部分一直保留，每轮都不白跑。
$passes = @(
    @{ MinBps = 51200; StallSec = 20 },
    @{ MinBps = 10240; StallSec = 30 },
    @{ MinBps = 1024; StallSec = 60 }
)
for ($pass = 1; $pass -le $passes.Count -and -not $downloaded; $pass++) {
    $urls = @($sources.Good) + @($sources.Bad)
    if ($pass -eq 1 -and @($sources.Good).Count -gt 0) { $urls = @($sources.Good) }
    if ($pass -gt 1) {
        Write-Host ('[bootstrap] 第 ' + ($pass - 1) + ' 轮没有下完，放宽速度要求后继续（已下载的部分保留）...')
        Start-Sleep -Seconds 3
    }
    foreach ($u in $urls) {
        try {
            if ((Test-Path $partPath) -and (Test-FileHash $partPath $expectedHash)) {
                $downloaded = $true
                break
            }
            Write-Host ('[bootstrap] 下载: ' + $u)
            Download-File $u $partPath $passes[$pass - 1].MinBps $passes[$pass - 1].StallSec ($pass -gt 1)
            if (Test-FileHash $partPath $expectedHash) {
                $downloaded = $true
                break
            }
            $actualHash = (Get-FileHash -Path $partPath -Algorithm SHA256).Hash.ToLower()
            Remove-Item $partPath -Force -ErrorAction SilentlyContinue
            $lastErr = ('校验失败！期望 ' + $expectedHash + '，实际 ' + $actualHash + '——该地址返回的内容被篡改或损坏，换下一个地址')
            Write-Host ('[bootstrap] ' + $lastErr)
        }
        catch {
            $lastErr = $_.Exception.Message
            Write-Host ('[bootstrap] 下载失败: ' + $lastErr)
        }
    }
}
if (-not $downloaded) {
    # 未完成的部分留着：下次启动接着下
    throw ('所有下载源都失败或校验不通过（已自动重试 ' + $passes.Count + ' 轮）。最后错误: ' + $lastErr)
}
Move-Item -Path $partPath -Destination $ArchivePath -Force
Get-ChildItem -Path ($ArchivePath + '.*.part') -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host ('[bootstrap] 校验通过，解压到 ' + $LauncherDir + ' ...')
& $sysTar -xzf $ArchivePath -C $LauncherDir
if ($LASTEXITCODE -ne 0) { throw ('tar 解压失败 (exit ' + $LASTEXITCODE + ')') }

$py = Join-Path $LauncherDir 'python\python.exe'
if (-not (Test-Path $py)) { throw ('解压完成但没有找到 ' + $py) }

# PEP 668 的 EXTERNALLY-MANAGED 标记会让 pip 拒绝安装（uv 托管的 Python 就是
# 靠这个标记把用户挡住的）。启动器自带的是私有便携环境，装依赖是本职工作，
# 如果归档里带了这个标记就删掉，否则首次启动装依赖会直接报
# externally-managed-environment。
$pep668Marker = Join-Path $LauncherDir 'python\Lib\EXTERNALLY-MANAGED'
if (Test-Path $pep668Marker) {
    Remove-Item $pep668Marker -Force
    Write-Host '[bootstrap] 已移除 EXTERNALLY-MANAGED 标记（启动器私有环境需要允许 pip 安装依赖）'
}

try { Remove-Item $ArchivePath -Force -ErrorAction SilentlyContinue } catch { }
Write-Host ('[bootstrap] 便携 Python 就绪: ' + $py)
