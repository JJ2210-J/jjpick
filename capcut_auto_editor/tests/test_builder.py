"""드래프트 생성 통합 테스트.

ffmpeg으로 짧은 테스트 영상을 만들고, 실제로 드래프트를 생성해
요청서 3.3 / 3.5 / 3.10 / 3.11 / 3.12가 결과 파일에 반영됐는지 확인합니다.
ffmpeg이나 pycapcut이 없으면 건너뜁니다.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from . import _helper  # noqa: F401
from app.config import ffmpeg_path

try:
    import pycapcut  # noqa: F401
    HAS_PYCAPCUT = True
except ImportError:
    HAS_PYCAPCUT = False


def make_video(path, seconds=6):
    subprocess.run([
        ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True, capture_output=True)


@unittest.skipUnless(ffmpeg_path() and HAS_PYCAPCUT, "ffmpeg 또는 pycapcut이 없어 건너뜁니다")
class BuildCutDraft(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.media = base / "media"
        cls.media.mkdir()
        cls.videos = [cls.media / "a.mp4", cls.media / "b.mp4"]
        for v in cls.videos:
            make_video(v)

        cls.root = base / "drafts"
        cls.root.mkdir()
        (cls.root / "root_meta_info.json").write_text(
            json.dumps({"all_draft_store": []}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _timeline(self):
        from app.services.audio_analysis import probe
        from app.services.sources import SourceTimeline
        return SourceTimeline.from_probes([probe(v) for v in self.videos])

    def _build(self, name, kept):
        from app.services import builder
        from app.services.cut_edit import CutMap
        timeline = self._timeline()
        cut_map = CutMap(total_duration=timeline.total_duration, kept=kept)
        return builder.build_cut_draft(
            draft_root=self.root, draft_name=name, timeline=timeline, cut_map=cut_map,
        ), timeline

    def test_경계를_가로지르는_구간이_여러_세그먼트가_된다(self):
        result, timeline = self._build("경계테스트", [(0.0, 3.0), (4.0, 8.0)])
        # 4~8초는 a.mp4(6초)와 b.mp4에 걸칩니다 = 조각 2개
        self.assertEqual(result["segment_count"], 3)

    def test_track_render_index가_교정된다(self):
        from app.services import capcut_draft as cd
        self._build("레이어테스트", [(0.0, 4.0)])
        data = cd.read_draft_content(self.root / "레이어테스트")
        for index, track in enumerate(data["tracks"]):
            self.assertEqual(track["track_render_index"], index)
            for seg in track["segments"]:
                self.assertEqual(seg["track_render_index"], index)

    def test_canvas_ratio가_교정된다(self):
        from app.services import capcut_draft as cd
        self._build("비율테스트", [(0.0, 4.0)])
        data = cd.read_draft_content(self.root / "비율테스트")
        self.assertEqual(data["canvas_config"]["ratio"], "16:9")

    def test_draft_meta_info_경로가_실제_폴더를_가리킨다(self):
        self._build("메타테스트", [(0.0, 4.0)])
        meta = json.loads((self.root / "메타테스트" / "draft_meta_info.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(meta["draft_name"], "메타테스트")
        self.assertIn("메타테스트", meta["draft_fold_path"])

    def test_레지스트리에_등록된다(self):
        self._build("등록테스트", [(0.0, 4.0)])
        store = json.loads((self.root / "root_meta_info.json")
                           .read_text(encoding="utf-8"))["all_draft_store"]
        self.assertIn("등록테스트", [e["draft_name"] for e in store])

    def test_컷_서명이_함께_돌아온다(self):
        result, _ = self._build("서명테스트", [(0.0, 3.0), (4.0, 8.0)])
        self.assertEqual(result["cut_signature"]["segment_count"], 2)
        self.assertAlmostEqual(result["cut_signature"]["kept_duration"], 7.0, places=1)

    def test_소재_길이를_넘는_구간은_clamp된다(self):
        """3.8 — pyCapCut은 1µs라도 넘으면 거부합니다."""
        result, _ = self._build("클램프테스트", [(0.0, 999.0)])
        self.assertGreater(result["segment_count"], 0)

    def test_넣을_구간이_없으면_이유를_알려준다(self):
        from app.services.capcut_draft import DraftError
        with self.assertRaises(DraftError) as ctx:
            self._build("빈테스트", [])
        self.assertIn("컷을 너무 많이 선택", str(ctx.exception))


@unittest.skipUnless(ffmpeg_path() and HAS_PYCAPCUT, "ffmpeg 또는 pycapcut이 없어 건너뜁니다")
class InjectSubtitles(unittest.TestCase):
    """3.5 — 캘리브레이션 원본 소재를 복제해 텍스트만 갈아끼웁니다."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.video = base / "a.mp4"
        make_video(cls.video)
        cls.root = base / "drafts"
        cls.root.mkdir()
        (cls.root / "root_meta_info.json").write_text(
            json.dumps({"all_draft_store": []}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        from app.services import builder, capcut_draft as cd
        from app.services.audio_analysis import probe
        from app.services.cut_edit import CutMap
        from app.services.sources import SourceTimeline

        timeline = SourceTimeline.from_probes([probe(self.video)])
        builder.build_cut_draft(
            draft_root=self.root, draft_name="자막테스트", timeline=timeline,
            cut_map=CutMap(total_duration=timeline.total_duration, kept=[(0.0, 5.0)]),
        )
        self.draft = self.root / "자막테스트"
        self.profile = cd.manual_style_profile(canvas_width=640, canvas_height=360)

    def test_자막이_들어가고_다시_읽어_확인된다(self):
        from app.services import builder
        from app.services.script_align import Subtitle
        subs = [Subtitle(id="a", start=0.5, end=2.0, text="첫 번째 자막"),
                Subtitle(id="b", start=2.0, end=4.0, text="두 번째 자막")]
        result = builder.inject_subtitles(
            draft_root=self.root, draft_dir=self.draft,
            subtitles=subs, style_profile=self.profile)
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["verified"], 2)

    def test_배경_설정이_보존된다(self):
        from app.services import builder, capcut_draft as cd
        from app.services.script_align import Subtitle
        builder.inject_subtitles(
            draft_root=self.root, draft_dir=self.draft,
            subtitles=[Subtitle(id="a", start=0.5, end=2.0, text="자막")],
            style_profile=self.profile)
        data = cd.read_draft_content(self.draft)
        material = data["materials"]["texts"][0]
        self.assertEqual(material["check_flag"] & 16, 16)
        self.assertGreaterEqual(material["background_style"], 1)

    def test_자막_트랙이_영상_트랙보다_위에_온다(self):
        from app.services import builder, capcut_draft as cd
        from app.services.script_align import Subtitle
        builder.inject_subtitles(
            draft_root=self.root, draft_dir=self.draft,
            subtitles=[Subtitle(id="a", start=0.5, end=2.0, text="자막")],
            style_profile=self.profile)
        data = cd.read_draft_content(self.draft)
        video = next(t for t in data["tracks"] if t["type"] == "video")
        text = next(t for t in data["tracks"] if t["type"] == "text")
        self.assertGreater(text["track_render_index"], video["track_render_index"])

    def test_다시_넣으면_기존_트랙을_교체한다(self):
        from app.services import builder, capcut_draft as cd
        from app.services.script_align import Subtitle
        for text in ("처음", "두번째"):
            builder.inject_subtitles(
                draft_root=self.root, draft_dir=self.draft,
                subtitles=[Subtitle(id="a", start=0.5, end=2.0, text=text)],
                style_profile=self.profile)
        data = cd.read_draft_content(self.draft)
        text_tracks = [t for t in data["tracks"] if t["type"] == "text"]
        self.assertEqual(len(text_tracks), 1)
        self.assertEqual(len(data["materials"]["texts"]), 1)
        self.assertEqual(json.loads(data["materials"]["texts"][0]["content"])["text"], "두번째")


if __name__ == "__main__":
    unittest.main()
