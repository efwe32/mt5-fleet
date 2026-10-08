@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title MT5 批量终端 - 创建终端副本（可选）
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
echo 本工具只在本文件夹的 terminals 目录里创建 MT5 副本，不会修改或关闭你桌面上正在用的 MT5。
echo.
"%PY%" "%~dp0tools\setup_terminals.py" %*
echo.
pause
