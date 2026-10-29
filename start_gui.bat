@echo off
rem LAS CRS Doctor launcher for Windows.
rem First run: creates a private Python environment in .venv and installs the tool.
rem Next runs: starts the GUI directly.
setlocal
cd /d "%~dp0"

if exist ".venv\setup.ok" goto run

echo === LAS CRS Doctor: first-time setup, this takes a minute ===
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

%PY% -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto nopython

if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto fail
echo ok> ".venv\setup.ok"
echo === Setup complete ===

:run
rem Start without a console window. If the GUI never appears, run
rem   .venv\Scripts\python.exe -m lascrs
rem from this folder to see the error message.
start "" ".venv\Scripts\pythonw.exe" -m lascrs
exit /b 0

:nopython
echo.
echo Python 3.9 or newer with Tkinter was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then run this file again. The Microsoft Store shortcut "python" does not count.
pause
exit /b 1

:fail
echo.
echo Setup failed, see the messages above. Delete the .venv folder and try again.
pause
exit /b 1
