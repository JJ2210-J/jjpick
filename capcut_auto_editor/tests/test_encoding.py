"""인코딩 함정 — 요청서 3.15(하위 프로세스 한글)와 3.16(bat 파일 CP949)."""

import unittest
from pathlib import Path

from . import _helper  # noqa: F401
from app.services import native_picker

ROOT = Path(__file__).resolve().parent.parent
BATS = ["캡컷 자동 편집기 실행.bat", "처음 한 번 설치.bat"]


class BatEncoding(unittest.TestCase):
    """3.16 — UTF-8 저장 + chcp 65001 조합은 창이 깜빡하고 즉시 종료됩니다."""

    def test_bat_파일이_존재한다(self):
        for name in BATS:
            self.assertTrue((ROOT / name).is_file(), f"{name}이 없습니다")

    def test_CP949로_저장돼_있다(self):
        for name in BATS:
            raw = (ROOT / name).read_bytes()
            try:
                raw.decode("cp949")
            except UnicodeDecodeError:
                self.fail(f"{name}이 CP949로 읽히지 않습니다")

    def test_UTF8이_아니다(self):
        # 한글이 든 CP949 파일은 UTF-8로 디코드되지 않습니다.
        for name in BATS:
            raw = (ROOT / name).read_bytes()
            with self.assertRaises(UnicodeDecodeError, msg=f"{name}이 UTF-8로 저장된 것 같습니다"):
                raw.decode("utf-8")

    def test_BOM이_없다(self):
        for name in BATS:
            self.assertNotEqual((ROOT / name).read_bytes()[:3], b"\xef\xbb\xbf")

    def test_chcp를_부르지_않는다(self):
        """주석(rem)에 적힌 설명은 괜찮고, 실제 실행되는 줄에 있으면 안 됩니다."""
        for name in BATS:
            text = (ROOT / name).read_bytes().decode("cp949")
            live = [line for line in text.splitlines()
                    if line.strip() and not line.strip().lower().startswith(("rem", "::", "echo"))]
            offenders = [line for line in live if "chcp" in line.lower()]
            self.assertEqual(offenders, [], f"{name}에 실행되는 chcp가 있습니다")

    def test_pythonw를_쓰지_않는다(self):
        """pythonw.exe로 띄우면 sys.stdout이 None이라 시작 메시지에서 죽습니다."""
        for name in BATS:
            text = (ROOT / name).read_bytes().decode("cp949")
            self.assertNotIn("pythonw", text.lower())

    def test_CRLF_줄바꿈이다(self):
        for name in BATS:
            self.assertIn(b"\r\n", (ROOT / name).read_bytes())


class MojibakeDetection(unittest.TestCase):
    """3.15 — 자식 stdout이 cp949면 한글 경로가 깨집니다."""

    def test_깨진_글자를_잡아낸다(self):
        result = native_picker._check_mojibake(
            {"ok": True, "paths": ["C:/Users/\ufffd\ufffd/유툽/영상.mp4"], "error": ""})
        self.assertFalse(result["ok"])
        self.assertTrue(result["mojibake"])
        self.assertIn("한글이 깨졌습니다", result["error"])

    def test_정상_한글은_통과한다(self):
        result = native_picker._check_mojibake(
            {"ok": True, "paths": ["C:/Users/개인/유툽/영상.mp4"], "error": ""})
        self.assertTrue(result["ok"])
        self.assertNotIn("mojibake", result)

    def test_빈_목록도_통과한다(self):
        self.assertTrue(native_picker._check_mojibake({"ok": True, "paths": []})["ok"])


class SafeOutput(unittest.TestCase):
    """3.16 — stdout이 None이어도 죽지 않아야 합니다."""

    def test_stdout이_None이어도_죽지_않는다(self):
        import sys
        from app.services.logging_util import safe_print
        original = sys.stdout
        try:
            sys.stdout = None
            safe_print("이 호출은 예외를 내면 안 됩니다")
        finally:
            sys.stdout = original


class RunPyDefenses(unittest.TestCase):
    def test_run_py가_UTF8_환경변수를_심는다(self):
        text = (ROOT / "run.py").read_text(encoding="utf-8")
        self.assertIn("PYTHONIOENCODING", text)
        self.assertIn("PYTHONUTF8", text)

    def test_native_picker가_자식_환경에_인코딩을_심는다(self):
        text = (ROOT / "app" / "services" / "native_picker.py").read_text(encoding="utf-8")
        self.assertIn('env["PYTHONIOENCODING"] = "utf-8"', text)
        self.assertIn("sys.stdout.reconfigure", text)


if __name__ == "__main__":
    unittest.main()
