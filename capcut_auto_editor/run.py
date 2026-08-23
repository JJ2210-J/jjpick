#!/usr/bin/env python
"""캡컷 자동 편집기 실행 — 서버 기동 + 브라우저 자동 오픈.

요청서 3.16: pythonw.exe로 띄우면 sys.stdout이 None이라
시작 메시지 print()에서 즉시 죽습니다. 안전한 출력 함수를 씁니다.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DEFAULT_PORT = 8733


def say(*args: object) -> None:
    """pythonw.exe에서 stdout이 None이어도 죽지 않는 print (요청서 3.16)."""
    if sys.stdout is None:
        return
    try:
        print(*args)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def find_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(
        f"{start}부터 {start + tries - 1}까지 모두 사용 중이라 서버를 띄우지 못했습니다.\n"
        "다른 프로그램을 끄거나 잠시 뒤 다시 실행하세요."
    )


def check_requirements() -> list[str]:
    problems: list[str] = []
    if sys.version_info[:2] != (3, 11):
        problems.append(
            f"Python {sys.version_info.major}.{sys.version_info.minor}에서 실행 중입니다. "
            "검증된 버전은 3.11입니다. 3.12 이상은 미검증입니다."
        )
    for module, hint in (
        ("fastapi", "pip install -r requirements.txt"),
        ("uvicorn", "pip install -r requirements.txt"),
        ("pycapcut", "pip install pycapcut==0.0.3"),
    ):
        try:
            __import__(module)
        except ImportError:
            problems.append(f"{module}이(가) 설치돼 있지 않습니다. {hint}")
    return problems


def open_browser_later(url: str, delay: float = 1.2) -> None:
    def opener() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            say(f"브라우저를 자동으로 열지 못했습니다. 직접 열어 주세요: {url}")
    threading.Thread(target=opener, daemon=True).start()


def main() -> int:
    # 자식 프로세스와 주고받는 한글을 UTF-8로 못 박습니다 (요청서 3.15).
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8")   # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    problems = check_requirements()
    fatal = [p for p in problems if "설치돼 있지 않습니다" in p]
    for p in problems:
        say("[확인 필요]", p)
    if fatal:
        say("\n필요한 패키지를 설치한 뒤 다시 실행하세요.")
        return 1

    try:
        import uvicorn
        from app.main import app
    except Exception as exc:  # noqa: BLE001
        say(f"앱을 불러오지 못했습니다: {type(exc).__name__}: {exc}")
        return 1

    port = find_port()
    url = f"http://127.0.0.1:{port}"

    say("=" * 56)
    say("  캡컷 자동 편집기")
    say(f"  주소: {url}")
    say("  종료: 이 창에서 Ctrl+C")
    say("=" * 56)

    if "--no-browser" not in sys.argv:
        open_browser_later(url)

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        say("\n종료합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
