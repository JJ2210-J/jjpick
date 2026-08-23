"""캡컷 드래프트 읽기·쓰기·백업·캘리브레이션·레이어 교정.

요청서 3절의 함정 대부분이 이 파일에서 처리됩니다.
  3.2  정규화 좌표 (y 위쪽 양수)
  3.3  track_render_index — 실물은 "트랙에 없고 세그먼트에 0" (docs/investigation.md 참조)
  3.4  check_flag 비트마스크 검증
  3.5  캡컷 원본 텍스트 소재 통째 보존 → 텍스트만 교체
  3.6  폰트 절대경로 주입 (계열 이름이 맞아야 통과)
  3.7  트랜지션 effect_id 역조회
  3.10 canvas_config.ratio 교정
  3.11 draft_meta_info.json 경로 덮어쓰기
  3.12 root_meta_info.json 레지스트리 등록
  3.13 캡컷 실행 감지
"""

from __future__ import annotations

import json
import math
import re
import shutil
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import (
    BACKUP_DIR,
    CAPCUT_PROCESS_NAMES,
    IMAGE_EXTS,
    STYLE_PROFILE_PATH,
    VERIFIED_PYCAPCUT_VERSION,
    list_installed_fonts,
    write_json_atomic,
)
from .logging_util import get_logger

log = get_logger()

DRAFT_CONTENT = "draft_content.json"
DRAFT_META = "draft_meta_info.json"
ROOT_META = "root_meta_info.json"


class DraftError(RuntimeError):
    """드래프트 작업 실패. 메시지는 그대로 사용자에게 보여집니다."""


# ══════════════════════════════════════════════════════════════════════════
# 3.13  캡컷 실행 감지
# ══════════════════════════════════════════════════════════════════════════
def running_capcut_processes() -> List[str]:
    """실행 중인 캡컷 프로세스명 목록.

    캡컷은 멀티프로세스라 7~8개가 뜹니다. 개수는 무의미하고
    "하나라도 있으면 차단"이 규칙입니다 (요청서 3.13).
    """
    try:
        import psutil
    except ImportError:
        log.warning("psutil이 없어 캡컷 실행 감지를 건너뜁니다. pip install psutil 권장.")
        return []

    found: List[str] = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except Exception:  # noqa: BLE001 - 죽은 프로세스는 조용히 건너뜁니다
            continue
        if not name:
            continue
        stem = name[:-4] if name.endswith(".exe") else name
        if stem in CAPCUT_PROCESS_NAMES:
            found.append(name)
    return found


def capcut_guard(stage: str = "이 작업") -> None:
    """캡컷이 켜져 있으면 예외를 던져 작업을 차단합니다.

    캡컷은 프로젝트를 메모리에 들고 있다가 저장할 때 파일을 통째로 덮어씁니다.
    켜진 상태로 JSON을 고치면 작업이 사라집니다.
    """
    procs = running_capcut_processes()
    if procs:
        raise DraftError(
            f"캡컷이 실행 중이라 {stage}을(를) 진행할 수 없습니다.\n"
            f"감지된 프로세스: {', '.join(sorted(set(procs)))}\n"
            "캡컷을 완전히 종료한 뒤 다시 시도하세요. "
            "(켜진 상태로 드래프트를 고치면 캡컷이 저장할 때 작업이 사라집니다)"
        )


def capcut_reopened_warning() -> Optional[str]:
    """작업 직후 재검사용. 그 사이 캡컷이 켜졌으면 경고 문구를 돌려줍니다."""
    procs = running_capcut_processes()
    if not procs:
        return None
    return (
        "작업하는 사이에 캡컷이 실행됐습니다. "
        "지금 캡컷에 열려 있는 프로젝트를 저장하면 방금 만든 내용이 덮어써질 수 있습니다. "
        "캡컷을 저장하지 말고 완전히 종료한 뒤 다시 여세요."
    )


# ══════════════════════════════════════════════════════════════════════════
# 백업 (요청서 2번 원칙 · 7절)
# ══════════════════════════════════════════════════════════════════════════
def backup_draft(draft_dir: Path, tag: str = "") -> Path:
    """드래프트 폴더 전체를 backups/{프로젝트}_{타임스탬프}/로 복사합니다."""
    draft_dir = Path(draft_dir)
    if not draft_dir.is_dir():
        raise DraftError(f"드래프트 폴더를 찾을 수 없습니다: {draft_dir}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{tag}" if tag else ""
    dest = BACKUP_DIR / f"{draft_dir.name}_{stamp}{suffix}"
    n = 1
    while dest.exists():
        n += 1
        dest = BACKUP_DIR / f"{draft_dir.name}_{stamp}{suffix}_{n}"

    shutil.copytree(draft_dir, dest)
    log.info("백업 생성: %s", dest)
    return dest


def list_backups(draft_name: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not BACKUP_DIR.is_dir():
        return out
    for p in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        if draft_name and not p.name.startswith(draft_name + "_"):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append({"name": p.name, "path": str(p), "modified": mtime})
    return out


def restore_backup(backup_path: Path, draft_dir: Path) -> None:
    """되돌리기. 복원 전에 현재 상태도 백업해 둡니다 (되돌리기의 되돌리기)."""
    backup_path, draft_dir = Path(backup_path), Path(draft_dir)
    if not (backup_path / DRAFT_CONTENT).is_file():
        raise DraftError(f"백업이 온전하지 않습니다 ({DRAFT_CONTENT} 없음): {backup_path}")
    capcut_guard("되돌리기")

    if draft_dir.exists():
        backup_draft(draft_dir, tag="before_restore")
        shutil.rmtree(draft_dir)
    shutil.copytree(backup_path, draft_dir)
    log.info("백업 복원: %s -> %s", backup_path, draft_dir)


# ══════════════════════════════════════════════════════════════════════════
# 드래프트 읽기 / 쓰기
# ══════════════════════════════════════════════════════════════════════════
def read_draft_content(draft_dir: Path) -> Dict[str, Any]:
    path = Path(draft_dir) / DRAFT_CONTENT
    if not path.is_file():
        raise DraftError(f"{DRAFT_CONTENT}을(를) 찾을 수 없습니다: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise DraftError(f"{DRAFT_CONTENT} 파싱 실패 ({exc}). 백업에서 되돌리세요.") from exc


def write_draft_content(draft_dir: Path, data: Dict[str, Any]) -> None:
    write_json_atomic(Path(draft_dir) / DRAFT_CONTENT, data)


# ══════════════════════════════════════════════════════════════════════════
# 3.3  track_render_index 교정
# ══════════════════════════════════════════════════════════════════════════
def fix_track_render_index(data: Dict[str, Any]) -> Dict[str, int]:
    """트랙 순서대로 track_render_index를 다시 매깁니다.

    실측(docs/investigation.md 1~2절):
      · pycapcut 0.0.3의 **트랙 dict에는 track_render_index 키가 아예 없습니다.**
      · 대신 **모든 세그먼트**에 `track_render_index: 0`이 하드코딩돼 나갑니다
        (segment.py:68). 캡컷은 이 값을 보고 레이어를 정하므로,
        영상과 텍스트가 같은 레이어가 되어 자막이 가려집니다.

    그래서 트랙과 세그먼트 **양쪽 모두** 고쳐야 합니다.
    세그먼트를 빼먹으면 파일에는 자막이 있는데 화면에는 안 보입니다.
    """
    tracks = data.get("tracks") or []
    changed = {"tracks": 0, "segments": 0}
    for idx, track in enumerate(tracks):
        if track.get("track_render_index") != idx:
            track["track_render_index"] = idx
            changed["tracks"] += 1
        for seg in track.get("segments") or []:
            if seg.get("track_render_index") != idx:
                seg["track_render_index"] = idx
                changed["segments"] += 1
    if changed["tracks"] or changed["segments"]:
        log.info("track_render_index 교정: 트랙 %d개 / 세그먼트 %d개",
                 changed["tracks"], changed["segments"])
    return changed


# ══════════════════════════════════════════════════════════════════════════
# 3.10  canvas_config.ratio 교정
# ══════════════════════════════════════════════════════════════════════════
_KNOWN_RATIOS: List[Tuple[float, str]] = [
    (9 / 16, "9:16"), (16 / 9, "16:9"), (1.0, "1:1"),
    (4 / 3, "4:3"), (3 / 4, "3:4"), (21 / 9, "21:9"), (2 / 3, "2:3"), (3 / 2, "3:2"),
]


def ratio_for(width: int, height: int) -> str:
    """캡컷 실물이 쓰는 ratio 문자열. 아는 비율이 없으면 'original'."""
    if not width or not height:
        return "original"
    r = width / height
    for value, name in _KNOWN_RATIOS:
        if math.isclose(r, value, rel_tol=0.01):
            return name
    return "original"


def fix_canvas_ratio(data: Dict[str, Any]) -> Optional[str]:
    """dumps()가 항상 'original'로 쓰는 것을 실제 비율로 교정합니다 (3.10).

    소스 확인: script_file.py:778 에 "ratio": "original" 이 하드코딩돼 있습니다.
    """
    canvas = data.get("canvas_config") or {}
    width, height = canvas.get("width"), canvas.get("height")
    correct = ratio_for(width, height)
    if canvas.get("ratio") != correct:
        canvas["ratio"] = correct
        data["canvas_config"] = canvas
        log.info("canvas_config.ratio 교정: %sx%s -> %s", width, height, correct)
        return correct
    return None


# ══════════════════════════════════════════════════════════════════════════
# 3.11  draft_meta_info.json 경로 교정
# ══════════════════════════════════════════════════════════════════════════
def fix_draft_meta_info(draft_dir: Path) -> None:
    """draft_fold_path / draft_name을 실제 폴더 기준으로 덮어씁니다.

    캡컷에서 프로젝트 이름을 바꿔도 이 값들이 갱신되지 않고,
    pyCapCut의 duplicate_as_template도 고쳐주지 않습니다 (3.11).
    """
    draft_dir = Path(draft_dir)
    meta_path = draft_dir / DRAFT_META
    meta: Dict[str, Any] = {}
    if meta_path.is_file():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("%s를 읽지 못해 새로 만듭니다: %s", DRAFT_META, meta_path)
            meta = {}

    # 캡컷은 슬래시 경로를 씁니다.
    fold_path = str(draft_dir).replace("\\", "/")
    meta["draft_fold_path"] = fold_path
    meta["draft_name"] = draft_dir.name
    meta["draft_root_path"] = str(draft_dir.parent).replace("\\", "/")
    if not meta.get("draft_id"):
        meta["draft_id"] = str(uuid.uuid4()).upper()
    meta["tm_draft_modified"] = int(time.time() * 1_000_000)

    write_json_atomic(meta_path, meta)
    log.info("%s 경로 교정 완료: %s", DRAFT_META, fold_path)


# ══════════════════════════════════════════════════════════════════════════
# 3.12  root_meta_info.json 레지스트리 등록
# ══════════════════════════════════════════════════════════════════════════
def register_in_root_meta(draft_root: Path, draft_dir: Path) -> bool:
    """드래프트를 캡컷 프로젝트 목록에 등록합니다.

    pyCapCut의 create_draft는 폴더 생성 + 메타 템플릿 복사만 하고
    root_meta_info.json의 all_draft_store에는 등록하지 않습니다 (3.12).

    엔트리 형식을 추측하지 않기 위해, **기존 엔트리 하나를 그대로 복제해서
    식별 필드만 갈아끼웁니다.** 기존 엔트리가 하나도 없으면 최소 형태로 만들되
    그 사실을 로그에 남깁니다 (이 경로는 미검증).
    """
    draft_root, draft_dir = Path(draft_root), Path(draft_dir)
    root_path = draft_root / ROOT_META
    if not root_path.is_file():
        log.warning("%s가 없어 레지스트리 등록을 건너뜁니다: %s", ROOT_META, root_path)
        return False

    try:
        with open(root_path, "r", encoding="utf-8") as f:
            root = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("%s를 읽지 못해 등록을 건너뜁니다: %s", ROOT_META, exc)
        return False

    store = root.get("all_draft_store")
    if not isinstance(store, list):
        store = []
        root["all_draft_store"] = store

    fold_path = str(draft_dir).replace("\\", "/")
    json_file = str(draft_dir / DRAFT_CONTENT).replace("\\", "/")

    # 이미 등록돼 있으면 갱신만 합니다.
    for entry in store:
        if not isinstance(entry, dict):
            continue
        existing = str(entry.get("draft_fold_path", "")).replace("\\", "/")
        if existing.rstrip("/") == fold_path.rstrip("/") or entry.get("draft_name") == draft_dir.name:
            entry["draft_fold_path"] = fold_path
            entry["draft_json_file"] = json_file
            entry["draft_name"] = draft_dir.name
            entry["tm_draft_modified"] = int(time.time() * 1_000_000)
            write_json_atomic(root_path, root)
            log.info("레지스트리 엔트리 갱신: %s", draft_dir.name)
            return True

    now_us = int(time.time() * 1_000_000)
    meta_id = str(uuid.uuid4()).upper()

    template = next((e for e in store if isinstance(e, dict)), None)
    if template is not None:
        # 기존 엔트리의 키 구성을 그대로 따라갑니다 (형식을 추측하지 않기 위해).
        entry = json.loads(json.dumps(template))
        entry.update({
            "draft_id": meta_id,
            "draft_name": draft_dir.name,
            "draft_fold_path": fold_path,
            "draft_json_file": json_file,
            "tm_draft_create": now_us,
            "tm_draft_modified": now_us,
        })
        for key in ("draft_cover", "draft_cloud_last_action_download",
                    "draft_cloud_purchase_info", "draft_cloud_template_id"):
            if key in entry:
                entry[key] = "" if isinstance(entry[key], str) else entry[key]
    else:
        log.warning("기존 레지스트리 엔트리가 없어 최소 형태로 등록합니다 (미검증 경로).")
        entry = {
            "draft_id": meta_id,
            "draft_name": draft_dir.name,
            "draft_fold_path": fold_path,
            "draft_json_file": json_file,
            "draft_removable": True,
            "draft_enable_cache": True,
            "tm_draft_create": now_us,
            "tm_draft_modified": now_us,
        }

    store.insert(0, entry)
    write_json_atomic(root_path, root)
    log.info("레지스트리 등록 완료: %s", draft_dir.name)
    return True


# ══════════════════════════════════════════════════════════════════════════
# 색 변환 (pyCapCut은 타입마다 표현이 다릅니다 — investigation.md 1절)
# ══════════════════════════════════════════════════════════════════════════
def hex_to_rgb_tuple(value: str) -> Tuple[float, float, float]:
    """'#RRGGBB' -> (r, g, b) 0~1 실수 튜플. TextStyle/TextBorder용."""
    s = (value or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return (1.0, 1.0, 1.0)
    try:
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (1.0, 1.0, 1.0)


def rgb_tuple_to_hex(rgb: Any) -> str:
    try:
        r, g, b = (max(0.0, min(1.0, float(c))) for c in rgb)
    except (TypeError, ValueError):
        return "#ffffff"
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


# ══════════════════════════════════════════════════════════════════════════
# 3.2  정규화 좌표
# ══════════════════════════════════════════════════════════════════════════
def px_to_norm_y(y_px: float, canvas_height: int) -> float:
    """픽셀 Y -> 정규화 Y. 화면 중앙이 0, **위쪽이 양수**입니다.

    요청서 3.2의 함정: 4K 기준 -788을 1080 캔버스에 그대로 넣으면
    -788/540 = -1.459 로 화면 밖입니다.
    """
    if not canvas_height:
        return 0.0
    return y_px / (canvas_height / 2.0)


def norm_y_to_px(y_norm: float, canvas_height: int) -> float:
    return y_norm * (canvas_height / 2.0)


def rescale_norm_y(y_norm: float, from_height: int, to_height: int) -> float:
    """정규화 좌표는 캔버스 높이에 대해 이미 정규화돼 있으므로 값이 그대로 유지됩니다.

    (같은 '화면상 위치'를 뜻합니다. 픽셀값을 옮길 때만 환산이 필요합니다.)
    """
    return y_norm


# ══════════════════════════════════════════════════════════════════════════
# 3.6  폰트 절대경로 탐색
# ══════════════════════════════════════════════════════════════════════════
_WEIGHT_TOKENS = [
    "extrabold", "ultrabold", "semibold", "demibold", "extralight", "ultralight",
    "black", "heavy", "bold", "semilight", "medium", "regular", "normal",
    "light", "thin", "italic", "oblique",
]


def _normalize_font_token(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def split_font_name(name: str) -> Tuple[str, str]:
    """'Pretendard-Bold' -> ('pretendard', 'bold'). 무게가 없으면 ('...', '')."""
    norm = _normalize_font_token(name)
    for token in _WEIGHT_TOKENS:
        if norm.endswith(token) and len(norm) > len(token):
            return norm[: -len(token)], token
        if norm.startswith(token) and len(norm) > len(token):
            return norm[len(token):], token
    return norm, ""


def find_font_file(font_name: str) -> Optional[str]:
    """글꼴 이름으로 설치된 폰트 파일의 절대경로를 찾습니다 (3.6).

    요청서 경고: "Bold"만 맞아도 통과시키면 `Arial Bold` -> `NotoSansKR-Bold`
    같은 오매칭이 납니다. **글꼴 계열 이름이 반드시 맞아야** 합니다.
    """
    if not font_name:
        return None

    want_family, want_weight = split_font_name(font_name)
    if not want_family:
        return None

    exact: List[str] = []
    family_only: List[str] = []

    for font in list_installed_fonts():
        cand_family, cand_weight = split_font_name(font["stem"])
        if not cand_family:
            continue
        # 계열 이름이 일치(또는 한쪽이 다른 쪽을 포함)해야만 후보입니다.
        if not (cand_family == want_family
                or cand_family.startswith(want_family)
                or want_family.startswith(cand_family)):
            continue
        if want_weight and cand_weight == want_weight:
            exact.append(font["path"])
        elif not want_weight and cand_weight in ("", "regular", "normal"):
            exact.append(font["path"])
        else:
            family_only.append(font["path"])

    if exact:
        return sorted(exact, key=len)[0]
    if family_only:
        log.warning("글꼴 '%s'의 무게가 정확히 맞는 파일이 없어 같은 계열로 대체합니다.", font_name)
        return sorted(family_only, key=len)[0]
    return None


def apply_font_path(material: Dict[str, Any], font_path: str, font_name: str = "") -> None:
    """텍스트 소재에 폰트 절대경로를 주입합니다 (3.6).

    pyCapCut은 폰트를 resource_id로만 참조하고 path엔 더미 문자열('C:/이름.ttf')을 씁니다.
    캡컷 실물은 font_path에 절대경로를 쓰고, content.styles[0].font.path에도 같은 경로가
    들어가야 합니다. 두 곳 모두 채웁니다.
    """
    if not font_path:
        return
    normalized = str(font_path).replace("\\", "/")
    material["font_path"] = normalized
    if font_name:
        material["font_name"] = font_name
        material["font_title"] = font_name

    content = material.get("content")
    if not isinstance(content, str):
        return
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return
    styles = parsed.get("styles")
    if isinstance(styles, list):
        for style in styles:
            font = style.get("font")
            if not isinstance(font, dict):
                font = {}
                style["font"] = font
            font["path"] = normalized
            if font_name:
                font["id"] = font.get("id", "")
    material["content"] = json.dumps(parsed, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════
# 3.5  캘리브레이션 — 캡컷 원본 소재를 통째로 저장
# ══════════════════════════════════════════════════════════════════════════
def _material_index(data: Dict[str, Any], kind: str) -> Dict[str, Dict[str, Any]]:
    return {m.get("id"): m for m in (data.get("materials", {}).get(kind) or []) if isinstance(m, dict)}


def _text_of(material: Dict[str, Any]) -> str:
    try:
        return json.loads(material.get("content") or "{}").get("text", "")
    except json.JSONDecodeError:
        return ""


def calibrate_from_draft(draft_dir: Path) -> Dict[str, Any]:
    """참조 드래프트에서 자막 스타일과 트랜지션을 추출합니다.

    요청서 3.5: 빈 껍데기에 스타일만 얹으면 안 됩니다.
    **캡컷 원본 소재를 통째로(모든 필드) 저장**해 두고, 나중에 텍스트만 갈아끼웁니다.
    """
    draft_dir = Path(draft_dir)
    data = read_draft_content(draft_dir)

    canvas = data.get("canvas_config") or {}
    width = int(canvas.get("width") or 0)
    height = int(canvas.get("height") or 0)

    texts = _material_index(data, "texts")
    samples: List[Dict[str, Any]] = []

    for track in data.get("tracks") or []:
        if track.get("type") != "text":
            continue
        for seg in track.get("segments") or []:
            material = texts.get(seg.get("material_id"))
            if not material:
                continue
            samples.append({
                "material": json.loads(json.dumps(material)),   # 깊은 복사 = 전체 필드 보존
                "clip": json.loads(json.dumps(seg.get("clip") or {})),
                "text": _text_of(material),
                "field_count": len(material),
            })

    if not samples:
        raise DraftError(
            "참조 드래프트에서 자막을 찾지 못했습니다.\n"
            "캡컷에서 자막이 들어간 프로젝트를 고르세요. "
            "(자막 트랙이 하나도 없는 드래프트로는 스타일을 가져올 수 없습니다)"
        )

    # 가장 필드가 많은 소재를 대표로 씁니다 = 캡컷이 가장 온전하게 채운 것.
    samples.sort(key=lambda s: s["field_count"], reverse=True)
    primary = samples[0]
    material = primary["material"]

    # 위치는 위/아래를 나눠 둡니다 (요청서 3.2의 상단 +0.6338 / 하단 -0.7394).
    positions: List[float] = []
    for s in samples:
        ty = ((s["clip"] or {}).get("transform") or {}).get("y")
        if isinstance(ty, (int, float)):
            positions.append(float(ty))
    top_y = max(positions) if positions else 0.6338
    bottom_y = min(positions) if positions else -0.7394

    font_path = material.get("font_path") or ""
    font_name = material.get("font_name") or material.get("font_title") or ""
    if not font_path:
        try:
            parsed = json.loads(material.get("content") or "{}")
            font_path = (parsed.get("styles") or [{}])[0].get("font", {}).get("path", "")
        except (json.JSONDecodeError, IndexError, AttributeError):
            font_path = ""
    # 더미 경로('C:/이름.ttf')는 실제 파일이 아니므로 폰트 재탐색합니다.
    if font_path and not Path(font_path).is_file():
        resolved = find_font_file(font_name or Path(font_path).stem)
        if resolved:
            font_path = resolved

    profile: Dict[str, Any] = {
        "source_draft": str(draft_dir),
        "source_draft_name": draft_dir.name,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "canvas": {"width": width, "height": height},
        "sample_count": len(samples),
        "material_field_count": len(material),
        # 요청서 3.5 처방: 원본 소재 전체를 그대로 보관합니다.
        "material_template": material,
        "clip_template": primary["clip"],
        "position": {"top_y": top_y, "bottom_y": bottom_y},
        "font": {"name": font_name, "path": font_path},
        "readable": {
            "font_size": material.get("font_size"),
            "check_flag": material.get("check_flag"),
            "background_style": material.get("background_style"),
            "background_color": material.get("background_color"),
            "background_alpha": material.get("background_alpha"),
            "background_width": material.get("background_width"),
            "background_height": material.get("background_height"),
            "background_round_radius": material.get("background_round_radius"),
            "alignment": material.get("alignment"),
        },
        "estimated": False,
        "transitions": extract_transitions(data),
        "stats": subtitle_stats(samples),
    }
    return profile


def subtitle_stats(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """참조 드래프트의 자막 길이 분포. 요청서 6절 36자 상한의 근거를 사용자에게 보여줍니다."""
    lengths = sorted(len(s["text"]) for s in samples if s["text"])
    line_counts = [s["text"].count("\n") + 1 for s in samples if s["text"]]
    if not lengths:
        return {"count": 0}

    def pct(p: float) -> int:
        idx = min(len(lengths) - 1, max(0, int(round(p * (len(lengths) - 1)))))
        return lengths[idx]

    return {
        "count": len(lengths),
        "median": pct(0.5),
        "p90": pct(0.9),
        "max": lengths[-1],
        "all_single_line": all(c == 1 for c in line_counts),
        "max_lines": max(line_counts) if line_counts else 1,
    }


def extract_transitions(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """드래프트에서 트랜지션 effect_id를 뽑습니다 (3.7).

    이름(중국어)이 아니라 effect_id로만 역조회합니다.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for tr in (data.get("materials", {}).get("transitions") or []):
        if not isinstance(tr, dict):
            continue
        eid = str(tr.get("effect_id") or "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        out.append({
            "effect_id": eid,
            "resource_id": str(tr.get("resource_id") or ""),
            "raw_name": tr.get("name") or "",
            "duration": tr.get("duration"),
            "enum_name": lookup_transition_name(eid),
        })
    return out


def lookup_transition_name(effect_id: str) -> Optional[str]:
    """effect_id -> pyCapCut TransitionType 멤버 이름 (3.7).

    실측: 1137개 멤버, effect_id 중복 0건이라 역조회가 안전합니다.
    """
    try:
        from pycapcut.metadata.transition_meta import TransitionType
    except ImportError:
        return None
    target = str(effect_id)
    for member in TransitionType:
        if str(member.value.effect_id) == target:
            return member.name
    return None


def list_all_transitions() -> List[Dict[str, str]]:
    """전체 트랜지션 목록 (드롭다운용). 이름이 중국어라 effect_id를 함께 노출합니다."""
    try:
        from pycapcut.metadata.transition_meta import TransitionType
    except ImportError:
        return []
    return [
        {"name": m.name, "effect_id": str(m.value.effect_id),
         "default_duration": str(getattr(m.value, "default_duration", "") or "")}
        for m in TransitionType
    ]


def manual_style_profile(
    *,
    canvas_width: int,
    canvas_height: int,
    font_name: str = "Pretendard-Bold",
    font_size: float = 5.0,
    text_color: str = "#000000",
    background_color: str = "#ffffff",
    bottom_y: float = -0.7394,
    top_y: float = 0.6338,
) -> Dict[str, Any]:
    """수동 입력 폴백 (요청서: 추정값임을 명확히 표시).

    요청서가 반드시 채우라고 한 세 가지를 여기서 보장합니다.
      · background_style = 1  (0이면 배경이 안 나옵니다)
      · check_flag 배경 비트 (7 + 8 테두리 + 16 배경 = 31)
      · 폰트 파일 절대경로
    """
    font_path = find_font_file(font_name) or ""
    material = {
        "id": uuid.uuid4().hex,
        "type": "text",
        "alignment": 1,
        "background_color": background_color,
        "background_style": 1,          # 반드시 1 이상
        "background_alpha": 1.0,
        "background_width": 0.28,
        "background_height": 0.28,
        "background_round_radius": 0.4,
        "background_horizontal_offset": 0.0,
        "background_vertical_offset": 0.0,
        "check_flag": 7 | 8 | 16,       # = 31
        "font_size": font_size,
        "font_name": font_name,
        "font_path": font_path.replace("\\", "/"),
        "force_apply_line_max_width": False,
        "global_alpha": 1.0,
        "letter_spacing": 0.0,
        "line_feed": 1,
        "line_max_width": 0.82,
        "line_spacing": 0.02,
        "typesetting": 0,
        "content": json.dumps({
            "styles": [{
                "fill": {"alpha": 1.0, "content": {
                    "render_type": "solid",
                    "solid": {"alpha": 1.0, "color": list(hex_to_rgb_tuple(text_color))},
                }},
                "range": [0, 1],
                "size": font_size,
                "bold": True, "italic": False, "underline": False,
                "strokes": [{"content": {"solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}},
                             "width": 0.08}],
                "font": {"id": "", "path": font_path.replace("\\", "/")},
            }],
            "text": " ",
        }, ensure_ascii=False),
    }
    return {
        "source_draft": "",
        "source_draft_name": "(수동 입력)",
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "canvas": {"width": canvas_width, "height": canvas_height},
        "sample_count": 0,
        "material_field_count": len(material),
        "material_template": material,
        "clip_template": {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
                          "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
                          "transform": {"x": 0.0, "y": bottom_y}},
        "position": {"top_y": top_y, "bottom_y": bottom_y},
        "font": {"name": font_name, "path": font_path},
        "readable": {
            "font_size": font_size, "check_flag": 31, "background_style": 1,
            "background_color": background_color, "alignment": 1,
        },
        "estimated": True,     # ← UI가 "추정값" 배지를 띄우는 근거
        "font_found": bool(font_path),
        "transitions": [],
        "stats": {"count": 0},
    }


def load_style_profile() -> Optional[Dict[str, Any]]:
    if not STYLE_PROFILE_PATH.is_file():
        return None
    try:
        with open(STYLE_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_style_profile(profile: Dict[str, Any]) -> None:
    write_json_atomic(STYLE_PROFILE_PATH, profile)


# ══════════════════════════════════════════════════════════════════════════
# 3.5  텍스트만 갈아끼우기
# ══════════════════════════════════════════════════════════════════════════
def graft_text_material(profile: Dict[str, Any], text: str) -> Dict[str, Any]:
    """캘리브레이션한 원본 소재를 복제하고 **텍스트만** 교체합니다.

    빈 껍데기에 스타일을 얹는 대신 캡컷이 만든 온전한 소재를 그대로 쓰는 것이
    요청서 3.5의 처방입니다.
    """
    template = profile.get("material_template") or {}
    material = json.loads(json.dumps(template))
    material["id"] = uuid.uuid4().hex

    content_raw = material.get("content")
    try:
        parsed = json.loads(content_raw) if isinstance(content_raw, str) else {}
    except json.JSONDecodeError:
        parsed = {}

    parsed["text"] = text
    styles = parsed.get("styles")
    if isinstance(styles, list) and styles:
        # range는 문자 인덱스라 새 텍스트 길이에 맞춰야 합니다.
        styles[0]["range"] = [0, len(text)]
        for extra in styles[1:]:
            extra["range"] = [0, len(text)]
        parsed["styles"] = styles[:1]      # 부분 스타일은 새 텍스트에 의미가 없습니다
    parsed.setdefault("styles", [])
    material["content"] = json.dumps(parsed, ensure_ascii=False)

    # words 등 원본 텍스트에 종속된 필드는 새 텍스트에서 유효하지 않으므로 비웁니다.
    for key in ("words", "sub_type", "typesetting_words"):
        if key in material and isinstance(material[key], (list, dict)):
            material[key] = [] if isinstance(material[key], list) else {}

    font_path = (profile.get("font") or {}).get("path") or ""
    if font_path:
        apply_font_path(material, font_path, (profile.get("font") or {}).get("name", ""))
    return material


# ══════════════════════════════════════════════════════════════════════════
# 3.4  저장 후 검증
# ══════════════════════════════════════════════════════════════════════════
def verify_subtitle_visibility(draft_dir: Path) -> Dict[str, Any]:
    """저장한 파일을 **다시 읽어** 자막이 실제로 보일 상태인지 확인합니다.

    요청서 6번 원칙: "됐다"고 말하기 전에 파일을 다시 읽어 확인할 것.
    화면에는 요청한 개수가 아니라 **여기서 확인된 개수**를 표시합니다.
    """
    data = read_draft_content(draft_dir)
    texts = _material_index(data, "texts")

    text_tracks = [t for t in (data.get("tracks") or []) if t.get("type") == "text"]
    video_tracks = [t for t in (data.get("tracks") or []) if t.get("type") == "video"]

    problems: List[str] = []
    counted = 0
    missing_bg = 0
    missing_font = 0
    multiline = 0

    text_layers = {t.get("track_render_index") for t in text_tracks}
    video_layers = {t.get("track_render_index") for t in video_tracks}
    if text_layers & video_layers:
        problems.append(
            "자막 트랙과 영상 트랙의 track_render_index가 겹칩니다 — "
            "캡컷에서 자막이 영상에 가려집니다. (요청서 3.3)"
        )

    for track in text_tracks:
        for seg in track.get("segments") or []:
            material = texts.get(seg.get("material_id"))
            if not material:
                problems.append("자막 세그먼트가 참조하는 소재가 없습니다.")
                continue
            counted += 1
            if seg.get("track_render_index") != track.get("track_render_index"):
                problems.append("세그먼트의 track_render_index가 트랙과 다릅니다. (요청서 3.3)")

            check_flag = material.get("check_flag")
            bg_style = material.get("background_style")
            if material.get("background_color"):
                if not isinstance(check_flag, int) or not (check_flag & 16):
                    missing_bg += 1
                if not isinstance(bg_style, int) or bg_style < 1:
                    missing_bg += 1

            font_path = material.get("font_path") or ""
            if not font_path or not Path(str(font_path)).is_file():
                missing_font += 1

            if "\n" in _text_of(material):
                multiline += 1

    if missing_bg:
        problems.append(
            f"자막 {missing_bg}건에서 배경이 안 나올 설정이 확인됐습니다 "
            "(check_flag 배경 비트 16 또는 background_style<1). 요청서 3.4"
        )
    if missing_font:
        problems.append(
            f"자막 {missing_font}건의 font_path가 비었거나 실제 파일이 아닙니다. "
            "글꼴이 시스템 기본으로 바뀝니다. (요청서 3.6)"
        )
    if multiline:
        problems.append(f"줄바꿈이 들어간 자막 {multiline}건 — 모든 자막은 한 줄이어야 합니다.")

    canvas = data.get("canvas_config") or {}
    expected_ratio = ratio_for(canvas.get("width"), canvas.get("height"))
    if canvas.get("ratio") != expected_ratio:
        problems.append(
            f"canvas_config.ratio가 '{canvas.get('ratio')}'입니다 "
            f"('{expected_ratio}'여야 합니다). 요청서 3.10"
        )

    return {
        "verified_subtitle_count": counted,   # ← 요청한 개수가 아니라 확인된 개수
        "text_track_count": len(text_tracks),
        "problems": problems,
        "ok": not problems,
        "canvas": canvas,
    }


# ══════════════════════════════════════════════════════════════════════════
# 저장 후 후처리 일괄 적용
# ══════════════════════════════════════════════════════════════════════════
def postprocess_saved_draft(draft_root: Path, draft_dir: Path) -> Dict[str, Any]:
    """pyCapCut이 저장한 직후 반드시 돌려야 하는 교정 묶음.

    순서가 의미 있습니다: 내용 교정 -> 메타 교정 -> 레지스트리 등록.
    """
    data = read_draft_content(draft_dir)
    render_fix = fix_track_render_index(data)
    ratio_fix = fix_canvas_ratio(data)
    write_draft_content(draft_dir, data)

    fix_draft_meta_info(draft_dir)
    registered = register_in_root_meta(draft_root, draft_dir)

    return {
        "render_index_fixed": render_fix,
        "ratio_fixed": ratio_fix,
        "registered": registered,
    }


def pycapcut_version() -> str:
    try:
        from importlib.metadata import version
        return version("pycapcut")
    except Exception:  # noqa: BLE001
        return "(확인 불가)"


def version_banner() -> Dict[str, Any]:
    """검증 버전과 다르면 UI가 경고 배너를 띄우도록 정보를 제공합니다 (요청서 7절)."""
    current = pycapcut_version()
    return {
        "pycapcut": current,
        "pycapcut_verified": VERIFIED_PYCAPCUT_VERSION,
        "pycapcut_matches": current == VERIFIED_PYCAPCUT_VERSION,
    }


def draft_capcut_version(draft_dir: Path) -> Optional[str]:
    """드래프트를 만든 캡컷 버전 (platform.app_version)."""
    try:
        data = read_draft_content(draft_dir)
    except DraftError:
        return None
    return ((data.get("platform") or {}).get("app_version")) or None


def is_photo_material(material: Dict[str, Any]) -> bool:
    """이미지 클립 판정 (요청서 6절 3차).

    1차: materials.videos[].type == "photo"  (실측 확인 — local_materials.py)
    2차: 확장자 교차 검증
    """
    if str(material.get("type") or "").lower() == "photo":
        return True
    path = str(material.get("path") or "")
    return Path(path).suffix.lower() in IMAGE_EXTS
