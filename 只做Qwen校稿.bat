@echo off
setlocal
chcp 65001 >nul
title Qwen Proofreading
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0scripts\proofread_only.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%BROADCAST_TRANSCRIBE_NO_PAUSE%"=="1" pause
exit /b %RESULT%
