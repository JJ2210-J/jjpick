"""마케팅 문구 프롬프트 조립.

요청서 1절: 외부 LLM API는 사용하지 않습니다.
문구 생성이 필요한 곳은 **완성된 프롬프트 텍스트**를 만들어
클립보드 복사와 txt 저장으로 제공합니다.

요청서 6절 4차/5차의 지시를 그대로 반영합니다.
  · 사전 질문 3문항 + 자막 전문
  · 채널 정보, 타겟 시청자, 톤 가이드 포함
  · **"em dash나 별표 강조 같은 사람이 잘 쓰지 않는 표기를 쓰지 말 것"** 지시 포함
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

CHANNEL_DEFAULTS: Dict[str, str] = {
    "channel_name": "",
    "channel_topic": "한국 수출입 물류(포워딩) 실무와 업무 자동화",
    "audience": "포워딩·수출입 업무를 직접 하는 실무자 (경력 1~10년), "
                "반복 업무를 줄이고 싶어 하는 사람",
    "tone": "현장에서 바로 쓰는 실무 톤. 과장 없이, 근거와 절차 중심. "
            "초보자도 알아들을 수 있게 용어를 한 번씩 풀어 줌.",
}

# 요청서 4차: 사전 질문 3문항
QUESTIONS: List[Dict[str, str]] = [
    {"key": "takeaway", "label": "이번 영상에서 시청자가 가져가는 가장 큰 하나는?",
     "placeholder": "예: B/L 발행 전에 확인해야 할 3가지를 놓치면 Demurrage가 붙는다"},
    {"key": "situation", "label": "어떤 상황에 놓인 사람에게 필요한가? (직무, 업무 상황)",
     "placeholder": "예: 포워딩 오퍼레이션 담당, 수입 건 B/L 처리 중 실수한 적 있는 사람"},
    {"key": "tools", "label": "다루는 도구나 기능의 정확한 이름은?",
     "placeholder": "예: CargoWise, Claude, 엑셀 파워쿼리"},
]

# 사람이 잘 쓰지 않는 표기 금지 (요청서 4절 지시)
_STYLE_RULES = """
표기 규칙 (반드시 지킬 것):
- em dash(—)를 쓰지 마세요. 필요하면 쉼표나 마침표로 끊으세요.
- 별표(**)로 강조하지 마세요. 마크다운 서식을 쓰지 마세요.
- 사람이 실제로 쓰지 않는 번역투("~할 수 있습니다"의 남발, "~에 대해")를 피하세요.
- 이모지는 쓰지 마세요.
- 과장된 표현("충격", "역대급", "무조건")을 쓰지 마세요.
""".strip()


def _channel_block(channel: Dict[str, str]) -> str:
    merged = dict(CHANNEL_DEFAULTS)
    merged.update({k: v for k, v in (channel or {}).items() if v})
    name = merged["channel_name"] or "(채널 이름 미입력)"
    return (
        f"채널: {name}\n"
        f"다루는 주제: {merged['channel_topic']}\n"
        f"타겟 시청자: {merged['audience']}\n"
        f"톤 가이드: {merged['tone']}"
    )


def _answers_block(answers: Dict[str, str]) -> str:
    lines: List[str] = []
    for q in QUESTIONS:
        value = (answers or {}).get(q["key"], "").strip()
        lines.append(f"- {q['label']}\n  {value or '(미입력)'}")
    return "\n".join(lines)


def _trim(text: str, limit: int = 12000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    return f"{head}\n\n…(중략: 전체 {len(text)}자 중 일부만 실었습니다)…\n\n{tail}"


# ══════════════════════════════════════════════════════════════════════════
# 4차: 유튜브 제목 · 설명글 · 해시태그
# ══════════════════════════════════════════════════════════════════════════
def build_youtube_prompt(
    *,
    subtitle_text: str,
    answers: Dict[str, str],
    channel: Optional[Dict[str, str]] = None,
    chapters: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """유튜브 제목 2안 / 설명글(타임스탬프 목차 포함) / 해시태그 5개."""
    chapter_block = ""
    if chapters:
        lines = [f"{_hhmmss(c.get('start', 0))} {c.get('title', '')}".strip()
                 for c in chapters]
        chapter_block = "\n\n영상 안에서 뽑은 목차 후보 (시각은 그대로 쓰세요):\n" + "\n".join(lines)

    return f"""아래 영상의 유튜브 제목, 설명글, 해시태그를 만들어 주세요.

{_channel_block(channel or {})}

이 영상에 대해 제작자가 답한 것:
{_answers_block(answers)}
{chapter_block}

영상 자막 전문:
\"\"\"
{_trim(subtitle_text)}
\"\"\"

만들 것:

1. 제목 2안
   - 각 20자 내외 (공백 포함)
   - 1안은 문제/상황을 먼저 말하는 형태
   - 2안은 결과/해결을 먼저 말하는 형태
   - 낚시성 표현 금지. 영상에 실제로 없는 내용을 제목에 넣지 마세요.

2. 설명글
   - 첫 두 줄에 이 영상이 누구에게 왜 필요한지 씁니다 (미리보기에 잘리는 부분).
   - 그 아래 타임스탬프 목차를 넣습니다. 형식은 `00:00 항목명`.
     목차 항목은 5~8개, 각 항목명은 15자 이내.
   - 마지막에 관련 영상이나 구독 안내를 한 줄로 덧붙입니다.
   - 전체 길이는 800자 이내.

3. 해시태그 5개
   - 실제로 검색되는 말로. 너무 일반적인 단어(#정보, #꿀팁)는 피하세요.
   - 물류 실무 용어와 도구 이름을 섞으세요.

{_STYLE_RULES}
"""


def _hhmmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def suggest_chapters(subtitles: Sequence[Any], count: int = 6) -> List[Dict[str, Any]]:
    """자막을 균등하게 나눠 목차 후보 시각을 뽑습니다.

    제목은 LLM이 짓게 두고, 여기서는 **시각과 첫 문장**만 제공합니다.
    """
    items = [s for s in subtitles if getattr(s, "text", "").strip()]
    if not items:
        return []
    count = max(1, min(count, len(items)))
    step = max(1, len(items) // count)
    out: List[Dict[str, Any]] = []
    for i in range(0, len(items), step):
        sub = items[i]
        out.append({"start": getattr(sub, "start", 0.0), "title": getattr(sub, "text", "")[:20]})
        if len(out) >= count:
            break
    return out


# ══════════════════════════════════════════════════════════════════════════
# 5차: 세로 영상 상단/하단 문구
# ══════════════════════════════════════════════════════════════════════════
def build_vertical_prompt(
    *,
    clip_text: str,
    platform: str = "shorts",       # shorts | reels
    answers: Optional[Dict[str, str]] = None,
    channel: Optional[Dict[str, str]] = None,
    profile_link: str = "",
) -> str:
    """세로 영상의 상단/하단 문구 프롬프트.

    요청서 6절 5차: 쇼츠용 / 릴스용, **릴스는 프로필 링크 CTA 필수**.
    """
    is_reels = platform == "reels"
    platform_name = "인스타그램 릴스" if is_reels else "유튜브 쇼츠"

    cta_block = (
        "\n4. 프로필 링크 CTA (릴스는 필수)\n"
        "   - 인스타그램은 본문에 링크가 걸리지 않으므로 프로필 링크로 유도해야 합니다.\n"
        f"   - 유도할 링크: {profile_link or '(프로필 링크 미입력 — 자리만 잡아 두세요)'}\n"
        "   - 하단 문구 마지막 줄이나 캡션 끝에 자연스럽게 넣으세요.\n"
        "   - '프로필 링크 확인' 같은 상투적 표현 말고, 무엇을 얻는지 적으세요.\n"
    ) if is_reels else ""

    return f"""아래 세로 영상(20~30초)에 얹을 문구를 만들어 주세요. 플랫폼은 {platform_name}입니다.

{_channel_block(channel or {})}

{_answers_block(answers or {})}

이 클립의 자막:
\"\"\"
{_trim(clip_text, 3000)}
\"\"\"

만들 것:

1. 상단 문구 (화면 위쪽에 크게 들어갑니다)
   - 12자 이내. 스크롤을 멈추게 하는 한 줄.
   - 질문형이나 상황 제시형으로. 답을 먼저 말하지 마세요.

2. 하단 문구 (화면 아래쪽, 상단보다 작게)
   - 20자 이내. 상단 문구를 받아 무엇을 알려 주는지 말합니다.

3. 캡션 (게시물 본문)
   - 3줄 이내. 첫 줄에 이 영상이 누구에게 필요한지.
   - 해시태그 5개를 마지막 줄에.
{cta_block}
{_STYLE_RULES}

상단 문구와 하단 문구는 **화면에 그대로 올릴 것**이므로 따옴표나 부호로 감싸지 말고
글자만 주세요.
"""


def build_all(
    *,
    subtitle_text: str,
    answers: Dict[str, str],
    channel: Optional[Dict[str, str]] = None,
    chapters: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    return {
        "youtube": build_youtube_prompt(
            subtitle_text=subtitle_text, answers=answers,
            channel=channel, chapters=chapters,
        ),
    }
