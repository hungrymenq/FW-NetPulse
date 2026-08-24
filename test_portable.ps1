$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$version = (Get-Content -Raw -LiteralPath ".\VERSION").Trim()
$sourceReleaseDir = (Resolve-Path ".\release\FW-NetPulse-v$version-win64").Path
$databaseWasPackaged = Test-Path -LiteralPath (Join-Path $sourceReleaseDir "data\network_history.db")
if ($databaseWasPackaged) {
    throw "В релиз ошибочно попала база истории разработчика"
}

$testRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "build"))
$releaseDir = [System.IO.Path]::GetFullPath((Join-Path $testRoot ("portable-test-" + [guid]::NewGuid().ToString("N"))))
if (-not $releaseDir.StartsWith($testRoot.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Небезопасный путь временного теста: $releaseDir"
}
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
Copy-Item -Path (Join-Path $sourceReleaseDir "*") -Destination $releaseDir -Recurse -Force

$exePath = Join-Path $releaseDir "FW-NetPulse.exe"
$testPort = 8898
$occupiedPort = Get-NetTCPConnection -LocalPort $testPort -State Listen -ErrorAction SilentlyContinue
if ($occupiedPort) {
    throw "Тестовый порт $testPort уже занят процессом PID $($occupiedPort.OwningProcess)"
}

$env:FW_NETPULSE_NO_BROWSER = "1"
$env:FW_NETPULSE_PORT = "$testPort"
$hudIdsBefore = @(Get-Process -Name "FW-NetPulse-HUD" -ErrorAction SilentlyContinue).Id
$newHudProcesses = @()
$monitorServerProcessId = $null
$monitorProcess = Start-Process -FilePath $exePath -WorkingDirectory $releaseDir -WindowStyle Hidden -PassThru

try {
    $serverReady = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/api/status?timeframe=60" -TimeoutSec 2
            $page = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$testPort/" -TimeoutSec 2
            $javaScript = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$testPort/app.js" -TimeoutSec 2
            $chartLibrary = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$testPort/vendor/chart.umd.js" -TimeoutSec 2
            $monitorServerProcessId = (Get-NetTCPConnection -LocalPort $testPort -State Listen -ErrorAction Stop).OwningProcess
            $serverReady = $true
            break
        }
        catch {
            # Однофайловому EXE требуется несколько секунд на распаковку при первом запуске.
        }
    }

    if (-not $serverReady) {
        throw "EXE не поднял веб-сервер за 20 секунд"
    }
    if (@($status.chart.icmp_loss).Count -ne @($status.chart.labels).Count -or
        @($status.chart.tcp_loss).Count -ne @($status.chart.labels).Count) {
        throw "EXE не вернул корректные серии потерь пакетов"
    }
    if ($javaScript.Content -notmatch "Потеря TCP") {
        throw "Во встроенном интерфейсе EXE отсутствует красная серия потерь"
    }
    if ($page.Content -match "cdn.jsdelivr.net" -or $chartLibrary.RawContentLength -lt 100000) {
        throw "График зависит от внешнего CDN или локальная Chart.js собрана не полностью"
    }
    if (-not $page.Headers["Content-Security-Policy"] -or $page.Headers["Access-Control-Allow-Origin"]) {
        throw "Веб-панель не вернула ожидаемые локальные заголовки безопасности"
    }
    if (-not $status.optimizer -or -not $status.optimizer.analysis -or -not $status.optimizer.dpi.available) {
        throw "EXE не вернул готовый модуль анализа маршрута и точечного обхода"
    }
    if ($page.Content -notmatch "Обход ограничений" -or
        $javaScript.Content -notmatch "updateOptimizer") {
        throw "Во встроенном интерфейсе EXE отсутствует управление оптимизатором"
    }

    try {
        Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:$testPort/api/hud/launch" `
            -Method Post `
            -Headers @{ Origin = "https://example.invalid" } `
            -ContentType "application/json" `
            -Body "{}" `
            -TimeoutSec 5 | Out-Null
        throw "API ошибочно принял запуск HUD со стороннего сайта"
    }
    catch {
        if ($_.Exception.Response.StatusCode -ne 403) { throw }
    }
    foreach ($toolFile in @("winws.exe", "WinDivert.dll", "WinDivert64.sys", "cygwin1.dll")) {
        if (-not (Test-Path -LiteralPath (Join-Path $releaseDir "tools\zapret\bin\$toolFile"))) {
            throw "В переносной версии отсутствует компонент $toolFile"
        }
    }

    $hudResult = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$testPort/api/hud/launch" `
        -Method Post `
        -ContentType "application/json" `
        -Body "{}" `
        -TimeoutSec 5
    Start-Sleep -Seconds 2
    $newHudProcesses = @(Get-Process -Name "FW-NetPulse-HUD" -ErrorAction SilentlyContinue | Where-Object {
        $_.Id -notin $hudIdsBefore
    })

    [pscustomobject]@{
        "EXE запущен" = $true
        "Цель API" = "$($status.target.ip):$($status.target.port)"
        "Точек графика" = @($status.chart.labels).Count
        "Размер главной страницы" = $page.RawContentLength
        "Размер JavaScript" = $javaScript.RawContentLength
        "Chart.js работает без интернета" = $chartLibrary.RawContentLength -gt 100000
        "База истории создана" = Test-Path (Join-Path $releaseDir "data\network_history.db")
        "Личная база не упакована" = -not $databaseWasPackaged
        "Локальный API защищён" = $true
        "Серии потерь встроены" = $true
        "Анализ маршрута встроен" = $true
        "Точечный DPI-модуль найден" = [bool]$status.optimizer.dpi.available
        "HUD запущен из панели" = [bool]($hudResult.success -and $newHudProcesses.Count -gt 0)
    } | Format-List
}
finally {
    foreach ($hudProcess in $newHudProcesses) {
        if (-not $hudProcess.HasExited) {
            Stop-Process -Id $hudProcess.Id -Force
        }
    }
    if ($monitorServerProcessId) {
        Stop-Process -Id $monitorServerProcessId -Force -ErrorAction SilentlyContinue
    }
    if (-not $monitorProcess.HasExited) {
        Stop-Process -Id $monitorProcess.Id -Force
        $monitorProcess.WaitForExit(5000)
    }
    $env:FW_NETPULSE_NO_BROWSER = $null
    $env:FW_NETPULSE_PORT = $null
}

Start-Sleep -Milliseconds 700
if (Get-NetTCPConnection -LocalPort $testPort -State Listen -ErrorAction SilentlyContinue) {
    throw "После теста порт $testPort не освободился"
}

if (Test-Path -LiteralPath $releaseDir) {
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}

Write-Host "Тестовый процесс остановлен, порт $testPort освобождён." -ForegroundColor Green
