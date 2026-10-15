@echo off
REM ============================================================
REM  NEXUS AI - one-click launcher for Windows
REM  Double-click this file (or run it from cmd) inside the
REM  nexus-ai repo folder. No pip packages are required.
REM ============================================================
setlocal

REM --- Always run from the repo root (folder this script is in) ---
cd /d "%~dp0"

echo ===============================================
echo  NEXUS AI - starting agent service
echo ===============================================
echo.

REM --- 1. Python must be on PATH ---
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo Found Python %PYVER%

REM --- 2. Python 3.10+ required ---
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] NEXUS needs Python 3.10 or newer. Found %PYVER%.
    echo.
    pause
    exit /b 1
)

REM --- 3. First run: create .env from the example ---
if not exist ".env" (
    if exist ".env.example" (
        echo First run: creating .env from .env.example ...
        copy /y ".env.example" ".env" >nul
        echo Tip: edit .env to set NEXUS_PROVIDER and your model keys.
        echo.
    )
)

REM --- 4. Sanity check: are we in the nexus-ai repo? ---
if not exist "services\agent\nexus\service\app.py" (
    echo [ERROR] Agent service not found.
    echo Put run-nexus.bat in the nexus-ai repo root folder and try again.
    echo.
    pause
    exit /b 1
)

REM --- 5. Start the service (DB migrations run automatically) ---
echo Starting NEXUS agent on http://127.0.0.1:8765 ...
echo Press Ctrl+C to stop.
echo.
cd /d "%~dp0services\agent"
python -m nexus.service.app

echo.
echo NEXUS stopped.
pause
