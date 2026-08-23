"""FastAPI 앱.

요청서 5절: 모든 실패는 조용히 넘어가지 말고
**무엇이 / 왜 실패했고 / 무엇을 하면 되는지** 표시합니다.
그래서 예외를 전부 잡아 사람이 읽을 수 있는 한국어 메시지로 바꿔 내려보냅니다.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import STATIC_DIR
from .routers import calibration, editing, files, publishing, sessions, setup
from .services.audio_analysis import MediaError
from .services.capcut_draft import DraftError
from .services.jobs import JobCancelled
from .services.logging_util import get_logger, setup_logging
from .services.session import SessionNotFound, StepLocked
from .services.transcribe import TranscribeError

log = setup_logging()

app = FastAPI(title="캡컷 자동 편집기", docs_url=None, redoc_url=None)

app.include_router(setup.router)
app.include_router(files.router)
app.include_router(sessions.router)
app.include_router(calibration.router)
app.include_router(editing.router)
app.include_router(publishing.router)


def _error(status: int, message: str, kind: str = "error") -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "kind": kind, "detail": message})


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return _error(exc.status_code, str(exc.detail))


@app.exception_handler(StepLocked)
async def step_locked(_request: Request, exc: StepLocked) -> JSONResponse:
    return _error(409, str(exc), kind="locked")


@app.exception_handler(SessionNotFound)
async def session_missing(_request: Request, exc: SessionNotFound) -> JSONResponse:
    return _error(404, str(exc), kind="not_found")


@app.exception_handler(DraftError)
async def draft_error(_request: Request, exc: DraftError) -> JSONResponse:
    return _error(400, str(exc), kind="draft")


@app.exception_handler(MediaError)
async def media_error(_request: Request, exc: MediaError) -> JSONResponse:
    return _error(400, str(exc), kind="media")


@app.exception_handler(TranscribeError)
async def transcribe_error(_request: Request, exc: TranscribeError) -> JSONResponse:
    return _error(400, str(exc), kind="stt")


@app.exception_handler(JobCancelled)
async def job_cancelled(_request: Request, _exc: JobCancelled) -> JSONResponse:
    return _error(499, "작업이 취소되었습니다.", kind="cancelled")


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err.get('loc', [])[1:])}: {err.get('msg', '')}"
        for err in exc.errors()
    )
    return _error(422, f"요청 값이 올바르지 않습니다. {problems}", kind="validation")


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
    log.exception("처리되지 않은 예외")
    return _error(
        500,
        f"예상치 못한 오류가 발생했습니다.\n{type(exc).__name__}: {exc}\n"
        "logs/ 폴더의 오늘 날짜 로그에 자세한 내용이 남아 있습니다.",
        kind="unhandled",
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> Any:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
async def health() -> Any:
    return {"ok": True}
