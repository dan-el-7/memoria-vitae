@echo off
setlocal

cd /d "%~dp0desktop"

if not exist ".venv\Scripts\python.exe" (
    echo Desktop Python environment was not found:
    echo %CD%\.venv\Scripts\python.exe
    pause
    exit /b 1
)

echo Starting Memoria Vitae at http://localhost:8619
echo Press Ctrl+C to stop the desktop server.
".venv\Scripts\python.exe" -m uvicorn vma.app:app --host 0.0.0.0 --port 8619

if errorlevel 1 (
    echo.
    echo Desktop server stopped with an error.
    pause
)
