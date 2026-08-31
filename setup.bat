@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    py -3.11 -m venv venv 2>nul || python -m venv venv
    if errorlevel 1 exit /b 1
)

"venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 'Python 3.11 is required')"
if errorlevel 1 exit /b 1

"venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo Setup complete. Python: %CD%\venv\Scripts\python.exe
