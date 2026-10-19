@echo off
setlocal
chcp 65001 >nul
title Broadcast Transcription
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONDONTWRITEBYTECODE=1
if not exist "%~dp0system\runtime\python\python.exe" (
  echo Please extract the ENTIRE ZIP first, then open this file again.
  pause
  exit /b 1
)
if not "%~1"=="" goto run_direct
start "" "%~dp0system\runtime\python\pythonw.exe" "%~dp0system\scripts\friend_start.py"
exit /b 0
:run_direct
"%~dp0system\runtime\python\python.exe" -u "%~dp0system\scripts\friend_start.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%BROADCAST_TRANSCRIBE_NO_PAUSE%"=="1" pause
exit /b %RESULT%
