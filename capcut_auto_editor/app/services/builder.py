"""pyCapCut 호출 + 후처리 주입.

요청서 3.6이 제시한 **2단 구성**을 그대로 씁니다.
    구조는 pyCapCut이, 스타일은 후처리가 담당합니다.

pyCapCut으로 타임라인 골격(트랙·세그먼트·소재 참조)을 만들고 저장한 뒤,
저장된 JSON을 열어 캘리브레이션한 캡컷 원본 소재를 이식하고
track_render_index / ratio / 메타 / 레지스트리를 교정합니다.

여기서 지키는 함정:
  3.3  저장 후 track_render_index 교정 (postprocess_saved_draft)
  3.5  텍스트 소재를 캘리브레이션 원본으로 갈아끼움
  3.8  소재 길이를 pyCapCut이 읽은 값에 맞춰 clamp
  3.9  겹치는 자막은 script_align.sanitize가 이미 정리
  3.10 canvas_config.ratio 교정
  3.13 쓰기 전 캡컷 실행 감지, 쓴 뒤 재검사
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import load_settings
from . import capcut_draft as cdraft
from .capcut_draft import DraftError
from .cut_edit import CutMap
from .logging_util import get_logger
from .script_align import Subtitle
from .sources import SourceTimeline

log = get_logger()

SEC_US = 1_000_000


def _us(seconds: float) -> int:
    """초 -> 마이크로초 정수 (요청서 3.1: 시간 단위는 µs 정수)."""
    return int(round(max(0.0, seconds) * SEC_US))


def backup_if_exists(draft_root: Path, draft_name: str, tag: str) -> Optional[str]:
    """덮어쓰기 전에 기존 드래프트를 백업합니다.

    pyCapCut의 create_draft(allow_replace=True)는 기존 폴더를 **통째로 지웁니다**
    (draft_folder.py: shutil.rmtree). 자막까지 넣어 둔 드래프트를 다시 만들면
    그 작업이 아무 경고 없이 사라집니다.
    요청서 2번 원칙(원본을 절대 파괴하지 말 것)에 따라 지우기 전에 스냅샷을 남깁니다.
    """
    existing = Path(draft_root) / draft_name
    if not (existing / "draft_content.json").is_file():
        return None
    dest = cdraft.backup_draft(existing, tag=tag)
    log.info("덮어쓰기 전 백업: %s", dest)
    return str(dest)


def _import_pycapcut() -> Any:
    try:
        import pycapcut
        return pycapcut
    except ImportError as exc:
        raise DraftError(
            "pycapcut이 설치돼 있지 않습니다.\n"
            "pip install pycapcut==0.0.3 을 실행한 뒤 다시 시도하세요."
        ) from exc


# ══════════════════════════════════════════════════════════════════════════
# 3.8  소재 길이 clamp
# ══════════════════════════════════════════════════════════════════════════
class MaterialCache:
    """경로별 VideoMaterial을 한 번만 만들어 재사용합니다.

    ⚠ 요청서 3.8: pyCapCut은 **자기가 읽은 길이**를 1µs라도 넘으면 거부합니다.
        `截取的素材时间范围 [start=..., end=437767000] 超出了素材时长(437766000)`
      길이는 pymediainfo가 **ms 단위로 읽은 뒤 1000배**한 값이라
      ffprobe의 초 단위 값과 미묘하게 다릅니다.
      그래서 세그먼트를 만들기 전에 VideoMaterial.duration을 읽어 clamp합니다.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self.clamped: List[str] = []

    def get(self, path: str) -> Any:
        if path not in self._cache:
            pc = _import_pycapcut()
            try:
                self._cache[path] = pc.VideoMaterial(path)
            except Exception as exc:  # noqa: BLE001
                raise DraftError(
                    f"'{Path(path).name}' 소재를 읽지 못했습니다.\n{type(exc).__name__}: {exc}"
                ) from exc
        return self._cache[path]

    def clamp(self, path: str, start_us: int, end_us: int) -> Tuple[int, int]:
        """구간을 소재 길이 안으로 밀어넣습니다."""
        material = self.get(path)
        limit = int(material.duration)
        start_us = max(0, min(start_us, limit))
        if end_us > limit:
            self.clamped.append(
                f"{Path(path).name}: {end_us}µs -> {limit}µs (소재 길이 초과분 잘림)"
            )
            end_us = limit
        return start_us, max(start_us, end_us)

    def materials(self) -> List[Any]:
        return list(self._cache.values())


# ══════════════════════════════════════════════════════════════════════════
# 1차: 컷 편집 드래프트
# ══════════════════════════════════════════════════════════════════════════
def build_cut_draft(
    *,
    draft_root: Path,
    draft_name: str,
    timeline: SourceTimeline,
    cut_map: CutMap,
    allow_replace: bool = True,
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """남는 구간만 이어붙인 드래프트를 만듭니다.

    ⚠ 구간이 영상 경계를 가로지르면 반드시 쪼갭니다 (요청서 4절).
    """
    pc = _import_pycapcut()
    cdraft.capcut_guard("드래프트 만들기")

    canvas = timeline.canvas()
    fps = timeline.fps()

    replaced_backup = backup_if_exists(draft_root, draft_name, "before_rebuild") \
        if allow_replace else None

    folder = pc.DraftFolder(str(draft_root))
    script = folder.create_draft(draft_name, canvas["width"], canvas["height"], fps,
                                 allow_replace=allow_replace)
    script.add_track(pc.TrackType.video)

    cache = MaterialCache()
    placed = 0
    skipped: List[str] = []
    target_us = 0

    for span_index, (kept_start, kept_end) in enumerate(cut_map.kept):
        for piece in timeline.split_span(kept_start, kept_end):
            src_start_us = _us(piece["local_start"])
            src_end_us = _us(piece["local_end"])
            src_start_us, src_end_us = cache.clamp(piece["path"], src_start_us, src_end_us)
            duration_us = src_end_us - src_start_us
            if duration_us <= 0:
                skipped.append(f"{piece['name']} {piece['local_start']:.2f}s (길이 0)")
                continue

            material = cache.get(piece["path"])
            segment = pc.VideoSegment(
                material,
                pc.Timerange(target_us, duration_us),
                source_timerange=pc.Timerange(src_start_us, duration_us),
            )
            script.add_segment(segment)
            target_us += duration_us
            placed += 1

        if progress is not None and cut_map.kept:
            progress((span_index + 1) / len(cut_map.kept))

    if placed == 0:
        raise DraftError(
            "드래프트에 넣을 구간이 하나도 없습니다.\n"
            "컷을 너무 많이 선택했는지 확인하세요. (1차 검수 화면에서 선택을 줄여 보세요)"
        )

    script.save()
    draft_dir = Path(draft_root) / draft_name
    post = cdraft.postprocess_saved_draft(draft_root, draft_dir)

    warning = cdraft.capcut_reopened_warning()
    result = {
        "draft_name": draft_name,
        "draft_dir": str(draft_dir),
        "segment_count": placed,
        "timeline_duration": target_us / SEC_US,
        "canvas": canvas,
        "fps": fps,
        "skipped": skipped,
        "clamped": cache.clamped,
        "postprocess": post,
        "cut_signature": cut_map.signature(),
        "capcut_warning": warning,
        "replaced_backup": replaced_backup,
    }
    log.info("컷 드래프트 생성: %s (세그먼트 %d개, %.1f초)",
             draft_name, placed, target_us / SEC_US)
    return result


# ══════════════════════════════════════════════════════════════════════════
# 2차: 자막 주입 (3.5 — 캘리브레이션 원본 이식)
# ══════════════════════════════════════════════════════════════════════════
def _text_segment_skeleton(
    pc: Any,
    text: str,
    start_us: int,
    duration_us: int,
    clip: Dict[str, Any],
) -> Dict[str, Any]:
    """pyCapCut으로 텍스트 세그먼트 **구조**만 만듭니다.

    소재는 뒤에서 캘리브레이션 원본으로 갈아끼우므로 여기서는 스타일을 신경 쓰지 않습니다.
    (요청서 3.6의 "구조는 pyCapCut이, 스타일은 후처리가")
    """
    transform = clip.get("transform") or {}
    scale = clip.get("scale") or {}
    segment = pc.TextSegment(
        text or " ",
        pc.Timerange(start_us, max(1, duration_us)),
        clip_settings=pc.ClipSettings(
            alpha=float(clip.get("alpha", 1.0)),
            rotation=float(clip.get("rotation", 0.0)),
            scale_x=float(scale.get("x", 1.0)),
            scale_y=float(scale.get("y", 1.0)),
            transform_x=float(transform.get("x", 0.0)),
            transform_y=float(transform.get("y", 0.0)),
        ),
    )
    return segment.export_json()


def inject_subtitles(
    *,
    draft_root: Path,
    draft_dir: Path,
    subtitles: Sequence[Subtitle],
    style_profile: Dict[str, Any],
    position: str = "bottom",
    track_name: str = "자막",
    replace_existing: bool = True,
) -> Dict[str, Any]:
    """자막을 드래프트에 넣습니다.

    pyCapCut의 TextSegment는 소재 필드가 20개뿐이라 그대로 쓰면
    캡컷에서 스타일이 죽습니다 (요청서 3.5). 그래서
    **캘리브레이션으로 저장해 둔 캡컷 원본 소재를 복제하고 텍스트만 갈아끼웁니다.**
    """
    pc = _import_pycapcut()
    cdraft.capcut_guard("자막 넣기")

    draft_dir = Path(draft_dir)
    data = cdraft.read_draft_content(draft_dir)

    materials = data.setdefault("materials", {})
    texts = materials.setdefault("texts", [])
    tracks = data.setdefault("tracks", [])

    if replace_existing:
        removed_ids = set()
        for track in list(tracks):
            if track.get("type") == "text" and track.get("name") == track_name:
                for seg in track.get("segments") or []:
                    if seg.get("material_id"):
                        removed_ids.add(seg["material_id"])
                tracks.remove(track)
        if removed_ids:
            materials["texts"] = [m for m in texts if m.get("id") not in removed_ids]
            texts = materials["texts"]
            log.info("기존 '%s' 트랙과 소재 %d개를 교체합니다.", track_name, len(removed_ids))

    clip_template = json.loads(json.dumps(style_profile.get("clip_template") or {}))
    pos = style_profile.get("position") or {}
    target_y = pos.get("top_y" if position == "top" else "bottom_y")
    if isinstance(target_y, (int, float)):
        clip_template.setdefault("transform", {})["y"] = float(target_y)

    segments: List[Dict[str, Any]] = []
    for sub in subtitles:
        start_us = _us(sub.start)
        duration_us = _us(sub.end) - start_us
        if duration_us <= 0:
            continue

        material = cdraft.graft_text_material(style_profile, sub.text)
        texts.append(material)

        seg = _text_segment_skeleton(pc, sub.text, start_us, duration_us, clip_template)
        seg["material_id"] = material["id"]     # 갈아끼운 소재를 가리키게 합니다
        segments.append(seg)

    if not segments:
        raise DraftError("넣을 자막이 없습니다. 2차 자막 생성을 먼저 끝내세요.")

    tracks.append({
        "attribute": 0,
        "flag": 0,
        "id": uuid.uuid4().hex,
        "is_default_name": False,
        "name": track_name,
        "segments": segments,
        "type": "text",
    })

    # 트랙 길이가 늘었으면 드래프트 전체 길이도 늘려 줍니다.
    end_us = max((s["target_timerange"]["start"] + s["target_timerange"]["duration"])
                 for s in segments)
    data["duration"] = max(int(data.get("duration") or 0), end_us)

    cdraft.write_draft_content(draft_dir, data)
    post = cdraft.postprocess_saved_draft(draft_root, draft_dir)

    # 요청서 6번 원칙: "됐다"고 말하기 전에 파일을 다시 읽어 확인합니다.
    verified = cdraft.verify_subtitle_visibility(draft_dir)

    return {
        "requested": len(subtitles),
        "verified": verified["verified_subtitle_count"],   # ← 화면에는 이 값을 표시합니다
        "problems": verified["problems"],
        "ok": verified["ok"],
        "postprocess": post,
        "capcut_warning": cdraft.capcut_reopened_warning(),
    }


# ══════════════════════════════════════════════════════════════════════════
# 3차: 트랜지션 · 효과음
# ══════════════════════════════════════════════════════════════════════════
def list_image_clips(draft_dir: Path) -> List[Dict[str, Any]]:
    """드래프트에서 이미지 클립을 뽑습니다 (요청서 6절 3차).

    1차 판정: `materials.videos[].type == "photo"` (실측 확인)
    2차 교차검증: 확장자
    """
    data = cdraft.read_draft_content(draft_dir)
    videos = {m.get("id"): m for m in (data.get("materials", {}).get("videos") or [])
              if isinstance(m, dict)}

    out: List[Dict[str, Any]] = []
    for t_index, track in enumerate(data.get("tracks") or []):
        if track.get("type") != "video":
            continue
        segs = track.get("segments") or []
        for s_index, seg in enumerate(segs):
            material = videos.get(seg.get("material_id"))
            if not material or not cdraft.is_photo_material(material):
                continue
            trange = seg.get("target_timerange") or {}
            out.append({
                "track_index": t_index,
                "segment_index": s_index,
                "segment_id": seg.get("id"),
                "name": material.get("material_name") or Path(str(material.get("path") or "")).name,
                "path": material.get("path", ""),
                "type": material.get("type", ""),
                "start": (trange.get("start") or 0) / SEC_US,
                "duration": (trange.get("duration") or 0) / SEC_US,
                "has_prev": s_index > 0,
                "has_next": s_index < len(segs) - 1,
            })
    return out


def _transition_material(effect_id: str, duration_us: int) -> Dict[str, Any]:
    """트랜지션 소재 dict. enum에서 이름·resource_id를 역조회합니다 (3.7)."""
    enum_name = cdraft.lookup_transition_name(effect_id)
    resource_id = effect_id
    is_overlap = False
    try:
        from pycapcut.metadata.transition_meta import TransitionType
        for member in TransitionType:
            if str(member.value.effect_id) == str(effect_id):
                resource_id = str(member.value.resource_id)
                is_overlap = bool(getattr(member.value, "is_overlap", False))
                break
    except ImportError:
        pass

    return {
        "id": uuid.uuid4().hex,
        "effect_id": str(effect_id),
        "resource_id": resource_id,
        "name": enum_name or "",
        "duration": duration_us,
        "is_overlap": is_overlap,
        "path": "",
        "platform": "all",
        "type": "transition",
        "category_id": "",
        "category_name": "",
        "request_id": "",
    }


def apply_transitions(
    *,
    draft_root: Path,
    draft_dir: Path,
    selections: Sequence[Dict[str, Any]],
    duration_sec: float = 0.5,
) -> Dict[str, Any]:
    """선택한 위치에 트랜지션을 붙입니다.

    ⚠ **트랜지션은 앞쪽 세그먼트에 붙습니다** (pyCapCut 규칙).
    "이미지 앞에 넣기"는 곧 "앞 세그먼트에 붙이기"입니다.
    """
    cdraft.capcut_guard("트랜지션 적용")
    draft_dir = Path(draft_dir)
    data = cdraft.read_draft_content(draft_dir)

    materials = data.setdefault("materials", {})
    transitions = materials.setdefault("transitions", [])
    tracks = data.get("tracks") or []

    duration_us = _us(duration_sec)
    applied: List[str] = []
    problems: List[str] = []

    for sel in selections:
        t_index = int(sel.get("track_index", 0))
        s_index = int(sel.get("segment_index", 0))
        side = str(sel.get("side", "before"))       # before | after
        effect_id = str(sel.get("effect_id") or "")
        if not effect_id:
            problems.append("트랜지션이 선택되지 않은 항목이 있습니다.")
            continue

        if t_index >= len(tracks):
            problems.append(f"트랙 {t_index}을(를) 찾을 수 없습니다.")
            continue
        segs = tracks[t_index].get("segments") or []

        # 트랜지션은 앞쪽 세그먼트에 붙으므로, "앞에 넣기"는 이전 세그먼트가 대상입니다.
        host_index = s_index - 1 if side == "before" else s_index
        if host_index < 0 or host_index >= len(segs) - 1:
            problems.append(
                f"{sel.get('name', '항목')}: {'앞' if side == 'before' else '뒤'}에 "
                "이어지는 클립이 없어 트랜지션을 넣을 수 없습니다."
            )
            continue

        material = _transition_material(effect_id, duration_us)
        transitions.append(material)
        refs = segs[host_index].setdefault("extra_material_refs", [])
        refs.append(material["id"])
        applied.append(f"{sel.get('name', '')} {'앞' if side == 'before' else '뒤'}"
                       f"({material['name'] or effect_id})")

    cdraft.write_draft_content(draft_dir, data)
    post = cdraft.postprocess_saved_draft(draft_root, draft_dir)

    return {
        "applied": len(applied),
        "applied_labels": applied,
        "problems": problems,
        "postprocess": post,
        "capcut_warning": cdraft.capcut_reopened_warning(),
    }


def apply_sfx(
    *,
    draft_root: Path,
    draft_dir: Path,
    placements: Sequence[Dict[str, Any]],
    volume: float = 0.6,
    track_name: str = "효과음",
) -> Dict[str, Any]:
    """`assets/sfx/`의 로컬 wav를 오디오 트랙으로 넣습니다."""
    pc = _import_pycapcut()
    cdraft.capcut_guard("효과음 넣기")

    draft_dir = Path(draft_dir)
    data = cdraft.read_draft_content(draft_dir)
    materials = data.setdefault("materials", {})
    audios = materials.setdefault("audios", [])
    tracks = data.setdefault("tracks", [])

    for track in list(tracks):
        if track.get("type") == "audio" and track.get("name") == track_name:
            ids = {s.get("material_id") for s in (track.get("segments") or [])}
            materials["audios"] = [a for a in audios if a.get("id") not in ids]
            audios = materials["audios"]
            tracks.remove(track)

    segments: List[Dict[str, Any]] = []
    problems: List[str] = []

    for place in placements:
        path = str(place.get("path") or "")
        if not path or not Path(path).is_file():
            problems.append(f"효과음 파일을 찾을 수 없습니다: {path}")
            continue
        try:
            material = pc.AudioMaterial(path)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"'{Path(path).name}'을(를) 읽지 못했습니다: {exc}")
            continue

        start_us = _us(float(place.get("start", 0.0)))
        duration_us = min(int(material.duration), _us(float(place.get("duration", 0.0)))
                          or int(material.duration))
        segment = pc.AudioSegment(
            material, pc.Timerange(start_us, duration_us),
            source_timerange=pc.Timerange(0, duration_us),
            volume=float(place.get("volume", volume)),
        )
        audios.append(material.export_json())
        segments.append(segment.export_json())

    if segments:
        tracks.append({
            "attribute": 0, "flag": 0, "id": uuid.uuid4().hex,
            "is_default_name": False, "name": track_name,
            "segments": segments, "type": "audio",
        })

    cdraft.write_draft_content(draft_dir, data)
    post = cdraft.postprocess_saved_draft(draft_root, draft_dir)
    return {
        "applied": len(segments), "problems": problems, "postprocess": post,
        "capcut_warning": cdraft.capcut_reopened_warning(),
    }


# ══════════════════════════════════════════════════════════════════════════
# 5차: 세로용 드래프트
# ══════════════════════════════════════════════════════════════════════════
def build_vertical_draft(
    *,
    draft_root: Path,
    draft_name: str,
    timeline: SourceTimeline,
    original_spans: Sequence[Tuple[float, float]],
    subtitles: Sequence[Subtitle],
    style_profile: Dict[str, Any],
    background_image: str = "",
    edited_offset: float = 0.0,
    allow_replace: bool = True,
) -> Dict[str, Any]:
    """1080x1920 세로 드래프트를 만듭니다.

    구성 (요청서 6절 5차):
      · `assets/bg/` 배경 이미지를 최하단 (scale 1.0, transform (0,0))
      · 16:9 클립을 오버레이 (**scale 1.8, transform.y -0.078125** — 실측 채택)
      · 해당 구간 자막을 세로용 위치로 재배치

    `original_spans`는 **편집본 구간을 원본으로 역변환한 결과**여야 합니다.
    (요청서 5차 함정: 사용자가 고르는 건 편집본 시각인데 소재는 원본입니다)
    """
    pc = _import_pycapcut()
    cdraft.capcut_guard("세로 영상 드래프트 만들기")

    settings = load_settings()
    width = int(settings.get("vertical_width", 1080))
    height = int(settings.get("vertical_height", 1920))
    scale = float(settings.get("vertical_scale", 1.8))
    transform_y = float(settings.get("vertical_transform_y", -0.078125))
    fps = timeline.fps()

    replaced_backup = backup_if_exists(draft_root, draft_name, "before_rebuild") \
        if allow_replace else None

    folder = pc.DraftFolder(str(draft_root))
    script = folder.create_draft(draft_name, width, height, fps, allow_replace=allow_replace)

    total_us = sum(_us(e - s) for s, e in original_spans)
    if total_us <= 0:
        raise DraftError("고른 구간의 길이가 0입니다. 시작/끝 지점을 다시 확인하세요.")

    # ── 배경 트랙 (최하단) ────────────────────────────────────────────────
    background_used = ""
    if background_image and Path(background_image).is_file():
        script.add_track(pc.TrackType.video, track_name="배경")
        bg_material = pc.VideoMaterial(background_image)
        bg_duration = min(int(bg_material.duration), total_us)
        script.add_segment(
            pc.VideoSegment(
                bg_material, pc.Timerange(0, bg_duration),
                source_timerange=pc.Timerange(0, bg_duration),
                clip_settings=pc.ClipSettings(scale_x=1.0, scale_y=1.0,
                                              transform_x=0.0, transform_y=0.0),
            ),
            track_name="배경",
        )
        background_used = background_image

    # ── 오버레이 트랙 (16:9 원본) ─────────────────────────────────────────
    script.add_track(pc.TrackType.video, track_name="본편")
    cache = MaterialCache()
    target_us = 0
    placed = 0

    for span_start, span_end in original_spans:
        for piece in timeline.split_span(span_start, span_end):
            src_start_us = _us(piece["local_start"])
            src_end_us = _us(piece["local_end"])
            src_start_us, src_end_us = cache.clamp(piece["path"], src_start_us, src_end_us)
            duration_us = src_end_us - src_start_us
            if duration_us <= 0:
                continue
            script.add_segment(
                pc.VideoSegment(
                    cache.get(piece["path"]),
                    pc.Timerange(target_us, duration_us),
                    source_timerange=pc.Timerange(src_start_us, duration_us),
                    clip_settings=pc.ClipSettings(scale_x=scale, scale_y=scale,
                                                  transform_x=0.0, transform_y=transform_y),
                ),
                track_name="본편",
            )
            target_us += duration_us
            placed += 1

    if placed == 0:
        raise DraftError("세로 영상에 넣을 구간이 없습니다. 시작/끝 지점을 다시 확인하세요.")

    script.save()
    draft_dir = Path(draft_root) / draft_name

    # ── 자막 재배치 ───────────────────────────────────────────────────────
    shifted: List[Subtitle] = []
    for sub in subtitles:
        start = sub.start - edited_offset
        end = sub.end - edited_offset
        if end <= 0 or start >= target_us / SEC_US:
            continue
        shifted.append(Subtitle(
            id=sub.id, start=max(0.0, start), end=min(target_us / SEC_US, end),
            text=sub.text, source=sub.source, confidence=sub.confidence,
        ))

    sub_result: Dict[str, Any] = {"requested": 0, "verified": 0, "problems": [], "ok": True}
    if shifted and style_profile:
        sub_result = inject_subtitles(
            draft_root=draft_root, draft_dir=draft_dir, subtitles=shifted,
            style_profile=style_profile, position="bottom", track_name="자막",
        )
    else:
        cdraft.postprocess_saved_draft(draft_root, draft_dir)

    log.info("세로 드래프트 생성: %s (%dx%d, %.1f초, 자막 %d개)",
             draft_name, width, height, target_us / SEC_US, len(shifted))

    return {
        "draft_name": draft_name,
        "draft_dir": str(draft_dir),
        "canvas": {"width": width, "height": height},
        "duration": target_us / SEC_US,
        "segment_count": placed,
        "background": background_used,
        "scale": scale,
        "transform_y": transform_y,
        "subtitles": sub_result,
        "clamped": cache.clamped,
        "capcut_warning": cdraft.capcut_reopened_warning(),
        "replaced_backup": replaced_backup,
    }
