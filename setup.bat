@echo off
rem Check Tracker setup (Windows): creates the .venv, installs the
rem requirements and creates an empty database. Needs Python 3.12 or newer.
rem Safe to run again, e.g. after a git pull changed requirements.txt
rem (stop the app first).
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info[:2] < (3, 12))" >NUL 2>&1 && goto venv_ready
    echo The existing .venv is broken or uses a Python older than 3.12.
    echo Delete the .venv folder and run setup.bat again.
    goto fail
)

set "PY="
for %%c in ("py -3.14" "py -3.13" "py -3.12" "python") do if not defined PY call :try_python %%~c
if not defined PY (
    echo Python 3.12 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and run setup.bat again.
    goto fail
)
for /f "delims=" %%v in ('%PY% --version') do echo Using %%v ^(%PY%^)
echo Creating .venv ...
%PY% -m venv .venv || goto fail

:venv_ready
echo Installing requirements ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto fail
".venv\Scripts\python.exe" db.py init || goto fail
echo.
echo Setup complete. Start the app with start.bat.
pause
exit /b 0

:fail
echo.
echo Setup did NOT complete; see the messages above.
pause
exit /b 1

:try_python
%* -c "import sys; sys.exit(sys.version_info[:2] < (3, 12))" >NUL 2>&1 && set "PY=%*"
exit /b 0
