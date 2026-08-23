"""0차 이전 — 시스템 상태, 설정, 사전, 로그."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body

from .. import config
from ..services import capcut_draft as cdraft
from ..services import transcribe
from ..services.logging_util import recent_logs
from ._common import ok

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status")
def status() -> Dict[str, Any]:
    """시작 화면에 필요한 전부. 문제가 있으면 무엇을 하면 되는지까지 담습니다."""
    draft_root = config.find_capcut_draft_root()
    media = config.media_tools_status()
    versions = cdraft.version_banner()
    running = cdraft.running_capcut_processes()
    py = config.python_info()

    blockers: List[str] = []
    warnings: List[str] = []

    if draft_root is None:
        blockers.append(
            "캡컷 드래프트 폴더를 찾지 못했습니다. 캡컷을 한 번 실행해 프로젝트를 만들거나, "
            "아래 설정에서 폴더 경로를 직접 지정하세요."
        )
    if not media["ok"]:
        blockers.append(media["hint"])
    if running:
        warnings.append(
            f"캡컷이 실행 중입니다 ({', '.join(sorted(set(running)))}). "
            "드래프트를 쓰는 작업은 차단됩니다. 캡컷을 완전히 종료하세요."
        )
    if not py["supported"]:
        warnings.append(
            f"Python {py['version']}에서 실행 중입니다. 검증된 버전은 3.11입니다. "
            "3.12 이상은 미검증이라 pycapcut/faster-whisper가 동작하지 않을 수 있습니다."
        )
    if not versions["pycapcut_matches"]:
        warnings.append(
            f"pycapcut {versions['pycapcut']}을(를) 쓰고 있습니다. "
            f"검증된 버전은 {versions['pycapcut_verified']}입니다. "
            "드래프트 구조가 달라 결과가 어긋날 수 있습니다."
        )

    return {
        "draft_root": str(draft_root) if draft_root else "",
        "draft_root_found": draft_root is not None,
        "draft_count": len(config.list_local_drafts(draft_root)) if draft_root else 0,
        "media": media,
        "versions": versions,
        "capcut_running": running,
        "capcut_verified": config.VERIFIED_CAPCUT_VERSION,
        "python": py,
        "is_windows": config.IS_WINDOWS,
        "settings": config.load_settings(),
        "whisper_models": transcribe.MODEL_CHOICES,
        "model_status": transcribe.model_status(),
        "blockers": blockers,
        "warnings": warnings,
    }


@router.get("/capcut-running")
def capcut_running() -> Dict[str, Any]:
    """작업 버튼을 누르기 직전에 다시 확인합니다 (요청서 3.13)."""
    running = cdraft.running_capcut_processes()
    return {
        "running": bool(running),
        "processes": sorted(set(running)),
        "message": (
            f"캡컷이 실행 중입니다 ({', '.join(sorted(set(running)))}). "
            "켜진 상태로 드래프트를 고치면 캡컷이 저장할 때 작업이 사라집니다. "
            "완전히 종료한 뒤 다시 시도하세요."
        ) if running else "",
    }


@router.post("/settings")
def update_settings(patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return ok(settings=config.save_settings(patch))


@router.get("/filler-words")
def get_filler_words() -> Dict[str, Any]:
    return {"words": config.load_filler_words()}


@router.post("/filler-words")
def set_filler_words(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return ok(words=config.save_filler_words(payload.get("words") or []))


@router.get("/glossary")
def get_glossary() -> Dict[str, Any]:
    return {"glossary": config.load_glossary()}


@router.post("/glossary")
def set_glossary(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return ok(glossary=config.save_glossary(payload.get("glossary") or {}))


@router.get("/fonts")
def fonts() -> Dict[str, Any]:
    return {"fonts": config.list_installed_fonts()}


@router.get("/model-status")
def model_status(model: str = "") -> Dict[str, Any]:
    """모델을 내려받아야 하는지 **누르기 전에** 알려 줍니다 (요청서 3.19)."""
    return transcribe.model_status(model or None)


@router.get("/logs")
def logs(after: int = 0) -> Dict[str, Any]:
    return {"logs": recent_logs(after_seq=after)}
