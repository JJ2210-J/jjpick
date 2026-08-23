"""자막 줄바꿈 규칙 (요청서 6절 2차)."""

import unittest

from . import _helper  # noqa: F401
from app.services import line_break as lb


class SplitByPunctuation(unittest.TestCase):
    def test_부호는_앞_조각에_붙는다(self):
        self.assertEqual(
            lb.split_by_punctuation("안녕하세요. 반갑습니다!"),
            ["안녕하세요.", "반갑습니다!"],
        )

    def test_연속_부호는_원문을_보존한다(self):
        self.assertEqual(lb.split_by_punctuation("정말요..."), ["정말요..."])
        self.assertEqual(lb.split_by_punctuation("진짜?!"), ["진짜?!"])

    def test_부호만_있는_조각은_자막이_되지_않는다(self):
        # 요청서 3.9: 문장부호 분할이 '.' 하나짜리 자막을 만들기도 합니다.
        result = lb.split_by_punctuation("네. . 알겠습니다.")
        self.assertEqual(result, ["네.", "알겠습니다."])
        for piece in result:
            self.assertFalse(lb.is_punctuation_only(piece))

    def test_구절_부호에서도_끊는다(self):
        self.assertEqual(
            lb.split_by_punctuation("그러니까 이제, 컨테이너가 도착하면"),
            ["그러니까 이제,", "컨테이너가 도착하면"],
        )


class SplitToLines(unittest.TestCase):
    def test_상한_이하면_그대로_둔다(self):
        self.assertEqual(lb.split_to_lines("짧은 문장", 36), ["짧은 문장"])

    def test_모든_조각이_상한을_지킨다(self):
        text = "컨테이너가 항구에 도착하고 나면 프리타임이 끝나는 시점부터 데머리지가 붙기 시작합니다"
        for piece in lb.split_to_lines(text, 36):
            self.assertLessEqual(len(piece), 36)

    def test_꽉_채우지_않고_고르게_나눈다(self):
        """요청서: 꽉 채우면 [34자] + [5자]처럼 짧은 꼬리가 생깁니다."""
        text = "컨테이너가 항구에 도착하고 나면 프리타임이 끝나는 시점부터 데머리지가 붙기 시작합니다"
        pieces = lb.split_to_lines(text, 36)
        lengths = [len(p) for p in pieces]
        # 가장 짧은 조각이 가장 긴 조각의 절반 미만이면 꼬리가 생긴 것입니다.
        self.assertGreaterEqual(min(lengths), max(lengths) * 0.5, f"길이 분포가 치우쳤습니다: {lengths}")

    def test_어절_중간을_끊지_않는다(self):
        text = "안녕하세요 포워딩 실무를 다루는 채널입니다 오늘도 잘 부탁드립니다 감사합니다"
        pieces = lb.split_to_lines(text, 20)
        rejoined = " ".join(pieces).split()
        self.assertEqual(rejoined, text.split())

    def test_하이픈으로_영단어를_자르지_않는다(self):
        text = "Demurrage Detention Consignee Shipper Booking Manifest CargoWise Incoterms"
        for piece in lb.split_to_lines(text, 20):
            self.assertNotIn("-", piece.replace("B/L", ""))

    def test_상한을_넘는_어절_하나는_그대로_둔다(self):
        long_token = "가" * 50
        self.assertEqual(lb.split_to_lines(long_token, 36), [long_token])


class BreakText(unittest.TestCase):
    def test_자막은_항상_한_줄이다(self):
        text = "안녕하세요. 오늘은 포워딩 실무에서 자주 쓰는 B/L 발행 절차를 정리해 보겠습니다."
        pieces = lb.break_text(text, 36)
        for piece in pieces:
            self.assertNotIn("\n", piece)
        self.assertTrue(lb.analyze_lines(pieces, 36)["all_single_line"])

    def test_빈_입력은_빈_목록(self):
        self.assertEqual(lb.break_text("", 36), [])
        self.assertEqual(lb.break_text("...", 36), [])


class DistributeTime(unittest.TestCase):
    def test_겹치지_않는다(self):
        pieces = ["가나다", "라마바사아", "자차카타파하"]
        times = lb.distribute_time(pieces, 10.0, 16.0)
        for i in range(len(times) - 1):
            self.assertLessEqual(times[i][1], times[i + 1][0] + 1e-9)

    def test_최소_길이를_지킨다(self):
        pieces = ["가", "나", "다"]
        for start, end in lb.distribute_time(pieces, 0.0, 5.0, min_duration=0.35):
            self.assertGreaterEqual(end - start, 0.35 - 1e-9)

    def test_구간이_짧아도_겹치지_않는다(self):
        pieces = ["가", "나", "다", "라"]
        times = lb.distribute_time(pieces, 0.0, 0.5, min_duration=0.35)
        for i in range(len(times) - 1):
            self.assertLessEqual(times[i][1], times[i + 1][0] + 1e-9)


if __name__ == "__main__":
    unittest.main()
