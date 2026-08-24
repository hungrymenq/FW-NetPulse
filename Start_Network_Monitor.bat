@echo off
setlocal
chcp 65001 >nul 2>&1
title FW-NetPulse - Сетевой монитор
cd /d "%~dp0"

echo ======================================================================
echo    FW-NetPulse: мониторинг и диагностика игровой сети
echo ======================================================================
echo.

set "PY_BIN="
where python >nul 2>&1
if "%ERRORLEVEL%"=="0" set "PY_BIN=python"

if "%PY_BIN%"=="" (
    where py >nul 2>&1
    if "%ERRORLEVEL%"=="0" set "PY_BIN=py"
)

if "%PY_BIN%"=="" (
    echo [ОШИБКА] Python не найден.
    echo Для обычного использования скачайте готовый архив из раздела Releases.
    echo Для запуска исходников установите Python 3.11 или новее.
    pause
    exit /b 1
)

netstat -aon | findstr ":8899" | findstr "LISTENING" >nul 2>&1
if "%ERRORLEVEL%"=="0" (
    echo [ОШИБКА] Порт 8899 уже занят. Возможно, FW-NetPulse уже запущен.
    echo Откройте http://127.0.0.1:8899 или закройте программу, которая заняла порт.
    pause
    exit /b 1
)

echo Запуск через: %PY_BIN%
echo Веб-панель: http://127.0.0.1:8899
echo.

"%PY_BIN%" run_monitor.py

if "%ERRORLEVEL%" NEQ "0" (
    echo.
    echo [ОШИБКА] Программа завершилась с кодом %ERRORLEVEL%.
    pause
)
