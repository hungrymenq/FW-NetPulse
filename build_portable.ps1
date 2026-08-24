param(
    [string]$ZapretSourceDir = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Set-Location -LiteralPath $PSScriptRoot

function Assert-GeneratedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$AllowedRoot
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullRoot = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Небезопасный путь сборки: $fullPath"
    }
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Write-Host "ОШИБКА: Python не найден. Сборка выполняется только на компьютере разработчика." -ForegroundColor Red
    exit 1
}
$pythonPath = $pythonCommand.Source

$versionPath = Join-Path $PSScriptRoot "VERSION"
if (-not (Test-Path -LiteralPath $versionPath)) {
    throw "Не найден файл VERSION"
}
$version = (Get-Content -Raw -LiteralPath $versionPath).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    throw "VERSION должен иметь формат X.Y.Z.W"
}

$releaseRoot = Join-Path $PSScriptRoot "release"
$releaseName = "FW-NetPulse-v$version-win64"
$releaseDir = Join-Path $releaseRoot $releaseName
$zipPath = Join-Path $releaseRoot "$releaseName.zip"
$checksumPath = Join-Path $releaseRoot "SHA256SUMS.txt"
$buildDir = Join-Path $PSScriptRoot "build"
$specDir = Join-Path $buildDir "specs"
$staticDir = Join-Path $PSScriptRoot "web\static"
$configPath = Join-Path $PSScriptRoot "config.json"
$monitorScript = Join-Path $PSScriptRoot "run_monitor.py"
$hudScript = Join-Path $PSScriptRoot "hud_overlay.py"

Assert-GeneratedPath -Path $releaseDir -AllowedRoot $releaseRoot
Assert-GeneratedPath -Path $zipPath -AllowedRoot $releaseRoot
Assert-GeneratedPath -Path $buildDir -AllowedRoot $PSScriptRoot

if (Test-Path -LiteralPath $releaseDir) {
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
New-Item -ItemType Directory -Force -Path $releaseDir, $buildDir, $specDir | Out-Null

$pyInstallerCheck = & $pythonPath -m PyInstaller --version 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller не найден. Установите зависимости: python -m pip install -r requirements-build.txt"
}
Write-Host "PyInstaller: $pyInstallerCheck" -ForegroundColor DarkGray

Write-Host "Сборка основного сетевого монитора v$version..." -ForegroundColor Cyan
& $pythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name "FW-NetPulse" `
    --distpath $releaseDir `
    --workpath (Join-Path $buildDir "monitor") `
    --specpath $specDir `
    --add-data "$staticDir;web\static" `
    --add-data "$configPath;." `
    --add-data "$versionPath;." `
    --hidden-import "core.tcp_tweaker" `
    --hidden-import "core.route_optimizer" `
    $monitorScript
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Сборка настольного HUD..." -ForegroundColor Cyan
& $pythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "FW-NetPulse-HUD" `
    --distpath $releaseDir `
    --workpath (Join-Path $buildDir "hud") `
    --specpath $specDir `
    --add-data "$configPath;." `
    --add-data "$versionPath;." `
    $hudScript
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

foreach ($fileName in @(
    "config.json",
    "VERSION",
    "ПРОЧИТАЙ МЕНЯ.txt",
    "ОПТИМИЗАЦИЯ_МАРШРУТА.md",
    "THIRD_PARTY_NOTICES.md"
)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $fileName) -Destination (Join-Path $releaseDir $fileName) -Force
}

$zapretCandidates = @()
if ($ZapretSourceDir) { $zapretCandidates += $ZapretSourceDir }
if ($env:FW_NETPULSE_ZAPRET_DIR) { $zapretCandidates += $env:FW_NETPULSE_ZAPRET_DIR }
$zapretCandidates += (Join-Path (Split-Path $PSScriptRoot -Parent) "zapret-discord-youtube-main")

$resolvedZapretDir = $null
foreach ($candidate in $zapretCandidates) {
    $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
    if (Test-Path -LiteralPath (Join-Path $expanded "bin\winws.exe")) {
        $resolvedZapretDir = [System.IO.Path]::GetFullPath($expanded)
        break
    }
}
if (-not $resolvedZapretDir) {
    throw "Не найдена папка zapret. Передайте -ZapretSourceDir или задайте FW_NETPULSE_ZAPRET_DIR."
}

$zapretBinTarget = Join-Path $releaseDir "tools\zapret\bin"
New-Item -ItemType Directory -Force -Path $zapretBinTarget | Out-Null
foreach ($fileName in @("winws.exe", "WinDivert.dll", "WinDivert64.sys", "cygwin1.dll")) {
    $sourcePath = Join-Path $resolvedZapretDir "bin\$fileName"
    if (-not (Test-Path -LiteralPath $sourcePath)) {
        throw "Не найден компонент точечного обхода: $sourcePath"
    }
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $zapretBinTarget $fileName) -Force
}
Copy-Item `
    -LiteralPath (Join-Path $resolvedZapretDir "LICENSE.txt") `
    -Destination (Join-Path $releaseDir "tools\zapret\LICENSE-ZAPRET-WINDIVERT.txt") `
    -Force

Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
"$zipHash  $([System.IO.Path]::GetFileName($zipPath))" | Set-Content -LiteralPath $checksumPath -Encoding ascii

Write-Host ""
Write-Host "Готово: $releaseDir" -ForegroundColor Green
Write-Host "Архив релиза: $zipPath" -ForegroundColor Green
Write-Host "Контрольная сумма: $checksumPath" -ForegroundColor Green
Write-Host "Пользователю не требуется устанавливать Python." -ForegroundColor Green
