"""윈도우 기본 선택 창 (별도 프로세스).

요청서 5절 경로 입력 3종 중 ②입니다. 별도 프로세스로 띄워 **타임아웃**을 걸 수 있게
합니다. tkinter 대화상자는 부모 프로세스 안에서 열면 서버 이벤트 루프를 막고,
사용자가 창을 닫지 않으면 영원히 돌아오지 않습니다.

⚠ 요청서 3.15 — 하위 프로세스와 주고받는 한글은 UTF-8로 못을 박습니다
    자식 파이썬의 stdout 인코딩은 로케일(한국어 윈도우는 cp949)로 정해지는데
    부모가 UTF-8로 읽으면 한글 경로가 깨집니다.
        C:\\Users\\...\\개인\\유툽\\...  ->  C:\\Users\\...\\????\\????\\...
    **환경에 따라 나타났다 안 나타났다 합니다.** 개발 터미널에 PYTHONIOENCODING이
    설정돼 있으면 정상 동작하다가, 탐색기에서 bat으로 띄우면 깨집니다.

    세 겹으로 막습니다.
      ① 자식이 시작하자마자 sys.stdout.reconfigure(encoding="utf-8")
      ② 부모가 자식 환경에 PYTHONIOENCODING=utf-8
      ③ 그래도 U+FFFD가 섞이면 "경로의 한글이 깨졌습니다"라고 명확히 알림

이 파일은 **모듈이면서 스크립트**입니다. 그래서 최상위에서 패키지 상대 임포트를
하지 않습니다 (자식은 `python native_picker.py`로 직접 실행됩니다).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPLACEMENT_CHAR = "\ufffd"
DEFAULT_TIMEOUT = 180.0


# ══════════════════════════════════════════════════════════════════════════
# 자식 프로세스 (대화상자를 실제로 여는 쪽)
# ══════════════════════════════════════════════════════════════════════════
def _child_main() -> int:
    # ① 시작하자마자 stdout을 UTF-8로 고정합니다.
    try:
        sys.stdout.reconfigure(encoding="utf-8")       # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - pythonw 등에서 stdout이 없을 수 있습니다
        pass

    mode = sys.argv[1] if len(sys.argv) > 1 else "files"
    initial = sys.argv[2] if len(sys.argv) > 2 else ""

    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        _emit({"ok": False, "paths": [],
               "error": f"tkinter를 쓸 수 없습니다 ({exc}). "
                        "앱 내장 찾아보기나 경로 직접 붙여넣기를 사용하세요."})
        return 1

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    kwargs: Dict[str, Any] = {}
    if initial and Path(initial).is_dir():
        kwargs["initialdir"] = initial

    try:
        if mode == "folder":
            picked = filedialog.askdirectory(title="폴더 선택", **kwargs)
            paths = [picked] if picked else []
        else:
            selection = filedialog.askopenfilenames(
                title="영상 파일 선택 (여러 개 선택 가능)",
                filetypes=[
                    ("영상 파일", "*.mp4 *.mov *.avi *.mkv *.m4v *.wmv *.webm"),
                    ("모든 파일", "*.*"),
                ],
                **kwargs,
            )
            paths = list(selection)
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "paths": [], "error": f"선택 창을 열지 못했습니다: {exc}"})
        return 1
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    _emit({"ok": True, "paths": paths, "error": ""})
    return 0


def _emit(payload: Dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        # stdout이 막혔으면 바이트로 직접 씁니다.
        try:
            sys.stdout.buffer.write(text.encode("utf-8"))    # type: ignore[union-attr]
            sys.stdout.buffer.flush()                        # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


# ══════════════════════════════════════════════════════════════════════════
# 부모 프로세스
# ══════════════════════════════════════════════════════════════════════════
def _spawn(mode: str, initial: str, timeout: float) -> Dict[str, Any]:
    script = str(Path(__file__).resolve())

    # ② 자식 환경에 PYTHONIOENCODING을 심습니다.
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    exe = sys.executable
    # pythonw.exe로 서버를 띄운 경우에도 자식은 콘솔 없는 실행 파일을 그대로 씁니다.
    creationflags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW

    try:
        proc = subprocess.run(
            [exe, script, mode, initial or ""],
            capture_output=True, timeout=timeout, env=env,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "paths": [],
            "error": f"선택 창이 {int(timeout)}초 안에 닫히지 않아 취소했습니다. "
                     "창이 다른 윈도우 뒤에 가려져 있을 수 있습니다. "
                     "앱 내장 찾아보기를 쓰거나 경로를 직접 붙여넣어 보세요.",
        }
    except OSError as exc:
        return {"ok": False, "paths": [], "error": f"선택 창 프로세스를 띄우지 못했습니다: {exc}"}

    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "paths": [],
                "error": f"선택 창이 아무것도 돌려주지 않았습니다.\n{err[:400]}"}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "paths": [],
                "error": f"선택 창 응답을 해석하지 못했습니다: {raw[:200]}"}

    return _check_mojibake(payload)


def _check_mojibake(payload: Dict[str, Any]) -> Dict[str, Any]:
    """③ 그래도 깨진 글자가 섞이면 명확히 알립니다 (요청서 3.15)."""
    paths: List[str] = payload.get("paths") or []
    broken = [p for p in paths if REPLACEMENT_CHAR in p]
    if broken:
        payload["ok"] = False
        payload["mojibake"] = True
        payload["error"] = (
            "경로의 한글이 깨졌습니다. 하위 프로세스가 UTF-8로 응답하지 않았습니다.\n"
            f"받은 값: {broken[0][:120]}\n"
            "앱 내장 찾아보기를 쓰거나, 경로를 직접 붙여넣어 주세요. "
            "(이 증상은 탐색기에서 bat으로 띄웠을 때만 나타나기도 합니다)"
        )
    return payload


def pick_files(initial_dir: str = "", timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """윈도우 기본 파일 선택 창. 다중 선택 지원."""
    return _spawn("files", initial_dir, timeout)


def pick_folder(initial_dir: str = "", timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """윈도우 기본 폴더 선택 창."""
    return _spawn("folder", initial_dir, timeout)


def available() -> Dict[str, Any]:
    """tkinter를 쓸 수 있는지 미리 확인합니다 (버튼을 비활성화할지 판단)."""
    try:
        import tkinter  # noqa: F401
        return {"available": True, "reason": ""}
    except ImportError as exc:
        return {
            "available": False,
            "reason": f"tkinter가 없어 윈도우 기본 선택 창을 쓸 수 없습니다 ({exc}). "
                      "앱 내장 찾아보기와 직접 붙여넣기는 그대로 쓸 수 있습니다.",
        }


if __name__ == "__main__":
    raise SystemExit(_child_main())
