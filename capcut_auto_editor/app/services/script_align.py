"""대본 정렬, 용어 사전, 자막·SRT.

⚠ 요청서 3.9 — 자막 시간이 겹치면 pyCapCut이 거부합니다
    `New segment overlaps with existing segment [start: ..., end: ...]`
    음성 인식은 겹치는 구간을 심심찮게 내놓습니다
    (앞 문장이 463.87초까지인데 다음 문장이 463.47초에 시작).
    또 문장부호 분할이 `.` 하나짜리 자막을 만들기도 합니다.

    → 자막을 만들 때와 SRT로 내보낼 때 **두 번** 정리합니다 (`sanitize`).
      부호만 있는 조각은 앞 조각에 붙이고, 겹치면 앞 자막 끝을 당기거나 뒤 자막을 밉니다.
      자막 하나의 최소 길이는 0.35초입니다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import line_break
from .cut_edit import CutMap
from .logging_util import get_logger

log = get_logger()

MIN_SUBTITLE_SEC = 0.35
ALIGN_SCORE_THRESHOLD = 70.0     # 이 점수 미만이면 즉흥 발화로 보고 STT를 씁니다


# ══════════════════════════════════════════════════════════════════════════
@dataclass
class Subtitle:
    id: str
    start: float           # 편집본 기준 초
    end: float
    text: str
    source: str = "stt"    # stt | script
    confidence: float = 1.0
    original_start: float = 0.0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "start": round(self.start, 3), "end": round(self.end, 3),
            "duration": round(self.end - self.start, 3), "text": self.text,
            "source": self.source, "confidence": round(self.confidence, 3),
            "original_start": round(self.original_start, 3), "note": self.note,
            "length": len(self.text),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Subtitle":
        return Subtitle(
            id=str(d.get("id", "")), start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)), text=str(d.get("text", "")),
            source=str(d.get("source", "stt")), confidence=float(d.get("confidence", 1.0)),
            original_start=float(d.get("original_start", 0.0)), note=str(d.get("note", "")),
        )


# ══════════════════════════════════════════════════════════════════════════
# 대본 읽기
# ══════════════════════════════════════════════════════════════════════════
_SRT_BLOCK = re.compile(
    r"\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*\n",
    re.MULTILINE,
)


def parse_script(path: Path) -> List[str]:
    """대본 파일(txt/srt/md)에서 문장 목록을 뽑습니다."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 한국어 윈도우에서 메모장으로 저장하면 cp949인 경우가 흔합니다.
        raw = path.read_text(encoding="cp949", errors="replace")
    except OSError as exc:
        raise ValueError(f"대본 파일을 읽지 못했습니다: {exc}") from exc

    if path.suffix.lower() == ".srt" or _SRT_BLOCK.search(raw):
        raw = _SRT_BLOCK.sub("\n", raw)
        raw = re.sub(r"^\d+$", "", raw, flags=re.MULTILINE)

    if path.suffix.lower() in (".md", ".markdown"):
        raw = re.sub(r"^#{1,6}\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"[*_`>]", "", raw)

    lines: List[str] = []
    for block in raw.splitlines():
        block = block.strip()
        if not block:
            continue
        for piece in line_break.split_by_punctuation(block):
            if piece:
                lines.append(piece)
    return lines


# ══════════════════════════════════════════════════════════════════════════
# 용어 사전 (요청서 6절 2차)
# ══════════════════════════════════════════════════════════════════════════
def enabled_terms(glossary: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for entries in (glossary or {}).values():
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("enabled", True):
                term = str(entry.get("term", "")).strip()
                if term:
                    out.append(term)
            elif isinstance(entry, str):
                out.append(entry)
    return sorted(set(out), key=len, reverse=True)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^0-9a-z가-힣]", "", text)


def apply_glossary(text: str, terms: Sequence[str]) -> str:
    """음성 인식이 한글로 받아적은 영어 용어를 원문 표기로 되돌립니다.

    예: "비엘 발행" -> "B/L 발행" 이 아니라, 이미 영문으로 인식된 것의
    대소문자·표기를 사전 표기에 맞춥니다 (CargoWise, LLM 등).
    무리한 치환은 오히려 해가 되므로 **철자가 같은 경우만** 바꿉니다.
    """
    if not text or not terms:
        return text
    result = text
    for term in terms:
        pattern = re.compile(r"(?<![0-9A-Za-z])" + re.escape(term) + r"(?![0-9A-Za-z])",
                             re.IGNORECASE)
        result = pattern.sub(term, result)
    return result


# ══════════════════════════════════════════════════════════════════════════
# 대본 정렬
# ══════════════════════════════════════════════════════════════════════════
def align_script(
    stt_segments: Sequence[Dict[str, Any]],
    script_lines: Sequence[str],
    *,
    threshold: float = ALIGN_SCORE_THRESHOLD,
) -> List[Dict[str, Any]]:
    """STT 세그먼트에 대본 문장을 붙입니다.

    성립하는 구간은 **대본을 정본**으로 쓰고 타임코드는 STT에서 가져옵니다.
    점수가 낮으면 즉흥 발화로 보고 STT 텍스트를 그대로 둡니다.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        log.warning("rapidfuzz가 없어 대본 정렬을 건너뜁니다. STT 텍스트를 그대로 씁니다.")
        return [{"text": s.get("text", ""), "source": "stt", "score": 0.0,
                 "start": s.get("start", 0.0), "end": s.get("end", 0.0)}
                for s in stt_segments]

    out: List[Dict[str, Any]] = []
    cursor = 0
    lookahead = 6      # 대본 순서를 크게 벗어나지 않는다고 보고 앞쪽만 봅니다

    for seg in stt_segments:
        stt_text = str(seg.get("text", "")).strip()
        best_score, best_idx = 0.0, -1
        for offset in range(0, lookahead):
            idx = cursor + offset
            if idx >= len(script_lines):
                break
            score = fuzz.token_set_ratio(_norm(stt_text), _norm(script_lines[idx]))
            if score > best_score:
                best_score, best_idx = score, idx

        if best_idx >= 0 and best_score >= threshold:
            out.append({
                "text": script_lines[best_idx], "source": "script",
                "score": best_score, "stt_text": stt_text,
                "start": float(seg.get("start", 0.0)), "end": float(seg.get("end", 0.0)),
            })
            cursor = best_idx + 1
        else:
            out.append({
                "text": stt_text, "source": "stt", "score": best_score, "stt_text": stt_text,
                "start": float(seg.get("start", 0.0)), "end": float(seg.get("end", 0.0)),
            })
    return out


# ══════════════════════════════════════════════════════════════════════════
# 3.9  겹침·부호 정리
# ══════════════════════════════════════════════════════════════════════════
def sanitize(subs: List[Subtitle], *, min_duration: float = MIN_SUBTITLE_SEC) -> Tuple[List[Subtitle], Dict[str, int]]:
    """자막 목록을 겹침 없이, 부호만 있는 조각 없이 정리합니다 (요청서 3.9).

    자막을 만들 때와 SRT로 내보낼 때 **두 번 다** 호출합니다.
    """
    stats = {"merged_punct": 0, "fixed_overlap": 0, "stretched": 0, "dropped": 0}

    cleaned: List[Subtitle] = []
    for sub in sorted(subs, key=lambda s: (s.start, s.end)):
        text = (sub.text or "").strip()
        if not text:
            stats["dropped"] += 1
            continue
        if line_break.is_punctuation_only(text):
            # 부호만 있는 조각은 앞 조각에 붙입니다.
            if cleaned:
                prev = cleaned[-1]
                if not (prev.text and prev.text[-1] in line_break.ALL_MARKS
                        and set(text) <= {prev.text[-1]}):
                    prev.text = (prev.text + text).strip()
                prev.end = max(prev.end, sub.end)
                stats["merged_punct"] += 1
            else:
                stats["dropped"] += 1
            continue
        sub.text = text
        cleaned.append(sub)

    for i in range(len(cleaned) - 1):
        cur, nxt = cleaned[i], cleaned[i + 1]
        if cur.end > nxt.start:
            # 겹침. 앞 자막 끝을 당겨 봅니다.
            pulled = nxt.start
            if pulled - cur.start >= min_duration:
                cur.end = pulled
            else:
                # 앞 자막이 너무 짧아지면 뒤 자막을 밉니다.
                cur.end = cur.start + min_duration
                nxt.start = max(nxt.start, cur.end)
                if nxt.end < nxt.start + min_duration:
                    nxt.end = nxt.start + min_duration
            stats["fixed_overlap"] += 1

    final: List[Subtitle] = []
    for i, sub in enumerate(cleaned):
        if sub.end - sub.start < min_duration:
            limit = cleaned[i + 1].start if i + 1 < len(cleaned) else sub.start + min_duration
            new_end = min(sub.start + min_duration, limit)
            if new_end - sub.start < min_duration * 0.5:
                stats["dropped"] += 1
                continue
            sub.end = new_end
            stats["stretched"] += 1
        final.append(sub)

    for i, sub in enumerate(final):
        sub.id = f"sub{i:04d}"
    return final, stats


# ══════════════════════════════════════════════════════════════════════════
# 자막 생성
# ══════════════════════════════════════════════════════════════════════════
def build_subtitles(
    aligned: Sequence[Dict[str, Any]],
    cut_map: CutMap,
    *,
    glossary_terms: Sequence[str] = (),
    max_chars: int = line_break.DEFAULT_MAX_CHARS,
    min_duration: float = MIN_SUBTITLE_SEC,
) -> Tuple[List[Subtitle], Dict[str, Any]]:
    """정렬 결과 -> 편집본 타임라인 위의 한 줄 자막 목록.

    원본 시각을 컷 맵으로 편집본 시각으로 옮긴 뒤,
    한 줄 규칙으로 쪼개고, 겹침을 정리합니다.
    """
    subs: List[Subtitle] = []

    for item in aligned:
        text = apply_glossary(str(item.get("text", "")).strip(), glossary_terms)
        if not text:
            continue

        o_start = float(item.get("start", 0.0))
        o_end = float(item.get("end", 0.0))
        # 잘린 구간에 걸쳐 있으면 가장 가까운 남은 지점으로 당깁니다.
        e_start = cut_map.original_to_edited_clamped(o_start)
        e_end = cut_map.original_to_edited_clamped(o_end)
        if e_end <= e_start:
            e_end = e_start + min_duration

        pieces = line_break.break_text(text, max_chars)
        if not pieces:
            continue
        for piece, (ps, pe) in zip(pieces, line_break.distribute_time(
                pieces, e_start, e_end, min_duration=min_duration)):
            subs.append(Subtitle(
                id="", start=ps, end=pe, text=piece,
                source=str(item.get("source", "stt")),
                confidence=float(item.get("score", 0.0)) / 100.0 if item.get("score") else 1.0,
                original_start=o_start,
                note="" if item.get("source") == "script" else "즉흥 발화(STT)",
            ))

    # 1차 정리 (요청서 3.9: 자막을 만들 때)
    subs, stats = sanitize(subs, min_duration=min_duration)

    lines = [s.text for s in subs]
    analysis = line_break.analyze_lines(lines, max_chars)
    return subs, {"sanitize": stats, "lines": analysis}


# ══════════════════════════════════════════════════════════════════════════
# SRT
# ══════════════════════════════════════════════════════════════════════════
def _srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(subs: Sequence[Subtitle], *, min_duration: float = MIN_SUBTITLE_SEC) -> str:
    """SRT 문자열. 내보낼 때 **한 번 더** 정리합니다 (요청서 3.9)."""
    cleaned, stats = sanitize([Subtitle.from_dict(s.to_dict()) for s in subs],
                              min_duration=min_duration)
    if any(stats.values()):
        log.info("SRT 내보내기 정리: %s", stats)

    blocks: List[str] = []
    for i, sub in enumerate(cleaned, start=1):
        blocks.append(
            f"{i}\n{_srt_time(sub.start)} --> {_srt_time(sub.end)}\n{sub.text}\n"
        )
    return "\n".join(blocks)


def parse_srt(text: str) -> List[Subtitle]:
    """SRT 문자열 -> 자막 목록 (되읽기용)."""
    out: List[Subtitle] = []
    pattern = re.compile(
        r"(\d+)\s*\n(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n(.*?)(?=\n\s*\n|\Z)",
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        start = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4)) + int(m.group(5)) / 1000
        end = int(m.group(6)) * 3600 + int(m.group(7)) * 60 + int(m.group(8)) + int(m.group(9)) / 1000
        body = " ".join(line.strip() for line in m.group(10).strip().splitlines() if line.strip())
        out.append(Subtitle(id=f"sub{len(out):04d}", start=start, end=end, text=body))
    return out


def full_text(subs: Sequence[Subtitle]) -> str:
    """4차 마케팅 프롬프트에 넣을 자막 전문."""
    return " ".join(s.text for s in subs).strip()
