Param(
    [string]$AppName = "TakealotAutoLister",
    [string]$AppVersion = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

if ([string]::IsNullOrWhiteSpace($AppVersion)) {
    $AppVersion = $env:APP_VERSION
}
if ([string]::IsNullOrWhiteSpace($AppVersion)) {
    $AppVersion = "0.0.0-dev"
}
$versionMetaDir = Join-Path "$root\\.runtime" "build"
New-Item -ItemType Directory -Force -Path $versionMetaDir | Out-Null
$versionMetaFile = Join-Path $versionMetaDir "APP_VERSION.txt"
Set-Content -Path $versionMetaFile -Value $AppVersion -Encoding UTF8
$bundledEnvFile = Join-Path $versionMetaDir ".env"

if (Test-Path "$root\.env") {
    $allowedPrefixes = @("OSS_")
    $bundledLines = New-Object System.Collections.Generic.List[string]
    foreach ($line in Get-Content "$root\.env" -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
        if ($trimmed.StartsWith("#")) { continue }
        foreach ($prefix in $allowedPrefixes) {
            if ($trimmed.StartsWith($prefix)) {
                [void]$bundledLines.Add($trimmed)
                break
            }
        }
    }
    if ($bundledLines.Count -gt 0) {
        Set-Content -Path $bundledEnvFile -Value $bundledLines -Encoding UTF8
        Write-Host "Bundled OSS-only .env: $bundledEnvFile"
    } elseif (Test-Path $bundledEnvFile) {
        Remove-Item -Force $bundledEnvFile
    }
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    $PyCmd = "python"
    $PyArgs = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    # GitHub Actions setup-python puts the expected Python in PATH as `python`.
    # Fallback to `py -3.11` to avoid accidentally using 3.14+ and breaking pinned wheels.
    $PyCmd = "py"
    $PyArgs = @("-3.11")
} else {
    throw "No Python interpreter found (python/py)."
}

if (!(Test-Path ".venv")) {
    & $PyCmd @PyArgs -m venv .venv
}

Write-Host "Python path: .\\.venv\\Scripts\\python.exe"
& ".\.venv\Scripts\python.exe" --version
& ".\.venv\Scripts\python.exe" -m pip install -U pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
& ".\.venv\Scripts\python.exe" -m pip --version

if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }

$pyiArgs = @(
    "--noconfirm",
    "--windowed",
    "--name", "$AppName",
    "--paths", "$root\src",
    "--collect-submodules", "takealot_autolister",
    "--collect-data", "PIL",
    "--hidden-import", "PIL.WebPImagePlugin",
    "--hidden-import", "PIL.JpegImagePlugin",
    "--hidden-import", "PIL.PngImagePlugin",
    "--add-data", "$root\config;config",
    "--add-data", "$root\input;input",
    "--add-data", "$root\.env.example;.",
    "--add-data", "$versionMetaFile;.",
    "--add-data", "$root\README.md;."
)
if (Test-Path $bundledEnvFile) {
    $pyiArgs += @("--add-data", "$bundledEnvFile;.")
}
$pyiArgs += "gui_qt.py"

& ".\.venv\Scripts\pyinstaller.exe" @pyiArgs

Write-Host "Windows build done: $root\\dist\\$AppName"

$zipName = "TakealotAutoLister-win-$AppVersion.zip"
$zipPath = Join-Path "$root\dist" $zipName
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path "$root\dist\$AppName\*" -DestinationPath $zipPath

Write-Host "Zip created: $zipPath"
Write-Host "Tip: You can package an installer with Inno Setup."
