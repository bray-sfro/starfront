@echo off
rem  Double-click this on a machine where Starfront will not start.
rem
rem  A batch file rather than a Python script on purpose: the first thing that
rem  can be wrong is that there is no Python at all, and a Python script cannot
rem  report that.  This finds one, runs the real check, and keeps the window
rem  open so the answer can be read.

setlocal
cd /d "%~dp0"
title Starfront - startup check

echo.
echo Looking for Python...
echo.

set "PY="

rem  The py launcher first: it is what a python.org install leaves behind and it
rem  knows about every version on the machine.
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 (
        rem  A bare "python" on Windows may be the Store stub, which exits 9009
        rem  and opens the Microsoft Store instead of running anything.
        python -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PY=python"
    )
)

if not defined PY (
    echo   Python is not installed on this machine, or is not on the PATH.
    echo.
    echo   That is why nothing happens when you double-click Starfront.pyw:
    echo   a .pyw file needs Python to run it, and there is none to run it with.
    echo.
    echo   Install Python 3.10 or newer from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" in the installer. Then run this
    echo   check again.
    echo.
    pause
    exit /b 1
)

echo   Using: %PY%
%PY% -c "import sys; print('  ' + sys.version)"
echo.

%PY% "tools\doctor.py"
set "RESULT=%ERRORLEVEL%"

echo.
if "%RESULT%"=="0" (
    echo Check finished with nothing to fix.
) else (
    echo Check finished. Fix the items above, then run this again.
)
echo.
pause
exit /b %RESULT%
