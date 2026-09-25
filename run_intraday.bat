@echo off
call "%~dp0run.bat" --profile intraday %*
exit /b %errorlevel%
