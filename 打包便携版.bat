@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title MT5 批量终端 - 打包便携版
if not exist ".venv\Scripts\python.exe" (
  echo 请先双击「安装依赖.bat」。打包需要本机已安装的 Python 3.13 和联网下载。
  pause
  exit /b 1
)
echo 会生成：本文件夹\新建文件夹\MT5批量终端\ 和 MT5批量终端.zip（自带 Python，复制到别的电脑解压后双击「一键启动」即可）。
echo 不会改动正在运行的程序、data 和 terminals 文件夹。
echo.
".venv\Scripts\python.exe" "%~dp0tools\build_portable.py" %*
echo.
pause
