@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title MT5 批量终端 - 安装依赖

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [错误] 没有找到 Python。请先安装 64 位 Python 3.10 或更高版本：https://www.python.org/downloads/windows/
  echo        安装时勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) and sys.maxsize > 2**32 else 1)"
if errorlevel 1 (
  echo [错误] 需要 64 位 Python 3.10 或更高版本。当前版本：
  %PY% --version
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建虚拟环境 .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto fail
)

echo 正在安装依赖（fastapi / uvicorn / psutil / MetaTrader5）...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo 官方源安装失败，改用清华镜像重试...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 goto fail
)

".venv\Scripts\python.exe" -c "import MetaTrader5 as m; print('MetaTrader5 版本', m.__version__)"
echo.
echo ===== 安装完成 =====
echo 下一步：双击「一键启动.vbs」（不显示窗口），在网页「账号」页点「添加并登录」填账号、密码、服务器即可（终端副本会自动创建）。
pause
exit /b 0

:fail
echo.
echo [错误] 安装失败，请把上面的报错截图发给技术支持。
pause
exit /b 1
