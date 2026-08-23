"""경로 입력 3종 + 미디어 정보 (요청서 5절)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body

from ..config import ASSET_BG_DIR, ASSET_SFX_DIR, AUDIO_EXTS, IMAGE_EXTS, load_settings
from ..services import filesystem, native_picker
from ..services.audio_analysis import MediaError, probe
from ._common import fail, ok

router = APIRouter(prefix="/api/files", tags=["files"])


# ① 앱 내장 찾아보기 ────────────────────────────────────────────────────────
@router.get("/browse")
def browse(path: str = "") -> Dict[str, Any]:
    """마지막에 쓴 폴더에서 시작합니다 (빈 화면으로 시작하면 고장난 것처럼 보입니다)."""
    return filesystem.browse(path)


@router.get("/shortcuts")
def shortcuts() -> Dict[str, Any]:
    return {"shortcuts": filesystem.shortcuts(),
            "last_dir": load_settings().get("last_browse_dir", "")}


# ② 윈도우 기본 선택 창 ─────────────────────────────────────────────────────
@router.get("/native/available")
def native_available() -> Dict[str, Any]:
    return native_picker.available()


@router.post("/native/pick")
def native_pick(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """별도 프로세스로 띄우고 타임아웃을 겁니다. 한글 깨짐도 여기서 잡습니다 (3.15)."""
    mode = str(payload.get("mode") or "files")
    initial = str(payload.get("initial_dir") or load_settings().get("last_browse_dir", ""))
    timeout = float(payload.get("timeout") or native_picker.DEFAULT_TIMEOUT)

    result = (native_picker.pick_folder(initial, timeout) if mode == "folder"
              else native_picker.pick_files(initial, timeout))
    if not result.get("ok"):
        return result

    resolved = filesystem.resolve_inputs(result.get("paths") or [])
    return {"ok": True, "videos": resolved["videos"], "problems": resolved["problems"]}


# ③ 직접 붙여넣기 ──────────────────────────────────────────────────────────
@router.post("/resolve")
def resolve(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """붙여넣은 경로들을 영상 목록으로 풉니다. 폴더면 안의 영상을 펼칩니다."""
    items = payload.get("paths")
    if isinstance(items, str):
        items = [line for line in items.splitlines() if line.strip()]
    if not items:
        raise fail("경로를 입력하세요.")
    return ok(**filesystem.resolve_inputs(list(items)))


@router.get("/diagnose")
def diagnose(path: str) -> Dict[str, Any]:
    """오타 난 경로에서 어디까지가 맞는지 알려 줍니다."""
    return filesystem.diagnose_path(path)


@router.get("/videos-in-folder")
def videos_in_folder(path: str) -> Dict[str, Any]:
    return filesystem.videos_in_folder(path)


# ── 미디어 정보 ───────────────────────────────────────────────────────────
@router.post("/probe")
def probe_files(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """해상도·fps·길이·오디오 트랙을 읽습니다 (요청서 6절 0차)."""
    paths = payload.get("paths") or []
    if not paths:
        raise fail("확인할 파일이 없습니다.")

    results: List[Dict[str, Any]] = []
    problems: List[str] = []
    for p in paths:
        try:
            results.append(probe(Path(p)))
        except MediaError as exc:
            problems.append(str(exc))
    return ok(probes=results, problems=problems)


# ── 에셋 (3차 효과음 / 5차 배경) ──────────────────────────────────────────
def _list_assets(directory: Path, exts: set) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not directory.is_dir():
        return out
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.suffix.lower() in exts:
            out.append({"name": p.name, "path": str(p), "size": p.stat().st_size})
    return out


@router.get("/assets")
def assets() -> Dict[str, Any]:
    sfx = _list_assets(ASSET_SFX_DIR, AUDIO_EXTS)
    bg = _list_assets(ASSET_BG_DIR, IMAGE_EXTS)
    return {
        "sfx": sfx,
        "bg": bg,
        "sfx_dir": str(ASSET_SFX_DIR),
        "bg_dir": str(ASSET_BG_DIR),
        "sfx_hint": "" if sfx else f"효과음이 없습니다. wav 파일을 {ASSET_SFX_DIR} 에 넣고 새로고침하세요.",
        "bg_hint": "" if bg else f"배경 이미지가 없습니다. png/jpg를 {ASSET_BG_DIR} 에 넣고 새로고침하세요.",
    }
