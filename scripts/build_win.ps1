Param(
    [string]$AppName = "西安众创南非Takealot自建链接AI工具",
    [string]$AppVersion = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

if ([string]::IsNullOrWhiteSpace($AppVersion)) {
    $AppVersion = $env:APP_VERSION
}
if ([string]::IsNullOrWhiteSpace($AppVersion)) {
    $AppVersion = "0.0.0-dev"
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    $PyCmd = "py"
    $PyArgs = @("-3")
} else {
    $PyCmd = "python"
    $PyArgs = @()
}

if (!(Test-Path ".venv")) {
    & $PyCmd @PyArgs -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -U pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller

if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }

& ".\.venv\Scripts\pyinstaller.exe" `
    --noconfirm `
    --windowed `
    --name "$AppName" `
    --paths "$root\src" `
    --collect-submodules takealot_autolister `
    --add-data "$root\config;config" `
    --add-data "$root\input;input" `
    --add-data "$root\.env.example;." `
    --add-data "$root\README.md;." `
    gui_qt.py

Write-Host "✅ Windows 构建完成: $root\dist\$AppName"

$zipName = "TakealotAutoLister-win-$AppVersion.zip"
$zipPath = Join-Path "$root\dist" $zipName
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path "$root\dist\$AppName\*" -DestinationPath $zipPath

Write-Host "✅ 压缩包已生成: $zipPath"
Write-Host "提示：可继续用 Inno Setup 打包安装程序。"
