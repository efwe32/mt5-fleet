@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title MT5 批量终端 - 打包 exe
if not exist ".venv\Scripts\python.exe" (
  echo 请先双击「安装依赖.bat」。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --name MT5Fleet ^
  --add-data "static;static" ^
  --hidden-import MetaTrader5 --collect-submodules uvicorn --collect-submodules fleet ^
  app.py
if errorlevel 1 (
  echo [错误] 打包失败。
  pause
  exit /b 1
)
copy /y "dist\MT5Fleet.exe" "MT5Fleet.exe" >nul
echo.
echo 完成：%cd%\MT5Fleet.exe
echo 把 MT5Fleet.exe 放在本文件夹（和 terminals、data 同级）双击即可运行。
pause
