import json
import tempfile
import unittest
from pathlib import Path

from academic_twin.api import AcademicTwinService
from academic_twin.reviews import ReviewStore

XFER = ("S003", "XFER-01", "114-1")  # 轉學抵免，類別「抵免」不在規則庫 → Manual Review


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.service = AcademicTwinService()  # 覆核紀錄只在記憶體

    def decide(self, action, **kw):
        self.service.add_review(*XFER, action, "王助教", **kw)
        return self.service.student("S003")

    def test_confirming_as_dept_elective_counts_toward_dept_credits(self):
        before = self.service.student("S003")
        self.assertEqual(before["status"], "MANUAL_REVIEW")
        s = self.decide("dept_elective")
        self.assertNotEqual(s["status"], "MANUAL_REVIEW")
        self.assertEqual(s["credit_breakdown"]["本系選修"], before["credit_breakdown"].get("本系選修", 0) + 3)
        self.assertEqual(s["reviews"][0]["reviewer"], "王助教")

    def test_excluding_course_keeps_credits_out(self):
        before = self.service.student("S003")["total_credits"]
        s = self.decide("exclude", note="非本系認可")
        self.assertEqual(s["total_credits"], before)
        self.assertEqual(s["reviews"][0]["note"], "非本系認可")

    def test_assigning_category_resolves_manual_review(self):
        self.assertEqual(self.service.student("S003")["status"], "MANUAL_REVIEW")
        self.service.add_review("S003", "XFER-01", "114-1", "category", "王助教", category="通識")
        s = self.service.student("S003")
        self.assertNotEqual(s["status"], "MANUAL_REVIEW")
        self.assertEqual(s["reviews"][0]["label"], "歸入「通識」")

    def test_revoke_restores_flag_and_keeps_history(self):
        self.service.add_review("S003", "XFER-01", "114-1", "outside", "王助教")
        self.service.add_review("S003", "XFER-01", "114-1", "revoke", "李組長", note="待查抵免文件")
        s = self.service.student("S003")
        self.assertEqual(s["status"], "MANUAL_REVIEW")
        self.assertEqual(s["reviews"], [])
        self.assertEqual([e["action"] for e in s["review_history"]], ["revoke", "outside"])

    def test_cannot_override_records_the_rules_already_decide(self):
        with self.assertRaises(ValueError):
            self.service.add_review("S001", "IM1001", "114-1", "exclude", "王助教")  # 規則庫必修
        with self.assertRaises(ValueError):
            self.service.add_review("S001", "IM9001", "117-1", "exclude", "王助教")  # IM 課號即本系選修

    def test_rejects_missing_reviewer_and_unknown_category(self):
        with self.assertRaises(ValueError):
            self.service.add_review(*XFER, "dept_elective", "  ")
        with self.assertRaises(ValueError):
            self.service.add_review("S003", "XFER-01", "114-1", "category", "王助教", category="不存在")

    def test_batch_and_export_count_reviews(self):
        self.decide("outside")
        row = next(r for r in self.service.batch() if r["id"] == "S003")
        self.assertEqual((row["status"], row["reviewed"]), ("FAIL", 1))
        self.assertIn("S003,FAIL,", self.service.export_csv())

    def test_decisions_persist_and_flag_rule_changes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reviews" / "decisions.json"
            AcademicTwinService(reviews_path=path).add_review("S003", "XFER-01", "114-1", "outside", "王助教")
            self.assertEqual(len(ReviewStore(path).entries), 1)

            reloaded = AcademicTwinService(reviews_path=path)
            self.assertFalse(reloaded.student("S003")["reviews"][0]["outdated"])

            data = json.loads(path.read_text(encoding="utf-8"))
            data[0]["rule_version"] = "114-old"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertTrue(AcademicTwinService(reviews_path=path).student("S003")["reviews"][0]["outdated"])


if __name__ == "__main__":
    unittest.main()
