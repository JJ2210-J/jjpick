"""컷 편집 — 요청서 3.17(앞뒤 여유 분리)과 3.18(컷 서명)."""

import unittest

from . import _helper  # noqa: F401
from app.services import cut_edit as ce


def words(text, start=0.0, step=0.35, dur=0.3):
    out, t = [], start
    for w in text.split():
        out.append({"word": w, "start": round(t, 3), "end": round(t + dur, 3), "probability": 0.9})
        t += step
    return out


class TailHeadPadding(unittest.TestCase):
    """3.17 — 무음 구간의 시작은 말이 끝난 직후, 끝은 다음 말 시작 직전."""

    def test_앞뒤_여유가_따로_적용된다(self):
        cands = ce.silence_candidates([(10.0, 13.0)], [], tail_pad=0.35, head_pad=0.15)
        self.assertEqual(len(cands), 1)
        self.assertAlmostEqual(cands[0].start, 10.35, places=6)
        self.assertAlmostEqual(cands[0].end, 12.85, places=6)

    def test_꼬리_여유를_늘리면_남는_길이가_늘어난다(self):
        spans = [(t, t + 1.0) for t in range(5, 200, 10)]
        short = ce.build_cut_map(
            ce.silence_candidates(spans, [], tail_pad=0.15, head_pad=0.15), 300.0)
        long = ce.build_cut_map(
            ce.silence_candidates(spans, [], tail_pad=0.35, head_pad=0.15), 300.0)
        self.assertGreater(long.kept_duration, short.kept_duration)

    def test_여유를_빼고_남는_게_없으면_후보로_만들지_않는다(self):
        # 0.45초 무음에서 앞뒤 0.5초를 빼면 자를 게 없습니다 = 자연스러운 호흡
        cands = ce.silence_candidates([(10.0, 10.45)], [], tail_pad=0.35, head_pad=0.15)
        self.assertEqual(cands, [])


class Candidates(unittest.TestCase):
    def test_필러워드를_잡는다(self):
        w = words("어 그러니까 이제 시작합니다")
        found = ce.filler_candidates(w, ["어", "그러니까 이제"])
        self.assertEqual({c.type for c in found}, {ce.CUT_FILLER})
        self.assertGreaterEqual(len(found), 2)

    def test_반복은_마지막_하나만_남긴다(self):
        w = words("그 그 그 항구에")
        found = ce.repeat_candidates(w)
        self.assertEqual(len(found), 1)
        # 세 번째 '그'의 끝(= index 2의 end)보다 앞에서 끝나야 마지막이 남습니다.
        self.assertLess(found[0].end, w[2]["end"] + 0.06)

    def test_무음_안에_들어가는_필러는_중복으로_만들지_않는다(self):
        w = words("어 음", start=20.6)
        merged = ce.build_candidates([(20.0, 23.0)], w, ["어", "음"], total_duration=30.0)
        self.assertEqual([c.type for c in merged], [ce.CUT_SILENCE])

    def test_반복_안에_들어가는_필러는_반복으로만_보여준다(self):
        w = words("그 그 그 항구에")
        merged = ce.build_candidates([], w, ["그"], total_duration=30.0)
        types = [c.type for c in merged]
        self.assertIn(ce.CUT_REPEAT, types)
        # '그' 3개가 필러 3건으로 또 뜨면 검수 화면이 지저분해집니다.
        self.assertLessEqual(types.count(ce.CUT_FILLER), 1)


class CutMapConversion(unittest.TestCase):
    def setUp(self):
        self.cm = ce.build_cut_map(
            [ce.CutCandidate("a", ce.CUT_SILENCE, 10.0, 20.0),
             ce.CutCandidate("b", ce.CUT_SILENCE, 40.0, 45.0)],
            100.0,
        )

    def test_남는_구간(self):
        self.assertEqual(self.cm.kept, [(0.0, 10.0), (20.0, 40.0), (45.0, 100.0)])
        self.assertAlmostEqual(self.cm.kept_duration, 85.0)

    def test_원본에서_편집본으로(self):
        self.assertAlmostEqual(self.cm.original_to_edited(5.0), 5.0)
        self.assertAlmostEqual(self.cm.original_to_edited(30.0), 20.0)

    def test_잘린_구간은_None(self):
        self.assertIsNone(self.cm.original_to_edited(15.0))

    def test_clamped는_가장_가까운_남은_지점으로(self):
        self.assertAlmostEqual(self.cm.original_to_edited_clamped(15.0), 10.0)

    def test_편집본에서_원본으로(self):
        self.assertAlmostEqual(self.cm.edited_to_original(25.0), 35.0)

    def test_왕복해도_같은_지점(self):
        for t in (0.5, 9.9, 21.0, 50.0, 99.0):
            edited = self.cm.original_to_edited(t)
            if edited is None:
                continue
            self.assertAlmostEqual(self.cm.edited_to_original(edited), t, places=6)

    def test_편집본_한_구간이_원본에서는_여러_조각(self):
        """요청서 5차 함정: 고르는 건 편집본 시각인데 소재는 원본입니다."""
        spans = self.cm.edited_span_to_original(5.0, 40.0)
        self.assertEqual(len(spans), 3)
        self.assertAlmostEqual(spans[0][0], 5.0)
        self.assertAlmostEqual(spans[1][0], 20.0)
        self.assertAlmostEqual(spans[2][0], 45.0)


class Signature(unittest.TestCase):
    """3.18 — 드래프트와 컷 설정이 어긋나면 자막이 통째로 밀립니다."""

    def test_같은_설정이면_통과(self):
        sig = {"kept_duration": 623.0, "segment_count": 246}
        self.assertTrue(ce.compare_signatures(sig, dict(sig))["ok"])

    def test_요청서_실측_사례를_잡아낸다(self):
        result = ce.compare_signatures(
            {"kept_duration": 623.0, "segment_count": 246},
            {"kept_duration": 975.3, "segment_count": 440},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "signature_mismatch")
        self.assertIn("352.3초 차이", result["message"])

    def test_구간_수만_달라도_잡아낸다(self):
        result = ce.compare_signatures(
            {"kept_duration": 100.0, "segment_count": 10},
            {"kept_duration": 100.0, "segment_count": 11},
        )
        self.assertFalse(result["ok"])

    def test_드래프트가_없으면_이유를_알려준다(self):
        result = ce.compare_signatures(None, {"kept_duration": 1.0, "segment_count": 1})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_draft")


class Summary(unittest.TestCase):
    def test_40퍼센트를_넘으면_경고한다(self):
        cands = [ce.CutCandidate(f"s{i}", ce.CUT_SILENCE, i * 10.0, i * 10.0 + 5.0)
                 for i in range(10)]
        cm = ce.build_cut_map(cands, 100.0)
        summary = ce.cut_summary(cands, cm)
        self.assertTrue(summary["cut_ratio_warn"])
        self.assertTrue(summary["warnings"])

    def test_긴_무음_살리기(self):
        cands = [ce.CutCandidate("a", ce.CUT_SILENCE, 0.0, 5.0),
                 ce.CutCandidate("b", ce.CUT_SILENCE, 10.0, 11.0)]
        revived = ce.revive_long_silences(cands)
        self.assertEqual(revived, 1)
        self.assertFalse(cands[0].selected)
        self.assertTrue(cands[1].selected)


if __name__ == "__main__":
    unittest.main()
