@echo off
rem Fusion Needle launcher for Windows. Double-click it.
rem Finds the repo's .venv, else a 64-bit Python 3.12+ (py launcher, then python on PATH), and starts the app.
rem
rem Written so that it survives a folder name with spaces, brackets or an ampersand:
rem   - every path is built from this file's own folder and quoted;
rem   - the arguments are never expanded inside a bracketed block, where a closing bracket in one ends the block;
rem   - delayed expansion stays off, so an exclamation mark in a path is kept.
rem Keep this file in CRLF line endings and plain ASCII.
setlocal EnableExtensions DisableDelayedExpansion
title Fusion Needle
rem The app does not depend on the current folder; from a network share this cd fails, harmlessly.
cd /d "%~dp0" 2>nul

set "PYEXE="
set "PYARGS="

rem 1. a packaged build next to this file
if not exist "%~dp0FusionNeedle\FusionNeedle.exe" goto find_python
start "" "%~dp0FusionNeedle\FusionNeedle.exe" %*
exit /b 0

:find_python

rem 2. the repo's virtual environment (it has stepserver and needle installed)
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"

rem 3. the py launcher, newest first (64-bit only: the model engine has no 32-bit build)
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 (
    for %%V in (3.14 3.13 3.12) do (
      if not defined PYEXE (
        py -%%V -c "import sys; raise SystemExit(0 if sys.maxsize > 2**32 else 1)" >nul 2>nul
        if not errorlevel 1 (
          set "PYEXE=py"
          set "PYARGS=-%%V"
        )
      )
    )
  )
)

rem 4. python on PATH, if it is new enough
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) and sys.maxsize > 2**32 else 1)" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
  )
)

if not defined PYEXE (
  echo.
  echo   Fusion Needle needs a 64-bit Python 3.12 or newer, and none was found.
  echo.
  echo   Install it from https://www.python.org/downloads/windows/  ^(tick "Add python.exe to PATH"^),
  echo   then set up the project once from the repository folder:
  echo.
  echo       py -3.12 -m venv .venv
  echo       .venv\Scripts\python -m pip install -e .
  echo.
  echo   and double-click this file again.
  echo.
  pause
  exit /b 1
)

"%PYEXE%" %PYARGS% -c "import stepserver" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Note: this Python has no "stepserver" package, so the app cannot start the model itself.
  echo   Set the project up once from the repository folder:
  echo.
  echo       py -3.12 -m venv .venv
  echo       .venv\Scripts\python -m pip install -e .
  echo.
  echo   The app starts anyway; in Settings you can point it at a model server that is already running.
  echo.
)

"%PYEXE%" %PYARGS% "%~dp0fusion_needle.py" %*
if errorlevel 1 (
  echo.
  echo   Fusion Needle stopped with an error ^(see above^).
  pause
)
endlocal
