@echo off
setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo   InfoHub Windows Deployment
echo ========================================
echo.

echo [1/4] Checking Docker Desktop...
docker info >nul 2>&1
if errorlevel 1 goto docker_error

echo [2/4] Pulling the latest code from GitHub...
git pull --ff-only origin main
if errorlevel 1 goto deploy_error

echo [3/4] Building and starting InfoHub...
docker compose up -d --build
if errorlevel 1 goto deploy_error

echo [4/4] Checking service status...
docker compose ps

echo.
echo Deployment completed successfully.
echo Open: http://100.69.211.16:8000
echo.
pause
exit /b 0

:docker_error
echo.
echo Docker Desktop is not running.
echo Start Docker Desktop, wait until it is ready, then run this file again.
echo.
pause
exit /b 1

:deploy_error
echo.
echo Deployment failed. Keep this window open and take a photo of the error.
echo.
pause
exit /b 1
