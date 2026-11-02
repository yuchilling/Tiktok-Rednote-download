@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 需要 Python 3.9+（安装时勾选 Add Python to PATH）
where python >nul 2>nul
if not "%errorlevel%"=="0" (
    echo 没找到 Python。装一个 Python 3.9 以上版本（勾选 Add to PATH）再双击本文件，
    echo 或者改用上一层的 素材下载器.exe（不需要 Python）。
    pause
    exit /b 1
)

rem 优先 pythonw（不弹黑窗），没有就退回 python
where pythonw >nul 2>nul
if "%errorlevel%"=="0" (
    start "" pythonw "main.py"
    exit /b 0
)
python "main.py"
if errorlevel 1 pause
