"""세션 상태, 단계 잠금.

요청서 4절: 영상 한 편 작업 = 한 세션. 상태를 JSON으로 저장해
브라우저를 껐다 켜도 이어서 작업할 수 있게 합니다.

요청서 5절: 각 단계는 선행 단계가 미완이면 **비활성 + 이유 표시**.
그 "이유"를 여기서 만들어 UI로 넘깁니다.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import SESSION_DIR, STYLE_PROFILE_PATH, WORK_DIR, write_json_atomic
from .logging_util import get_logger

log = get_logger()

# 단계 정의 (요청서 5절: 좌측 0차~5차 네비게이션)
STEPS: List[Dict[str, Any]] = [
    {"key": "prepare",     "order": 0, "icon": "📁", "title": "0차 프로젝트 준비",
     "requires": []},
    {"key": "calibration", "order": 1, "icon": "🎯", "title": "자막 스타일 캘리브레이션",
     "requires": ["prepare"]},
    {"key": "cut",         "order": 2, "icon": "✂️", "title": "1차 컷 편집",
     "requires": ["prepare"]},
    {"key": "subtitle",    "order": 3, "icon": "💬", "title": "2차 자막 생성",
     "requires": ["prepare", "calibration", "cut"]},
    {"key": "transition",  "order": 4, "icon": "🔀", "title": "3차 트랜지션·효과음",
     "requires": ["cut"]},
    {"key": "export",      "order": 5, "icon": "📤", "title": "4차 내보내기·문구",
     "requires": ["cut"]},
    {"key": "vertical",    "order": 6, "icon": "📱", "title": "5차 세로용 영상",
     "requires": ["subtitle"]},
]

STEP_BY_KEY = {s["key"]: s for s in STEPS}

# 선행 단계가 미완일 때 보여줄 이유 (요청서 5절: 비활성 + 이유 표시)
_REQUIRE_REASON = {
    "prepare": "0차에서 원본 영상을 먼저 선택하세요.",
    "calibration": "자막 스타일 캘리브레이션을 먼저 하세요. (2차 자막이 캡컷 화면에 보이려면 필요합니다)",
    "cut": "1차 컷 편집을 먼저 끝내고 드래프트를 만드세요.",
    "subtitle": "2차 자막 생성을 먼저 끝내세요.",
}


@dataclass
class Session:
    id: str
    name: str
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)

    # ── 단계 완료 표시 ─────────────────────────────────────────────────────
    @property
    def completed(self) -> Dict[str, bool]:
        return self.data.setdefault("completed", {})

    def mark(self, step: str, done: bool = True) -> None:
        self.completed[step] = done
        self.touch()

    def is_done(self, step: str) -> bool:
        """단계 완료 여부.

        캘리브레이션만 예외입니다. 자막 스타일 프로필은 세션이 아니라
        `data/style_profile.json` 한 곳에 전역으로 저장되므로(요청서 6절),
        완료 여부도 세션 플래그가 아니라 **그 파일이 있는지**에서 파생시킵니다.
        이렇게 하지 않으면 캘리브레이션을 해도 세션에는 기록되지 않아
        2차 자막이 영원히 잠깁니다.
        """
        if step == "calibration":
            try:
                return STYLE_PROFILE_PATH.is_file()
            except OSError:
                return False
        return bool(self.completed.get(step))

    def touch(self) -> None:
        self.updated = time.time()

    # ── 단계 잠금 판정 ─────────────────────────────────────────────────────
    def step_states(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for step in STEPS:
            missing = [r for r in step["requires"] if not self.is_done(r)]
            reasons = [_REQUIRE_REASON.get(m, f"'{m}' 단계를 먼저 끝내세요.") for m in missing]
            out.append({
                "key": step["key"],
                "order": step["order"],
                "icon": step["icon"],
                "title": step["title"],
                "done": self.is_done(step["key"]),
                "locked": bool(missing),
                "lock_reason": reasons[0] if reasons else "",
                "missing": missing,
            })
        return out

    def require(self, step: str) -> None:
        """API 진입점에서 선행 단계를 강제합니다. 미완이면 이유와 함께 예외."""
        meta = STEP_BY_KEY.get(step)
        if not meta:
            raise ValueError(f"알 수 없는 단계: {step}")
        missing = [r for r in meta["requires"] if not self.is_done(r)]
        if missing:
            reason = _REQUIRE_REASON.get(missing[0], f"'{missing[0]}' 단계를 먼저 끝내세요.")
            raise StepLocked(reason)

    # ── 직렬화 ────────────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "created": self.created,
                "updated": self.updated, "data": self.data}

    def summary(self) -> Dict[str, Any]:
        sources = self.data.get("sources") or {}
        return {
            "id": self.id,
            "name": self.name,
            "created": self.created,
            "updated": self.updated,
            "clip_count": len(sources.get("clips") or []),
            "total_duration": sources.get("total_duration", 0.0),
            "draft_name": self.data.get("draft_name", ""),
            "steps": self.step_states(),
            "progress_label": self._progress_label(),
        }

    def _progress_label(self) -> str:
        done = [s for s in STEPS if self.is_done(s["key"])]
        if not done:
            return "시작 전"
        return max(done, key=lambda s: s["order"])["title"] + " 완료"

    @property
    def work_dir(self) -> Path:
        d = WORK_DIR / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d


class StepLocked(RuntimeError):
    """선행 단계 미완. 메시지가 그대로 사용자에게 보입니다."""


class SessionNotFound(KeyError):
    pass


# ══════════════════════════════════════════════════════════════════════════
def _path(session_id: str) -> Path:
    return SESSION_DIR / f"{session_id}.json"


def create(name: str = "") -> Session:
    session_id = uuid.uuid4().hex[:12]
    name = (name or "").strip() or f"새 작업 {time.strftime('%m/%d %H:%M')}"
    session = Session(id=session_id, name=name)
    save(session)
    log.info("세션 생성: %s (%s)", name, session_id)
    return session


def load(session_id: str) -> Session:
    path = _path(session_id)
    if not path.is_file():
        raise SessionNotFound(f"세션을 찾을 수 없습니다: {session_id}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise SessionNotFound(f"세션 파일이 손상됐습니다: {session_id} ({exc})") from exc
    return Session(id=raw["id"], name=raw.get("name", ""),
                   created=raw.get("created", time.time()),
                   updated=raw.get("updated", time.time()),
                   data=raw.get("data") or {})


def save(session: Session) -> None:
    session.touch()
    write_json_atomic(_path(session.id), session.to_dict())


def delete(session_id: str) -> bool:
    path = _path(session_id)
    removed = False
    if path.is_file():
        path.unlink()
        removed = True
    work = WORK_DIR / session_id
    if work.is_dir():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    log.info("세션 삭제: %s", session_id)
    return removed


def list_all() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in SESSION_DIR.glob("*.json"):
        try:
            out.append(load(path.stem).summary())
        except SessionNotFound:
            continue
    out.sort(key=lambda s: s["updated"], reverse=True)
    return out


def rename(session_id: str, name: str) -> Session:
    session = load(session_id)
    session.name = (name or "").strip() or session.name
    save(session)
    return session
