@echo off
cd /d "%~dp0"

REM ── Python 설치 / 버전 확인 ──────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 goto :install_python

REM Python 버전이 3.8 미만이면 재설치
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (set PYMAJ=%%a & set PYMIN=%%b)
if %PYMAJ% LSS 3 goto :install_python
if %PYMAJ% EQU 3 if %PYMIN% LSS 8 (
    echo 현재 Python %PYVER% 은 너무 오래된 버전입니다. Python 3.12 를 설치합니다...
    echo.
    goto :install_python
)
goto :check_packages

:install_python
echo Python 을 설치합니다...
echo.
winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo [오류] 자동 설치에 실패했습니다.
    echo 직접 https://www.python.org 에서 Python 3.8 이상을 설치 후 다시 실행하세요.
    pause
    exit /b 1
)
call refreshenv >nul 2>&1
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
echo Python 설치 완료.
echo.

:check_packages
REM ── 패키지 설치 확인 ─────────────────────────────────────────────────────
python -c "import PyQt5" >nul 2>&1
if errorlevel 1 (
    echo 필요한 패키지를 설치합니다...
    python -m pip install --upgrade pip --user --quiet 2>nul
    python -m pip install "PyQt5>=5.15" "PyQtWebEngine>=5.15" --user --quiet
    if errorlevel 1 (
        echo [오류] 패키지 설치에 실패했습니다.
        echo Python 버전이 너무 오래된 경우 3.8 이상으로 업그레이드 후 다시 실행하세요.
        pause
        exit /b 1
    )
    echo 패키지 설치 완료.
    echo.
)

REM ── 실행 ─────────────────────────────────────────────────────────────────
python main.py
if errorlevel 1 (
    echo.
    echo [오류] 실행에 실패했습니다.
    pause
)