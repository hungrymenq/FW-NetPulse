$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$testPort = 8897
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$screenshotPath = Join-Path $PSScriptRoot ("build\multi-client-ui-test-" + [guid]::NewGuid().ToString("N") + ".png")
$browserProfile = Join-Path $PSScriptRoot ("build\chrome-multi-client-test-" + [guid]::NewGuid().ToString("N"))
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
        -ArgumentList "tests\multi_client_demo.py" `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden `
        -PassThru

    $status = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/api/status?timeframe=60" -TimeoutSec 5
            $endpointCount = @($status.multi_client.endpoints.PSObject.Properties).Count
            if ($status.multi_client.windows.Count -eq 4 -and $endpointCount -eq 4) {
                break
            }
        }
        catch {
            $status = $null
        }
    }
    if (-not $status) { throw "Тестовый сервер не запустился" }
    if ($status.multi_client.windows.Count -ne 4) { throw "API не вернул четыре игровых окна" }
    $endpointCount = @($status.multi_client.endpoints.PSObject.Properties).Count
    if ($endpointCount -ne 4) { throw "API не создал отдельные измерители зеркал" }

    $chromeArguments = @(
        "--headless=new"
        "--disable-gpu"
        "--hide-scrollbars"
        "--no-first-run"
        "--user-data-dir=$browserProfile"
        "--window-size=1440,4000"
        "--virtual-time-budget=7000"
        "--screenshot=$screenshotPath"
        "http://127.0.0.1:$testPort/"
    )
    $chromeProcess = Start-Process -FilePath $chrome -ArgumentList $chromeArguments -Wait -PassThru -WindowStyle Hidden
    if ($chromeProcess.ExitCode -ne 0) { throw "Chrome не смог открыть многоклиентскую панель" }
    if (-not (Test-Path -LiteralPath $screenshotPath) -or (Get-Item -LiteralPath $screenshotPath).Length -lt 80000) {
        throw "Chrome не создал полноценный снимок многоклиентского интерфейса"
    }

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) {
        throw "Node.js не найден. Для браузерного теста установите зависимости командой npm install."
    }
    $env:FW_NETPULSE_TEST_URL = "http://127.0.0.1:$testPort/"
    & $nodeCommand.Source ".\tests\ui_client_click.test.js"
    if ($LASTEXITCODE -ne 0) {
        throw "Браузерный тест выбора игрового окна завершился с ошибкой"
    }

    Write-Host "Проверены четыре окна, четыре набора измерений, графики и вкладки." -ForegroundColor Green
    Write-Host "Снимок интерфейса: $screenshotPath" -ForegroundColor Green
}
finally {
    $env:FW_NETPULSE_PORT = $null
    $env:FW_NETPULSE_NO_BROWSER = $null
    $env:FW_NETPULSE_TEST_URL = $null
    if ($monitorProcess -and -not $monitorProcess.HasExited) {
        Stop-Process -Id $monitorProcess.Id -Force
        $monitorProcess.WaitForExit(5000)
    }
}

Start-Sleep -Milliseconds 700
if (Get-NetTCPConnection -LocalPort $testPort -State Listen -ErrorAction SilentlyContinue) {
    throw "После многоклиентского UI-теста порт $testPort не освободился"
}
Write-Host "Многоклиентский UI-тест завершён." -ForegroundColor Green
