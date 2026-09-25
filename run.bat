@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "builder_python=.venv\Scripts\python.exe"
if not exist "!builder_python!" set "builder_python=venv\Scripts\python.exe"
if not exist "!builder_python!" (
    echo Local venv is missing. Run setup.bat first.
    exit /b 1
)

"!builder_python!" main.py %* & set "run_exit_code=!errorlevel!" & call;
exit /b !run_exit_code!
