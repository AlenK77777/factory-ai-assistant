@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PIDFILE=%ROOT%\logs\chat.pid"
set "STOPPED="
if exist "%ProgramFiles%\Docker\Docker\resources\bin" set "PATH=%ProgramFiles%\Docker\Docker\resources\bin;%PATH%"

echo Asking the chat server to stop...
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:7777/api/shutdown' -Method POST -UseBasicParsing -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 goto force_stop

set /a N=0
:wait_down
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:7777/api/health' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Stopped the chat server.
  set "STOPPED=1"
  goto after_chat
)
set /a N+=1
if !N! GEQ 15 goto force_stop
ping -n 2 127.0.0.1 >nul
goto wait_down

:force_stop
set "PID="
if exist "%PIDFILE%" set /p PID=<"%PIDFILE%"
if not defined PID for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "(Get-NetTCPConnection -LocalPort 7777 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)"`) do set "PID=%%P"
if defined PID (
  echo Stopping process !PID! ...
  taskkill /PID !PID! /T /F >nul 2>&1
  if not errorlevel 1 (
    echo Stopped the chat server, process !PID!.
    set "STOPPED=1"
  )
)
if exist "%PIDFILE%" del "%PIDFILE%" >nul 2>&1

:after_chat
if not defined STOPPED echo The chat server was not running.

echo Stopping NGINX...
docker compose stop nginx
if errorlevel 1 (
  echo Could not stop NGINX. It may not be running.
) else (
  echo Stopped NGINX.
)

if /I "%~1"=="all" (
  echo Stopping the database container...
  docker compose stop db
  if errorlevel 1 (
    echo Could not stop the database container.
  ) else (
    echo Stopped the database container.
  )
  echo Docker Desktop and Ollama are still running.
) else (
  echo Docker Desktop, the database and Ollama were left running.
  echo To stop the database as well, run: stop-chat.bat all
)

echo.
pause
exit /b 0
