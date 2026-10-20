@echo off
setlocal

:: 自动切换到脚本所在目录（backend），不再依赖硬编码绝对路径
cd /d "%~dp0"

:: Python 解释器：默认用 Anaconda 环境，可改成你的 venv/conda 路径
if "%MVH_PYTHON%"=="" set CONDA_PYTHON=C:\Users\lenovo\anaconda3\envs\music_video_helper\python.exe
if not "%MVH_PYTHON%"=="" set CONDA_PYTHON=%MVH_PYTHON%

echo ==========================================
echo   SyncStage Dev Environment Startup
echo ==========================================
echo.
timeout /t 1 >nul

echo [1/3] Starting Redis Server...
start "Redis Server" cmd /k redis-server
timeout /t 2 >nul

echo [2/3] Starting Flask Backend...
start "Flask Backend" cmd /k %CONDA_PYTHON% app.py
timeout /t 3 >nul

echo [3/3] Starting Celery Worker...
start "Celery Worker" cmd /k %CONDA_PYTHON% -m celery -A task worker --loglevel=info --pool=solo
timeout /t 2 >nul

echo.
echo ==========================================
echo   All services started successfully!
echo ==========================================
echo.
echo   本机访问:   http://localhost:5000/
echo   手机访问:   http://^<本机局域网IP^>:5000/   ^(需与电脑连同一 WiFi^)
echo   查看本机IP:  新开一个 cmd 输入  ipconfig  找 IPv4 地址
echo   API:        http://localhost:5000/api
echo   Health:     http://localhost:5000/health
echo.
echo   前端已由 Flask 同源托管，直接打开上面的地址即可，
echo   不再需要单独打开 cupnb.html，也不会被 CORS 拦截。
echo.
pause
