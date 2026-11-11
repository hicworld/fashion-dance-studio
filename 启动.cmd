@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    if errorlevel 1 goto config_error
    echo 首次启动：请按 README 填写所选平台的配置，保存后关闭记事本。
    start "" /wait notepad.exe "%CD%\.env"
)

where ffmpeg >nul 2>nul
if errorlevel 1 goto ffmpeg_error
where ffprobe >nul 2>nul
if errorlevel 1 goto ffmpeg_error

if exist ".venv\Scripts\python.exe" goto install_dependencies
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if not errorlevel 1 (
    set "WORKFLOW_PYTHON=python"
    goto create_venv
)
py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if not errorlevel 1 (
    set "WORKFLOW_PYTHON=py -3"
    goto create_venv
)
echo 未找到可用的 Python。请安装 Python 3.11 或 3.12 并加入 PATH。
goto failed

:create_venv
%WORKFLOW_PYTHON% -m venv .venv
if errorlevel 1 goto failed

:install_dependencies
echo 正在检查并安装运行依赖，首次启动需要联网。
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo.
echo 启动后，请用浏览器打开 http://127.0.0.1:7860
echo 页面会显示各平台所需配置；只需配置你要使用的组合。
echo 保持本窗口运行。停止服务请按 Ctrl+C。
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto failed
exit /b 0

:ffmpeg_error
echo 未找到 ffmpeg 或 ffprobe。请安装 FFmpeg，将其 bin 目录加入 PATH。
echo 完成后重新打开本脚本。具体步骤见 README.md。
goto failed

:config_error
echo 无法创建 .env。请确认 .env.example 位于本脚本所在目录。

:failed
echo.
echo 启动未完成，请查看上方提示和 README.md。
pause
exit /b 1
