@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title MT5 批量终端（调试窗口）
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
echo 这是调试启动：会显示这个黑色窗口和运行信息。平时请双击「一键启动.vbs」（不显示窗口）。
echo 正在检查并清理上次未退出的后台（只结束本程序自己的旧进程，MT5 终端和 EA 保持运行）……
echo 正在启动 MT5 批量终端，浏览器会自动打开 http://127.0.0.1:8765/
echo 退出：网页左下角「退出程序」，或在本窗口按 Ctrl+C。
"%PY%" "%~dp0app.py" %*
echo.
echo MT5 批量终端 已退出。
pause
