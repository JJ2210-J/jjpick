"""컷 후보 생성, 원본↔편집본 타임코드 매핑, 컷 서명.

요청서의 두 함정이 여기 있습니다.

⚠ 3.17 컷 여유는 **앞뒤를 따로** 둡니다
    무음 구간의 시작 = 말이 끝난 직후, 끝 = 다음 말 시작 직전이라
    하나의 패딩 값으로 처리하면 안 됩니다.
        start = silence.start + tail_pad   (기본 0.35초 — 넉넉히)
        end   = silence.end   - head_pad   (기본 0.15초)
    silencedetect는 소리가 임계값 아래로 떨어지는 순간을 무음 시작으로 봅니다.
    말끝의 자음·여운은 이미 그 아래라, 꼬리 여유가 짧으면 마지막 음절이 잘려 들립니다.

⚠ 3.18 컷 서명
    자막 시각은 "지금 컷 설정" 기준으로 계산되는데 드래프트는 "만들 당시 컷 설정"입니다.
    드래프트를 만든 뒤 컷 선택을 바꾸고 자막만 넣으면 뒤로 갈수록 벌어집니다.
    그래서 드래프트를 만들 때 서명(남는 길이 + 구간 수)을 기록하고,
    자막을 넣기 전에 비교해 다르면 **차단**합니다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

CUT_SILENCE = "silence"
CUT_FILLER = "filler"
CUT_REPEAT = "repeat"

CUT_TYPE_LABELS = {
    CUT_SILENCE: "무음",
    CUT_FILLER: "필러",
    CUT_REPEAT: "반복",
}

# 이 길이 이상의 무음은 "화면만 보여주며 말을 안 하는 시연 구간"일 수 있습니다.
LONG_SILENCE_SEC = 3.0
CUT_RATIO_WARN = 0.40


# ══════════════════════════════════════════════════════════════════════════
@dataclass
class CutCandidate:
    id: str
    type: str
    start: float           # 원본(소스 타임라인) 기준 초
    end: float
    selected: bool = True
    reason: str = ""
    context_before: str = ""
    context_after: str = ""
    text: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "type_label": CUT_TYPE_LABELS.get(self.type, self.type),
            "start": round(self.start, 3), "end": round(self.end, 3),
            "duration": round(self.duration, 3), "selected": self.selected,
            "reason": self.reason, "text": self.text,
            "context_before": self.context_before, "context_after": self.context_after,
            "is_long_silence": self.type == CUT_SILENCE and self.duration >= LONG_SILENCE_SEC,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CutCandidate":
        return CutCandidate(
            id=str(d.get("id", "")), type=str(d.get("type", CUT_SILENCE)),
            start=float(d.get("start", 0.0)), end=float(d.get("end", 0.0)),
            selected=bool(d.get("selected", True)), reason=str(d.get("reason", "")),
            context_before=str(d.get("context_before", "")),
            context_after=str(d.get("context_after", "")),
            text=str(d.get("text", "")),
        )


# ══════════════════════════════════════════════════════════════════════════
# 후보 생성
# ══════════════════════════════════════════════════════════════════════════
def _norm_word(word: str) -> str:
    w = unicodedata.normalize("NFKC", word or "").strip().lower()
    return re.sub(r"[^0-9a-z가-힣]", "", w)


def _context(words: Sequence[Dict[str, Any]], start: float, end: float,
             span: float = 3.0, limit: int = 40) -> Tuple[str, str]:
    before = " ".join(
        str(w.get("word", "")).strip()
        for w in words if start - span <= float(w.get("end", 0)) <= start
    ).strip()
    after = " ".join(
        str(w.get("word", "")).strip()
        for w in words if end <= float(w.get("start", 0)) <= end + span
    ).strip()
    return before[-limit:], after[:limit]


def silence_candidates(
    silence_spans: Sequence[Tuple[float, float]],
    words: Sequence[Dict[str, Any]],
    *,
    tail_pad: float = 0.35,
    head_pad: float = 0.15,
    total_duration: float = 0.0,
) -> List[CutCandidate]:
    """무음 구간 -> 컷 후보. 앞뒤 여유를 **따로** 적용합니다 (3.17)."""
    out: List[CutCandidate] = []
    for i, (s, e) in enumerate(silence_spans):
        start = s + tail_pad          # 말 끝난 뒤 여유를 남기고 자르기 시작
        end = e - head_pad            # 다음 말 시작 전 여유를 남기고 자르기 끝
        if total_duration:
            end = min(end, total_duration)
        if end - start <= 0.05:
            # 여유를 빼고 나면 자를 게 없는 짧은 무음. 자연스러운 호흡이라 남깁니다.
            continue
        before, after = _context(words, s, e)
        out.append(CutCandidate(
            id=f"s{i}", type=CUT_SILENCE, start=start, end=end,
            reason=f"무음 {e - s:.2f}초 (여유 앞 {tail_pad:.2f} / 뒤 {head_pad:.2f} 적용)",
            context_before=before, context_after=after,
        ))
    return out


def filler_candidates(
    words: Sequence[Dict[str, Any]],
    filler_words: Sequence[str],
    *,
    pad: float = 0.05,
) -> List[CutCandidate]:
    """필러워드 -> 컷 후보.

    사전은 사용자가 편집합니다 (data/filler_words.json).
    여러 어절짜리 필러("그러니까 이제")도 잡기 위해 연속 어절을 함께 봅니다.
    """
    normalized = {}
    max_len = 1
    for phrase in filler_words:
        tokens = [_norm_word(t) for t in str(phrase).split() if _norm_word(t)]
        if not tokens:
            continue
        normalized[tuple(tokens)] = phrase
        max_len = max(max_len, len(tokens))

    out: List[CutCandidate] = []
    used: set[int] = set()
    n = len(words)
    for i in range(n):
        if i in used:
            continue
        for length in range(min(max_len, n - i), 0, -1):
            key = tuple(_norm_word(str(words[j].get("word", ""))) for j in range(i, i + length))
            if "" in key or key not in normalized:
                continue
            start = float(words[i].get("start", 0.0)) - pad
            end = float(words[i + length - 1].get("end", 0.0)) + pad
            if end <= start:
                break
            before, after = _context(words, start, end)
            out.append(CutCandidate(
                id=f"f{i}", type=CUT_FILLER, start=max(0.0, start), end=end,
                reason=f"필러워드 '{normalized[key]}'",
                text=" ".join(str(words[j].get("word", "")).strip() for j in range(i, i + length)),
                context_before=before, context_after=after,
            ))
            used.update(range(i, i + length))
            break
    return out


def repeat_candidates(
    words: Sequence[Dict[str, Any]],
    *,
    max_phrase: int = 3,
    pad: float = 0.05,
) -> List[CutCandidate]:
    """말더듬/반복 -> 컷 후보. **앞쪽 반복을 자르고 마지막 것을 남깁니다.**

    같은 어절이 바로 이어 나오거나("그 그 그"), 같은 2~3어절이 곧바로 반복되는 경우
    ("이거는 이거는")를 잡습니다. 자동 판정이라 기본 체크지만 검수 대상입니다.
    """
    out: List[CutCandidate] = []
    n = len(words)
    norms = [_norm_word(str(w.get("word", ""))) for w in words]
    i = 0
    while i < n:
        matched = False
        for length in range(min(max_phrase, (n - i) // 2), 0, -1):
            a = norms[i:i + length]
            b = norms[i + length:i + 2 * length]
            if not all(a) or a != b:
                continue
            # 반복이 몇 번 이어지는지 셉니다.
            reps = 1
            while norms[i + reps * length:i + (reps + 1) * length] == a:
                reps += 1
            # 마지막 반복만 남기고 앞쪽을 자릅니다.
            cut_start = float(words[i].get("start", 0.0)) - pad
            cut_end = float(words[i + (reps - 1) * length - 1].get("end", 0.0)) + pad
            if cut_end > cut_start:
                before, after = _context(words, cut_start, cut_end)
                phrase = " ".join(str(words[j].get("word", "")).strip()
                                  for j in range(i, i + length))
                out.append(CutCandidate(
                    id=f"r{i}", type=CUT_REPEAT, start=max(0.0, cut_start), end=cut_end,
                    reason=f"'{phrase}' {reps}회 반복 — 마지막 하나만 남김",
                    text=phrase, context_before=before, context_after=after,
                ))
            i += reps * length
            matched = True
            break
        if not matched:
            i += 1
    return out


def build_candidates(
    silence_spans: Sequence[Tuple[float, float]],
    words: Sequence[Dict[str, Any]],
    filler_words: Sequence[str],
    *,
    tail_pad: float = 0.35,
    head_pad: float = 0.15,
    total_duration: float = 0.0,
) -> List[CutCandidate]:
    """세 종류의 후보를 만들고 겹치는 것을 정리합니다.

    무음이 가장 확실한 근거라 우선하고, 무음 안에 완전히 들어가는
    필러/반복 후보는 중복이므로 버립니다.
    """
    silences = silence_candidates(
        silence_spans, words, tail_pad=tail_pad, head_pad=head_pad,
        total_duration=total_duration,
    )
    repeats = repeat_candidates(words)
    fillers = filler_candidates(words, filler_words)

    def inside_any(cand: CutCandidate, others: Sequence[CutCandidate]) -> bool:
        return any(o.start - 1e-6 <= cand.start and cand.end <= o.end + 1e-6 for o in others)

    # 무음이 가장 확실한 근거라 우선합니다.
    repeats = [c for c in repeats if not inside_any(c, silences)]
    # 반복 후보 안에 통째로 들어가는 필러는 같은 구간을 두 번 보여주는 셈이라 버립니다.
    # ("그 그 그"는 필러 3건이 아니라 반복 1건으로 보는 편이 검수하기 낫습니다)
    fillers = [c for c in fillers if not inside_any(c, silences) and not inside_any(c, repeats)]

    return sorted(silences + repeats + fillers, key=lambda c: (c.start, c.end))


# ══════════════════════════════════════════════════════════════════════════
# 컷 적용 + 타임코드 매핑
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class CutMap:
    """원본 ↔ 편집본 타임코드 상호 변환.

    요청서 6절 1차: "이 매핑은 2차와 5차에서 반드시 필요합니다."
    """
    total_duration: float
    kept: List[Tuple[float, float]] = field(default_factory=list)   # 원본 기준 남는 구간
    _edited_offsets: List[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        offsets, acc = [], 0.0
        for start, end in self.kept:
            offsets.append(acc)
            acc += max(0.0, end - start)
        self._edited_offsets = offsets

    # ── 정보 ──────────────────────────────────────────────────────────────
    @property
    def kept_duration(self) -> float:
        return sum(max(0.0, e - s) for s, e in self.kept)

    @property
    def removed_duration(self) -> float:
        return max(0.0, self.total_duration - self.kept_duration)

    @property
    def cut_ratio(self) -> float:
        return (self.removed_duration / self.total_duration) if self.total_duration else 0.0

    def signature(self) -> Dict[str, Any]:
        """컷 서명 (요청서 3.18). 남는 길이 + 구간 수.

        드래프트를 만들 때 세션에 기록하고, 자막을 넣기 전에 비교합니다.
        """
        return {
            "kept_duration": round(self.kept_duration, 3),
            "segment_count": len(self.kept),
        }

    # ── 변환 ──────────────────────────────────────────────────────────────
    def original_to_edited(self, t: float) -> Optional[float]:
        """원본 시각 -> 편집본 시각. 잘려나간 구간이면 None."""
        for i, (start, end) in enumerate(self.kept):
            if start <= t < end:
                return self._edited_offsets[i] + (t - start)
            if t < start:
                return None            # 잘려나간 구간 안
        if self.kept and t >= self.kept[-1][1]:
            return self.kept_duration
        return None

    def original_to_edited_clamped(self, t: float) -> float:
        """잘린 구간이면 가장 가까운 남은 지점으로 당깁니다 (자막 시각 계산용)."""
        exact = self.original_to_edited(t)
        if exact is not None:
            return exact
        for i, (start, end) in enumerate(self.kept):
            if t < start:
                return self._edited_offsets[i]
        return self.kept_duration

    def edited_to_original(self, t: float) -> float:
        """편집본 시각 -> 원본 시각."""
        if not self.kept:
            return t
        for i, (start, end) in enumerate(self.kept):
            length = end - start
            offset = self._edited_offsets[i]
            if offset <= t < offset + length:
                return start + (t - offset)
        return self.kept[-1][1]

    def edited_span_to_original(self, start: float, end: float) -> List[Tuple[float, float]]:
        """편집본 구간 -> 원본 구간 **목록**.

        ⚠ 요청서 5차 함정: 사용자가 고르는 구간은 컷 편집 **후** 시각인데
        실제로 잘라 쓸 소재는 **원본**입니다. 그 사이 잘려나간 컷이 있으면
        원본에서는 여러 조각이 됩니다. 그래서 리스트를 돌려줍니다.
        """
        out: List[Tuple[float, float]] = []
        for i, (k_start, k_end) in enumerate(self.kept):
            offset = self._edited_offsets[i]
            length = k_end - k_start
            seg_start = max(start, offset)
            seg_end = min(end, offset + length)
            if seg_end - seg_start <= 1e-6:
                continue
            out.append((k_start + (seg_start - offset), k_start + (seg_end - offset)))
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_duration": round(self.total_duration, 3),
            "kept": [[round(s, 3), round(e, 3)] for s, e in self.kept],
            "kept_duration": round(self.kept_duration, 3),
            "removed_duration": round(self.removed_duration, 3),
            "cut_ratio": round(self.cut_ratio, 4),
            "signature": self.signature(),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CutMap":
        return CutMap(
            total_duration=float(d.get("total_duration", 0.0)),
            kept=[(float(a), float(b)) for a, b in (d.get("kept") or [])],
        )


def build_cut_map(
    candidates: Sequence[CutCandidate],
    total_duration: float,
    *,
    min_keep: float = 0.05,
) -> CutMap:
    """선택된 후보를 잘라내고 남는 구간을 계산합니다."""
    cuts = sorted(
        ((c.start, c.end) for c in candidates if c.selected and c.end > c.start),
        key=lambda t: t[0],
    )

    merged: List[List[float]] = []
    for start, end in cuts:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    kept: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        start = max(0.0, min(start, total_duration))
        end = max(0.0, min(end, total_duration))
        if start - cursor > min_keep:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if total_duration - cursor > min_keep:
        kept.append((cursor, total_duration))

    return CutMap(total_duration=total_duration, kept=kept)


def cut_summary(candidates: Sequence[CutCandidate], cut_map: CutMap) -> Dict[str, Any]:
    """검수 화면 요약 + 40% 경고 (요청서 6절 1차)."""
    by_type: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        bucket = by_type.setdefault(c.type, {"total": 0, "selected": 0, "duration": 0.0})
        bucket["total"] += 1
        if c.selected:
            bucket["selected"] += 1
            bucket["duration"] += c.duration

    long_silences = [c for c in candidates
                     if c.type == CUT_SILENCE and c.selected and c.duration >= LONG_SILENCE_SEC]

    warnings: List[str] = []
    if cut_map.cut_ratio > CUT_RATIO_WARN:
        warnings.append(
            f"원본의 {cut_map.cut_ratio * 100:.1f}%가 잘려나갑니다. "
            "화면만 보여주며 말을 안 하는 시연 구간이 통째로 날아갔을 수 있습니다. "
            f"아래 '긴 무음({LONG_SILENCE_SEC:.0f}초 이상) 살리기'로 {len(long_silences)}개 구간을 되살릴 수 있습니다."
        )

    return {
        "by_type": {
            k: {"total": v["total"], "selected": v["selected"], "duration": round(v["duration"], 2)}
            for k, v in by_type.items()
        },
        "type_labels": CUT_TYPE_LABELS,
        "total_candidates": len(candidates),
        "selected_candidates": sum(1 for c in candidates if c.selected),
        "long_silence_count": len(long_silences),
        "cut_ratio": round(cut_map.cut_ratio, 4),
        "cut_ratio_warn": cut_map.cut_ratio > CUT_RATIO_WARN,
        "kept_duration": round(cut_map.kept_duration, 2),
        "removed_duration": round(cut_map.removed_duration, 2),
        "segment_count": len(cut_map.kept),
        "warnings": warnings,
    }


def revive_long_silences(candidates: List[CutCandidate],
                         threshold: float = LONG_SILENCE_SEC) -> int:
    """긴 무음 살리기. 되살린 개수를 돌려줍니다."""
    n = 0
    for c in candidates:
        if c.type == CUT_SILENCE and c.selected and c.duration >= threshold:
            c.selected = False
            n += 1
    return n


def set_selection_by_type(candidates: List[CutCandidate], cut_type: str, selected: bool) -> int:
    n = 0
    for c in candidates:
        if c.type == cut_type:
            c.selected = selected
            n += 1
    return n


# ══════════════════════════════════════════════════════════════════════════
# 3.18  컷 서명 비교
# ══════════════════════════════════════════════════════════════════════════
def compare_signatures(draft_sig: Optional[Dict[str, Any]],
                       current_sig: Dict[str, Any]) -> Dict[str, Any]:
    """드래프트를 만들 때의 컷 설정과 지금 컷 설정이 같은지 확인합니다 (3.18).

    다르면 자막이 앞은 맞고 뒤로 갈수록 벌어집니다. 사용자는 "자막 싱크가
    안 맞는다"고만 말하고 원인을 못 찾으므로 **도구가 먼저 잡아냅니다.**
    """
    if not draft_sig:
        return {
            "ok": False,
            "reason": "no_draft",
            "message": "1차 컷 편집으로 드래프트를 먼저 만들어야 자막을 넣을 수 있습니다.",
        }

    d_dur = float(draft_sig.get("kept_duration", 0.0))
    c_dur = float(current_sig.get("kept_duration", 0.0))
    d_cnt = int(draft_sig.get("segment_count", 0))
    c_cnt = int(current_sig.get("segment_count", 0))

    if abs(d_dur - c_dur) < 0.05 and d_cnt == c_cnt:
        return {"ok": True, "reason": "", "message": "", "draft": draft_sig, "current": current_sig}

    return {
        "ok": False,
        "reason": "signature_mismatch",
        "draft": draft_sig,
        "current": current_sig,
        "message": (
            "드래프트를 만든 뒤 컷 선택이 바뀌었습니다. 이대로 자막을 넣으면 "
            "앞부분만 맞고 뒤로 갈수록 어긋납니다.\n"
            f"  드래프트  : {d_dur:.1f}초 / {d_cnt}개 구간\n"
            f"  지금 설정 : {c_dur:.1f}초 / {c_cnt}개 구간   ← {abs(d_dur - c_dur):.1f}초 차이\n"
            "1차에서 드래프트를 다시 만들거나, 컷 선택을 드래프트 만들 때 상태로 되돌리세요."
        ),
    }
