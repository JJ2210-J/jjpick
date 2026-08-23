"""3차 트랜지션·효과음 / 4차 내보내기·문구 / 5차 세로 영상."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body
from fastapi.responses import PlainTextResponse

from ..config import ASSET_BG_DIR, load_settings, save_settings
from ..services import builder, cut_edit, prompt_builder, script_align
from ..services import capcut_draft as cdraft
from ..services import session as session_svc
from ..services.jobs import Job, manager
from ..services.script_align import Subtitle
from ..services.sources import SourceTimeline
from ._common import fail, get_session, ok, require_draft_root, require_step

router = APIRouter(prefix="/api/publishing", tags=["publishing"])


def _draft_dir(session: session_svc.Session) -> Path:
    root = require_draft_root()
    name = session.data.get("draft_name")
    if not name:
        raise fail("드래프트가 없습니다. 1차에서 드래프트를 먼저 만드세요.")
    return Path(root) / name


def _subtitles(session: session_svc.Session) -> List[Subtitle]:
    return [Subtitle.from_dict(d) for d in (session.data.get("subtitles") or [])]


# ══════════════════════════════════════════════════════════════════════════
# 3차 트랜지션 및 효과음
# ══════════════════════════════════════════════════════════════════════════
@router.get("/{session_id}/image-clips")
def image_clips(session_id: str) -> Dict[str, Any]:
    """드래프트의 이미지 클립 목록.

    `materials.videos[].type == "photo"`로 1차 판정하고 확장자로 교차 검증합니다.
    """
    session = get_session(session_id)
    require_step(session, "transition")
    try:
        clips = builder.list_image_clips(_draft_dir(session))
    except cdraft.DraftError as exc:
        raise fail(str(exc))
    return {
        "clips": clips,
        "message": "" if clips else (
            "드래프트에 이미지 클립이 없습니다. "
            "캡컷에서 이미지를 타임라인에 넣은 뒤 다시 확인하세요. "
            "(영상 클립 사이 트랜지션은 캡컷에서 직접 넣는 편이 빠릅니다)"
        ),
    }


@router.post("/{session_id}/transitions")
def apply_transitions(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """선택한 위치에 트랜지션을 적용합니다.

    ⚠ 트랜지션은 **앞쪽 세그먼트에 붙습니다** (pyCapCut 규칙).
    """
    session = get_session(session_id)
    require_step(session, "transition")
    root = require_draft_root()
    draft_dir = _draft_dir(session)

    selections = payload.get("selections") or []
    if not selections:
        raise fail("적용할 트랜지션을 하나 이상 고르세요.")

    settings = load_settings()
    duration = float(payload.get("duration_sec", settings["transition_duration_sec"]))

    cdraft.backup_draft(draft_dir, tag="before_transitions")
    try:
        result = builder.apply_transitions(
            draft_root=root, draft_dir=draft_dir,
            selections=selections, duration_sec=duration,
        )
    except cdraft.DraftError as exc:
        raise fail(str(exc))

    session.mark("transition", True)
    session_svc.save(session)
    return ok(**result, note="캡컷에서 열어 육안으로 확인하세요. "
                             "pyCapCut은 베타라 트랜지션이 의도대로 적용되지 않는 사례가 보고돼 있습니다.")


@router.post("/{session_id}/sfx")
def apply_sfx(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """`assets/sfx/`의 wav를 오디오 트랙으로 넣습니다."""
    session = get_session(session_id)
    require_step(session, "transition")
    root = require_draft_root()
    draft_dir = _draft_dir(session)

    placements = payload.get("placements") or []
    if not placements:
        raise fail("넣을 효과음을 하나 이상 고르세요.")

    settings = load_settings()
    cdraft.backup_draft(draft_dir, tag="before_sfx")
    try:
        result = builder.apply_sfx(
            draft_root=root, draft_dir=draft_dir, placements=placements,
            volume=float(payload.get("volume", settings["sfx_volume"])),
        )
    except cdraft.DraftError as exc:
        raise fail(str(exc))
    return ok(**result)


# ══════════════════════════════════════════════════════════════════════════
# 4차 내보내기 및 마케팅 문구
# ══════════════════════════════════════════════════════════════════════════
@router.get("/{session_id}/export-info")
def export_info(session_id: str) -> Dict[str, Any]:
    """기본은 캡컷에서 직접 내보내기 유도입니다."""
    session = get_session(session_id)
    require_step(session, "export")
    settings = load_settings()
    name = session.data.get("draft_name") or ""
    return {
        "draft_name": name,
        "draft_dir": session.data.get("draft_dir") or "",
        "guidance": (
            f"드래프트 '{name}' 저장이 끝났습니다.\n"
            "캡컷을 열면 프로젝트 목록에 보입니다. 거기서 확인하고 직접 내보내세요.\n"
            "※ 이미 캡컷에 열어 둔 프로젝트라면 완전히 종료했다가 다시 열어야 반영됩니다."
        ),
        "auto_export_enabled": bool(settings.get("auto_export_enabled")),
        "auto_export_warning": (
            "자동 내보내기는 pycapcut이 캡컷 UI를 직접 조작하는 방식입니다.\n"
            "· 이 기능만 캡컷이 실행 중이어야 합니다 (다른 모든 단계와 반대입니다)\n"
            "· 한국어 UI에서 동작할지 검증되지 않았습니다\n"
            "· Windows 전용이고 캡컷 버전·UI 언어에 민감합니다\n"
            "· uiautomation 패키지가 따로 필요합니다 (pip install uiautomation)\n"
            "부가 기능으로만 쓰세요. 확인란을 체크해야 실행됩니다."
        ),
        "questions": prompt_builder.QUESTIONS,
        "channel_defaults": prompt_builder.CHANNEL_DEFAULTS,
        "answers": session.data.get("prompt_answers") or {},
        "channel": session.data.get("channel") or {},
    }


@router.post("/{session_id}/auto-export")
def auto_export(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """자동 내보내기. **기본 OFF, 확인 체크박스 필수** (요청서 6절 4차)."""
    session = get_session(session_id)
    require_step(session, "export")

    if not payload.get("confirmed"):
        raise fail(
            "확인란을 체크해야 실행됩니다. 이 기능은 미검증이며 캡컷 UI를 직접 조작합니다."
        )

    try:
        import uiautomation  # noqa: F401
    except ImportError:
        raise fail(
            "uiautomation 패키지가 없어 자동 내보내기를 쓸 수 없습니다.\n"
            "pip install uiautomation 을 실행한 뒤 다시 시도하세요. (Windows 전용)"
        )

    # 이 기능만 캡컷이 켜져 있어야 합니다.
    if not cdraft.running_capcut_processes():
        raise fail(
            "자동 내보내기는 캡컷이 실행 중이어야 합니다. "
            "(다른 모든 단계와 반대입니다) 캡컷을 실행한 뒤 다시 시도하세요."
        )

    output = str(payload.get("output_path") or "").strip()
    if not output:
        raise fail("저장할 파일 경로를 지정하세요.")

    draft_name = session.data.get("draft_name")

    def work(job: Job) -> Dict[str, Any]:
        job.set_progress(0.1, "캡컷 UI를 조작하는 중", draft_name)
        from pycapcut.jianying_controller import ControlFinder  # noqa: F401
        from pycapcut import jianying_controller as jc

        controller = jc.JianyingController()
        controller.export_draft(draft_name, output)
        job.set_progress(1.0, "완료", output)
        return {"output": output}

    job = manager.submit(session_id, "auto_export", work, message="자동 내보내기 (미검증 기능)")
    return ok(job=job.snapshot(), warning="미검증 기능입니다. 실패하면 캡컷에서 직접 내보내세요.")


@router.post("/{session_id}/prompt")
def build_prompt(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """유튜브 제목·설명글·해시태그 프롬프트를 조립합니다.

    외부 LLM API는 쓰지 않습니다. 완성된 프롬프트 텍스트만 만들어 줍니다.
    """
    session = get_session(session_id)
    require_step(session, "export")

    subs = _subtitles(session)
    if not subs:
        raise fail("자막이 없습니다. 2차 자막 생성을 먼저 끝내세요. "
                   "(프롬프트에 자막 전문이 들어갑니다)")

    answers = {k: str(v) for k, v in (payload.get("answers") or {}).items()}
    channel = {k: str(v) for k, v in (payload.get("channel") or {}).items()}

    missing = [q["label"] for q in prompt_builder.QUESTIONS if not answers.get(q["key"], "").strip()]

    chapters = prompt_builder.suggest_chapters(subs, int(payload.get("chapter_count") or 6))
    text = prompt_builder.build_youtube_prompt(
        subtitle_text=script_align.full_text(subs),
        answers=answers, channel=channel, chapters=chapters,
    )

    session.data["prompt_answers"] = answers
    session.data["channel"] = channel
    session.data["youtube_prompt"] = text
    session.mark("export", True)
    session_svc.save(session)

    return ok(prompt=text, chapters=chapters,
              warnings=[f"미입력 항목이 있습니다: {', '.join(missing)}"] if missing else [])


@router.get("/{session_id}/prompt.txt", response_class=PlainTextResponse)
def prompt_file(session_id: str, kind: str = "youtube") -> str:
    session = get_session(session_id)
    key = "youtube_prompt" if kind == "youtube" else "vertical_prompt"
    text = session.data.get(key)
    if not text:
        raise fail("아직 만들어진 프롬프트가 없습니다.")
    return text


# ══════════════════════════════════════════════════════════════════════════
# 5차 세로용 영상
# ══════════════════════════════════════════════════════════════════════════
@router.get("/{session_id}/vertical/timeline")
def vertical_timeline(session_id: str) -> Dict[str, Any]:
    """자막 타임라인을 보여 주고 시작/끝을 고르게 합니다."""
    session = get_session(session_id)
    require_step(session, "vertical")
    subs = _subtitles(session)
    settings = load_settings()
    bg = [{"name": p.name, "path": str(p)} for p in sorted(ASSET_BG_DIR.iterdir())
          if p.is_file()] if ASSET_BG_DIR.is_dir() else []
    return {
        "subtitles": [s.to_dict() for s in subs],
        "edited_duration": max((s.end for s in subs), default=0.0),
        "backgrounds": bg,
        "bg_dir": str(ASSET_BG_DIR),
        "vertical": {
            "width": settings["vertical_width"], "height": settings["vertical_height"],
            "scale": settings["vertical_scale"], "transform_y": settings["vertical_transform_y"],
        },
        "note": (
            "여기 보이는 시각은 컷 편집 후(편집본) 기준입니다. "
            "실제로 잘라 쓸 소재는 원본이라, 그 사이 잘려나간 컷이 있으면 "
            "원본에서는 여러 조각이 됩니다. 도구가 자동으로 역변환해 쪼갭니다."
        ),
    }


@router.post("/{session_id}/vertical/preview-span")
def preview_span(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """고른 편집본 구간을 원본 구간으로 역변환해 미리 보여 줍니다 (요청서 5차 함정)."""
    session = get_session(session_id)
    require_step(session, "vertical")

    sources = session.data.get("sources")
    if not sources:
        raise fail("0차 원본 정보가 없습니다.")
    timeline = SourceTimeline.from_dict(sources)
    cut_map = cut_edit.build_cut_map(
        [cut_edit.CutCandidate.from_dict(d) for d in (session.data.get("candidates") or [])],
        timeline.total_duration,
    )

    start = float(payload.get("start", 0.0))
    end = float(payload.get("end", 0.0))
    if end <= start:
        raise fail("끝 지점이 시작 지점보다 뒤여야 합니다.")

    spans = cut_map.edited_span_to_original(start, end)
    pieces: List[Dict[str, Any]] = []
    for s, e in spans:
        pieces.extend(timeline.split_span(s, e))

    length = end - start
    hint = ""
    if length < 20 or length > 30:
        hint = (f"고른 길이가 {length:.1f}초입니다. 쇼츠/릴스는 20~30초가 무난합니다. "
                "(강제하지는 않습니다)")

    return ok(
        edited_span=[start, end], length=length,
        original_spans=[[round(s, 3), round(e, 3)] for s, e in spans],
        pieces=pieces,
        piece_count=len(pieces),
        hint=hint,
        split_note=(f"편집본 한 구간이 원본에서는 {len(spans)}조각입니다."
                    if len(spans) > 1 else "원본에서도 한 조각입니다."),
    )


@router.post("/{session_id}/vertical/build")
def build_vertical(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """1080x1920 세로 드래프트를 만듭니다."""
    session = get_session(session_id)
    require_step(session, "vertical")
    root = require_draft_root()

    sources = session.data.get("sources")
    if not sources:
        raise fail("0차 원본 정보가 없습니다.")
    timeline = SourceTimeline.from_dict(sources)
    cut_map = cut_edit.build_cut_map(
        [cut_edit.CutCandidate.from_dict(d) for d in (session.data.get("candidates") or [])],
        timeline.total_duration,
    )

    start = float(payload.get("start", 0.0))
    end = float(payload.get("end", 0.0))
    if end <= start:
        raise fail("끝 지점이 시작 지점보다 뒤여야 합니다.")

    # 편집본 -> 원본 역변환 (요청서 5차 함정)
    original_spans = cut_map.edited_span_to_original(start, end)
    if not original_spans:
        raise fail("고른 구간이 전부 잘려나간 부분입니다. 다른 구간을 고르세요.")

    subs = [s for s in _subtitles(session) if s.end > start and s.start < end]
    profile = cdraft.load_style_profile()

    base = session.data.get("draft_name") or session.name
    draft_name = str(payload.get("draft_name") or "").strip() or f"{base}_세로"
    draft_name = "".join(ch for ch in draft_name if ch not in '\\/:*?"<>|').strip() or "세로영상"

    background = str(payload.get("background") or "")

    def work(job: Job) -> Dict[str, Any]:
        job.set_progress(0.1, "세로 드래프트를 만드는 중", draft_name)
        result = builder.build_vertical_draft(
            draft_root=root, draft_name=draft_name, timeline=timeline,
            original_spans=original_spans, subtitles=subs, style_profile=profile or {},
            background_image=background, edited_offset=start,
        )
        fresh = session_svc.load(session.id)
        fresh.data["vertical_draft"] = result["draft_name"]
        fresh.mark("vertical", True)
        session_svc.save(fresh)
        job.set_progress(1.0, "완료", "")
        return result

    job = manager.submit(session_id, "build_vertical", work, message="세로 영상을 만듭니다")
    return ok(job=job.snapshot())


@router.post("/{session_id}/vertical/prompt")
def vertical_prompt(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """상단/하단 문구 프롬프트. 릴스는 프로필 링크 CTA가 필수입니다."""
    session = get_session(session_id)
    require_step(session, "vertical")

    start = float(payload.get("start", 0.0))
    end = float(payload.get("end", 0.0))
    clip_subs = [s for s in _subtitles(session) if s.end > start and s.start < end]
    if not clip_subs:
        raise fail("고른 구간에 자막이 없습니다. 구간을 다시 고르거나 2차 자막을 확인하세요.")

    platform = str(payload.get("platform") or "shorts")
    text = prompt_builder.build_vertical_prompt(
        clip_text=script_align.full_text(clip_subs),
        platform=platform,
        answers={k: str(v) for k, v in (payload.get("answers")
                                        or session.data.get("prompt_answers") or {}).items()},
        channel={k: str(v) for k, v in (payload.get("channel")
                                        or session.data.get("channel") or {}).items()},
        profile_link=str(payload.get("profile_link") or ""),
    )
    session.data["vertical_prompt"] = text
    session_svc.save(session)

    warnings: List[str] = []
    if platform == "reels" and not payload.get("profile_link"):
        warnings.append("릴스는 프로필 링크 CTA가 필수인데 링크가 비어 있습니다. "
                        "프롬프트에 자리만 잡아 두었습니다.")
    return ok(prompt=text, warnings=warnings)


@router.post("/{session_id}/settings")
def update_vertical_settings(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    get_session(session_id)
    allowed = {"vertical_scale", "vertical_transform_y", "vertical_width", "vertical_height",
               "transition_duration_sec", "sfx_volume", "auto_export_enabled"}
    patch = {k: v for k, v in payload.items() if k in allowed}
    return ok(settings=save_settings(patch))
