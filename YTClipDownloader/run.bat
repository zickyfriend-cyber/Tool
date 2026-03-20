@echo off
chcp 65001
cd /d "%~dp0"

set PYTHON=
call :find_python
if defined PYTHON goto :check_version
goto :install_python

:find_python
where python > "%TEMP%\_pycheck.txt" 2>&1
if not errorlevel 1 (
    set /p PYTHON=<"%TEMP%\_pycheck.txt"
    if exist "%TEMP%\_pycheck.txt" del "%TEMP%\_pycheck.txt"
    goto :eof
)
if exist "%TEMP%\_pycheck.txt" del "%TEMP%\_pycheck.txt"
for %%v in (313 312 311 310 39 38) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
        set PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe
        goto :eof
    )
    if exist "C:\Python%%v\python.exe" (
        set PYTHON=C:\Python%%v\python.exe
        goto :eof
    )
)
goto :eof

:check_version
for /f "tokens=2 delims= " %%v in ('%PYTHON% --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (set PYMAJ=%%a & set PYMIN=%%b)
if %PYMAJ% LSS 3 goto :install_python
if %PYMAJ% EQU 3 if %PYMIN% LSS 8 (
    echo Python %PYVER% is too old. Installing 3.12...
    goto :install_python
)
goto :check_packages

:install_python
echo Installing Python 3.12...
echo.
winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo [ERROR] Auto-install failed.
    echo Please install Python 3.8+ from https://www.python.org and run again.
    pause
    exit /b 1
)
echo Python installed. Searching path...
echo.
set PYTHON=
call :find_python
if not defined PYTHON (
    echo [ERROR] Python executable not found.
    echo Please restart your computer and try again.
    pause
    exit /b 1
)

:check_packages
"%PYTHON%" -c "import PyQt5" > "%TEMP%\_pyqt5check.txt" 2>&1
set PYQ5ERR=%errorlevel%
if exist "%TEMP%\_pyqt5check.txt" del "%TEMP%\_pyqt5check.txt"
if %PYQ5ERR% NEQ 0 (
    echo Installing required packages...
    "%PYTHON%" -m pip install --upgrade pip --quiet
    "%PYTHON%" -m pip install "PyQt5>=5.15" "PyQtWebEngine>=5.15" --quiet
    if errorlevel 1 (
        echo [ERROR] Package installation failed.
        pause
        exit /b 1
    )
    echo Packages installed.
    echo.
)

"%PYTHON%" main.py
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to run.
    pause
)