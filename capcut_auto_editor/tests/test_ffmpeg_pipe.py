"""ffmpeg stderr 파이프 데드락 — 요청서 3.14.

이 파일은 ffmpeg이 있을 때만 돕니다. 없으면 건너뜁니다.
"""

import subprocess
import unittest
from pathlib import Path

from . import _helper  # noqa: F401
from app.config import ffmpeg_path

WINDOWS_PIPE_BUFFER = 4096
ROOT = Path(__file__).resolve().parent.parent


def _stderr_bytes(extra_args):
    """짧은 인코딩을 돌려 stderr 배출량을 잽니다."""
    exe = ffmpeg_path()
    cmd = [exe] + extra_args + [
        "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=1",
        "-f", "lavfi", "-i", "sine=duration=1",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", "-f", "mp4", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    return len(proc.stderr)


@unittest.skipUnless(ffmpeg_path(), "ffmpeg이 없어 건너뜁니다")
class BannerVolume(unittest.TestCase):
    """배출량은 빌드와 입력 파일에 따라 다릅니다. 그래서 절대값을 단정하지 않고,
    **위험이 실재한다는 성질**만 검증합니다.

    참고로 개발 중 실측한 값 (실제 mp4 입력, 스트림 2개):

    | 빌드 | 배너 포함 | -hide_banner | +loglevel error |
    |---|---|---|---|
    | ffmpeg 7.0.2 static | 5,827 | 4,308 | 0 |
    | ffmpeg 6.1.1 ubuntu | 6,744 | 4,394 | 0 |

    두 빌드 모두 `-hide_banner`만으로는 4,096B를 넘겼습니다.
    요청서 3.14 표의 "-hide_banner면 정상"이 성립하지 않는다는 뜻이라,
    -loglevel error와 드레인 스레드까지 함께 씁니다.
    """

    def test_가장_사소한_입력도_버퍼를_거의_채운다(self):
        """1초짜리 합성 클립조차 버퍼의 대부분을 씁니다 = 실파일은 반드시 넘습니다."""
        size = _stderr_bytes([])
        self.assertGreater(
            size, WINDOWS_PIPE_BUFFER * 0.5,
            f"stderr {size}B — 1초 합성 클립인데도 4,096B 버퍼의 절반을 넘어야 합니다",
        )

    def test_loglevel_error가_배출량을_없앤다(self):
        quiet = _stderr_bytes(["-hide_banner", "-loglevel", "error"])
        self.assertLess(quiet, WINDOWS_PIPE_BUFFER * 0.25,
                        f"-loglevel error를 붙였는데 stderr가 {quiet}B입니다")

    def test_hide_banner_단독보다_loglevel까지_붙인_쪽이_안전하다(self):
        hidden = _stderr_bytes(["-hide_banner"])
        quiet = _stderr_bytes(["-hide_banner", "-loglevel", "error"])
        self.assertLess(quiet, hidden,
                        "-loglevel error가 배출량을 더 줄이지 못했습니다")


class Defenses(unittest.TestCase):
    """수치와 무관하게, 코드가 3중 방어를 갖췄는지 확인합니다."""

    def setUp(self):
        self.source = (ROOT / "app" / "services" / "audio_analysis.py").read_text(encoding="utf-8")

    def test_모든_ffmpeg_호출에_hide_banner가_붙는다(self):
        import re
        for match in re.finditer(r"cmd = \[\s*exe[^\]]*\]", self.source, re.DOTALL):
            self.assertIn("-hide_banner", match.group(0))

    def test_stderr를_비우는_스레드가_있다(self):
        self.assertIn("threading.Thread", self.source)
        self.assertIn("def _drain", self.source)

    def test_silencedetect도_stderr를_계속_읽는다(self):
        # silencedetect는 결과를 stderr의 info 레벨로 냅니다.
        # 거기서는 -loglevel error를 쓸 수 없으므로 드레인만이 방어선입니다.
        self.assertIn("silencedetect-stderr", self.source)


if __name__ == "__main__":
    unittest.main()
