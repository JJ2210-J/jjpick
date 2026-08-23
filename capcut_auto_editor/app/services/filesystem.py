"""폴더 탐색, 경로 판정.

요청서 5절 "경로 입력은 세 가지를 모두 제공하십시오" 중 ①앱 내장 찾아보기와
③직접 붙여넣기를 담당합니다. (②윈도우 기본 선택 창은 native_picker.py)

지킬 것:
  · 마지막에 쓴 폴더에서 시작합니다. **시작 위치가 빈 화면이면 고장난 것처럼 보입니다.**
  · 폴더 경로를 넣으면 그 안의 영상 목록을 열어 줍니다 (자주 하는 실수).
  · 경로에 오타가 있으면 **어디까지가 맞는 경로인지** 알려 줍니다.
  · 따옴표·환경변수를 알아서 정리합니다.
"""

from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    IS_WINDOWS,
    VIDEO_EXTS,
    load_settings,
    save_settings,
)

MAX_ENTRIES = 500


def normalize_path(text: str) -> str:
    """붙여넣은 경로를 정리합니다.

    탐색기에서 "경로 복사"를 하면 따옴표가 붙고, 사용자가 %USERPROFILE% 같은
    환경변수를 그대로 쓰는 경우도 있습니다.
    """
    s = (text or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    s = s.strip()
    if not s:
        return ""
    s = os.path.expandvars(s)
    s = os.path.expanduser(s)
    # 윈도우에서 & 로 시작하는 PowerShell 복사 형식 정리
    if s.startswith("& "):
        s = s[2:].strip().strip("\"'")
    return s


def diagnose_path(text: str) -> Dict[str, Any]:
    """오타 난 경로에서 **어디까지가 맞는지** 알려 줍니다 (요청서 5절)."""
    raw = normalize_path(text)
    if not raw:
        return {"input": text, "exists": False, "message": "경로가 비어 있습니다."}

    path = Path(raw)
    if path.exists():
        return {
            "input": text, "normalized": str(path), "exists": True,
            "is_dir": path.is_dir(), "is_file": path.is_file(), "message": "",
        }

    # 존재하는 가장 깊은 조상을 찾습니다.
    current = path
    while current != current.parent and not current.exists():
        current = current.parent

    if not current.exists():
        return {
            "input": text, "normalized": str(path), "exists": False,
            "valid_prefix": "", "message": f"'{path}' 경로를 찾을 수 없습니다.",
        }

    remainder = str(path)[len(str(current)):].lstrip("\\/")
    first_missing = remainder.split(os.sep)[0].split("/")[0] if remainder else ""

    suggestions: List[str] = []
    if first_missing:
        try:
            lowered = first_missing.lower()
            for entry in sorted(current.iterdir())[:MAX_ENTRIES]:
                name = entry.name
                if lowered in name.lower() or name.lower().startswith(lowered[:3]):
                    suggestions.append(name)
                if len(suggestions) >= 8:
                    break
        except OSError:
            pass

    message = f"'{current}' 까지는 맞는 경로입니다. 그 안에 '{first_missing}' 이(가) 없습니다."
    if suggestions:
        message += f" 비슷한 이름: {', '.join(suggestions)}"

    return {
        "input": text, "normalized": str(path), "exists": False,
        "valid_prefix": str(current), "first_missing": first_missing,
        "suggestions": suggestions, "message": message,
    }


def _kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return "other"


def _entry(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    return {
        "name": path.name, "path": str(path), "is_dir": False,
        "kind": _kind(path), "size": size, "modified": mtime,
    }


def shortcuts() -> List[Dict[str, str]]:
    """바로 가기 + 드라이브 목록."""
    out: List[Dict[str, str]] = []
    home = Path.home()
    named = [
        ("바탕 화면", home / "Desktop"), ("문서", home / "Documents"),
        ("동영상", home / "Videos"), ("다운로드", home / "Downloads"),
        ("홈", home),
    ]
    for label, path in named:
        if path.is_dir():
            out.append({"label": label, "path": str(path)})

    if IS_WINDOWS:
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                out.append({"label": f"{letter}: 드라이브", "path": str(drive)})
    else:
        out.append({"label": "루트", "path": "/"})
    return out


def browse(path_text: str = "", *, remember: bool = True) -> Dict[str, Any]:
    """폴더 내용을 나열합니다.

    경로가 비어 있으면 **마지막에 쓴 폴더**에서 시작합니다.
    파일 경로를 넣으면 그 파일이 든 폴더를 엽니다.
    """
    settings = load_settings()
    raw = normalize_path(path_text)

    if not raw:
        raw = settings.get("last_browse_dir") or ""
    if not raw or not Path(raw).exists():
        # 시작 위치가 빈 화면이면 고장난 것처럼 보입니다. 홈으로 대체합니다.
        raw = str(Path.home())

    target = Path(raw)
    if target.is_file():
        target = target.parent

    if not target.is_dir():
        diag = diagnose_path(path_text)
        return {
            "path": str(target), "ok": False, "message": diag.get("message", ""),
            "entries": [], "shortcuts": shortcuts(), "parent": "",
            "diagnosis": diag,
        }

    dirs: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []
    truncated = False
    try:
        for i, entry in enumerate(sorted(target.iterdir(), key=lambda p: p.name.lower())):
            if i >= MAX_ENTRIES:
                truncated = True
                break
            try:
                if entry.is_dir():
                    if not entry.name.startswith("."):
                        dirs.append({"name": entry.name, "path": str(entry), "is_dir": True,
                                     "kind": "dir"})
                elif _kind(entry) in ("video", "image", "audio"):
                    files.append(_entry(entry))
            except OSError:
                continue
    except PermissionError:
        return {
            "path": str(target), "ok": False, "entries": [], "parent": str(target.parent),
            "shortcuts": shortcuts(),
            "message": f"'{target}' 폴더를 읽을 권한이 없습니다. 다른 폴더를 고르거나 관리자 권한으로 실행하세요.",
        }

    if remember:
        save_settings({"last_browse_dir": str(target)})

    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "ok": True,
        "entries": dirs + files,
        "video_count": sum(1 for f in files if f["kind"] == "video"),
        "shortcuts": shortcuts(),
        "truncated": truncated,
        "message": (f"{MAX_ENTRIES}개까지만 표시합니다. 하위 폴더로 좁혀 보세요."
                    if truncated else ""),
    }


def videos_in_folder(path_text: str) -> Dict[str, Any]:
    """폴더 경로를 넣으면 그 안의 영상 목록을 돌려줍니다 (요청서 5절 — 자주 하는 실수)."""
    raw = normalize_path(path_text)
    path = Path(raw) if raw else None

    if not path or not path.exists():
        diag = diagnose_path(path_text)
        return {"ok": False, "videos": [], "message": diag.get("message", ""), "diagnosis": diag}

    if path.is_file():
        if _kind(path) == "video":
            return {"ok": True, "videos": [_entry(path)], "folder": str(path.parent), "message": ""}
        return {
            "ok": False, "videos": [], "folder": str(path.parent),
            "message": f"'{path.name}'은(는) 영상 파일이 아닙니다. "
                       f"지원 확장자: {', '.join(sorted(VIDEO_EXTS))}",
        }

    videos: List[Dict[str, Any]] = []
    try:
        for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file() and _kind(entry) == "video":
                videos.append(_entry(entry))
    except (OSError, PermissionError) as exc:
        return {"ok": False, "videos": [], "message": f"폴더를 읽지 못했습니다: {exc}"}

    return {
        "ok": True, "videos": videos, "folder": str(path),
        "message": "" if videos else
                   f"'{path.name}' 폴더에 영상 파일이 없습니다. "
                   f"(찾는 확장자: {', '.join(sorted(VIDEO_EXTS))})",
    }


def resolve_inputs(items: List[str]) -> Dict[str, Any]:
    """붙여넣기/드롭으로 들어온 경로 목록을 영상 파일 목록으로 풉니다.

    폴더가 섞여 있으면 그 안의 영상을 펼쳐 넣습니다.
    """
    videos: List[Dict[str, Any]] = []
    problems: List[str] = []
    seen: set[str] = set()

    for item in items:
        raw = normalize_path(item)
        if not raw:
            continue
        path = Path(raw)
        if not path.exists():
            problems.append(diagnose_path(item).get("message", f"'{item}' 없음"))
            continue
        if path.is_dir():
            found = videos_in_folder(raw)
            if not found["videos"]:
                problems.append(found.get("message", ""))
            for v in found["videos"]:
                if v["path"] not in seen:
                    seen.add(v["path"])
                    videos.append(v)
        elif _kind(path) == "video":
            if str(path) not in seen:
                seen.add(str(path))
                videos.append(_entry(path))
        else:
            problems.append(f"'{path.name}'은(는) 영상 파일이 아닙니다.")

    return {"videos": videos, "problems": [p for p in problems if p]}
