"""자막 스타일 캘리브레이션 (1차보다 먼저 — 요청서 6절).

이게 있어야 2차 자막이 캡컷 화면에 실제로 보입니다.
같은 화면에서 트랜지션도 함께 가져옵니다 (요청서 3.7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body

from ..config import find_capcut_draft_root, list_local_drafts
from ..services import capcut_draft as cdraft
from ..services import session as session_svc
from ._common import fail, get_session, ok, require_draft_root

router = APIRouter(prefix="/api/calibration", tags=["calibration"])


@router.get("/drafts")
def drafts() -> Dict[str, Any]:
    """참조로 쓸 수 있는 로컬 드래프트 목록 (클라우드/템플릿 제외)."""
    root = find_capcut_draft_root()
    if root is None:
        return {"drafts": [], "draft_root": "",
                "message": "캡컷 드래프트 폴더를 찾지 못했습니다. 설정에서 경로를 지정하세요."}
    return {"drafts": list_local_drafts(root), "draft_root": str(root), "message": ""}


@router.get("/profile")
def profile() -> Dict[str, Any]:
    saved = cdraft.load_style_profile()
    return {"profile": saved, "exists": saved is not None}


@router.post("/capture")
def capture(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """참조 드래프트에서 폰트·크기·색상·배경·좌표를 그대로 추출합니다.

    **원본 소재 전체를 함께 저장합니다** (요청서 3.5).
    """
    root = require_draft_root()
    name = str(payload.get("draft_name") or "").strip()
    if not name:
        raise fail("참조할 드래프트를 고르세요.")

    draft_dir = Path(root) / name
    if not (draft_dir / "draft_content.json").is_file():
        raise fail(f"'{name}' 드래프트에 draft_content.json이 없습니다. 다른 드래프트를 고르세요.")

    try:
        prof = cdraft.calibrate_from_draft(draft_dir)
    except cdraft.DraftError as exc:
        raise fail(str(exc))

    cdraft.save_style_profile(prof)

    notes = []
    if not (prof.get("font") or {}).get("path"):
        notes.append(
            "폰트 파일 경로를 찾지 못했습니다. 자막 글꼴이 시스템 기본으로 바뀝니다. "
            "아래 글꼴 목록에서 직접 고르세요. (요청서 3.6)"
        )
    stats = prof.get("stats") or {}
    if stats.get("count") and not stats.get("all_single_line"):
        notes.append(
            f"참조 드래프트에 줄바꿈이 들어간 자막이 있습니다 (최대 {stats.get('max_lines')}줄). "
            "이 도구는 항상 한 줄로 만듭니다."
        )

    return ok(profile=prof, notes=notes)


@router.post("/manual")
def manual(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """수동 입력 폴백. **추정값임을 명확히 표시**합니다 (요청서 6절)."""
    prof = cdraft.manual_style_profile(
        canvas_width=int(payload.get("canvas_width") or 1920),
        canvas_height=int(payload.get("canvas_height") or 1080),
        font_name=str(payload.get("font_name") or "Pretendard-Bold"),
        font_size=float(payload.get("font_size") or 5.0),
        text_color=str(payload.get("text_color") or "#000000"),
        background_color=str(payload.get("background_color") or "#ffffff"),
        bottom_y=float(payload.get("bottom_y") if payload.get("bottom_y") is not None else -0.7394),
        top_y=float(payload.get("top_y") if payload.get("top_y") is not None else 0.6338),
    )
    cdraft.save_style_profile(prof)

    notes = ["이 값은 추정값입니다. 캡컷에서 자막을 만든 드래프트로 캘리브레이션하는 편이 정확합니다."]
    if not prof.get("font_found"):
        notes.append(
            f"'{(prof.get('font') or {}).get('name')}' 글꼴 파일을 찾지 못했습니다. "
            "설치된 글꼴 목록에서 실제로 있는 이름을 고르세요."
        )
    return ok(profile=prof, notes=notes)


@router.post("/font")
def set_font(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """캘리브레이션 결과의 폰트만 바꿉니다 (경로를 못 찾았을 때)."""
    prof = cdraft.load_style_profile()
    if not prof:
        raise fail("먼저 캘리브레이션을 하세요.")

    path = str(payload.get("font_path") or "").strip()
    name = str(payload.get("font_name") or "").strip()
    if not path and name:
        path = cdraft.find_font_file(name) or ""
    if not path or not Path(path).is_file():
        raise fail(f"글꼴 파일을 찾을 수 없습니다: {path or name}")

    prof["font"] = {"name": name or Path(path).stem, "path": path}
    cdraft.apply_font_path(prof["material_template"], path, prof["font"]["name"])
    cdraft.save_style_profile(prof)
    return ok(profile=prof)


# ── 트랜지션 (3.7) ────────────────────────────────────────────────────────
@router.get("/transitions")
def transitions() -> Dict[str, Any]:
    """캘리브레이션으로 가져온 것 + 전체 목록(드롭다운용)."""
    prof = cdraft.load_style_profile() or {}
    return {
        "captured": prof.get("transitions") or [],
        "all": cdraft.list_all_transitions(),
        "note": (
            "트랜지션 이름은 대부분 중국어라 한국어 UI 이름과 매칭되지 않습니다. "
            "캡컷에서 원하는 트랜지션을 한 번 적용한 드래프트로 캘리브레이션하면 "
            "effect_id로 정확히 찾아옵니다."
        ),
    }


@router.post("/transitions/capture")
def capture_transitions(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """자막과 별개로, 트랜지션만 다른 드래프트에서 가져오고 싶을 때."""
    root = require_draft_root()
    name = str(payload.get("draft_name") or "").strip()
    if not name:
        raise fail("드래프트를 고르세요.")

    draft_dir = Path(root) / name
    try:
        data = cdraft.read_draft_content(draft_dir)
    except cdraft.DraftError as exc:
        raise fail(str(exc))

    found = cdraft.extract_transitions(data)
    prof = cdraft.load_style_profile()
    if prof is not None:
        prof["transitions"] = found
        cdraft.save_style_profile(prof)

    message = "" if found else (
        f"'{name}' 드래프트에 트랜지션이 없습니다. "
        "캡컷에서 클립 사이에 트랜지션을 한 번 적용한 뒤 다시 시도하세요."
    )
    return ok(transitions=found, message=message)
