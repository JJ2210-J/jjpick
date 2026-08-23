"""문장부호 기준 한 줄 자막.

요청서 6절 2차의 규칙을 그대로 구현합니다.
  · 자막은 **항상 한 줄** (max_lines = 1)
  · `. ! ? …`(문장 끝)과 `, · ; :`(구절)에서 끊고, 부호는 **앞 조각에 붙입니다**
  · 한 줄 상한 36자 (참조 드래프트 실측: 전부 1줄, 중앙값 12자 / 90% 18자 / 최대 38자)
  · 상한을 넘는 구절만 어절 단위로 더 쪼개되, **앞에서부터 꽉 채우지 않고
    필요한 조각 수를 먼저 계산해 고르게 나눕니다**
    (꽉 채우면 `[34자] + [5자]`처럼 짧은 꼬리가 생겨 화면을 스쳐 지나갑니다)
  · 한글은 어절 단위, 영문도 **하이픈 없이** 단어 단위로만 끊습니다
  · 부호만 있는 조각(`.`)은 자막으로 만들지 않습니다
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

# 문장 끝 / 구절 구분 부호
SENTENCE_END = ".!?…"
CLAUSE_MARKS = ",·;:"
ALL_MARKS = SENTENCE_END + CLAUSE_MARKS

DEFAULT_MAX_CHARS = 36

# 부호와 공백만으로 이루어진 조각 판정
_PUNCT_ONLY = re.compile(r"^[\s" + re.escape(ALL_MARKS) + r"\"'()\[\]~\-–—]*$")


def is_punctuation_only(text: str) -> bool:
    """부호만 있는 조각인지. 이런 건 자막으로 만들지 않고 앞 조각에 붙입니다."""
    return bool(_PUNCT_ONLY.match(text or ""))


def split_by_punctuation(text: str) -> List[str]:
    """문장부호에서 끊습니다. 부호는 앞 조각에 붙입니다.

    부호만 남는 조각은 앞 조각에 흡수시킵니다 (`.` 하나짜리 자막 방지).
    """
    text = (text or "").strip()
    if not text:
        return []

    chunks: List[str] = []
    buf: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        buf.append(ch)
        if ch in ALL_MARKS:
            # 연속 부호(`...`, `?!`)는 한 덩어리로 모아 원문을 보존합니다.
            i += 1
            while i < n and text[i] in ALL_MARKS:
                buf.append(text[i])
                i += 1
            chunk = "".join(buf).strip()
            if chunk:
                chunks.append(chunk)
            buf = []
        else:
            i += 1
    tail = "".join(buf).strip()
    if tail:
        chunks.append(tail)

    merged: List[str] = []
    for chunk in chunks:
        if merged and is_punctuation_only(chunk):
            prev = merged[-1]
            marks = chunk.strip()
            # 앞 조각이 이미 같은 부호로 끝나면 덧붙이지 않습니다 ('네.' + '.' -> '네..' 방지)
            if prev and prev[-1] in ALL_MARKS and set(marks) <= {prev[-1]}:
                continue
            merged[-1] = (prev + marks).strip()
        else:
            merged.append(chunk)

    return [c for c in merged if c and not is_punctuation_only(c)]


def _tokens(text: str) -> List[str]:
    """어절(공백) 단위로만 자릅니다. 한글도 영문도 단어 중간을 끊지 않습니다."""
    return [t for t in re.split(r"\s+", text.strip()) if t]


def _join(tokens: List[str]) -> str:
    return " ".join(tokens)


def _balanced_split(tokens: List[str], parts: int, max_chars: int) -> Optional[List[str]]:
    """토큰을 `parts`개로 **고르게** 나눕니다. 못 나누면 None.

    앞에서부터 꽉 채우는 greedy 대신, 목표 길이에서 벗어난 정도의 제곱합을
    최소화하는 DP를 씁니다. 요청서가 경고한 `[34자] + [5자]` 꼬리를 막습니다.
    """
    n = len(tokens)
    if parts <= 0 or parts > n:
        return None

    # prefix[i] = tokens[:i]를 공백으로 이었을 때 길이
    prefix = [0] * (n + 1)
    for i, tok in enumerate(tokens):
        prefix[i + 1] = prefix[i] + len(tok) + (1 if i > 0 else 0)

    total = prefix[n]
    target = total / parts

    INF = float("inf")
    # cost[k][i] = 앞 i개 토큰을 k조각으로 나눈 최소 비용
    cost = [[INF] * (n + 1) for _ in range(parts + 1)]
    back = [[-1] * (n + 1) for _ in range(parts + 1)]
    cost[0][0] = 0.0

    for k in range(1, parts + 1):
        for i in range(k, n + 1):
            for j in range(k - 1, i):
                if cost[k - 1][j] == INF:
                    continue
                seg_len = prefix[i] - prefix[j] - (1 if j > 0 else 0)
                if seg_len > max_chars:
                    continue
                c = cost[k - 1][j] + (seg_len - target) ** 2
                if c < cost[k][i]:
                    cost[k][i] = c
                    back[k][i] = j

    if cost[parts][n] == INF:
        return None

    out: List[str] = []
    i, k = n, parts
    while k > 0:
        j = back[k][i]
        out.append(_join(tokens[j:i]))
        i, k = j, k - 1
    out.reverse()
    return out


def split_to_lines(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> List[str]:
    """한 덩어리를 상한 이하의 **한 줄짜리** 조각들로 나눕니다."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    tokens = _tokens(text)
    if len(tokens) <= 1:
        # 어절 하나가 상한을 넘는 경우. 하이픈으로 자르지 않고 그대로 둡니다.
        return [text]

    parts = max(2, math.ceil(len(text) / max_chars))
    while parts <= len(tokens):
        result = _balanced_split(tokens, parts, max_chars)
        if result:
            return result
        parts += 1

    # 어떤 분할로도 상한을 못 맞추는 경우 (아주 긴 어절 포함) — 어절 단위로만 나눕니다.
    out, cur = [], ""
    for tok in tokens:
        cand = f"{cur} {tok}".strip()
        if cur and len(cand) > max_chars:
            out.append(cur)
            cur = tok
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def break_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> List[str]:
    """자막 텍스트 -> 한 줄 조각 목록. 부호 분할 + 상한 분할을 함께 적용합니다."""
    out: List[str] = []
    for chunk in split_by_punctuation(text):
        out.extend(split_to_lines(chunk, max_chars))
    return [o for o in out if o and not is_punctuation_only(o)]


def distribute_time(
    pieces: List[str],
    start: float,
    end: float,
    *,
    min_duration: float = 0.35,
) -> List[Tuple[float, float]]:
    """조각들에 시간을 글자 수에 비례해 나눠 줍니다.

    구간이 짧아 최소 길이를 못 맞추면 조각 수를 줄이는 대신
    **겹치지 않게** 균등 분배합니다 (겹치면 pyCapCut이 거부합니다 — 요청서 3.9).
    """
    if not pieces:
        return []
    span = max(0.0, end - start)
    if len(pieces) == 1:
        return [(start, max(start + min_duration, end))]

    weights = [max(1, len(p)) for p in pieces]
    total_w = sum(weights)

    # 최소 길이를 다 주고도 남는 시간이 있는지 확인합니다.
    needed = min_duration * len(pieces)
    if span < needed:
        step = span / len(pieces) if span > 0 else min_duration
        return [(start + i * step, start + (i + 1) * step) for i in range(len(pieces))]

    spare = span - needed
    out: List[Tuple[float, float]] = []
    cursor = start
    for i, w in enumerate(weights):
        dur = min_duration + spare * (w / total_w)
        seg_end = cursor + dur if i < len(weights) - 1 else end
        out.append((cursor, seg_end))
        cursor = seg_end
    return out


def analyze_lines(lines: List[str], max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, object]:
    """검수 화면에 띄울 통계. 완료 기준(모든 자막 한 줄, 상한 준수) 확인용."""
    if not lines:
        return {"count": 0, "over_limit": 0, "multiline": 0, "max_len": 0, "all_single_line": True}
    lengths = [len(l) for l in lines]
    return {
        "count": len(lines),
        "over_limit": sum(1 for l in lengths if l > max_chars),
        "multiline": sum(1 for l in lines if "\n" in l),
        "max_len": max(lengths),
        "median_len": sorted(lengths)[len(lengths) // 2],
        "all_single_line": all("\n" not in l for l in lines),
    }
