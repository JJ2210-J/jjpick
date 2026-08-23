"""라우터 공통 헬퍼.

요청서 5절: 모든 실패는 조용히 넘어가지 말고
**무엇이 / 왜 실패했고 / 무엇을 하면 되는지** 표시합니다.
그래서 예외 메시지를 삼키지 않고 그대로 올려보냅니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException

from ..config import find_capcut_draft_root
from ..services import session as session_svc
from ..services.session import Session, SessionNotFound, StepLocked


def get_session(session_id: str) -> Session:
    try:
        return session_svc.load(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def require_step(session: Session, step: str) -> None:
    try:
        session.require(step)
    except StepLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def require_draft_root() -> Path:
    root = find_capcut_draft_root()
    if root is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "캡컷 드래프트 폴더를 찾지 못했습니다.\n"
                "캡컷을 한 번 실행해 프로젝트를 하나 만들었는지 확인하고, "
                "그래도 못 찾으면 설정 화면에서 폴더 경로를 직접 지정하세요.\n"
                "(보통 %LOCALAPPDATA%\\CapCut\\User Data\\Projects\\com.lveditor.draft 입니다)"
            ),
        )
    return root


def fail(message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def ok(**payload: Any) -> Dict[str, Any]:
    return {"ok": True, **payload}
