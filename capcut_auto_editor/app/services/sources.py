"""여러 영상을 이어붙인 소스 타임라인.

요청서 4절: 영상 여러 개를 순서대로 이어붙인 **가상 타임라인 하나**로 보고,
무음 감지·전사·컷 편집·자막을 전부 그 위에서 합니다.
드래프트를 만들 때만 (몇 번째 영상, 그 안에서 몇 초)로 되돌립니다.

⚠ **구간이 영상 경계를 가로지르면 반드시 쪼갭니다** (`split_span`).
쪼개지 않으면 존재하지 않는 위치를 참조해 pyCapCut이 거부하거나,
엉뚱한 영상의 화면이 나옵니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SourceClip:
    index: int
    path: str
    name: str
    duration: float        # 초
    offset: float          # 가상 타임라인상의 시작 위치 (초)
    width: int = 0
    height: int = 0
    fps: float = 0.0
    audio_track_count: int = 0

    @property
    def end(self) -> float:
        return self.offset + self.duration

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "path": self.path, "name": self.name,
            "duration": self.duration, "offset": self.offset, "end": self.end,
            "width": self.width, "height": self.height, "fps": self.fps,
            "audio_track_count": self.audio_track_count,
        }


class SourceTimeline:
    """여러 소스를 순서대로 이어붙인 가상 타임라인."""

    def __init__(self, clips: List[SourceClip]) -> None:
        self.clips = clips

    # ── 생성 ──────────────────────────────────────────────────────────────
    @staticmethod
    def from_probes(probes: List[Dict[str, Any]]) -> "SourceTimeline":
        clips: List[SourceClip] = []
        offset = 0.0
        for i, p in enumerate(probes):
            duration = float(p.get("duration") or 0.0)
            clips.append(SourceClip(
                index=i,
                path=str(p.get("path") or ""),
                name=str(p.get("name") or Path(str(p.get("path") or "")).name),
                duration=duration,
                offset=offset,
                width=int(p.get("width") or 0),
                height=int(p.get("height") or 0),
                fps=float(p.get("fps") or 0.0),
                audio_track_count=int(p.get("audio_track_count") or 0),
            ))
            offset += duration
        return SourceTimeline(clips)

    @staticmethod
    def from_dict(payload: Dict[str, Any]) -> "SourceTimeline":
        return SourceTimeline([SourceClip(**{k: v for k, v in c.items() if k != "end"})
                               for c in (payload.get("clips") or [])])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clips": [c.to_dict() for c in self.clips],
            "total_duration": self.total_duration,
            "canvas": self.canvas(),
            "mixed": self.mixed_properties(),
        }

    # ── 기본 정보 ─────────────────────────────────────────────────────────
    @property
    def total_duration(self) -> float:
        return sum(c.duration for c in self.clips)

    def canvas(self) -> Dict[str, int]:
        """해상도가 섞이면 가장 큰 해상도를 캔버스로 씁니다 (요청서 6절 0차)."""
        if not self.clips:
            return {"width": 1920, "height": 1080}
        best = max(self.clips, key=lambda c: (c.width * c.height, c.width))
        return {"width": best.width or 1920, "height": best.height or 1080}

    def fps(self) -> int:
        rates = [c.fps for c in self.clips if c.fps > 0]
        return int(round(max(rates))) if rates else 30

    def mixed_properties(self) -> Dict[str, Any]:
        """해상도/fps가 섞였는지. 경고는 하되 막지는 않습니다 (요청서 6절 0차)."""
        resolutions = {(c.width, c.height) for c in self.clips if c.width and c.height}
        rates = {round(c.fps, 2) for c in self.clips if c.fps > 0}
        no_audio = [c.name for c in self.clips if c.audio_track_count == 0]
        warnings: List[str] = []
        if len(resolutions) > 1:
            listed = ", ".join(f"{w}x{h}" for w, h in sorted(resolutions))
            warnings.append(
                f"해상도가 섞여 있습니다 ({listed}). "
                f"캔버스는 가장 큰 해상도({self.canvas()['width']}x{self.canvas()['height']}) 기준으로 만듭니다."
            )
        if len(rates) > 1:
            warnings.append(
                f"프레임레이트가 섞여 있습니다 ({', '.join(str(r) for r in sorted(rates))}fps). "
                f"드래프트는 {self.fps()}fps로 만듭니다."
            )
        if no_audio:
            warnings.append(
                f"오디오 트랙이 없는 영상이 있습니다: {', '.join(no_audio)}. "
                "이 영상 구간에서는 무음 감지와 자막이 나오지 않습니다."
            )
        return {
            "resolution_mixed": len(resolutions) > 1,
            "fps_mixed": len(rates) > 1,
            "warnings": warnings,
        }

    # ── 좌표 변환 ─────────────────────────────────────────────────────────
    def clip_at(self, global_t: float) -> Optional[SourceClip]:
        for c in self.clips:
            if c.offset <= global_t < c.end:
                return c
        return self.clips[-1] if self.clips and global_t >= self.total_duration else None

    def global_to_local(self, global_t: float) -> Tuple[int, float]:
        """가상 타임라인 시각 -> (몇 번째 영상, 그 안에서 몇 초)."""
        clip = self.clip_at(global_t)
        if clip is None:
            return (0, max(0.0, global_t))
        return (clip.index, max(0.0, min(clip.duration, global_t - clip.offset)))

    def local_to_global(self, clip_index: int, local_t: float) -> float:
        for c in self.clips:
            if c.index == clip_index:
                return c.offset + local_t
        return local_t

    def split_span(self, start: float, end: float) -> List[Dict[str, Any]]:
        """가상 타임라인 구간을 **영상 경계에서 쪼개** 실제 소재 구간 목록으로 바꿉니다.

        요청서 4절이 "반드시 쪼개십시오"라고 한 부분입니다.
        경계를 넘는 구간을 그대로 두면 존재하지 않는 위치를 참조하게 됩니다.
        """
        start = max(0.0, start)
        end = min(self.total_duration, end)
        out: List[Dict[str, Any]] = []
        if end <= start:
            return out

        for clip in self.clips:
            overlap_start = max(start, clip.offset)
            overlap_end = min(end, clip.end)
            if overlap_end - overlap_start <= 1e-6:
                continue
            out.append({
                "clip_index": clip.index,
                "path": clip.path,
                "name": clip.name,
                "local_start": overlap_start - clip.offset,
                "local_end": overlap_end - clip.offset,
                "global_start": overlap_start,
                "global_end": overlap_end,
                "duration": overlap_end - overlap_start,
            })
        return out


def summarize(timeline: SourceTimeline) -> Dict[str, Any]:
    """0차 화면에 띄울 요약."""
    return {
        "count": len(timeline.clips),
        "total_duration": timeline.total_duration,
        "canvas": timeline.canvas(),
        "fps": timeline.fps(),
        "clips": [c.to_dict() for c in timeline.clips],
        "warnings": timeline.mixed_properties()["warnings"],
    }
