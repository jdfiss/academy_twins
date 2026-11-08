"""本學期修課試算（B 組）：學生輸入的課，勾選者假設及格交給審查引擎重算，不影響正式審查結果。"""
import json
import unittest

from academic_twin.api import AcademicTwinService

# S002 修課紀錄到 115-1，本學期為 115-2
CURRENT = [
    {"code": "IM3031", "name": "亂填的課名", "credits": 9, "category": "", "counted": True},
    {"code": "MT2042", "counted": True},
    {"code": "CC0215", "name": "科學與生活", "credits": 2, "category": "自然科學", "counted": True},
]


def planned(sim):
    return {c["code"] for t in sim["base"]["terms"] for c in t["courses"]}


class CurrentTermTest(unittest.TestCase):
    def setUp(self):
        self.service = AcademicTwinService()

    def test_counted_courses_are_audited_and_plan_starts_next_term(self):
        sim = self.service.simulate("S002", current=CURRENT)
        cur = sim["current"]
        self.assertEqual(cur["term"], "115-2")
        self.assertEqual(sim["start"], "116-1")
        # 規則庫的課以規則庫課名、學分為準，不採學生填的
        self.assertEqual(cur["courses"][0]["name"], "電子商務")
        self.assertEqual(cur["trial_total_credits"] - cur["official_total_credits"], 8)
        self.assertFalse({"IM3031", "MT2042"} & planned(sim))

    def test_unchecked_courses_are_planned_again(self):
        current = [dict(c, counted=c["code"] != "IM3031") for c in CURRENT]
        sim = self.service.simulate("S002", current=current)
        self.assertIn("IM3031", planned(sim))
        self.assertNotIn("MT2042", planned(sim))
        self.assertEqual(sim["current"]["trial_total_credits"] - sim["current"]["official_total_credits"], 5)

    def test_json_string_from_query(self):
        sim = self.service.simulate("S002", current=json.dumps(CURRENT))
        self.assertEqual(len(sim["current"]["courses"]), 3)

    def test_official_result_unchanged(self):
        before = self.service.student("S002")
        self.service.simulate("S002", current=CURRENT)
        after = self.service.student("S002")
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(before["total_credits"], after["total_credits"])
        self.assertEqual([r["satisfied"] for r in before["requirements"]],
                         [r["satisfied"] for r in after["requirements"]])

    def test_without_input_keeps_old_behaviour(self):
        for current in (None, [], ""):
            sim = self.service.simulate("S002", current=current)
            self.assertIsNone(sim["current"])
            self.assertEqual(sim["start"], "115-2")

    def test_ask_uses_current_term(self):
        from unittest import mock
        with mock.patch("academic_twin.student_api.parse_question") as parse:
            self.service.ask("S002", "大三下交換會延畢嗎", current=CURRENT)
        planned_terms = parse.call_args.args[2]
        self.assertNotIn("IM3031", planned_terms)
        self.assertTrue(all(t >= "116-1" for t in planned_terms.values()))

    def test_invalid_input_rejected(self):
        bad = [
            "not json",
            {"code": "IM3031"},
            [{"code": ""}],
            [{"code": "IM3031"}, {"code": "IM3031"}],
            [{"code": "CC0215", "name": "科學與生活", "credits": 2, "category": "不存在的類別"}],
            [{"code": "CC0215", "name": "", "credits": 2}],
            [{"code": "CC0215", "name": "科學與生活", "credits": 30}],
            [{"code": f"X{i}", "name": "課", "credits": 1} for i in range(16)],
            [{"code": "IM1001"}],  # 已通過的本系課程
        ]
        for current in bad:
            with self.subTest(current=current), self.assertRaises(ValueError):
                self.service.simulate("S002", current=current)


if __name__ == "__main__":
    unittest.main()
