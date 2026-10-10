import tempfile
import unittest
from pathlib import Path

from academic_twin.api import AcademicTwinService
from academic_twin.roster import check_roster, load_roster

ROOT = Path(__file__).parent.parent


class RosterTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_every_roster_student_is_audited_or_reported(self):
        roster = ["S001", "S002", "S009", "S002"]
        check = check_roster(roster, {"S001", "S002", "S003"})
        self.assertEqual(check.to_audit, ["S001", "S002"])
        self.assertEqual(check.no_records, ["S009"])
        self.assertEqual(check.not_on_roster, ["S003"])
        self.assertEqual(check.duplicates, ["S002"])
        self.assertEqual(set(roster), set(check.to_audit) | set(check.no_records))

    def test_load_roster_reports_blank_ids_with_line_numbers(self):
        path = self.write("student_id,name\nS001,甲\n,乙\n S002 ,丙\n")
        roster = load_roster(path)
        self.assertEqual(roster.ids, ["S001", "S002"])
        self.assertEqual(roster.blank_rows, [3])

    def test_load_roster_accepts_excel_bom(self):
        path = self.write("﻿student_id\nS001\n")
        self.assertEqual(load_roster(path).ids, ["S001"])

    def test_english_passed_column_is_optional_and_validated(self):
        roster = load_roster(self.write("student_id,english_passed\nS001,Y\nS002,\nS003,否\n"))
        self.assertEqual(roster.english_passed, {"S001": True, "S003": False})
        with self.assertRaises(ValueError) as ctx:
            load_roster(self.write("student_id,english_passed\nS001,maybe\n"))
        self.assertIn("第 2 行", str(ctx.exception))

    def test_other_cohort_students_reported_not_audited(self):
        service = AcademicTwinService(roster_path=self.write("student_id,cohort\nS001,114\nS002,113\nS003,\n"))
        self.assertEqual([r["id"] for r in service.batch()], ["S001", "S003"])  # 空白視為 114
        anomalies = {(a["id"], a["kind"]) for a in service.roster_anomalies()}
        self.assertIn(("S002", "other_cohort"), anomalies)
        self.assertEqual(service.data_source()["roster_count"], 3)

    def test_cohort_must_be_a_year(self):
        with self.assertRaises(ValueError):
            load_roster(self.write("student_id,cohort\nS001,一一四\n"))

    def test_load_roster_requires_student_id_column(self):
        with self.assertRaises(ValueError):
            load_roster(self.write("學號\nS001\n"))

    def test_service_batch_follows_roster(self):
        service = AcademicTwinService(roster_path=self.write("student_id\nS002\nS777\n"))
        self.assertEqual([r["id"] for r in service.batch()], ["S002"])
        anomalies = {(a["id"], a["kind"]) for a in service.roster_anomalies()}
        self.assertIn(("S777", "no_records"), anomalies)
        self.assertIn(("S001", "not_on_roster"), anomalies)

    def test_sample_roster_flags_student_without_records(self):
        service = AcademicTwinService()
        self.assertEqual([a["id"] for a in service.roster_anomalies()], ["S005"])


if __name__ == "__main__":
    unittest.main()
