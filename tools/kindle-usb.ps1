# SPDX-License-Identifier: AGPL-3.0-or-later
<#
.SYNOPSIS
  检查插在 Windows 上的 Kindle（U 盘模式），并可选安装 octopus004.koplugin。

.DESCRIPTION
  Kindle 插上电脑就是一个普通 U 盘，根目录里能看到 koreader\ 文件夹。
  本脚本不需要 SSH，也不联网，只读写这个盘。

  先退出 KOReader 再插线，否则设备可能只充电、不出盘。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\kindle-usb.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\kindle-usb.ps1 -MountPoint D:\ -Install

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\kindle-usb.ps1 -OutFile kindle-report.txt
#>
[CmdletBinding()]
param(
    [string]$MountPoint = '',
    [switch]$Install,
    [string]$OutFile = '',
    [switch]$Help
)

$ErrorActionPreference = 'Continue'

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$script:Lines = New-Object System.Collections.ArrayList

function Say {
    param([string]$Text = '')
    Write-Host $Text
    [void]$script:Lines.Add($Text)
}
function Ok      { param([string]$Text) Say ("  [OK]   " + $Text) }
function Missing { param([string]$Text) Say ("  [缺失] " + $Text) }
function Warn    { param([string]$Text) Say ("  [注意] " + $Text) }
function Info    { param([string]$Text) Say ("  [信息] " + $Text) }

function Show-Usage {
    Say '用法: powershell -ExecutionPolicy Bypass -File tools\kindle-usb.ps1 [选项]'
    Say ''
    Say '  -MountPoint <路径>   Kindle 的盘符，例如 D:\ ；不给就自动找带 koreader 目录的盘'
    Say '  -Install             把仓库里的 octopus004.koplugin 复制到 koreader\plugins\'
    Say '  -OutFile <文件>      同时把输出写成 UTF-8 文本，方便贴出来'
    Say '  -Help                显示本帮助'
}

if ($Help) {
    Show-Usage
    exit 0
}

$repo = Split-Path -Parent $PSScriptRoot
$pluginSource = Join-Path $repo 'octopus004.koplugin'

function Test-KindleRoot {
    param([string]$Root)
    if ([string]::IsNullOrWhiteSpace($Root)) { return $false }
    return (Test-Path -LiteralPath (Join-Path $Root 'koreader'))
}

if (-not $MountPoint) {
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
        if (Test-KindleRoot $drive.Root) {
            $MountPoint = $drive.Root
            break
        }
    }
}

if (-not $MountPoint) {
    Write-Host '找不到已挂载的 Kindle（没有哪个盘里有 koreader 目录）。' -ForegroundColor Red
    Write-Host '请在 Kindle 上先退出 KOReader，再插 USB；或用 -MountPoint D:\ 指定盘符。'
    exit 1
}

$MountPoint = $MountPoint.TrimEnd('\', '/')
if ($MountPoint -match '^[A-Za-z]:$') { $MountPoint = $MountPoint + '\' }

$koreader = Join-Path $MountPoint 'koreader'
if (-not (Test-Path -LiteralPath $koreader)) {
    Write-Host ("错误: " + $koreader + " 不存在，这个盘不像 Kindle 根目录。") -ForegroundColor Red
    exit 1
}

Say ("Kindle 挂载点: " + $MountPoint)
Say ''

Say '== KOReader =='
$gitRev = Join-Path $koreader 'git-rev'
if (Test-Path -LiteralPath $gitRev) {
    $version = Get-Content -LiteralPath $gitRev -TotalCount 1 -ErrorAction SilentlyContinue
    Info ("版本标记 (koreader\git-rev): " + $version)
} else {
    Warn '没有 koreader\git-rev，老包或非官方打包；以 KOReader 内 帮助→关于 显示的版本为准'
}

Say ''
Say '== SSH 依赖（菜单里没有 SSH 服务器时看这里）=='
$dropbear = Join-Path $koreader 'dropbear'
if (Test-Path -LiteralPath $dropbear) {
    $dropbearSize = (Get-Item -LiteralPath $dropbear).Length
    Ok ("koreader\dropbear 存在（" + $dropbearSize + " 字节）")
    if ($dropbearSize -lt 100000) {
        Warn '  这个文件比正常 dropbear 小很多，可能不是真的可执行文件（解压不完整？）'
    }
} else {
    Missing 'koreader\dropbear 不存在 —— SSH.koplugin 会静默禁用自己，菜单里就不会出现 SSH 服务器'
}
if (Test-Path -LiteralPath (Join-Path $koreader 'dropbearkey')) {
    Info 'koreader\dropbearkey 存在（可选工具）'
} else {
    Info 'koreader\dropbearkey 没有：不影响菜单项，插件启动时用 dropbear 的 -R 自动生成 host key'
}
$sftp = Get-ChildItem -LiteralPath $koreader -Filter 'sftp-server*' -Recurse -Depth 1 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($sftp) {
    Ok '找到 sftp-server（SFTP 可用）'
} else {
    Warn '没找到 sftp-server：SFTP/新版 scp 可能不通，用 scp -O 或 tar|ssh'
}

Say ''
Say '== 插件开关（插件管理里被关掉时，菜单项也会消失）=='
$readerSettings = Join-Path (Join-Path $koreader 'settings') 'reader.lua'
if (Test-Path -LiteralPath $readerSettings) {
    $readerText = Get-Content -LiteralPath $readerSettings -Raw -ErrorAction SilentlyContinue
    if ($readerText -match '(?s)plugins_disabled\s*=\s*\{(.{0,4000}?)\}') {
        if ($matches[1] -match 'SSH') {
            Warn 'settings\reader.lua 的 plugins_disabled 里出现 SSH —— 插件可能被插件管理关掉了'
        } else {
            Ok 'plugins_disabled 里没有 SSH'
        }
    } else {
        Ok '没有 plugins_disabled，插件全部默认启用'
    }
} else {
    Info '还没有 settings\reader.lua（KOReader 还没写过设置）'
}

Say ''
Say '== 插件目录 =='
$plugins = Join-Path $koreader 'plugins'
$target = Join-Path $plugins 'octopus004.koplugin'
if (Test-Path -LiteralPath $plugins) {
    Ok 'koreader\plugins 存在'
    $found = Get-ChildItem -LiteralPath $plugins -Directory -Filter '*.koplugin' -ErrorAction SilentlyContinue
    if ($found) {
        foreach ($p in $found) { Info ("已安装: " + $p.Name) }
    } else {
        Info '还没有任何 *.koplugin 插件'
    }
} else {
    Missing 'koreader\plugins 不存在（KOReader 装得不对？）'
}

Say ''
Say '== 本仓库插件与设置 =='
if (-not (Test-Path -LiteralPath $pluginSource)) {
    Warn '脚本旁边没有 octopus004.koplugin（只做检查没关系；-Install 需要它）'
}
if (Test-Path -LiteralPath $target) {
    Ok 'octopus004.koplugin 已安装'
    foreach ($f in @('main.lua', 'wake.lua', '_meta.lua')) {
        if (-not (Test-Path -LiteralPath (Join-Path $target $f))) { Missing ("  缺少 " + $f) }
    }
} else {
    Warn 'octopus004.koplugin 还没装'
}

$settingsFile = Join-Path (Join-Path $koreader 'settings') 'octopus004.lua'
if (Test-Path -LiteralPath $settingsFile) {
    Ok '设置文件 koreader\settings\octopus004.lua 存在（重装插件不会丢设置）'
} else {
    Info '还没有 settings\octopus004.lua（首次运行后生成）'
}

$cacheFile = Join-Path $koreader 'octopus004-report.txt'
if (Test-Path -LiteralPath $cacheFile) {
    $size = (Get-Item -LiteralPath $cacheFile).Length
    Info ("已有缓存报表 " + $size + " 字节")
}

$authorized = Join-Path (Join-Path $koreader 'settings') 'SSH\authorized_keys'
if (Test-Path -LiteralPath $authorized) {
    $keyLines = @(Get-Content -LiteralPath $authorized -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne '' }).Count
    Info ("SSH 公钥 " + $keyLines + " 行")
}

$crashLog = Join-Path $koreader 'crash.log'
if (Test-Path -LiteralPath $crashLog) {
    Say ''
    Say '== crash.log 最后 15 行（可能含书名/路径，贴出来前先看一眼）=='
    Get-Content -LiteralPath $crashLog -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Say $_ }
}

$exitCode = 0

if ($Install) {
    Say ''
    Say '== 安装插件 =='
    if (-not (Test-Path -LiteralPath $pluginSource)) {
        Write-Host ('错误: 找不到 ' + $pluginSource + ' —— 把 octopus004.koplugin 文件夹和 tools 放在同一层目录里。') -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path -LiteralPath $plugins)) {
        Write-Host ('错误: ' + $plugins + ' 不存在，先修好 KOReader 安装再来。') -ForegroundColor Red
        exit 1
    }
    if ($target -notmatch '(?i)koreader\\plugins\\octopus004\.koplugin$') {
        Write-Host ('错误: 目标路径异常 (' + $target + ')，中止。') -ForegroundColor Red
        exit 1
    }
    try {
        if (Test-Path -LiteralPath $target) {
            Say ('  覆盖已有的 ' + $target)
            Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
        }
        Copy-Item -LiteralPath $pluginSource -Destination $plugins -Recurse -Force -ErrorAction Stop
    } catch {
        Write-Host ('错误: 复制失败（盘是只读的？换根 USB 线或换个口再试）: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
    Ok '已复制到 koreader\plugins\octopus004.koplugin'
    Get-ChildItem -LiteralPath $target -ErrorAction SilentlyContinue | ForEach-Object {
        Info ($_.Name + '  ' + $_.Length + ' 字节')
    }
    Say ''
    Say '完成。安全弹出后拔线，在 Kindle 上重新打开 KOReader：'
    Say '  工具(Tools) → 004 Report → Report URL / Show report'
} else {
    Say ''
    Say '要直接装插件，加上 -Install 再跑一次：'
    Say '  powershell -ExecutionPolicy Bypass -File tools\kindle-usb.ps1 -Install'
}

if ($OutFile) {
    try {
        $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutFile)
        $encoding = New-Object System.Text.UTF8Encoding($true)
        [System.IO.File]::WriteAllLines($full, [string[]]$script:Lines.ToArray(), $encoding)
        Write-Host ''
        Write-Host ('输出已写入 ' + $full + '（UTF-8）')
    } catch {
        Write-Host ('写入 ' + $OutFile + ' 失败: ' + $_.Exception.Message) -ForegroundColor Red
        $exitCode = 1
    }
}

exit $exitCode
