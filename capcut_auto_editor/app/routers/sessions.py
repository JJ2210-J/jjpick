"""세션 CRUD + 작업(Job) 상태·취소·SSE."""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Any, Dict

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from ..services import session as session_svc
from ..services.jobs import manager
from ._common import fail, get_session, ok

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
def list_sessions() -> Dict[str, Any]:
    """세션 목록 — 진행 단계와 열기/삭제용 정보를 함께 줍니다 (요청서 5절)."""
    return {"sessions": session_svc.list_all(), "steps": session_svc.STEPS}


@router.post("")
def create_session(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    session = session_svc.create(str(payload.get("name") or ""))
    return ok(session=session.summary())


@router.get("/{session_id}")
def get_one(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    return {"session": session.summary(), "data": session.data}


@router.post("/{session_id}/rename")
def rename(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    get_session(session_id)
    session = session_svc.rename(session_id, str(payload.get("name") or ""))
    return ok(session=session.summary())


@router.delete("/{session_id}")
def delete(session_id: str) -> Dict[str, Any]:
    get_session(session_id)
    return ok(removed=session_svc.delete(session_id))


# ── 작업 ──────────────────────────────────────────────────────────────────
@router.get("/{session_id}/jobs")
def list_jobs(session_id: str) -> Dict[str, Any]:
    get_session(session_id)
    return {"jobs": manager.list_for_session(session_id)}


@router.get("/{session_id}/jobs/{job_id}")
def job_status(session_id: str, job_id: str) -> Dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise fail(f"작업을 찾을 수 없습니다: {job_id}", 404)
    return job.snapshot()


@router.post("/{session_id}/jobs/{job_id}/cancel")
def cancel_job(session_id: str, job_id: str) -> Dict[str, Any]:
    """취소가 실제로 동작합니다 — 하위 프로세스까지 종료하고 부분 파일을 정리합니다."""
    job = manager.get(job_id)
    if job is None:
        raise fail(f"작업을 찾을 수 없습니다: {job_id}", 404)
    if not manager.cancel(job_id):
        return ok(cancelled=False, message="이미 끝난 작업입니다.")
    return ok(cancelled=True, message="취소 요청을 보냈습니다. 정리하는 중입니다.")


@router.get("/{session_id}/jobs/{job_id}/stream")
async def job_stream(session_id: str, job_id: str, request: Request) -> StreamingResponse:
    """SSE로 진행률과 현재 항목명을 실시간 전달합니다 (요청서 5절)."""
    job = manager.get(job_id)
    if job is None:
        raise fail(f"작업을 찾을 수 없습니다: {job_id}", 404)

    async def events() -> Any:
        q = job.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snapshot = await asyncio.get_event_loop().run_in_executor(
                        None, q.get, True, 15.0
                    )
                except queue.Empty:
                    yield ": keep-alive\n\n"      # 프록시가 끊지 않도록
                    continue
                yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                if snapshot.get("status") in ("done", "error", "cancelled"):
                    break
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )
