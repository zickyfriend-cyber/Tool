@echo off
cd /d "%~dp0"

REM ── Python 설치 확인 ─────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo Python이 설치되어 있지 않습니다. 자동으로 설치합니다...
    echo.

    REM winget으로 Python 설치 시도 (Windows 10 1709 이상)
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [오류] 자동 설치에 실패했습니다.
        echo 직접 https://www.python.org 에서 Python을 설치 후 다시 실행하세요.
        pause
        exit /b 1
    )

    REM 설치 후 PATH 갱신
    call refreshenv >nul 2>&1
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    echo Python 설치 완료.
    echo.
)

REM ── 패키지 설치 확인 ─────────────────────────────────────────────────────
python -c "import PyQt5" >nul 2>&1
if errorlevel 1 (
    echo 필요한 패키지를 설치합니다...
    python -m pip install PyQt5 PyQtWebEngine --quiet
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