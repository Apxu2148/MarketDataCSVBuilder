@echo off
call "%~dp0run.bat" --profile intraday_hyperliquid %*
exit /b %errorlevel%
