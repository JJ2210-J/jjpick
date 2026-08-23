"""0차 준비 / 1차 컷 편집 / 2차 자막 생성."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body, File, UploadFile
from fastapi.responses import PlainTextResponse

from ..config import (
    find_capcut_draft_root,
    load_filler_words,
    load_glossary,
    load_settings,
)
from ..services import audio_analysis, builder, cut_edit, line_break, script_align
from ..services import capcut_draft as cdraft
from ..services import session as session_svc
from ..services import transcribe
from ..services.audio_analysis import MediaError, probe
from ..services.cut_edit import CutCandidate, CutMap
from ..services.jobs import Job, manager
from ..services.logging_util import get_logger
from ..services.script_align import Subtitle
from ..services.sources import SourceTimeline, summarize
from ._common import fail, get_session, ok, require_draft_root, require_step

log = get_logger()
router = APIRouter(prefix="/api/editing", tags=["editing"])


def _timeline(session: session_svc.Session) -> SourceTimeline:
    sources = session.data.get("sources")
    if not sources or not sources.get("clips"):
        raise fail("0차에서 원본 영상을 먼저 선택하세요.")
    return SourceTimeline.from_dict(sources)


def _candidates(session: session_svc.Session) -> List[CutCandidate]:
    return [CutCandidate.from_dict(d) for d in (session.data.get("candidates") or [])]


def _store_candidates(session: session_svc.Session, cands: List[CutCandidate]) -> None:
    session.data["candidates"] = [c.to_dict() for c in cands]


def _cut_map(session: session_svc.Session) -> CutMap:
    timeline = _timeline(session)
    return cut_edit.build_cut_map(_candidates(session), timeline.total_duration)


def _subtitles(session: session_svc.Session) -> List[Subtitle]:
    return [Subtitle.from_dict(d) for d in (session.data.get("subtitles") or [])]


# ══════════════════════════════════════════════════════════════════════════
# 0차 프로젝트 준비
# ══════════════════════════════════════════════════════════════════════════
@router.post("/{session_id}/sources")
def set_sources(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """원본 영상 여러 개를 순서대로 등록합니다.

    해상도/fps가 섞이면 경고하되 막지 않습니다. 캔버스는 가장 큰 해상도 기준입니다.
    """
    session = get_session(session_id)
    paths = payload.get("paths") or []
    if not paths:
        raise fail("영상을 한 개 이상 선택하세요.")

    probes: List[Dict[str, Any]] = []
    problems: List[str] = []
    for p in paths:
        try:
            info = probe(Path(p))
        except MediaError as exc:
            problems.append(str(exc))
            continue
        if not info["has_video"]:
            problems.append(f"'{info['name']}'에 영상 트랙이 없습니다.")
            continue
        probes.append(info)

    if not probes:
        raise fail("읽을 수 있는 영상이 없습니다.\n" + "\n".join(problems))

    timeline = SourceTimeline.from_probes(probes)
    session.data["sources"] = timeline.to_dict()
    # 소스가 바뀌면 그 뒤 단계는 전부 무효입니다.
    for key in ("candidates", "stt", "subtitles", "cut_signature", "draft_name", "draft_dir"):
        session.data.pop(key, None)
    for step in ("cut", "subtitle", "transition", "export", "vertical"):
        session.completed[step] = False
    session.mark("prepare", True)
    session_svc.save(session)

    return ok(sources=summarize(timeline), problems=problems,
              steps=session.step_states())


@router.get("/{session_id}/sources")
def get_sources(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    sources = session.data.get("sources")
    if not sources:
        return {"sources": None}
    return {"sources": summarize(SourceTimeline.from_dict(sources))}


@router.post("/{session_id}/backup")
def backup(session_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """단계 실행 전 스냅샷. 되돌리기 버튼이 이걸 씁니다 (요청서 7절)."""
    session = get_session(session_id)
    root = require_draft_root()
    name = str(payload.get("draft_name") or session.data.get("draft_name") or "").strip()
    if not name:
        raise fail("백업할 드래프트가 없습니다. 1차에서 드래프트를 먼저 만드세요.")

    try:
        dest = cdraft.backup_draft(Path(root) / name, tag=str(payload.get("tag") or ""))
    except cdraft.DraftError as exc:
        raise fail(str(exc))

    session.data.setdefault("backups", []).append(str(dest))
    session_svc.save(session)
    return ok(backup=str(dest), backups=cdraft.list_backups(name))


@router.get("/{session_id}/backups")
def list_backups(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    name = session.data.get("draft_name") or None
    return {"backups": cdraft.list_backups(name)}


@router.post("/{session_id}/restore")
def restore(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """되돌리기. 복원 전 현재 상태도 백업해 둡니다."""
    session = get_session(session_id)
    root = require_draft_root()
    backup_path = str(payload.get("backup_path") or "").strip()
    name = str(payload.get("draft_name") or session.data.get("draft_name") or "").strip()
    if not backup_path or not name:
        raise fail("되돌릴 백업과 드래프트를 지정하세요.")
    try:
        cdraft.restore_backup(Path(backup_path), Path(root) / name)
    except cdraft.DraftError as exc:
        raise fail(str(exc))
    return ok(message=f"'{Path(backup_path).name}' 백업으로 되돌렸습니다.")


# ══════════════════════════════════════════════════════════════════════════
# 1차 컷 편집
# ══════════════════════════════════════════════════════════════════════════
@router.post("/{session_id}/analyze")
def analyze(session_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """오디오 추출 -> 무음 감지 -> 음성 인식 -> 컷 후보 생성.

    같은 세션의 같은 작업이 이미 돌고 있으면 그 작업에 다시 연결합니다.
    (버튼이 멈춘 것처럼 보이면 사용자는 두 번 누릅니다 — 요청서 7절)
    """
    session = get_session(session_id)
    require_step(session, "cut")
    timeline = _timeline(session)

    settings = load_settings()
    threshold = float(payload.get("silence_threshold_db", settings["silence_threshold_db"]))
    min_silence = float(payload.get("min_silence_sec", settings["min_silence_sec"]))
    tail_pad = float(payload.get("tail_pad_sec", settings["tail_pad_sec"]))
    head_pad = float(payload.get("head_pad_sec", settings["head_pad_sec"]))
    model = str(payload.get("whisper_model") or settings["whisper_model"])
    reuse_stt = bool(payload.get("reuse_stt", True))

    def work(job: Job) -> Dict[str, Any]:
        job.set_progress(0.02, "준비 중", "")
        clips = [Path(c.path) for c in timeline.clips]

        wav = session.work_dir / "audio_16k.wav"
        if not (reuse_stt and wav.is_file() and wav.stat().st_size > 0):
            audio_analysis.extract_audio(clips, wav, job=job,
                                         total_seconds=timeline.total_duration)
        else:
            job.set_progress(0.30, "이미 추출해 둔 오디오를 재사용합니다", wav.name)

        job.check_cancel()
        spans = audio_analysis.detect_silence(
            wav, threshold_db=threshold, min_silence_sec=min_silence,
            job=job, total_seconds=timeline.total_duration,
        )

        job.check_cancel()
        if not reuse_stt:
            transcribe.clear_cache(session.id)
        stt = transcribe.transcribe(
            wav, session.id, job=job, total_duration=timeline.total_duration,
            model_name=model,
        )

        job.set_progress(0.94, "컷 후보를 만드는 중", "")
        cands = cut_edit.build_candidates(
            spans, stt["words"], load_filler_words(),
            tail_pad=tail_pad, head_pad=head_pad,
            total_duration=timeline.total_duration,
        )

        fresh = session_svc.load(session.id)
        fresh.data["stt"] = {
            "model": stt["model"], "language": stt["language"],
            "segments": stt["segments"], "word_count": len(stt["words"]),
            "low_confidence": stt["low_confidence"],
        }
        fresh.data["silence_spans"] = [[round(a, 3), round(b, 3)] for a, b in spans]
        fresh.data["analyze_settings"] = {
            "silence_threshold_db": threshold, "min_silence_sec": min_silence,
            "tail_pad_sec": tail_pad, "head_pad_sec": head_pad, "whisper_model": model,
        }
        _store_candidates(fresh, cands)
        session_svc.save(fresh)

        cut_map = cut_edit.build_cut_map(cands, timeline.total_duration)
        job.set_progress(1.0, "완료", "")
        return {
            "candidates": [c.to_dict() for c in cands],
            "summary": cut_edit.cut_summary(cands, cut_map),
            "cut_map": cut_map.to_dict(),
            "stt": {"segment_count": len(stt["segments"]), "word_count": len(stt["words"]),
                    "resumed_chunks": stt["resumed_chunks"],
                    "low_confidence_count": len(stt["low_confidence"])},
        }

    job = manager.submit(session_id, "analyze", work, message="분석을 시작합니다")
    return ok(job=job.snapshot())


@router.get("/{session_id}/candidates")
def get_candidates(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    cands = _candidates(session)
    if not cands:
        return {"candidates": [], "summary": None, "cut_map": None}
    cut_map = _cut_map(session)
    return {
        "candidates": [c.to_dict() for c in cands],
        "summary": cut_edit.cut_summary(cands, cut_map),
        "cut_map": cut_map.to_dict(),
        "settings": session.data.get("analyze_settings") or {},
        "low_confidence": (session.data.get("stt") or {}).get("low_confidence") or [],
    }


@router.post("/{session_id}/candidates/selection")
def update_selection(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """체크박스 변경. 개별 / 유형별 일괄 / 긴 무음 살리기를 모두 처리합니다."""
    session = get_session(session_id)
    cands = _candidates(session)
    if not cands:
        raise fail("먼저 분석을 실행하세요.")

    action = str(payload.get("action") or "set")
    changed = 0

    if action == "set":
        wanted = {str(k): bool(v) for k, v in (payload.get("selection") or {}).items()}
        for c in cands:
            if c.id in wanted and c.selected != wanted[c.id]:
                c.selected = wanted[c.id]
                changed += 1
    elif action == "by_type":
        changed = cut_edit.set_selection_by_type(
            cands, str(payload.get("type") or ""), bool(payload.get("selected", True))
        )
    elif action == "revive_long_silence":
        changed = cut_edit.revive_long_silences(cands)
    elif action == "all":
        for c in cands:
            c.selected = bool(payload.get("selected", True))
            changed += 1
    else:
        raise fail(f"알 수 없는 동작입니다: {action}")

    _store_candidates(session, cands)
    session_svc.save(session)

    cut_map = _cut_map(session)
    signature_check = cut_edit.compare_signatures(
        session.data.get("cut_signature"), cut_map.signature()
    )
    return ok(
        changed=changed,
        candidates=[c.to_dict() for c in cands],
        summary=cut_edit.cut_summary(cands, cut_map),
        cut_map=cut_map.to_dict(),
        # 드래프트를 이미 만들었다면, 지금 바뀐 선택이 그것과 어긋나는지 미리 알려 줍니다.
        signature_check=signature_check if session.data.get("cut_signature") else None,
    )


@router.post("/{session_id}/preview")
def preview(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """구간 미리듣기용 짧은 wav를 만듭니다."""
    session = get_session(session_id)
    wav = session.work_dir / "audio_16k.wav"
    if not wav.is_file():
        raise fail("먼저 분석을 실행하세요. (오디오가 아직 추출되지 않았습니다)")

    start = max(0.0, float(payload.get("start", 0.0)) - float(payload.get("pad", 1.0)))
    duration = float(payload.get("duration", 0.0)) + float(payload.get("pad", 1.0)) * 2
    out = session.work_dir / "preview.wav"
    try:
        audio_analysis.slice_audio(wav, out, start, max(0.3, duration))
    except MediaError as exc:
        raise fail(str(exc))
    return ok(url=f"/api/editing/{session_id}/preview.wav")


@router.get("/{session_id}/preview.wav")
def preview_file(session_id: str) -> Any:
    from fastapi.responses import FileResponse
    session = get_session(session_id)
    out = session.work_dir / "preview.wav"
    if not out.is_file():
        raise fail("미리듣기 파일이 없습니다.", 404)
    return FileResponse(str(out), media_type="audio/wav")


@router.post("/{session_id}/build-draft")
def build_draft(session_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """1차 결과로 캡컷 드래프트를 만듭니다.

    이때 **컷 서명을 세션에 기록합니다** (요청서 3.18).
    자막을 넣기 전에 이 서명과 비교해 어긋나면 차단합니다.
    """
    session = get_session(session_id)
    require_step(session, "cut")
    root = require_draft_root()
    timeline = _timeline(session)
    cands = _candidates(session)
    if not cands:
        raise fail("먼저 분석을 실행하고 컷을 검수하세요.")

    cut_map = _cut_map(session)
    draft_name = str(payload.get("draft_name") or "").strip() or f"자동편집_{session.name}"
    draft_name = "".join(ch for ch in draft_name if ch not in '\\/:*?"<>|').strip() or "자동편집"

    def work(job: Job) -> Dict[str, Any]:
        job.set_progress(0.05, "드래프트를 만드는 중", draft_name)
        result = builder.build_cut_draft(
            draft_root=root, draft_name=draft_name, timeline=timeline, cut_map=cut_map,
            progress=lambda f: job.set_progress(0.05 + f * 0.85, "구간을 배치하는 중",
                                                f"{int(f * 100)}%"),
        )
        fresh = session_svc.load(session.id)
        fresh.data["draft_name"] = draft_name
        fresh.data["draft_dir"] = result["draft_dir"]
        fresh.data["cut_signature"] = result["cut_signature"]   # ← 3.18
        fresh.mark("cut", True)
        session_svc.save(fresh)
        job.set_progress(1.0, "완료", "")
        return result

    job = manager.submit(session_id, "build_draft", work, message="드래프트를 만듭니다")
    return ok(job=job.snapshot())


# ══════════════════════════════════════════════════════════════════════════
# 2차 자막 생성
# ══════════════════════════════════════════════════════════════════════════
@router.post("/{session_id}/script")
async def upload_script(session_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    """대본 파일(txt/srt/md) 업로드."""
    session = get_session(session_id)
    suffix = Path(file.filename or "script.txt").suffix.lower() or ".txt"
    if suffix not in (".txt", ".srt", ".md", ".markdown"):
        raise fail(f"지원하지 않는 대본 형식입니다: {suffix}\n(txt, srt, md만 됩니다)")

    dest = session.work_dir / f"script{suffix}"
    dest.write_bytes(await file.read())

    try:
        lines = script_align.parse_script(dest)
    except ValueError as exc:
        raise fail(str(exc))

    session.data["script"] = {"path": str(dest), "name": file.filename, "line_count": len(lines)}
    session_svc.save(session)
    return ok(line_count=len(lines), preview=lines[:10],
              message="" if lines else "대본에서 문장을 찾지 못했습니다. 파일 내용을 확인하세요.")


@router.delete("/{session_id}/script")
def clear_script(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    session.data.pop("script", None)
    session_svc.save(session)
    return ok(message="대본을 해제했습니다. STT 결과만 씁니다.")


@router.post("/{session_id}/subtitles")
def build_subtitles(session_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """자막을 만듭니다.

    ⚠ 먼저 컷 서명을 확인합니다 (요청서 3.18). 드래프트를 만든 뒤 컷 선택이
    바뀌었으면 여기서 **차단**합니다. 사용자는 "자막 싱크가 안 맞는다"고만
    말하고 원인을 못 찾으므로 도구가 먼저 잡아야 합니다.
    """
    session = get_session(session_id)
    require_step(session, "subtitle")

    timeline = _timeline(session)
    cut_map = _cut_map(session)

    check = cut_edit.compare_signatures(session.data.get("cut_signature"), cut_map.signature())
    if not check["ok"] and not payload.get("force"):
        raise fail(check["message"], 409)

    stt = session.data.get("stt") or {}
    segments = stt.get("segments") or []
    if not segments:
        raise fail("음성 인식 결과가 없습니다. 1차에서 분석을 먼저 실행하세요.")

    script_info = session.data.get("script") or {}
    script_lines: List[str] = []
    if script_info.get("path") and Path(script_info["path"]).is_file():
        script_lines = script_align.parse_script(Path(script_info["path"]))

    aligned = (script_align.align_script(segments, script_lines) if script_lines
               else [{"text": s.get("text", ""), "source": "stt", "score": 0.0,
                      "start": s.get("start", 0.0), "end": s.get("end", 0.0),
                      "words": s.get("words") or []}
                     for s in segments])

    settings = load_settings()
    max_chars = int(payload.get("max_chars", settings["subtitle_max_chars"]))
    terms = script_align.enabled_terms(load_glossary())

    subs, meta = script_align.build_subtitles(
        aligned, cut_map, glossary_terms=terms, max_chars=max_chars,
        min_duration=float(settings["subtitle_min_duration_sec"]),
    )

    session.data["subtitles"] = [s.to_dict() for s in subs]
    session.data["subtitle_meta"] = meta
    session_svc.save(session)

    script_count = sum(1 for s in subs if s.source == "script")
    return ok(
        subtitles=[s.to_dict() for s in subs],
        meta=meta,
        signature_check=check,
        stats={
            "total": len(subs),
            "from_script": script_count,
            "from_stt": len(subs) - script_count,
            "low_confidence": len(stt.get("low_confidence") or []),
        },
    )


@router.post("/{session_id}/subtitles/update")
def update_subtitles(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """검수 테이블 인라인 수정. 수정 후 다시 정리해 겹침을 막습니다 (3.9)."""
    session = get_session(session_id)
    subs = _subtitles(session)
    if not subs:
        raise fail("먼저 자막을 만드세요.")

    edits = {str(e.get("id")): e for e in (payload.get("edits") or [])}
    settings = load_settings()
    max_chars = int(settings["subtitle_max_chars"])

    for sub in subs:
        edit = edits.get(sub.id)
        if not edit:
            continue
        if "text" in edit:
            sub.text = str(edit["text"]).strip()
        if "start" in edit:
            sub.start = float(edit["start"])
        if "end" in edit:
            sub.end = float(edit["end"])

    deleted = {str(i) for i in (payload.get("delete") or [])}
    if deleted:
        subs = [s for s in subs if s.id not in deleted]

    subs, stats = script_align.sanitize(
        subs, min_duration=float(settings["subtitle_min_duration_sec"])
    )
    analysis = line_break.analyze_lines([s.text for s in subs], max_chars)

    session.data["subtitles"] = [s.to_dict() for s in subs]
    session.data["subtitle_meta"] = {"sanitize": stats, "lines": analysis}
    session_svc.save(session)

    warnings: List[str] = []
    if analysis["over_limit"]:
        warnings.append(
            f"{analysis['over_limit']}건이 한 줄 상한 {max_chars}자를 넘습니다. "
            "화면에서 잘리거나 자동 줄바꿈될 수 있습니다."
        )
    if analysis["multiline"]:
        warnings.append(f"{analysis['multiline']}건에 줄바꿈이 들어 있습니다. 자막은 한 줄이어야 합니다.")

    return ok(subtitles=[s.to_dict() for s in subs], meta={"sanitize": stats, "lines": analysis},
              warnings=warnings)


@router.get("/{session_id}/subtitles/srt", response_class=PlainTextResponse)
def export_srt(session_id: str) -> str:
    """SRT 내보내기. 내보낼 때 한 번 더 정리합니다 (요청서 3.9)."""
    session = get_session(session_id)
    subs = _subtitles(session)
    if not subs:
        raise fail("내보낼 자막이 없습니다.")
    return script_align.to_srt(subs)


@router.post("/{session_id}/subtitles/inject")
def inject_subtitles(session_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """드래프트에 자막을 넣습니다. 여기서도 컷 서명을 다시 확인합니다."""
    session = get_session(session_id)
    require_step(session, "subtitle")
    root = require_draft_root()

    draft_name = session.data.get("draft_name")
    if not draft_name:
        raise fail("드래프트가 없습니다. 1차에서 드래프트를 먼저 만드세요.")

    cut_map = _cut_map(session)
    check = cut_edit.compare_signatures(session.data.get("cut_signature"), cut_map.signature())
    if not check["ok"] and not payload.get("force"):
        raise fail(check["message"], 409)

    subs = _subtitles(session)
    if not subs:
        raise fail("넣을 자막이 없습니다. 자막을 먼저 만드세요.")

    profile = cdraft.load_style_profile()
    if not profile:
        raise fail("자막 스타일 캘리브레이션을 먼저 하세요. "
                   "(이게 없으면 자막이 캡컷 화면에 제대로 안 보입니다)")

    draft_dir = Path(root) / draft_name

    def work(job: Job) -> Dict[str, Any]:
        job.set_progress(0.1, "백업하는 중", draft_name)
        cdraft.backup_draft(draft_dir, tag="before_subtitles")

        job.set_progress(0.3, "자막을 넣는 중", f"{len(subs)}건")
        result = builder.inject_subtitles(
            draft_root=root, draft_dir=draft_dir, subtitles=subs,
            style_profile=profile, position=str(payload.get("position") or "bottom"),
        )

        fresh = session_svc.load(session.id)
        fresh.mark("subtitle", bool(result["ok"]))
        session_svc.save(fresh)
        job.set_progress(1.0, "완료", "")
        return result

    job = manager.submit(session_id, "inject_subtitles", work, message="자막을 넣습니다")
    return ok(job=job.snapshot())


@router.get("/{session_id}/subtitles")
def get_subtitles(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    return {
        "subtitles": session.data.get("subtitles") or [],
        "meta": session.data.get("subtitle_meta") or {},
        "script": session.data.get("script") or None,
    }
