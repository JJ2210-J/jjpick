"""ffprobe, 오디오 추출, 무음 감지.

⚠ 요청서 3.14 — ffmpeg stderr 파이프 데드락
    긴 ffmpeg 작업에 `-progress pipe:1`로 진행률을 받을 때 stderr를 읽지 않으면
    ffmpeg이 영원히 멈춥니다. 배너만 2KB가 넘고 윈도우 익명 파이프 기본 버퍼는
    4096바이트라 37바이트 차이로 막힙니다. 증상은 "진행률 2%에서 멈춤".

    처방(둘 다 적용):
      ① 모든 ffmpeg 호출에 `-hide_banner`
      ② stderr를 **별도 데몬 스레드로 계속 비움** (`_drain`)
    ①만으로는 스트림이 많은 파일에서 다시 넘칠 수 있습니다.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..config import ffmpeg_path, ffprobe_path
from .jobs import Job, JobCancelled
from .logging_util import get_logger

log = get_logger()

# 윈도우에서 콘솔 창이 튀어나오지 않게 합니다.
_CREATE_NO_WINDOW = 0x08000000


class MediaError(RuntimeError):
    """ffmpeg/ffprobe 실행 실패. 메시지가 그대로 사용자에게 보입니다."""


def _popen_kwargs() -> Dict[str, Any]:
    import os
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = _CREATE_NO_WINDOW
    return kw


def _require(kind: str) -> str:
    path = ffmpeg_path() if kind == "ffmpeg" else ffprobe_path()
    if not path:
        raise MediaError(
            f"{kind}를 찾지 못했습니다.\n"
            "winget install Gyan.FFmpeg 로 설치한 뒤 새 터미널에서 다시 실행하거나, "
            "설정 화면에서 실행 파일 경로를 직접 지정하세요."
        )
    return path


def _drain(stream: Any, sink: Deque[str]) -> None:
    """stderr를 계속 읽어 버려 파이프가 차지 않게 합니다 (요청서 3.14 처방 ②).

    마지막 몇 줄만 남겨 두었다가 실패했을 때 원인 표시에 씁니다.
    """
    try:
        for raw in iter(stream.readline, b""):
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:  # noqa: BLE001
                continue
            if line:
                sink.append(line)
    except (ValueError, OSError):
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


# ══════════════════════════════════════════════════════════════════════════
# ffprobe
# ══════════════════════════════════════════════════════════════════════════
def probe(path: Path) -> Dict[str, Any]:
    """해상도·fps·길이·오디오 트랙 정보를 읽습니다.

    짧게 끝나는 명령이라 subprocess.run을 씁니다 —
    run은 두 파이프를 함께 비우므로 3.14 데드락이 발생하지 않습니다.
    """
    exe = _require("ffprobe")
    path = Path(path)
    cmd = [exe, "-hide_banner", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, **_popen_kwargs())
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffprobe가 응답하지 않습니다: {path.name}") from exc

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise MediaError(f"'{path.name}' 정보를 읽지 못했습니다.\n{err[:500]}")

    try:
        info = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe 출력을 해석하지 못했습니다: {path.name}") from exc

    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    duration = 0.0
    fmt = info.get("format") or {}
    for source in (fmt.get("duration"), (video or {}).get("duration")):
        try:
            duration = float(source)
            break
        except (TypeError, ValueError):
            continue

    fps = 0.0
    if video:
        raw = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        try:
            num, _, den = raw.partition("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

    return {
        "path": str(path),
        "name": path.name,
        "exists": path.is_file(),
        "duration": duration,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "fps": round(fps, 3),
        "has_video": video is not None,
        "audio_track_count": len(audios),
        "video_codec": (video or {}).get("codec_name", ""),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }


# ══════════════════════════════════════════════════════════════════════════
# ffmpeg 실행 (진행률 + 데드락 방어)
# ══════════════════════════════════════════════════════════════════════════
_PROGRESS_RE = re.compile(rb"out_time_us=(\d+)")


def run_ffmpeg(
    args: List[str],
    *,
    total_seconds: float = 0.0,
    job: Optional[Job] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    label: str = "",
) -> None:
    """ffmpeg을 실행하고 진행률을 보고합니다.

    데드락 방어(요청서 3.14):
      · `-hide_banner`를 **항상** 맨 앞에 붙입니다.
      · stderr는 별도 데몬 스레드가 계속 비웁니다.
    """
    exe = _require("ffmpeg")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"] + args
    if total_seconds > 0:
        cmd += ["-progress", "pipe:1", "-nostats"]

    log.info("ffmpeg 실행%s: %s", f" [{label}]" if label else "", " ".join(cmd[:12]) + " …")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_popen_kwargs()
    )
    if job is not None:
        job.register_process(proc)

    tail: Deque[str] = deque(maxlen=40)
    drainer = threading.Thread(target=_drain, args=(proc.stderr, tail),
                               name="ffmpeg-stderr", daemon=True)
    drainer.start()

    try:
        if proc.stdout is not None:
            for raw in iter(proc.stdout.readline, b""):
                if job is not None and job.cancelled:
                    raise JobCancelled()
                match = _PROGRESS_RE.search(raw)
                if match and total_seconds > 0:
                    done = int(match.group(1)) / 1_000_000.0
                    frac = max(0.0, min(1.0, done / total_seconds))
                    if on_progress:
                        on_progress(frac)
            proc.stdout.close()
        proc.wait()
    except JobCancelled:
        proc.kill()
        proc.wait(timeout=10)
        raise
    finally:
        drainer.join(timeout=2)

    if proc.returncode != 0:
        detail = "\n".join(list(tail)[-12:]) or "(ffmpeg이 원인을 출력하지 않았습니다)"
        raise MediaError(f"ffmpeg 실행 실패 (코드 {proc.returncode}).\n{detail}")


def measure_banner_bytes() -> Dict[str, int]:
    """요청서 3.14 근거 재현용 — 배너 유무에 따른 stderr 배출량 측정.

    tests/에서 호출합니다. 윈도우 익명 파이프 기본 버퍼는 4096바이트입니다.
    """
    exe = _require("ffmpeg")
    with_banner = subprocess.run([exe, "-version"], capture_output=True, **_popen_kwargs())
    without = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, **_popen_kwargs())
    return {
        "with_banner": len(with_banner.stdout) + len(with_banner.stderr),
        "hide_banner": len(without.stdout) + len(without.stderr),
        "windows_pipe_buffer": 4096,
    }


# ══════════════════════════════════════════════════════════════════════════
# 오디오 추출
# ══════════════════════════════════════════════════════════════════════════
def extract_audio(
    video_paths: List[Path],
    out_wav: Path,
    *,
    job: Optional[Job] = None,
    total_seconds: float = 0.0,
) -> Path:
    """16kHz 모노 wav로 추출합니다. 영상이 여러 개면 각각 뽑아 이어붙입니다.

    concat demuxer 대신 filter_complex concat을 쓰는 이유:
    소재마다 샘플레이트·채널 수가 달라도 안전하게 이어집니다.
    """
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if job is not None:
        job.register_temp(out_wav)

    if not video_paths:
        raise MediaError("오디오를 추출할 영상이 없습니다.")

    args: List[str] = []
    for p in video_paths:
        args += ["-i", str(p)]

    if len(video_paths) == 1:
        args += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)]
    else:
        parts = "".join(f"[{i}:a]" for i in range(len(video_paths)))
        args += [
            "-filter_complex", f"{parts}concat=n={len(video_paths)}:v=0:a=1[out]",
            "-map", "[out]", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav),
        ]

    def report(frac: float) -> None:
        if job is not None:
            job.set_progress(0.05 + frac * 0.25, "오디오를 추출하는 중",
                             f"{int(frac * 100)}%")

    run_ffmpeg(args, total_seconds=total_seconds, job=job,
               on_progress=report, label="extract_audio")

    if not out_wav.is_file() or out_wav.stat().st_size == 0:
        raise MediaError("오디오 추출 결과 파일이 비어 있습니다. 영상에 오디오 트랙이 있는지 확인하세요.")
    return out_wav


# ══════════════════════════════════════════════════════════════════════════
# 무음 감지
# ══════════════════════════════════════════════════════════════════════════
_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silence(
    wav_path: Path,
    *,
    threshold_db: float = -35.0,
    min_silence_sec: float = 0.6,
    job: Optional[Job] = None,
    total_seconds: float = 0.0,
) -> List[Tuple[float, float]]:
    """ffmpeg silencedetect로 무음 구간 [(start, end), ...]를 찾습니다.

    silencedetect는 **stderr로** 결과를 냅니다. 그래서 여기서는 stderr를
    버리지 않고 모아야 하는데, 그렇더라도 파이프를 막으면 안 되므로
    (요청서 3.14) 역시 별도 스레드로 계속 읽어들입니다.
    """
    exe = _require("ffmpeg")
    cmd = [
        exe, "-hide_banner", "-nostdin", "-i", str(wav_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_sec}",
        "-f", "null", "-",
    ]
    if total_seconds > 0:
        cmd += ["-progress", "pipe:1", "-nostats"]

    log.info("무음 감지: threshold=%sdB, min=%ss", threshold_db, min_silence_sec)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **_popen_kwargs())
    if job is not None:
        job.register_process(proc)

    collected: List[str] = []

    def collect(stream: Any) -> None:
        try:
            for raw in iter(stream.readline, b""):
                collected.append(raw.decode("utf-8", errors="replace"))
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    reader = threading.Thread(target=collect, args=(proc.stderr,),
                              name="silencedetect-stderr", daemon=True)
    reader.start()

    try:
        if proc.stdout is not None:
            for raw in iter(proc.stdout.readline, b""):
                if job is not None and job.cancelled:
                    raise JobCancelled()
                match = _PROGRESS_RE.search(raw)
                if match and total_seconds > 0 and job is not None:
                    frac = min(1.0, (int(match.group(1)) / 1_000_000.0) / total_seconds)
                    job.set_progress(0.30 + frac * 0.15, "무음 구간을 찾는 중",
                                     f"{int(frac * 100)}%")
            proc.stdout.close()
        proc.wait()
    except JobCancelled:
        proc.kill()
        proc.wait(timeout=10)
        raise
    finally:
        reader.join(timeout=5)

    if proc.returncode != 0:
        detail = "".join(collected)[-800:]
        raise MediaError(f"무음 감지 실패 (코드 {proc.returncode}).\n{detail}")

    text = "".join(collected)
    starts = [float(m) for m in _SILENCE_START.findall(text)]
    ends = [float(m) for m in _SILENCE_END.findall(text)]

    spans: List[Tuple[float, float]] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else total_seconds
        if end > start:
            spans.append((max(0.0, start), end))

    log.info("무음 구간 %d개 감지", len(spans))
    return spans


def slice_audio(src: Path, dest: Path, start: float, duration: float) -> Path:
    """미리듣기용 짧은 구간 추출. 짧아서 subprocess 파이프 문제가 없습니다."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.05, duration):.3f}",
        "-i", str(src), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest),
    ], label="slice_audio")
    return dest
