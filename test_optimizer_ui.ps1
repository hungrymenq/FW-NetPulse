$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$testPort = 8898
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$screenshotPath = Join-Path $PSScriptRoot ("build\optimizer-ui-test-" + [guid]::NewGuid().ToString("N") + ".png")
$browserProfile = Join-Path $PSScriptRoot ("build\chrome-optimizer-test-" + [guid]::NewGuid().ToString("N"))
$monitorProcess = $null

if (-not (Test-Path -LiteralPath $chrome)) {
    throw "Google Chrome для визуального теста не найден: $chrome"
}
if (Get-NetTCPConnection -LocalPort $testPort -State Listen -ErrorAction SilentlyContinue) {
    throw "Тестовый порт $testPort уже занят"
}

New-Item -ItemType Directory -Force -Path (Split-Path $screenshotPath -Parent) | Out-Null
New-Item -ItemType Directory -Force -Path $browserProfile | Out-Null
$env:FW_NETPULSE_PORT = "$testPort"
$env:FW_NETPULSE_NO_BROWSER = "1"

try {
    $monitorProcess = Start-Process `
        -FilePath (Get-Command python).Source `
        -ArgumentList "run_monitor.py" `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden `
        -PassThru

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Get-NetTCPConnection -LocalPort $testPort -State Listen -ErrorAction SilentlyContinue) {
            $ready = $true
            break
        }
    }
    if (-not $ready) { throw "Тестовый сервер не запустился" }

    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/api/status?timeframe=60" -TimeoutSec 5
    if (-not $status.optimizer -or -not $status.optimizer.analysis -or -not $status.optimizer.dpi.profiles) {
        throw "API не вернул полное состояние оптимизатора"
    }

    $page = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$testPort/" -TimeoutSec 5
    $javaScript = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$testPort/app.js" -TimeoutSec 5
    if ($page.Content -notmatch "optimizerSection" -or
        $page.Content -notmatch "Обход ограничений" -or
        $javaScript.Content -notmatch "updateOptimizer") {
        throw "Веб-интерфейс оптимизатора собран не полностью"
    }

    try {
        Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:$testPort/api/optimizer/dpi/stop" `
            -Method Post `
            -Headers @{ Origin = "https://example.invalid" } `
            -ContentType "application/json" `
            -Body "{}" `
            -TimeoutSec 5 | Out-Null
        throw "API ошибочно принял управляющий запрос с внешнего сайта"
    }
    catch {
        if ($_.Exception.Response.StatusCode -ne 403) { throw }
    }

    $chromeArguments = @(
        "--headless=new"
        "--disable-gpu"
        "--hide-scrollbars"
        "--no-first-run"
        "--user-data-dir=$browserProfile"
        "--window-size=1440,2200"
        "--virtual-time-budget=5000"
        "--screenshot=$screenshotPath"
        "http://127.0.0.1:$testPort/"
    )
    $chromeProcess = Start-Process -FilePath $chrome -ArgumentList $chromeArguments -Wait -PassThru -WindowStyle Hidden
    if ($chromeProcess.ExitCode -ne 0) { throw "Chrome не смог открыть тестовую страницу" }
    if (-not (Test-Path -LiteralPath $screenshotPath) -or
        (Get-Item -LiteralPath $screenshotPath).Length -lt 50000) {
        throw "Chrome не создал полноценный снимок интерфейса"
    }

    Write-Host "Проверены API, защита управляющих запросов и встроенные файлы интерфейса."
    Write-Host "Снимок интерфейса: $screenshotPath" -ForegroundColor Green
}
finally {
    $env:FW_NETPULSE_PORT = $null
    $env:FW_NETPULSE_NO_BROWSER = $null
    if ($monitorProcess -and -not $monitorProcess.HasExited) {
        Stop-Process -Id $monitorProcess.Id -Force
        $monitorProcess.WaitForExit(5000)
    }
}

Start-Sleep -Milliseconds 700
if (Get-NetTCPConnection -LocalPort $testPort -State Listen -ErrorAction SilentlyContinue) {
    throw "После UI-теста порт $testPort не освободился"
}
Write-Host "UI-тест завершён, тестовый процесс остановлен." -ForegroundColor Green
