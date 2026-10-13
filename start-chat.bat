@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "LOG=%ROOT%\logs\chat.log"
set "ERRLOG=%ROOT%\logs\chat.err.log"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist "%ProgramFiles%\Docker\Docker\resources\bin" set "PATH=%ProgramFiles%\Docker\Docker\resources\bin;%PATH%"

if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
call :shortcuts

call :https
if not errorlevel 1 (
  call :health
  if not errorlevel 1 (
    echo The chat is already running. Opening the browser.
    start "" "https://localhost:8443/"
    ping -n 4 127.0.0.1 >nul
    exit /b 0
  )
)

echo Checking Docker Desktop...
call :docker
if errorlevel 1 goto failed

echo Starting the database...
docker compose up -d db
if errorlevel 1 (
  echo docker compose up -d db failed.
  goto failed
)
call :database
if errorlevel 1 goto failed

echo Checking Ollama...
call :ollama
if errorlevel 1 goto failed

if not exist "%PY%" (
  echo The project virtual environment was not found:
  echo   %PY%
  echo Create it from this folder with: python -m venv .venv
  goto failed
)

call :health
if not errorlevel 1 (
  echo The chat server is already running.
  goto certs
)

echo Starting the chat server.
echo Log: %LOG%
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; $p = Start-Process -FilePath '%PY%' -ArgumentList '-u','chat\server.py' -WorkingDirectory '%ROOT%' -WindowStyle Hidden -RedirectStandardOutput '%LOG%' -RedirectStandardError '%ERRLOG%' -PassThru; Set-Content -Path '%ROOT%\logs\chat.pid' -Value $p.Id -Encoding ascii"
if errorlevel 1 (
  echo Could not start the chat server.
  goto failed
)

:certs

echo Preparing the HTTPS certificate and the admin password...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\generate_certs.ps1"
if errorlevel 1 (
  echo Could not create the certificate or the admin password file.
  goto failed
)

if not exist "%ROOT%\logs\nginx" mkdir "%ROOT%\logs\nginx"
call :port_free 8088
if errorlevel 1 goto failed
call :port_free 8443
if errorlevel 1 goto failed
echo Starting NGINX...
docker compose up -d nginx
if errorlevel 1 (
  echo docker compose up -d nginx failed.
  goto failed
)

echo Waiting for http://127.0.0.1:7777 ...
set /a N=0
:wait_chat
call :health
if not errorlevel 1 goto wait_https
set /a N+=1
if !N! GEQ 45 (
  echo The chat server did not respond within 45 seconds.
  goto failed
)
ping -n 2 127.0.0.1 >nul
goto wait_chat

:wait_https
echo Waiting for https://localhost:8443 ...
set /a N=0
:https_loop
call :https
if not errorlevel 1 goto open_browser
set /a N+=1
if !N! GEQ 20 (
  echo NGINX did not respond on https://localhost:8443 within 40 seconds.
  echo Check: docker compose logs nginx
  goto failed
)
ping -n 3 127.0.0.1 >nul
goto https_loop

:open_browser
echo Chat is ready. Opening https://localhost:8443
echo Port 7777 still answers directly, for debugging.
start "" "https://localhost:8443/"
ping -n 4 127.0.0.1 >nul
exit /b 0

:failed
echo.
echo Something went wrong. The window will stay open.
echo Log files:
echo   %LOG%
echo   %ERRLOG%
echo   %ROOT%\logs\nginx\error.log
echo.
pause
exit /b 1

:port_free
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %~1 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if (-not $c) { exit 0 }; $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; $name = if ($p) { $p.ProcessName } else { 'unknown' }; Write-Host ('Port {0} is already in use by {1} (PID {2}).' -f %~1, $name, $c.OwningProcess); Write-Host ('NGINX needs 127.0.0.1:{0}. Stop that program, then run start-chat.bat again.' -f %~1); exit 1"
exit /b !ERRORLEVEL!

:https
curl.exe -k -s --max-time 5 -o NUL "https://localhost:8443/api/health"
exit /b !ERRORLEVEL!

:health
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:7777/api/health' -UseBasicParsing -TimeoutSec 3; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
exit /b !ERRORLEVEL!

:docker
docker info >nul 2>&1
if not errorlevel 1 exit /b 0
echo Docker Desktop is not running. Starting it...
if not exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
  echo Docker Desktop was not found in "%ProgramFiles%\Docker\Docker".
  exit /b 1
)
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
set /a N=0
:wait_docker
ping -n 4 127.0.0.1 >nul
docker info >nul 2>&1
if not errorlevel 1 exit /b 0
set /a N+=1
if !N! GEQ 40 (
  echo Docker Desktop did not become ready within 2 minutes.
  echo Open Docker Desktop, wait until it is running, then try again.
  exit /b 1
)
goto wait_docker

:database
set /a N=0
:wait_db
set "HEALTH="
for /f "usebackq delims=" %%H in (`docker compose ps db --format "{{.Health}}" 2^>nul`) do set "HEALTH=%%H"
if /i "!HEALTH!"=="healthy" exit /b 0
set /a N+=1
if !N! GEQ 30 (
  echo The database container did not become healthy within 60 seconds.
  echo Check: docker compose ps
  exit /b 1
)
ping -n 3 127.0.0.1 >nul
goto wait_db

:ollama
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/version' -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 exit /b 0
echo Ollama is not running. Starting it...
set "OLLAMA_EXE="
where ollama >nul 2>&1 && set "OLLAMA_EXE=ollama"
if not defined OLLAMA_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not defined OLLAMA_EXE (
  echo Ollama was not found. Install it, then try again.
  exit /b 1
)
start "Ollama" /MIN "%OLLAMA_EXE%" serve
set /a N=0
:wait_ollama
ping -n 3 127.0.0.1 >nul
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/version' -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 exit /b 0
set /a N+=1
if !N! GEQ 20 (
  echo Ollama did not respond at http://127.0.0.1:11434 within 40 seconds.
  exit /b 1
)
goto wait_ollama

:shortcuts
powershell -NoProfile -ExecutionPolicy Bypass -Command "$desktop = [Environment]::GetFolderPath('Desktop'); $shell = New-Object -ComObject WScript.Shell; foreach ($pair in @(@('Start Factory Chat.lnk','start-chat.bat'), @('Stop Factory Chat.lnk','stop-chat.bat'))) { $path = Join-Path $desktop $pair[0]; if (-not (Test-Path -LiteralPath $path)) { $link = $shell.CreateShortcut($path); $link.TargetPath = Join-Path '%ROOT%' $pair[1]; $link.WorkingDirectory = '%ROOT%'; $link.Save() } }"
exit /b 0
