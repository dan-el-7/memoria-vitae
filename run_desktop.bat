@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0desktop"

if not exist ".venv\Scripts\python.exe" (
    echo Desktop Python environment was not found:
    echo %CD%\.venv\Scripts\python.exe
    pause
    exit /b 1
)

rem ---- online mode ask (asked once; persisted via config.toml) ----
rem Skipped when VMA_RELAY_URL is set in the environment (scripted runs)
rem or when a relay is already configured (change it in the dashboard).
if not defined VMA_RELAY_URL (
    ".venv\Scripts\python.exe" -m vma.set_relay --check >nul 2>&1
    if errorlevel 9009 (
        echo [warning] could not run the config helper - continuing with LAN-only
        goto startserver
    )
    if errorlevel 1 (
        ".venv\Scripts\python.exe" -m vma.set_relay --wizard
    )
)

:startserver
echo Starting Visual Memory Agent at http://localhost:8619
echo Press Ctrl+C to stop the desktop server.
".venv\Scripts\python.exe" -m uvicorn vma.app:app --host 0.0.0.0 --port 8619

if errorlevel 1 (
    echo.
    echo Desktop server stopped with an error.
    pause
)
