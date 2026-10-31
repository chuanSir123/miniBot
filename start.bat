@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title MiniBot
cd /d "%~dp0"
if errorlevel 1 goto location_failed

echo =====================================================
echo MiniBot launcher
echo =====================================================

if exist ".venv\Scripts\python.exe" goto validate_venv

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto python_missing
set "BOOTSTRAP=py -3"
goto create_venv

:use_python
set "BOOTSTRAP=python"

:create_venv
echo Creating the project virtual environment...
%BOOTSTRAP% -m venv ".venv"
if errorlevel 1 goto venv_failed
".venv\Scripts\python.exe" -m pip config --site set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>&1

:validate_venv
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto venv_invalid
".venv\Scripts\python.exe" -u launcher.py
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" exit /b 0
if "%EXIT_CODE%"=="130" exit /b 130
echo.
echo MiniBot exited with code %EXIT_CODE%. See the error above.
goto failed

:location_failed
echo Cannot open the MiniBot project directory.
set "EXIT_CODE=1"
goto failed

:python_missing
echo Python 3.10 or newer is required. Install Python and enable Add to PATH.
set "EXIT_CODE=1"
goto failed

:venv_failed
echo Failed to create the project virtual environment. See the error above.
set "EXIT_CODE=1"
goto failed

:venv_invalid
echo The .venv Python interpreter is missing, broken, or older than Python 3.10.
echo Repair the virtual environment before trying again.
set "EXIT_CODE=1"

:failed
echo.
pause
exit /b %EXIT_CODE%
