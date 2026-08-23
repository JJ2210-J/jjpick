"""소스 타임라인 — 요청서 4절 '구간이 영상 경계를 가로지르면 반드시 쪼개십시오'."""

import unittest

from . import _helper  # noqa: F401
from app.services.sources import SourceTimeline


def probes():
    return [
        {"path": "/x/a.mp4", "name": "a.mp4", "duration": 12.0,
         "width": 1920, "height": 1080, "fps": 30, "audio_track_count": 1},
        {"path": "/x/b.mp4", "name": "b.mp4", "duration": 8.0,
         "width": 1280, "height": 720, "fps": 60, "audio_track_count": 0},
    ]


class Timeline(unittest.TestCase):
    def setUp(self):
        self.tl = SourceTimeline.from_probes(probes())

    def test_이어붙인_총_길이(self):
        self.assertAlmostEqual(self.tl.total_duration, 20.0)

    def test_두_번째_영상의_시작_위치(self):
        self.assertAlmostEqual(self.tl.clips[1].offset, 12.0)

    def test_캔버스는_가장_큰_해상도(self):
        self.assertEqual(self.tl.canvas(), {"width": 1920, "height": 1080})

    def test_fps는_가장_높은_값(self):
        self.assertEqual(self.tl.fps(), 60)

    def test_해상도와_fps가_섞이면_경고한다(self):
        mixed = self.tl.mixed_properties()
        self.assertTrue(mixed["resolution_mixed"])
        self.assertTrue(mixed["fps_mixed"])
        self.assertEqual(len(mixed["warnings"]), 3)     # 해상도 + fps + 오디오 없음

    def test_오디오가_없는_영상을_알려준다(self):
        joined = " ".join(self.tl.mixed_properties()["warnings"])
        self.assertIn("b.mp4", joined)


class Conversion(unittest.TestCase):
    def setUp(self):
        self.tl = SourceTimeline.from_probes(probes())

    def test_전역에서_지역으로(self):
        self.assertEqual(self.tl.global_to_local(5.0), (0, 5.0))
        self.assertEqual(self.tl.global_to_local(15.0), (1, 3.0))

    def test_지역에서_전역으로(self):
        self.assertAlmostEqual(self.tl.local_to_global(1, 3.0), 15.0)

    def test_왕복(self):
        for t in (0.0, 5.0, 11.9, 12.0, 19.0):
            index, local = self.tl.global_to_local(t)
            self.assertAlmostEqual(self.tl.local_to_global(index, local), t, places=6)


class SplitSpan(unittest.TestCase):
    def setUp(self):
        self.tl = SourceTimeline.from_probes(probes())

    def test_경계를_가로지르면_쪼갠다(self):
        pieces = self.tl.split_span(8.0, 16.0)
        self.assertEqual(len(pieces), 2)
        self.assertEqual(pieces[0]["clip_index"], 0)
        self.assertAlmostEqual(pieces[0]["local_start"], 8.0)
        self.assertAlmostEqual(pieces[0]["local_end"], 12.0)
        self.assertEqual(pieces[1]["clip_index"], 1)
        self.assertAlmostEqual(pieces[1]["local_start"], 0.0)
        self.assertAlmostEqual(pieces[1]["local_end"], 4.0)

    def test_한_영상_안이면_쪼개지_않는다(self):
        self.assertEqual(len(self.tl.split_span(2.0, 6.0)), 1)

    def test_쪼갠_조각의_합이_원래_길이와_같다(self):
        pieces = self.tl.split_span(3.0, 18.0)
        self.assertAlmostEqual(sum(p["duration"] for p in pieces), 15.0)

    def test_경계에_정확히_맞물리면_조각이_하나(self):
        self.assertEqual(len(self.tl.split_span(0.0, 12.0)), 1)

    def test_범위를_벗어나면_잘라낸다(self):
        pieces = self.tl.split_span(-5.0, 100.0)
        self.assertAlmostEqual(sum(p["duration"] for p in pieces), 20.0)

    def test_빈_구간은_빈_목록(self):
        self.assertEqual(self.tl.split_span(5.0, 5.0), [])

    def test_직렬화_왕복(self):
        restored = SourceTimeline.from_dict(self.tl.to_dict())
        self.assertEqual(len(restored.clips), 2)
        self.assertAlmostEqual(restored.total_duration, 20.0)
        self.assertEqual(restored.split_span(8.0, 16.0)[1]["clip_index"], 1)


if __name__ == "__main__":
    unittest.main()
