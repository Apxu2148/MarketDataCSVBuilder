@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Local venv is missing. Run setup.bat first.
    exit /b 1
)

"venv\Scripts\python.exe" main.py %* & set "run_exit_code=!errorlevel!" & call;
exit /b !run_exit_code!
