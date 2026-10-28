"""規則依入學年度取用：同一批次的學生各用自己年度的規則庫，缺年度時列入異常、不套用其他年度。

112 學年度規則庫是測試中由 114 規則庫改出來的虛構版本，只用來驗證切換機制，不代表正式規則。
"""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from academic_twin.api import KB_PATH, AcademicTwinService

ROSTER = "student_id,cohort\nS001,114\nS002,112\nS003,111\nS004,\n"


class CohortRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raw = json.loads(KB_PATH.read_text(encoding="utf-8"))
        raw["cohort"] = 112
        total = next(r for r in raw["requirements"] if r["kind"] == "total_credits")
        total["min_credits"] = 120  # 虛構：讓兩個年度的判定可以區分
        self.kb_112 = self.write("curriculum_fake_112.json", json.dumps(raw, ensure_ascii=False))
        self.roster = self.write("roster.csv", ROSTER)
        self.service = AcademicTwinService(roster_path=self.roster, extra_kb_paths=[self.kb_112])

    def write(self, name, text):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def total_need(student):
        return next(r["need"] for r in student["requirements"] if r["kind"] == "total_credits")

    def test_each_student_uses_own_cohort_rules(self):
        s114, s112 = self.service.student("S001"), self.service.student("S002")
        self.assertEqual((s114["cohort"], self.total_need(s114)), ("114", 128))
        self.assertEqual((s112["cohort"], self.total_need(s112)), ("112", 120))
        self.assertTrue(s112["rule_version"].startswith("112-"))
        self.assertEqual(s114["rule_version"], self.service.rule_version)

    def test_blank_cohort_uses_default_rules(self):
        self.assertEqual(self.service.student("S004")["cohort"], "114")

    def test_missing_cohort_is_anomaly_not_other_rules(self):
        self.assertEqual([r["id"] for r in self.service.batch()], ["S001", "S002", "S004"])
        anomaly = next(a for a in self.service.roster_anomalies() if a["id"] == "S003")
        self.assertEqual(anomaly["kind"], "other_cohort")
        self.assertIn("111", anomaly["message"])
        self.assertIn("112、114", anomaly["message"])
        with self.assertRaises(KeyError):
            self.service.student("S003")

    def test_batch_and_export_carry_each_students_rule_version(self):
        rows = {r["id"]: r for r in self.service.batch()}
        self.assertEqual(rows["S002"]["cohort"], 112)
        self.assertEqual(rows["S002"]["rule_version"], self.service.student("S002")["rule_version"])
        exported = {r["student_id"]: r for r in csv.DictReader(io.StringIO(self.service.export_csv()))}
        self.assertEqual(exported["S002"]["rule_version"], rows["S002"]["rule_version"])
        self.assertEqual(exported["S001"]["rule_version"], self.service.rule_version)

    def test_report_shows_student_rule_version(self):
        html = self.service.report_html("S002")
        self.assertIn(self.service.student("S002")["rule_version"], html)

    def test_both_sides_use_same_cohort_rules(self):
        office, twin = self.service.student("S002"), self.service.twin("S002")
        for key in ("cohort", "rule_version", "status", "total_credits"):
            self.assertEqual(office[key], twin[key])

    def test_review_records_student_rule_version(self):
        roster = self.write("roster_s003.csv", "student_id,cohort\nS003,112\n")
        service = AcademicTwinService(roster_path=roster, extra_kb_paths=[self.kb_112])
        s = service.add_review("S003", "XFER-01", "114-1", "exclude", "王助教")
        self.assertEqual(s["cohort"], "112")
        self.assertEqual(s["reviews"][0]["rule_version"], s["rule_version"])
        self.assertFalse(s["reviews"][0]["outdated"])

    def test_duplicate_cohort_rules_rejected(self):
        with self.assertRaises(ValueError):
            AcademicTwinService(extra_kb_paths=[KB_PATH])

    def test_meta_lists_loaded_cohorts(self):
        self.assertEqual(self.service.meta()["cohorts"], [112, 114])

    def test_single_ruleset_behaviour_unchanged(self):
        service = AcademicTwinService(roster_path=self.roster)
        self.assertEqual({a["id"] for a in service.roster_anomalies() if a["kind"] == "other_cohort"},
                         {"S002", "S003"})


if __name__ == "__main__":
    unittest.main()
