@echo off
setlocal
chcp 65001 >nul 2>&1
title FW-NetPulse - Настольный HUD
cd /d "%~dp0"

set "PY_BIN="
where python >nul 2>&1
if "%ERRORLEVEL%"=="0" set "PY_BIN=python"

if "%PY_BIN%"=="" (
    where py >nul 2>&1
    if "%ERRORLEVEL%"=="0" set "PY_BIN=py"
)

if "%PY_BIN%"=="" (
    echo [ОШИБКА] Python не найден.
    echo Для обычного использования запускайте FW-NetPulse-HUD.exe из готового релиза.
    pause
    exit /b 1
)

start "" "%PY_BIN%" hud_overlay.py
