"""자막 정리 — 요청서 3.9(겹침·부호)와 잘려나간 단어 제거."""

import unittest

from . import _helper  # noqa: F401
from app.services import script_align as sa
from app.services.cut_edit import CutMap


def sub(sid, start, end, text, source="stt"):
    return sa.Subtitle(id=sid, start=start, end=end, text=text, source=source)


class Sanitize(unittest.TestCase):
    """3.9 — pyCapCut은 겹치는 세그먼트를 거부합니다."""

    def test_요청서_실측_겹침을_정리한다(self):
        # 앞 문장이 463.87초까지인데 다음 문장이 463.47초에 시작
        subs, stats = sa.sanitize([
            sub("a", 460.00, 463.87, "앞 문장입니다"),
            sub("b", 463.47, 466.00, "뒤 문장입니다"),
        ])
        self.assertEqual(stats["fixed_overlap"], 1)
        self.assertAlmostEqual(subs[0].end, 463.47)
        self.assertLessEqual(subs[0].end, subs[1].start + 1e-9)

    def test_어떤_입력이든_겹치지_않게_만든다(self):
        subs, _ = sa.sanitize([
            sub("a", 0.0, 5.0, "하나"), sub("b", 1.0, 6.0, "둘"),
            sub("c", 2.0, 3.0, "셋"), sub("d", 2.5, 9.0, "넷"),
        ])
        for i in range(len(subs) - 1):
            self.assertLessEqual(subs[i].end, subs[i + 1].start + 1e-9)

    def test_부호만_있는_조각은_앞에_붙는다(self):
        subs, stats = sa.sanitize([
            sub("a", 0.0, 2.0, "안녕하세요"), sub("b", 2.0, 2.2, "."),
        ])
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0].text, "안녕하세요.")
        self.assertEqual(stats["merged_punct"], 1)

    def test_같은_부호가_중복되지_않는다(self):
        subs, _ = sa.sanitize([sub("a", 0.0, 2.0, "네."), sub("b", 2.0, 2.2, ".")])
        self.assertEqual(subs[0].text, "네.")

    def test_최소_길이를_보장한다(self):
        subs, stats = sa.sanitize([sub("a", 0.0, 0.05, "짧다")], min_duration=0.35)
        self.assertGreaterEqual(subs[0].end - subs[0].start, 0.35 - 1e-9)
        self.assertEqual(stats["stretched"], 1)

    def test_아이디를_다시_매긴다(self):
        subs, _ = sa.sanitize([sub("z", 5.0, 6.0, "뒤"), sub("a", 0.0, 1.0, "앞")])
        self.assertEqual([s.id for s in subs], ["sub0000", "sub0001"])
        self.assertEqual(subs[0].text, "앞")

    def test_두_번_돌려도_결과가_같다(self):
        first, _ = sa.sanitize([sub("a", 0.0, 5.0, "하나"), sub("b", 1.0, 6.0, "둘")])
        second, stats = sa.sanitize(first)
        self.assertEqual([s.to_dict() for s in first], [s.to_dict() for s in second])
        self.assertEqual(sum(stats.values()), 0)


class SurvivingText(unittest.TestCase):
    """컷은 시간을 지우지 텍스트를 지우지 않습니다. 잘린 단어를 빼야 합니다."""

    def setUp(self):
        # 10~20초를 잘라냅니다.
        self.cm = CutMap(total_duration=30.0, kept=[(0.0, 10.0), (20.0, 30.0)])

    def test_잘린_구간의_단어가_빠진다(self):
        words = [
            {"word": "안녕", "start": 1.0, "end": 1.5},
            {"word": "그", "start": 12.0, "end": 12.3},
            {"word": "그", "start": 13.0, "end": 13.3},
            {"word": "하세요", "start": 21.0, "end": 21.5},
        ]
        text, removed = sa.surviving_text(words, self.cm)
        self.assertEqual(text, "안녕 하세요")
        self.assertEqual(removed, 2)

    def test_아무것도_안_잘리면_그대로(self):
        words = [{"word": "가", "start": 1.0, "end": 1.2}, {"word": "나", "start": 2.0, "end": 2.2}]
        text, removed = sa.surviving_text(words, self.cm)
        self.assertEqual(text, "가 나")
        self.assertEqual(removed, 0)

    def test_전부_잘리면_빈_문자열(self):
        words = [{"word": "어", "start": 12.0, "end": 12.3}]
        text, removed = sa.surviving_text(words, self.cm)
        self.assertEqual(text, "")
        self.assertEqual(removed, 1)


class BuildSubtitles(unittest.TestCase):
    def setUp(self):
        self.cm = CutMap(total_duration=30.0, kept=[(0.0, 10.0), (20.0, 30.0)])

    def test_필러가_자막_텍스트에서도_사라진다(self):
        aligned = [{
            "text": "어 안녕하세요", "source": "stt", "score": 0.0, "start": 1.0, "end": 3.0,
            "words": [{"word": "어", "start": 12.0, "end": 12.3},
                      {"word": "안녕하세요", "start": 2.0, "end": 2.6}],
        }]
        subs, meta = sa.build_subtitles(aligned, self.cm)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0].text, "안녕하세요")
        self.assertEqual(meta["cut_words"]["removed"], 1)

    def test_대본이_정본인_구간은_그대로_둔다(self):
        aligned = [{
            "text": "안녕하세요, 반갑습니다.", "source": "script", "score": 95.0,
            "start": 1.0, "end": 3.0,
            "words": [{"word": "안녕하세요", "start": 12.0, "end": 12.6}],   # 잘린 구간
        }]
        subs, _ = sa.build_subtitles(aligned, self.cm)
        self.assertEqual(" ".join(s.text for s in subs), "안녕하세요, 반갑습니다.")

    def test_모두_한_줄이고_겹치지_않는다(self):
        aligned = [{
            "text": "안녕하세요. 오늘은 포워딩 실무에서 자주 쓰는 B/L 발행 절차를 정리해 보겠습니다.",
            "source": "script", "score": 95.0, "start": 1.0, "end": 8.0, "words": [],
        }]
        subs, meta = sa.build_subtitles(aligned, self.cm, max_chars=36)
        self.assertTrue(meta["lines"]["all_single_line"])
        self.assertEqual(meta["lines"]["over_limit"], 0)
        for i in range(len(subs) - 1):
            self.assertLessEqual(subs[i].end, subs[i + 1].start + 1e-9)

    def test_용어_사전이_적용된다(self):
        aligned = [{"text": "cargowise 워크플로", "source": "stt", "score": 0.0,
                    "start": 1.0, "end": 3.0, "words": []}]
        subs, _ = sa.build_subtitles(aligned, self.cm, glossary_terms=["CargoWise"])
        self.assertIn("CargoWise", " ".join(s.text for s in subs))


class Srt(unittest.TestCase):
    def test_내보내고_다시_읽으면_같다(self):
        subs = [sub("a", 1.5, 3.25, "첫 번째 자막"), sub("b", 4.0, 6.5, "두 번째 자막")]
        parsed = sa.parse_srt(sa.to_srt(subs))
        self.assertEqual([p.text for p in parsed], ["첫 번째 자막", "두 번째 자막"])
        self.assertAlmostEqual(parsed[0].start, 1.5, places=3)
        self.assertAlmostEqual(parsed[1].end, 6.5, places=3)

    def test_내보낼_때_한_번_더_정리한다(self):
        # 겹치는 자막을 넣어도 SRT에는 겹치지 않게 나가야 합니다.
        text = sa.to_srt([sub("a", 0.0, 5.0, "하나"), sub("b", 1.0, 6.0, "둘")])
        parsed = sa.parse_srt(text)
        self.assertLessEqual(parsed[0].end, parsed[1].start + 1e-3)


class Glossary(unittest.TestCase):
    def test_켜진_용어만_모은다(self):
        terms = sa.enabled_terms({"물류": [
            {"term": "B/L", "enabled": True}, {"term": "FCL", "enabled": False},
        ]})
        self.assertIn("B/L", terms)
        self.assertNotIn("FCL", terms)

    def test_단어_경계를_지킨다(self):
        # 'API'가 'RAPIDS' 안에서 바뀌면 안 됩니다.
        self.assertEqual(sa.apply_glossary("RAPIDS api 호출", ["API"]), "RAPIDS API 호출")


if __name__ == "__main__":
    unittest.main()
