@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title MT5 批量终端 - 停止后台
set "PY="
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY (
  echo [错误] 没有找到 Python 运行环境。
  echo        便携版：请确认本文件夹里的 python 文件夹完整（重新解压一次）；
  echo        源码版：请先双击「安装依赖.bat」。
  pause
  exit /b 1
)
echo 正在停止 MT5 批量终端的后台程序（只处理本文件夹的程序；桌面上你自己打开的 MT5 不受影响）……
echo 是否同时关闭本程序启动的 MT5 终端，按网页「设置 → 退出程序时关闭终端」执行。
echo.
"%PY%" "%~dp0app.py" --stop
echo.
pause
