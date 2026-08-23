"""세션 상태와 단계 잠금 — 요청서 5절 '비활성 + 이유 표시'."""

import tempfile
import unittest
from pathlib import Path

from . import _helper  # noqa: F401
from app.services import session as ss
from app.services.session import Session, StepLocked


class StepLocking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "style_profile.json"
        # 캘리브레이션 완료 여부는 전역 프로필 파일에서 파생됩니다.
        self._original = ss.STYLE_PROFILE_PATH
        ss.STYLE_PROFILE_PATH = self.profile
        self.session = Session(id="t", name="테스트")

    def tearDown(self):
        ss.STYLE_PROFILE_PATH = self._original
        self.tmp.cleanup()

    def test_처음에는_0차만_열려_있다(self):
        states = {s["key"]: s for s in self.session.step_states()}
        self.assertFalse(states["prepare"]["locked"])
        self.assertTrue(states["cut"]["locked"])
        self.assertTrue(states["subtitle"]["locked"])

    def test_잠긴_단계는_이유를_함께_준다(self):
        for state in self.session.step_states():
            if state["locked"]:
                self.assertTrue(state["lock_reason"], f"{state['key']}에 이유가 없습니다")

    def test_0차를_끝내면_1차가_열린다(self):
        self.session.mark("prepare")
        states = {s["key"]: s for s in self.session.step_states()}
        self.assertFalse(states["cut"]["locked"])
        self.assertFalse(states["calibration"]["locked"])

    def test_2차는_캘리브레이션과_1차를_모두_요구한다(self):
        self.session.mark("prepare")
        self.session.mark("cut")
        states = {s["key"]: s for s in self.session.step_states()}
        self.assertTrue(states["subtitle"]["locked"])
        self.assertIn("캘리브레이션", states["subtitle"]["lock_reason"])

    def test_캘리브레이션은_프로필_파일에서_파생된다(self):
        """전역 파일이라 세션 플래그로 두면 영원히 잠깁니다."""
        self.session.mark("prepare")
        self.session.mark("cut")
        self.assertFalse(self.session.is_done("calibration"))

        self.profile.write_text("{}", encoding="utf-8")
        self.assertTrue(self.session.is_done("calibration"))
        states = {s["key"]: s for s in self.session.step_states()}
        self.assertFalse(states["subtitle"]["locked"])

    def test_5차는_2차를_요구한다(self):
        self.session.mark("prepare")
        self.session.mark("cut")
        self.profile.write_text("{}", encoding="utf-8")
        states = {s["key"]: s for s in self.session.step_states()}
        self.assertTrue(states["vertical"]["locked"])
        self.session.mark("subtitle")
        states = {s["key"]: s for s in self.session.step_states()}
        self.assertFalse(states["vertical"]["locked"])

    def test_require는_이유와_함께_막는다(self):
        with self.assertRaises(StepLocked) as ctx:
            self.session.require("subtitle")
        self.assertIn("먼저", str(ctx.exception))

    def test_require는_조건이_맞으면_통과한다(self):
        self.session.mark("prepare")
        self.session.require("cut")


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._session_dir = ss.SESSION_DIR
        self._work_dir = ss.WORK_DIR
        ss.SESSION_DIR = Path(self.tmp.name) / "sessions"
        ss.WORK_DIR = Path(self.tmp.name) / "work"
        ss.SESSION_DIR.mkdir(parents=True)
        ss.WORK_DIR.mkdir(parents=True)

    def tearDown(self):
        ss.SESSION_DIR = self._session_dir
        ss.WORK_DIR = self._work_dir
        self.tmp.cleanup()

    def test_만들고_읽고_지운다(self):
        created = ss.create("내 작업")
        loaded = ss.load(created.id)
        self.assertEqual(loaded.name, "내 작업")

        loaded.data["draft_name"] = "테스트드래프트"
        loaded.mark("prepare")
        ss.save(loaded)

        again = ss.load(created.id)
        self.assertEqual(again.data["draft_name"], "테스트드래프트")
        self.assertTrue(again.is_done("prepare"))

        self.assertTrue(ss.delete(created.id))
        with self.assertRaises(ss.SessionNotFound):
            ss.load(created.id)

    def test_이름을_비우면_자동으로_붙는다(self):
        self.assertTrue(ss.create("").name)

    def test_목록은_최근_수정_순이다(self):
        first = ss.create("먼저")
        second = ss.create("나중")
        ss.save(ss.load(first.id))       # 먼저를 다시 저장 = 더 최근
        names = [s["name"] for s in ss.list_all()]
        self.assertEqual(names[0], "먼저")
        self.assertIn("나중", names)

    def test_없는_세션은_명확히_알려준다(self):
        with self.assertRaises(ss.SessionNotFound):
            ss.load("존재하지않음")


if __name__ == "__main__":
    unittest.main()
