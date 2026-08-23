"""faster-whisper 음성 인식 (청크 처리 + 재시작).

⚠ 요청서 3.19 — STT는 **CPU 전제**로 설계합니다
    NVIDIA GPU가 없으면 faster-whisper는 CPU로 돕니다 (Intel Arc는 CTranslate2 미지원).
      · 기본 모델은 `large-v3`가 아니라 **`medium`**
      · **첫 실행 시 모델을 내려받습니다 (medium ≈ 1.5GB)** — 누르기 전에 미리 알리고
        받는 동안 진행량을 표시합니다. 아무 안내 없이 "모델 로딩 중"에서 몇 분 멈춘 것처럼
        보이는 게 가장 흔한 오해입니다.
      · `huggingface_hub`가 `hf_xet` 백엔드를 쓰면 **staging 폴더에 먼저 받고 마지막에 옮깁니다.**
        모델 폴더 크기만 재면 끝날 때까지 0으로 보이므로 `~/.cache/huggingface/xet`도 함께 셉니다.
      · 긴 영상은 청크로 나눠 처리하고 청크별 결과를 디스크에 남겨 재시작 가능하게 합니다.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import WORK_DIR, load_settings, write_json_atomic
from .jobs import Job, JobCancelled
from .logging_util import get_logger

log = get_logger()

# 대략적인 다운로드 용량 (진행률 표시용). 정확할 필요는 없고 "얼마나 남았나"만 보이면 됩니다.
MODEL_SIZES_MB: Dict[str, int] = {
    "tiny": 75, "base": 145, "small": 480, "medium": 1530,
    "large-v2": 3090, "large-v3": 3090, "distil-large-v3": 1510,
}

MODEL_CHOICES = ["tiny", "base", "small", "medium", "large-v3"]


class TranscribeError(RuntimeError):
    """전사 실패. 메시지가 그대로 사용자에게 보입니다."""


# ══════════════════════════════════════════════════════════════════════════
# 모델 캐시 상태 (요청서 3.19: 누르기 전에 미리 알리기)
# ══════════════════════════════════════════════════════════════════════════
def _hf_cache_root() -> Path:
    import os
    env = os.environ.get("HF_HOME")
    if env:
        return Path(env) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _xet_staging_root() -> Path:
    """hf_xet 백엔드의 staging 폴더.

    여기에 먼저 받고 마지막에 옮기므로, 모델 폴더 크기만 재면
    끝날 때까지 0으로 보입니다 (요청서 3.19).
    """
    import os
    env = os.environ.get("HF_HOME")
    base = Path(env) if env else Path.home() / ".cache" / "huggingface"
    return base / "xet"


def _dir_size_mb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0.0
    return total / (1024 * 1024)


def _model_dir(model: str) -> Path:
    return _hf_cache_root() / f"models--Systran--faster-whisper-{model}"


def model_status(model: Optional[str] = None) -> Dict[str, Any]:
    """모델이 이미 받아져 있는지 / 얼마나 받아야 하는지."""
    model = model or load_settings().get("whisper_model", "medium")
    expected = MODEL_SIZES_MB.get(model, 1000)
    cached = _dir_size_mb(_model_dir(model))
    # 90% 이상 있으면 받아진 것으로 봅니다 (부수 파일 차이 흡수).
    downloaded = cached >= expected * 0.9
    return {
        "model": model,
        "expected_mb": expected,
        "cached_mb": round(cached, 1),
        "downloaded": downloaded,
        "cache_dir": str(_model_dir(model)),
        "notice": "" if downloaded else (
            f"'{model}' 모델을 처음 쓰는 것 같습니다. "
            f"시작하면 약 {expected}MB를 내려받습니다 (네트워크에 따라 수 분 걸립니다). "
            "받는 동안 진행률이 표시됩니다."
        ),
    }


class _DownloadWatcher:
    """모델 다운로드 진행량을 폴링해서 보고합니다.

    faster-whisper는 진행 콜백을 주지 않으므로 캐시 폴더 크기를 재는 수밖에 없습니다.
    staging 폴더를 같이 세는 것이 요청서 3.19의 핵심입니다.
    """

    def __init__(self, job: Job, model: str) -> None:
        self.job = job
        self.model = model
        self.expected = MODEL_SIZES_MB.get(model, 1000)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "_DownloadWatcher":
        self._thread = threading.Thread(target=self._run, name="model-download", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(1.5):
            got = _dir_size_mb(_model_dir(self.model)) + _dir_size_mb(_xet_staging_root())
            frac = min(0.99, got / self.expected) if self.expected else 0.0
            self.job.set_progress(
                0.45 + frac * 0.05,
                f"'{self.model}' 모델을 내려받는 중",
                f"{got:.0f}MB / 약 {self.expected}MB",
            )


# ══════════════════════════════════════════════════════════════════════════
# 전사
# ══════════════════════════════════════════════════════════════════════════
def _chunk_path(session_id: str, index: int) -> Path:
    d = WORK_DIR / session_id / "stt"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"chunk_{index:04d}.json"


def _load_chunk(session_id: str, index: int) -> Optional[List[Dict[str, Any]]]:
    path = _chunk_path(session_id, index)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def transcribe(
    wav_path: Path,
    session_id: str,
    *,
    job: Job,
    total_duration: float,
    model_name: Optional[str] = None,
    language: Optional[str] = None,
    chunk_sec: Optional[int] = None,
    progress_from: float = 0.50,
    progress_to: float = 0.92,
) -> Dict[str, Any]:
    """단어 단위 타임스탬프를 포함해 전사합니다.

    청크별 결과를 디스크에 남기므로, 중간에 취소하거나 실패해도
    다시 실행하면 **이미 끝난 청크는 건너뜁니다.**
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeError(
            "faster-whisper가 설치돼 있지 않습니다.\n"
            "pip install faster-whisper==1.2.1 을 실행한 뒤 다시 시도하세요."
        ) from exc

    settings = load_settings()
    model_name = model_name or settings.get("whisper_model", "medium")
    language = language or settings.get("whisper_language", "ko")
    compute_type = settings.get("whisper_compute_type", "int8")
    chunk_sec = int(chunk_sec or settings.get("whisper_chunk_sec", 600))

    status = model_status(model_name)
    job.set_progress(progress_from, "음성 인식 준비 중",
                     status["notice"] or f"'{model_name}' 모델 로드")

    log.info("STT 시작: model=%s language=%s compute=%s chunk=%ss",
             model_name, language, compute_type, chunk_sec)

    # CPU 전제. GPU가 있으면 faster-whisper가 알아서 쓰지만 기본은 cpu로 둡니다.
    load_start = time.time()
    try:
        if status["downloaded"]:
            model = WhisperModel(model_name, device="cpu", compute_type=compute_type)
        else:
            with _DownloadWatcher(job, model_name):
                model = WhisperModel(model_name, device="cpu", compute_type=compute_type)
    except Exception as exc:  # noqa: BLE001 - 원인을 그대로 보여줍니다
        raise TranscribeError(
            f"음성 인식 모델을 준비하지 못했습니다.\n{type(exc).__name__}: {exc}\n"
            "네트워크가 막혀 있으면 모델을 내려받지 못합니다."
        ) from exc
    log.info("모델 로드 완료 (%.1f초)", time.time() - load_start)

    job.check_cancel()

    chunk_count = max(1, int((total_duration + chunk_sec - 1) // chunk_sec)) if total_duration else 1
    all_words: List[Dict[str, Any]] = []
    all_segments: List[Dict[str, Any]] = []
    resumed = 0

    for index in range(chunk_count):
        job.check_cancel()
        offset = index * chunk_sec
        length = min(chunk_sec, max(0.0, total_duration - offset)) if total_duration else 0.0
        if total_duration and length <= 0:
            break

        cached = _load_chunk(session_id, index)
        if cached is not None:
            resumed += 1
            all_segments.extend(cached)
            for seg in cached:
                all_words.extend(seg.get("words") or [])
            frac = (index + 1) / chunk_count
            job.set_progress(progress_from + (progress_to - progress_from) * frac,
                             "음성 인식 중 (이미 끝난 구간 건너뜀)",
                             f"{index + 1}/{chunk_count} 구간")
            continue

        job.set_progress(progress_from + (progress_to - progress_from) * (index / chunk_count),
                         "음성 인식 중",
                         f"{index + 1}/{chunk_count} 구간 ({_fmt(offset)}~{_fmt(offset + length)})")

        try:
            segments, _info = model.transcribe(
                str(wav_path),
                language=language,
                word_timestamps=True,
                vad_filter=True,                       # Silero VAD를 onnxruntime으로 내장 실행
                vad_parameters={"min_silence_duration_ms": 500},
                clip_timestamps=[offset, offset + length] if total_duration else None,
            )
            chunk_segments: List[Dict[str, Any]] = []
            for seg in segments:
                job.check_cancel()
                words = [{
                    "word": (w.word or "").strip(),
                    "start": float(w.start or 0.0),
                    "end": float(w.end or 0.0),
                    "probability": float(getattr(w, "probability", 0.0) or 0.0),
                } for w in (seg.words or [])]
                chunk_segments.append({
                    "start": float(seg.start or 0.0),
                    "end": float(seg.end or 0.0),
                    "text": (seg.text or "").strip(),
                    "words": words,
                    "avg_logprob": float(getattr(seg, "avg_logprob", 0.0) or 0.0),
                    "no_speech_prob": float(getattr(seg, "no_speech_prob", 0.0) or 0.0),
                })
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TranscribeError(
                f"{index + 1}번째 구간 전사에 실패했습니다.\n{type(exc).__name__}: {exc}"
            ) from exc

        write_json_atomic(_chunk_path(session_id, index), chunk_segments)
        all_segments.extend(chunk_segments)
        for seg in chunk_segments:
            all_words.extend(seg.get("words") or [])

    all_segments.sort(key=lambda s: s["start"])
    all_words.sort(key=lambda w: w["start"])

    if resumed:
        log.info("이미 끝나 있던 구간 %d개를 건너뛰었습니다.", resumed)
    log.info("STT 완료: 세그먼트 %d개 / 단어 %d개", len(all_segments), len(all_words))

    return {
        "model": model_name,
        "language": language,
        "segments": all_segments,
        "words": all_words,
        "chunk_count": chunk_count,
        "resumed_chunks": resumed,
        "low_confidence": [
            {"start": s["start"], "end": s["end"], "text": s["text"],
             "avg_logprob": s["avg_logprob"]}
            for s in all_segments if s["avg_logprob"] < -1.0
        ],
    }


def clear_cache(session_id: str) -> int:
    """청크 캐시를 지웁니다 (설정을 바꿔 처음부터 다시 돌리고 싶을 때)."""
    d = WORK_DIR / session_id / "stt"
    if not d.is_dir():
        return 0
    n = 0
    for p in d.glob("chunk_*.json"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
