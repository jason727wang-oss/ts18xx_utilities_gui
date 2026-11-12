@echo off
rem Launch ts18xx_utilities_gui.py (must sit in the same folder as this .bat file)
cd /d "%~dp0"

if not exist "ts18xx_utilities_gui.py" (
    echo Cannot find ts18xx_utilities_gui.py in %~dp0
    pause
    exit /b 1
)

rem Prefer the Python launcher "py", otherwise fall back to "python"
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYEXE=py"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found. Please install Python 3 and tick "Add python.exe to PATH".
        pause
        exit /b 1
    )
    set "PYEXE=python"
)

rem Run with a console so that start-up errors stay visible.
rem To hide the black window, replace the next line with:  start "" pythonw ts18xx_utilities_gui.py
%PYEXE% ts18xx_utilities_gui.py
if errorlevel 1 (
    echo.
    echo ts18xx_utilities_gui.py exited with an error. See the message above.
    pause
)
