"""날짜별 로그 + UI 로그 패널용 링 버퍼."""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List

from ..config import LOG_DIR

_LOCK = threading.Lock()
_RING: Deque[Dict[str, Any]] = deque(maxlen=1000)
_SEQ = 0

LOGGER_NAME = "capcut_auto_editor"


class _RingHandler(logging.Handler):
    """UI 로그 패널이 폴링해 가져갈 수 있도록 메모리에도 남깁니다."""

    def emit(self, record: logging.LogRecord) -> None:
        global _SEQ
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - 로깅이 앱을 죽이면 안 됩니다
            msg = "<로그 포매팅 실패>"
        with _LOCK:
            _SEQ += 1
            _RING.append({
                "seq": _SEQ,
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": msg,
            })


def _safe_stream() -> Any:
    """pythonw.exe로 띄우면 sys.stdout이 None입니다 (요청서 3.16).

    그 상태로 StreamHandler를 붙이면 첫 로그에서 죽으므로 None이면 파일만 씁니다.
    """
    return sys.stdout if sys.stdout is not None else None


def setup_logging() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    stream = _safe_stream()
    if stream is not None:
        sh = logging.StreamHandler(stream)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    logger.addHandler(_RingHandler())
    return logger


def get_logger() -> logging.Logger:
    return setup_logging()


def recent_logs(after_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
    with _LOCK:
        items = [e for e in _RING if e["seq"] > after_seq]
    return items[-limit:]


def safe_print(*args: Any) -> None:
    """pythonw.exe에서 sys.stdout이 None이어도 죽지 않는 print (요청서 3.16)."""
    if sys.stdout is None:
        return
    try:
        print(*args)
    except Exception:  # noqa: BLE001
        pass
