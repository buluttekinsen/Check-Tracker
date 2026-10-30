@echo off
rem Check Tracker: start the local server and open it in the default browser.
rem Close this window (or press Ctrl+C) to stop the server.
rem The browser poller below is this same script, started in the background
rem with the URL inherited from this window's environment.
if "%~1"==":open-when-ready" goto open_when_ready

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Project venv not found. Run setup.bat first.
    pause
    exit /b 1
)

rem The port comes from config.py / config_local.py.
set "PORT="
for /f "delims=" %%p in ('.venv\Scripts\python.exe -c "import config; print(config.PORT)"') do set "PORT=%%p"
if not defined PORT (
    echo Could not read PORT from config.py / config_local.py.
    pause
    exit /b 1
)
set "URL=http://127.0.0.1:%PORT%/"
title Check Tracker - %URL%

rem Already running (e.g. a second double-click)? Just open the browser.
curl.exe -s -o NUL --max-time 2 "%URL%accounts" && (
    start "" "%URL%"
    exit /b 0
)

rem Open the browser in the background as soon as the server answers.
start "" /b cmd /c ""%~f0" :open-when-ready"

echo Check Tracker is running at %URL%
echo Close this window to stop it.
echo.
".venv\Scripts\python.exe" app.py
echo.
echo The server has stopped.
pause
exit /b

:open_when_ready
rem Poll for up to ~30 s; ping is used as a 1-second sleep.
for /l %%i in (1,1,30) do (
    curl.exe -s -o NUL --max-time 1 "%URL%accounts" && (
        start "" "%URL%"
        exit /b 0
    )
    ping -n 2 127.0.0.1 >NUL
)
exit /b 1
