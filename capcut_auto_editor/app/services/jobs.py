"""백그라운드 작업 + SSE 진행률.

요청서 7절 안전장치를 여기서 지킵니다.
  · 중복 작업 방지 — 같은 (세션, 종류) 작업은 하나만. 두 번째 요청은 진행 중인
    작업에 다시 연결합니다. (버튼이 멈춘 것처럼 보이면 사용자는 두 번 누릅니다)
  · 취소가 실제로 동작 — 하위 프로세스까지 종료하고 부분 파일을 정리합니다.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .logging_util import get_logger

log = get_logger()


class JobCancelled(Exception):
    """취소 요청으로 작업이 중단됨."""


@dataclass
class Job:
    job_id: str
    session_id: str
    kind: str
    status: str = "running"            # running | done | error | cancelled
    progress: float = 0.0              # 0.0 ~ 1.0
    message: str = ""
    detail: str = ""                   # 현재 처리 중인 항목명
    result: Any = None
    error: str = ""
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _listeners: List["queue.Queue[Dict[str, Any]]"] = field(default_factory=list, repr=False)
    _procs: List[Any] = field(default_factory=list, repr=False)
    _temp_files: List[Path] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── 작업 함수 쪽에서 쓰는 API ──────────────────────────────────────────
    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def set_progress(self, value: float, message: str = "", detail: str = "") -> None:
        self.progress = max(0.0, min(1.0, float(value)))
        if message:
            self.message = message
        self.detail = detail
        self._emit()

    def register_process(self, proc: Any) -> None:
        """취소 시 함께 죽일 하위 프로세스를 등록합니다."""
        with self._lock:
            self._procs.append(proc)

    def register_temp(self, path: Path) -> None:
        """취소·실패 시 지울 부분 파일을 등록합니다."""
        with self._lock:
            self._temp_files.append(Path(path))

    # ── 내부 ──────────────────────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "detail": self.detail,
            "error": self.error,
            "result": self.result if self.status == "done" else None,
        }

    def _emit(self) -> None:
        snap = self.snapshot()
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(snap)
            except queue.Full:
                pass

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=200)
        with self._lock:
            self._listeners.append(q)
        q.put_nowait(self.snapshot())
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            _kill_process_tree(p)


def _kill_process_tree(proc: Any) -> None:
    """하위 프로세스와 그 자식까지 확실히 종료합니다."""
    try:
        if proc.poll() is not None:
            return
    except Exception:  # noqa: BLE001
        return
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:  # noqa: BLE001
                pass
        parent.kill()
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._active: Dict[str, str] = {}   # "{session_id}:{kind}" -> job_id
        self._lock = threading.Lock()

    def _key(self, session_id: str, kind: str) -> str:
        return f"{session_id}:{kind}"

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def active_job(self, session_id: str, kind: str) -> Optional[Job]:
        with self._lock:
            job_id = self._active.get(self._key(session_id, kind))
        if not job_id:
            return None
        job = self._jobs.get(job_id)
        if job and job.status == "running":
            return job
        return None

    def submit(
        self,
        session_id: str,
        kind: str,
        fn: Callable[[Job], Any],
        *,
        message: str = "",
    ) -> Job:
        """작업을 시작합니다.

        같은 (세션, 종류)의 작업이 이미 돌고 있으면 **새로 만들지 않고**
        진행 중인 작업을 그대로 돌려줍니다 (요청서 7절 중복 작업 방지).
        """
        existing = self.active_job(session_id, kind)
        if existing is not None:
            log.info("이미 진행 중인 작업에 다시 연결합니다: %s (%s)", kind, existing.job_id)
            return existing

        job = Job(job_id=uuid.uuid4().hex[:12], session_id=session_id, kind=kind,
                  message=message or "시작하는 중…")
        with self._lock:
            self._jobs[job.job_id] = job
            self._active[self._key(session_id, kind)] = job.job_id

        def runner() -> None:
            try:
                job.result = fn(job)
                job.status = "done"
                job.progress = 1.0
                job.message = "완료"
            except JobCancelled:
                job.status = "cancelled"
                job.message = "취소되었습니다"
                _cleanup_temp(job)
                log.info("작업 취소: %s (%s)", job.kind, job.job_id)
            except Exception as exc:  # noqa: BLE001 - 사용자에게 원문을 보여줍니다
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = "실패"
                _cleanup_temp(job)
                log.exception("작업 실패: %s (%s)", job.kind, job.job_id)
            finally:
                job.finished = time.time()
                job.detail = ""
                with self._lock:
                    key = self._key(job.session_id, job.kind)
                    if self._active.get(key) == job.job_id:
                        self._active.pop(key, None)
                job._emit()

        threading.Thread(target=runner, name=f"job-{kind}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != "running":
            return False
        job.cancel()
        return True

    def list_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        return [j.snapshot() for j in self._jobs.values() if j.session_id == session_id]


def _cleanup_temp(job: Job) -> None:
    for p in job._temp_files:
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


manager = JobManager()
