@echo off
chcp 65001 >nul
title 安裝廣播轉文字到這台電腦
echo 正在設定這台電腦的桌面入口與模型路徑...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_on_this_pc.ps1"
echo.
echo 如果看到 RESULT: PASS，就可以雙擊桌面「開始轉錄.bat」。
pause