"""경로 탐지, 설정, 사전.

요청서 2절: 캡컷 드래프트 폴더 경로를 소스에 하드코딩하지 않습니다.
전부 런타임 탐지하고, 사용자가 설정으로 덮어쓸 수 있게 합니다.
"""

from __future__ import annotations

import json
import os
import shutil
import string
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 프로젝트 경로 ──────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = ROOT_DIR / "data"
BACKUP_DIR = ROOT_DIR / "backups"
LOG_DIR = ROOT_DIR / "logs"
ASSET_SFX_DIR = ROOT_DIR / "assets" / "sfx"
ASSET_BG_DIR = ROOT_DIR / "assets" / "bg"
SESSION_DIR = DATA_DIR / "sessions"
WORK_DIR = DATA_DIR / "work"
STATIC_DIR = APP_DIR / "static"

for _d in (DATA_DIR, BACKUP_DIR, LOG_DIR, ASSET_SFX_DIR, ASSET_BG_DIR, SESSION_DIR, WORK_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SETTINGS_PATH = DATA_DIR / "settings.json"
STYLE_PROFILE_PATH = DATA_DIR / "style_profile.json"
FILLER_WORDS_PATH = DATA_DIR / "filler_words.json"
GLOSSARY_PATH = DATA_DIR / "glossary_en.json"

IS_WINDOWS = os.name == "nt"

# ── 검증된 버전 (요청서 1절) ───────────────────────────────────────────────
VERIFIED_CAPCUT_VERSION = "9.3.0.3969"
VERIFIED_PYCAPCUT_VERSION = "0.0.3"

# 캡컷 프로세스명 (요청서 3.13). 멀티프로세스라 7~8개가 뜨므로 개수는 무의미하고
# "하나라도 있으면 차단"이 규칙입니다.
CAPCUT_PROCESS_NAMES = ("capcut", "jianyingpro")

# 요청서 3.1: 같은 Projects 폴더의 클라우드/템플릿 드래프트는 대상이 아닙니다.
DRAFT_DIR_NAME = "com.lveditor.draft"
EXCLUDED_DRAFT_DIR_PREFIXES = ("com.lveditor.cloud.draft", "com.lveditor.textTemplate.draft")

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".flv", ".webm", ".mpg", ".mpeg"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}

# ── 기본 설정 ──────────────────────────────────────────────────────────────
DEFAULT_SETTINGS: Dict[str, Any] = {
    "draft_root": "",              # 비우면 자동 탐지
    "ffmpeg_path": "",             # 비우면 PATH에서 탐색
    "ffprobe_path": "",
    "last_browse_dir": "",
    # 1차 컷 편집 기본값 (요청서 6절)
    "silence_threshold_db": -35.0,
    "min_silence_sec": 0.6,
    "tail_pad_sec": 0.35,          # 말 끝난 뒤 여유 (요청서 3.17 — 넉넉히)
    "head_pad_sec": 0.15,          # 다음 말 시작 전 여유
    # STT (요청서 3.19)
    "whisper_model": "medium",     # large-v3 아님. CPU 전제.
    "whisper_language": "ko",
    "whisper_compute_type": "int8",
    "whisper_chunk_sec": 600,
    # 자막 (요청서 6절)
    "subtitle_max_chars": 36,
    "subtitle_min_duration_sec": 0.35,
    # 세로 영상 (요청서 6절 5차 — 실측 채택값)
    "vertical_scale": 1.8,
    "vertical_transform_y": -0.078125,
    "vertical_width": 1080,
    "vertical_height": 1920,
    # 3차
    "transition_duration_sec": 0.5,
    "sfx_volume": 0.6,
    # 4차
    "auto_export_enabled": False,  # 요청서: 기본 OFF, 확인 체크박스 필수
}

DEFAULT_FILLER_WORDS: List[str] = [
    "어", "음", "그", "그니까", "저기", "뭐지", "이제", "아 그", "그러니까 이제",
    "약간", "뭐랄까", "인제", "그래서 이제", "아니 그", "뭐 그",
]

DEFAULT_GLOSSARY: Dict[str, List[Dict[str, Any]]] = {
    "물류": [
        {"term": t, "enabled": True} for t in [
            "Forwarding", "B/L", "HBL", "MBL", "Incoterms", "FCL", "LCL", "CBM",
            "ETD", "ETA", "Demurrage", "Detention", "Consignee", "Shipper",
            "Booking", "Manifest", "CargoWise",
        ]
    ],
    "AI": [
        {"term": t, "enabled": True} for t in [
            "Prompt", "Token", "Context", "Agent", "API", "LLM", "MCP",
            "Workflow", "Automation",
        ]
    ],
}


# ── 설정 읽기/쓰기 ─────────────────────────────────────────────────────────
def _read_json(path: Path, fallback: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return fallback
    except (json.JSONDecodeError, OSError):
        return fallback


def write_json_atomic(path: Path, payload: Any) -> None:
    """같은 폴더에 임시 파일로 쓴 뒤 교체합니다.

    중간에 죽어도 기존 파일이 깨지지 않습니다 (요청서 2번 원칙: 원본 파괴 금지).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_settings() -> Dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    merged.update(_read_json(SETTINGS_PATH, {}) or {})
    return merged


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    current = load_settings()
    current.update(patch)
    write_json_atomic(SETTINGS_PATH, current)
    return current


def load_filler_words() -> List[str]:
    words = _read_json(FILLER_WORDS_PATH, None)
    if not isinstance(words, list) or not words:
        write_json_atomic(FILLER_WORDS_PATH, DEFAULT_FILLER_WORDS)
        return list(DEFAULT_FILLER_WORDS)
    return [str(w) for w in words]


def save_filler_words(words: List[str]) -> List[str]:
    cleaned, seen = [], set()
    for w in words:
        w = str(w).strip()
        if w and w not in seen:
            seen.add(w)
            cleaned.append(w)
    write_json_atomic(FILLER_WORDS_PATH, cleaned)
    return cleaned


def load_glossary() -> Dict[str, Any]:
    data = _read_json(GLOSSARY_PATH, None)
    if not isinstance(data, dict) or not data:
        write_json_atomic(GLOSSARY_PATH, DEFAULT_GLOSSARY)
        return json.loads(json.dumps(DEFAULT_GLOSSARY))
    return data


def save_glossary(data: Dict[str, Any]) -> Dict[str, Any]:
    write_json_atomic(GLOSSARY_PATH, data)
    return data


# ── 캡컷 드래프트 폴더 자동 탐지 (요청서 2절) ──────────────────────────────
def _looks_like_draft_root(path: Path) -> bool:
    """드래프트 루트 판정.

    폴더 존재만으로는 부족합니다. 캡컷이 관리하는 정본 레지스트리인
    root_meta_info.json이 있어야 진짜 드래프트 루트입니다.
    """
    try:
        return path.is_dir() and (path / "root_meta_info.json").is_file()
    except OSError:
        return False


def _candidate_roots() -> List[Path]:
    rel = Path("CapCut") / "User Data" / "Projects" / DRAFT_DIR_NAME
    bases: List[Path] = []
    for env in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME"):
        v = os.environ.get(env)
        if v:
            bases.append(Path(v))
    home = Path.home()
    bases += [home / "AppData" / "Local", home / "AppData" / "Roaming", home]

    out: List[Path] = []
    seen = set()
    for b in bases:
        for cand in (b / rel, b / "Local" / rel):
            s = str(cand)
            if s not in seen:
                seen.add(s)
                out.append(cand)
    return out


def _scan_drives() -> List[Path]:
    """고정 드라이브 얕은 탐색. 마지막 수단이라 깊이를 제한합니다."""
    if not IS_WINDOWS:
        return []
    found: List[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            continue
        users = drive / "Users"
        if not users.is_dir():
            continue
        try:
            entries = list(users.iterdir())[:200]
        except OSError:
            continue
        for e in entries:
            cand = e / "AppData" / "Local" / "CapCut" / "User Data" / "Projects" / DRAFT_DIR_NAME
            if _looks_like_draft_root(cand):
                found.append(cand)
    return found


def find_capcut_draft_root() -> Optional[Path]:
    """드래프트 루트를 탐지합니다. 못 찾으면 None (호출부가 안내 문구를 띄웁니다)."""
    settings = load_settings()

    manual = (settings.get("draft_root") or "").strip()
    if manual:
        p = Path(manual)
        if _looks_like_draft_root(p):
            return p

    env = (os.environ.get("CAPCUT_DRAFT_ROOT") or "").strip()
    if env:
        p = Path(env)
        if _looks_like_draft_root(p):
            return p

    for cand in _candidate_roots():
        if _looks_like_draft_root(cand):
            return cand

    for cand in _scan_drives():
        return cand

    return None


def list_local_drafts(draft_root: Path) -> List[Dict[str, Any]]:
    """드래프트 루트의 로컬 드래프트만 나열합니다 (클라우드/템플릿 제외)."""
    out: List[Dict[str, Any]] = []
    try:
        entries = sorted(draft_root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        content = entry / "draft_content.json"
        if not content.is_file():
            continue
        try:
            mtime = content.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append({"name": entry.name, "path": str(entry), "modified": mtime})
    out.sort(key=lambda d: d["modified"], reverse=True)
    return out


def is_excluded_projects_dir(name: str) -> bool:
    """Projects 폴더 하위에서 우리가 다루면 안 되는 디렉터리인지."""
    return any(name.startswith(p) for p in EXCLUDED_DRAFT_DIR_PREFIXES)


# ── ffmpeg / ffprobe 탐지 ──────────────────────────────────────────────────
def _resolve_binary(kind: str) -> Optional[str]:
    settings = load_settings()
    manual = (settings.get(f"{kind}_path") or "").strip()
    if manual and Path(manual).is_file():
        return manual
    found = shutil.which(kind)
    if found:
        return found
    if IS_WINDOWS:
        bases = [Path("C:/ffmpeg/bin"), Path("C:/Program Files/ffmpeg/bin")]
        lad = os.environ.get("LOCALAPPDATA")
        if lad:
            bases.append(Path(lad) / "Microsoft" / "WinGet" / "Packages")
        for base in bases:
            if not base.exists():
                continue
            try:
                for p in base.rglob(f"{kind}.exe"):
                    return str(p)
            except OSError:
                continue
    return None


def ffmpeg_path() -> Optional[str]:
    return _resolve_binary("ffmpeg")


def ffprobe_path() -> Optional[str]:
    return _resolve_binary("ffprobe")


def media_tools_status() -> Dict[str, Any]:
    ff, fp = ffmpeg_path(), ffprobe_path()
    return {
        "ffmpeg": ff,
        "ffprobe": fp,
        "ok": bool(ff and fp),
        "hint": (
            "ffmpeg/ffprobe를 찾지 못했습니다. "
            "winget install Gyan.FFmpeg 로 설치한 뒤 새 터미널에서 다시 실행하거나, "
            "설정에서 실행 파일 경로를 직접 지정하세요."
        ) if not (ff and fp) else "",
    }


# ── 폰트 탐색 (요청서 3.6) ─────────────────────────────────────────────────
def font_dirs() -> List[Path]:
    dirs: List[Path] = []
    if IS_WINDOWS:
        lad = os.environ.get("LOCALAPPDATA")
        if lad:
            dirs.append(Path(lad) / "Microsoft" / "Windows" / "Fonts")
        dirs.append(Path("C:/Windows/Fonts"))
    else:  # 개발/테스트 환경
        dirs += [Path.home() / ".fonts", Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
    return dirs


FONT_EXTS = {".ttf", ".otf", ".ttc"}


def list_installed_fonts() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for d in font_dirs():
        if not d.is_dir():
            continue
        try:
            for p in d.rglob("*"):
                if p.suffix.lower() in FONT_EXTS and p.name not in seen:
                    seen.add(p.name)
                    out.append({"file": p.name, "stem": p.stem, "path": str(p)})
        except OSError:
            continue
    out.sort(key=lambda d: d["stem"].lower())
    return out


def python_info() -> Dict[str, Any]:
    return {
        "version": "%d.%d.%d" % sys.version_info[:3],
        "executable": sys.executable,
        "supported": sys.version_info[:2] == (3, 11),
    }
