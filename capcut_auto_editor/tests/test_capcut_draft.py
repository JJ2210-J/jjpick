"""드래프트 교정 — 요청서 3.2 / 3.3 / 3.4 / 3.5 / 3.6 / 3.10."""

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from . import _helper  # noqa: F401
from app.services import capcut_draft as cd


class TrackRenderIndex(unittest.TestCase):
    """3.3 — 실물은 트랙에 키가 없고 **세그먼트에 0**이 하드코딩됩니다."""

    def test_트랙과_세그먼트를_모두_교정한다(self):
        data = {"tracks": [
            {"type": "video", "segments": [{"track_render_index": 0}, {"track_render_index": 0}]},
            {"type": "text", "segments": [{"track_render_index": 0}]},
        ]}
        changed = cd.fix_track_render_index(data)
        self.assertEqual(data["tracks"][0]["track_render_index"], 0)
        self.assertEqual(data["tracks"][1]["track_render_index"], 1)
        self.assertEqual([s["track_render_index"] for s in data["tracks"][1]["segments"]], [1])
        self.assertEqual(changed["tracks"], 2)
        self.assertEqual(changed["segments"], 1)

    def test_영상과_자막이_같은_레이어가_되지_않는다(self):
        data = {"tracks": [
            {"type": "video", "segments": [{"track_render_index": 0}]},
            {"type": "text", "segments": [{"track_render_index": 0}]},
        ]}
        cd.fix_track_render_index(data)
        layers = [t["track_render_index"] for t in data["tracks"]]
        self.assertEqual(len(set(layers)), len(layers), "레이어가 겹치면 자막이 영상에 가려집니다")

    def test_이미_맞으면_바꾸지_않는다(self):
        data = {"tracks": [{"type": "video", "track_render_index": 0,
                            "segments": [{"track_render_index": 0}]}]}
        self.assertEqual(cd.fix_track_render_index(data), {"tracks": 0, "segments": 0})


class CanvasRatio(unittest.TestCase):
    """3.10 — dumps()가 ratio를 항상 'original'로 씁니다."""

    def test_세로는_9대16으로_교정된다(self):
        data = {"canvas_config": {"width": 1080, "height": 1920, "ratio": "original"}}
        self.assertEqual(cd.fix_canvas_ratio(data), "9:16")
        self.assertEqual(data["canvas_config"]["ratio"], "9:16")

    def test_가로는_16대9(self):
        self.assertEqual(cd.ratio_for(1920, 1080), "16:9")

    def test_아는_비율이_없으면_original(self):
        self.assertEqual(cd.ratio_for(999, 1234), "original")

    def test_이미_맞으면_None(self):
        data = {"canvas_config": {"width": 1080, "height": 1920, "ratio": "9:16"}}
        self.assertIsNone(cd.fix_canvas_ratio(data))


class Coordinates(unittest.TestCase):
    """3.2 — 정규화 좌표, 위쪽이 양수."""

    def test_요청서의_4K_함정을_재현한다(self):
        # 4K 기준 -788px을 1080 캔버스에 그대로 넣으면 화면 밖입니다.
        wrong = -788 / (1080 / 2)
        self.assertLess(wrong, -1.0, "화면 밖이어야 합니다")
        self.assertAlmostEqual(wrong, -1.4593, places=4)

    def test_1080에서_같은_위치는_394px(self):
        self.assertAlmostEqual(cd.px_to_norm_y(-394, 1080), -0.7296, places=4)

    def test_위쪽이_양수다(self):
        self.assertGreater(cd.px_to_norm_y(300, 1080), 0)

    def test_왕복(self):
        self.assertAlmostEqual(cd.norm_y_to_px(cd.px_to_norm_y(-394, 1080), 1080), -394)


class Colors(unittest.TestCase):
    """pyCapCut은 타입마다 색 표현이 다릅니다 (investigation.md 1절)."""

    def test_hex를_0에서_1_사이_튜플로(self):
        self.assertEqual(cd.hex_to_rgb_tuple("#000000"), (0.0, 0.0, 0.0))
        self.assertEqual(cd.hex_to_rgb_tuple("#ffffff"), (1.0, 1.0, 1.0))

    def test_짧은_형식도_읽는다(self):
        self.assertEqual(cd.hex_to_rgb_tuple("#fff"), (1.0, 1.0, 1.0))

    def test_잘못된_값은_흰색으로(self):
        self.assertEqual(cd.hex_to_rgb_tuple("이상한값"), (1.0, 1.0, 1.0))

    def test_왕복(self):
        self.assertEqual(cd.rgb_tuple_to_hex(cd.hex_to_rgb_tuple("#3b6ea5")), "#3b6ea5")


class FontMatching(unittest.TestCase):
    """3.6 — 'Bold'만 맞아도 통과시키면 Arial Bold -> NotoSansKR-Bold 오매칭이 납니다."""

    def test_계열과_무게를_분리한다(self):
        self.assertEqual(cd.split_font_name("Pretendard-Bold"), ("pretendard", "bold"))
        self.assertEqual(cd.split_font_name("Arial Bold"), ("arial", "bold"))
        self.assertEqual(cd.split_font_name("Pretendard"), ("pretendard", ""))

    def test_다른_계열은_섞이지_않는다(self):
        arial = cd.split_font_name("Arial Bold")
        noto = cd.split_font_name("NotoSansKR-Bold")
        self.assertEqual(arial[1], noto[1])          # 무게는 같지만
        self.assertNotEqual(arial[0], noto[0])       # 계열이 다르므로 매칭되면 안 됩니다


class ManualProfile(unittest.TestCase):
    """3.4 — 수동 폴백에서도 배경이 나오는 값을 반드시 채웁니다."""

    def test_배경_비트와_스타일이_채워진다(self):
        profile = cd.manual_style_profile(canvas_width=1080, canvas_height=1920)
        material = profile["material_template"]
        self.assertEqual(material["check_flag"] & 16, 16, "배경 비트가 없으면 배경이 안 나옵니다")
        self.assertGreaterEqual(material["background_style"], 1, "0이면 배경이 안 나옵니다")
        self.assertTrue(profile["estimated"], "추정값임을 UI가 표시해야 합니다")


class GraftTextMaterial(unittest.TestCase):
    """3.5 — 빈 껍데기에 스타일을 얹지 않고 원본 소재를 복제해 텍스트만 바꿉니다."""

    def setUp(self):
        self.profile = cd.manual_style_profile(canvas_width=1920, canvas_height=1080)

    def test_원본_필드가_보존된다(self):
        material = cd.graft_text_material(self.profile, "새 자막")
        template = self.profile["material_template"]
        for key in ("check_flag", "background_style", "background_color", "line_max_width"):
            self.assertEqual(material[key], template[key])

    def test_텍스트만_바뀐다(self):
        material = cd.graft_text_material(self.profile, "새 자막입니다")
        self.assertEqual(json.loads(material["content"])["text"], "새 자막입니다")

    def test_range가_새_텍스트_길이에_맞춰진다(self):
        text = "가나다라마"
        material = cd.graft_text_material(self.profile, text)
        self.assertEqual(json.loads(material["content"])["styles"][0]["range"], [0, len(text)])

    def test_소재_id가_매번_새로_생긴다(self):
        a = cd.graft_text_material(self.profile, "하나")
        b = cd.graft_text_material(self.profile, "둘")
        self.assertNotEqual(a["id"], b["id"])


class PhotoDetection(unittest.TestCase):
    """3차 — materials.videos[].type == 'photo'로 1차 판정, 확장자로 교차검증."""

    def test_type이_photo면_이미지(self):
        self.assertTrue(cd.is_photo_material({"type": "photo", "path": "a.bin"}))

    def test_확장자로_교차검증한다(self):
        self.assertTrue(cd.is_photo_material({"type": "", "path": "C:/x/b.PNG"}))

    def test_영상은_아니다(self):
        self.assertFalse(cd.is_photo_material({"type": "video", "path": "c.mp4"}))


class DraftMetaAndRegistry(unittest.TestCase):
    """3.11 / 3.12 — 경로 교정과 레지스트리 등록."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.draft = self.root / "프로젝트"
        self.draft.mkdir()
        (self.draft / "draft_content.json").write_text("{}", encoding="utf-8")
        (self.draft / "draft_meta_info.json").write_text(
            json.dumps({"draft_fold_path": "C:/옛날/경로", "draft_name": "옛날이름"}),
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_경로가_실제_폴더로_덮어써진다(self):
        cd.fix_draft_meta_info(self.draft)
        meta = json.loads((self.draft / "draft_meta_info.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["draft_name"], "프로젝트")
        self.assertEqual(meta["draft_fold_path"], str(self.draft).replace("\\", "/"))

    def test_기존_엔트리의_키_구성을_그대로_복제한다(self):
        (self.root / "root_meta_info.json").write_text(json.dumps({"all_draft_store": [{
            "draft_id": "OLD", "draft_name": "기존", "draft_fold_path": "C:/x",
            "draft_json_file": "C:/x/draft_content.json", "draft_cover": "c.jpg",
            "draft_removable": True, "tm_draft_create": 1, "tm_draft_modified": 1,
        }]}), encoding="utf-8")

        self.assertTrue(cd.register_in_root_meta(self.root, self.draft))
        store = json.loads((self.root / "root_meta_info.json").read_text(encoding="utf-8"))["all_draft_store"]
        self.assertEqual(len(store), 2)
        self.assertEqual(sorted(store[0].keys()), sorted(store[1].keys()))
        self.assertEqual(store[0]["draft_name"], "프로젝트")

    def test_두_번_등록해도_중복되지_않는다(self):
        (self.root / "root_meta_info.json").write_text(
            json.dumps({"all_draft_store": []}), encoding="utf-8")
        cd.register_in_root_meta(self.root, self.draft)
        cd.register_in_root_meta(self.root, self.draft)
        store = json.loads((self.root / "root_meta_info.json").read_text(encoding="utf-8"))["all_draft_store"]
        self.assertEqual(len(store), 1)

    def test_레지스트리가_없으면_조용히_건너뛴다(self):
        self.assertFalse(cd.register_in_root_meta(self.root, self.draft))


class DraftRootDetection(unittest.TestCase):
    """2절 — 경로를 소스에 하드코딩하지 않고 판정 규칙만 검증합니다."""

    def test_클라우드와_템플릿_폴더는_제외한다(self):
        self.assertTrue(cd_excluded("com.lveditor.cloud.draft_123"))
        self.assertTrue(cd_excluded("com.lveditor.textTemplate.draft"))
        self.assertFalse(cd_excluded("com.lveditor.draft"))


def cd_excluded(name):
    from app.config import is_excluded_projects_dir
    return is_excluded_projects_dir(name)


if __name__ == "__main__":
    unittest.main()
