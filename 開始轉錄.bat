@echo off
setlocal
chcp 65001 >nul
title Broadcast Transcription
set "RUNNER="
for /d %%D in (E:\*) do @if exist "%%D\config\model-manifest.json" if exist "%%D\scripts\run_transcribe.py" set "RUNNER=%%D\scripts\run_transcribe.py"
if not defined RUNNER (
  echo ERROR: The transcription project was not found on drive E:.
  if not "%BROADCAST_TRANSCRIBE_NO_PAUSE%"=="1" pause
  exit /b 1
)
for %%F in ("%RUNNER%") do set "SCRIPTS=%%~dpF"
if not exist "%SCRIPTS%..\.venv\Scripts\python.exe" (
  echo ERROR: The local Python runtime was not found.
  if not "%BROADCAST_TRANSCRIBE_NO_PAUSE%"=="1" pause
  exit /b 1
)
if not "%~1"=="" goto run_direct
start "" "%SCRIPTS%..\.venv\Scripts\pythonw.exe" "%SCRIPTS%launch_menu.py"
exit /b 0
:run_direct
"%SCRIPTS%..\.venv\Scripts\python.exe" -X utf8 "%RUNNER%" %*
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="2" set "RESULT=0"
if not "%RESULT%"=="0" echo ERROR: Transcription failed. Check the newest project log.
if not "%BROADCAST_TRANSCRIBE_NO_PAUSE%"=="1" pause
exit /b %RESULT%
