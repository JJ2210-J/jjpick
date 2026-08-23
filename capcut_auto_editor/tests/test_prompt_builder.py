"""프롬프트 조립 — 요청서 4차/5차."""

import unittest

from . import _helper  # noqa: F401
from app.services import prompt_builder as pb
from app.services.script_align import Subtitle


ANSWERS = {"takeaway": "프리타임 안에 확인할 3가지",
           "situation": "포워딩 오퍼레이션 담당",
           "tools": "CargoWise"}


class Youtube(unittest.TestCase):
    def setUp(self):
        self.text = pb.build_youtube_prompt(
            subtitle_text="컨테이너가 도착하면 데머리지가 붙습니다",
            answers=ANSWERS, channel={"channel_name": "물류실무TV"})

    def test_사전_질문_3문항이_모두_들어간다(self):
        for q in pb.QUESTIONS:
            self.assertIn(q["label"], self.text)

    def test_답변이_들어간다(self):
        for value in ANSWERS.values():
            self.assertIn(value, self.text)

    def test_채널_정보와_톤_가이드가_들어간다(self):
        self.assertIn("물류실무TV", self.text)
        self.assertIn("타겟 시청자", self.text)
        self.assertIn("톤 가이드", self.text)

    def test_자막_전문이_들어간다(self):
        self.assertIn("데머리지가 붙습니다", self.text)

    def test_사람이_잘_안_쓰는_표기_금지_지시가_있다(self):
        self.assertIn("em dash", self.text)
        self.assertIn("별표", self.text)

    def test_제목_2안과_해시태그_5개를_요구한다(self):
        self.assertIn("제목 2안", self.text)
        self.assertIn("해시태그 5개", self.text)

    def test_타임스탬프_목차를_요구한다(self):
        self.assertIn("타임스탬프", self.text)

    def test_아주_긴_자막은_잘라서_넣는다(self):
        long_text = pb.build_youtube_prompt(
            subtitle_text="가" * 40000, answers=ANSWERS)
        self.assertIn("중략", long_text)
        self.assertLess(len(long_text), 20000)


class Vertical(unittest.TestCase):
    def test_쇼츠에는_프로필_링크_CTA가_없다(self):
        text = pb.build_vertical_prompt(clip_text="짧은 클립", platform="shorts")
        self.assertNotIn("프로필 링크 CTA", text)

    def test_릴스는_프로필_링크_CTA가_필수다(self):
        text = pb.build_vertical_prompt(
            clip_text="짧은 클립", platform="reels",
            profile_link="https://instagram.com/x")
        self.assertIn("프로필 링크 CTA", text)
        self.assertIn("https://instagram.com/x", text)

    def test_링크가_없어도_자리를_잡아준다(self):
        text = pb.build_vertical_prompt(clip_text="짧은 클립", platform="reels")
        self.assertIn("프로필 링크 미입력", text)

    def test_상단과_하단_문구를_요구한다(self):
        text = pb.build_vertical_prompt(clip_text="짧은 클립")
        self.assertIn("상단 문구", text)
        self.assertIn("하단 문구", text)

    def test_표기_규칙이_들어간다(self):
        self.assertIn("별표", pb.build_vertical_prompt(clip_text="짧은 클립"))


class Chapters(unittest.TestCase):
    def test_자막에서_목차_후보를_뽑는다(self):
        subs = [Subtitle(id=f"s{i}", start=float(i * 10), end=float(i * 10 + 5),
                         text=f"{i}번째 항목입니다") for i in range(12)]
        chapters = pb.suggest_chapters(subs, 6)
        self.assertEqual(len(chapters), 6)
        self.assertEqual(chapters[0]["start"], 0.0)
        self.assertTrue(all(len(c["title"]) <= 20 for c in chapters))

    def test_자막이_없으면_빈_목록(self):
        self.assertEqual(pb.suggest_chapters([], 6), [])

    def test_자막이_요청_개수보다_적어도_문제없다(self):
        subs = [Subtitle(id="a", start=0.0, end=1.0, text="하나")]
        self.assertEqual(len(pb.suggest_chapters(subs, 6)), 1)


if __name__ == "__main__":
    unittest.main()
