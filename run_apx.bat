@echo off
call "%~dp0run.bat" --profile apx %*
exit /b %errorlevel%
