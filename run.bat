@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  信小兔爆料监控 - 本地启动
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python，请先安装 Python 3.8+ 并勾选 "Add to PATH"
    echo        下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo [1/2] 检查依赖 requests ...
python -c "import requests" >nul 2>nul
if errorlevel 1 (
    echo       未安装，正在安装 ...
    python -m pip install requests -i https://pypi.tuna.tsinghua.edu.cn/simple
)

echo [2/2] 启动监控（按 Ctrl+C 停止）...
echo.
python monitor.py

echo.
echo 监控已退出。
pause
