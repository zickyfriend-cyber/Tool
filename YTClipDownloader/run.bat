@echo off
cd /d "%~dp0"
python main.py
if errorlevel 1 (
    echo.
    echo [오류] 실행에 실패했습니다.
    echo setup.bat 을 먼저 실행해서 필요한 패키지를 설치하세요.
    pause
)