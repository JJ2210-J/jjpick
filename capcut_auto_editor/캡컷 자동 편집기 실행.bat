@echo off
title 캡컷 자동 편집기

rem 이 파일은 CP949(ANSI)로 저장돼 있습니다. UTF-8로 다시 저장하지 마세요.
rem 한국어 윈도우 cmd는 배치 파일을 코드페이지 949로 읽습니다.
rem UTF-8로 저장한 뒤 chcp 65001을 부르면 cmd가 읽던 위치를 잃고 아무 메시지 없이 즉시 종료됩니다.
rem 그래서 여기서는 chcp를 부르지 않습니다.

cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" run.py
    goto DONE
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.11 run.py
    if not errorlevel 1 goto DONE
)

python run.py
if errorlevel 1 goto FAILED
goto DONE

:FAILED
echo.
echo ============================================================
echo  실행에 실패했습니다.
echo.
echo  다음을 순서대로 확인하세요.
echo    1. Python 3.11 이 설치돼 있는지  ( python --version )
echo    2. 필요한 패키지를 설치했는지
echo         pip install -r requirements.txt
echo    3. ffmpeg 이 설치돼 있는지
echo         winget install Gyan.FFmpeg
echo.
echo  자세한 내용은 logs 폴더의 오늘 날짜 로그를 확인하세요.
echo ============================================================
echo.
pause

:DONE
