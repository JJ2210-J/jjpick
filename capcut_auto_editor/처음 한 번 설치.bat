@echo off
title 캡컷 자동 편집기 - 처음 한 번 설치

rem 이 파일도 CP949(ANSI)로 저장돼 있습니다. chcp를 부르지 않습니다.

cd /d "%~dp0"

echo ============================================================
echo  캡컷 자동 편집기 설치
echo  Python 3.11 이 필요합니다.
echo ============================================================
echo.

python --version
if errorlevel 1 goto NOPYTHON

echo.
echo 필요한 패키지를 설치합니다. 몇 분 걸립니다.
echo.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto FAILED

echo.
echo ffmpeg 이 있는지 확인합니다.
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [확인 필요] ffmpeg 을 찾지 못했습니다.
    echo  아래 명령으로 설치한 뒤 새 창을 열어 다시 실행하세요.
    echo      winget install Gyan.FFmpeg
    echo.
)

echo.
echo 설치가 끝났습니다. "캡컷 자동 편집기 실행.bat" 을 더블클릭하세요.
echo.
pause
goto DONE

:NOPYTHON
echo.
echo  Python 을 찾지 못했습니다.
echo  python.org 에서 3.11 을 설치하고, 설치할 때
echo  "Add Python to PATH" 를 반드시 체크하세요.
echo.
pause
goto DONE

:FAILED
echo.
echo  패키지 설치에 실패했습니다. 위 메시지를 확인하세요.
echo.
pause

:DONE
