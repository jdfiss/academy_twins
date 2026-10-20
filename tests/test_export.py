import csv
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from academic_twin.api import KB_PATH, AcademicTwinService


class ExportTest(unittest.TestCase):
    def rows(self, service: AcademicTwinService) -> list[dict]:
        text = service.export_csv(audited_at=datetime(2026, 10, 10, 9, 30))
        return list(csv.DictReader(io.StringIO(text)))

    def test_export_covers_audited_students_and_roster_anomalies(self):
        service = AcademicTwinService()
        rows = self.rows(service)
        audited = [r["student_id"] for r in rows if not r["status"].startswith("ROSTER_")]
        self.assertEqual(audited, [s["id"] for s in service.batch()])
        self.assertIn(("S005", "ROSTER_NO_RECORDS"), {(r["student_id"], r["status"]) for r in rows})

    def test_every_row_carries_rule_version_and_time(self):
        service = AcademicTwinService()
        for row in self.rows(service):
            self.assertEqual(row["rule_version"], service.rule_version)
            self.assertEqual(row["audited_at"], "2026-10-10T09:30:00")

    def test_rule_version_changes_with_rule_file(self):
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / "kb.json"
            copy.write_bytes(KB_PATH.read_bytes() + b"\n")
            self.assertNotEqual(AcademicTwinService(kb_path=copy).rule_version,
                                AcademicTwinService().rule_version)


class ReportTest(unittest.TestCase):
    def test_report_has_verdict_requirements_and_version(self):
        service = AcademicTwinService()
        html = service.report_html("S003", audited_at=datetime(2026, 10, 11, 9, 0))
        for text in ("S003", "需人工判斷（Manual Review）", "R12", "？ 待確認", service.rule_version,
                     "2026-10-11T09:00:00", "XFER-01"):
            self.assertIn(text, html)

    def test_report_escapes_uploaded_text(self):
        service = AcademicTwinService()
        service.load_data(records=("x.csv", "student_id,code,name,credits,grade,term\nS1,IM9001,<script>,3,80,114-1\n"),
                          roster=("r.csv", "student_id\nS1\n"))
        html = service.report_html("S1")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_unknown_student_raises(self):
        with self.assertRaises(KeyError):
            AcademicTwinService().report_html("S999")


class DemoFlagsTest(unittest.TestCase):
    """A 組虛構示範的核心、兩端輸出、覆核與匯出整合驗收。"""

    def setUp(self):
        root = Path(__file__).parent.parent
        self.service = AcademicTwinService(root / "data" / "curriculum_demo_flags.json",
                                           root / "data" / "students" / "demo_flags_records.csv",
                                           root / "data" / "students" / "demo_flags_roster.csv")

    def test_demo_covers_three_flags_and_distinguishes_actual_shortfalls(self):
        rows = {r["id"]: r for r in self.service.batch()}
        flagged = rows["DEMO-FLAGS"]
        self.assertEqual({f["type"] for f in flagged["flags"]},
                         {"overflow", "repeatable", "substitution_pending"})
        self.assertEqual((flagged["status"], flagged["total_credits"], flagged["action_count"]), ("FAIL", 13, 3))
        self.assertEqual((rows["DEMO-CLEAN"]["status"], rows["DEMO-CLEAN"]["flags"]), ("PASS", []))
        self.assertEqual(rows["DEMO-MISSING"]["flags"], [])
        self.assertEqual(rows["DEMO-REVIEW"]["status"], "MANUAL_REVIEW")

    def test_both_sides_preserve_handoff_with_real_detected_flags(self):
        for row in self.service.batch():
            with self.subTest(student=row["id"]):
                office = self.service.student(row["id"])
                twin = self.service.twin(row["id"])
                for key in ("status", "total_credits", "requirements", "credit_breakdown", "flags", "actions"):
                    self.assertEqual(office[key], twin[key])
                for flag in office["flags"]:
                    self.assertEqual(set(flag), {"type", "message", "credits", "courses", "requirement_ids"})
                for action in office["actions"]:
                    self.assertEqual(set(action), {"type", "title", "office", "requirement_ids", "courses", "from_flag"})

    def test_duplicate_review_exclusion_and_revocation_reaudit(self):
        sid = "DEMO-REVIEW"
        reviewed = self.service.add_review(sid, "DEMO-ONCE", "113-2", "exclude", "虛構承辦人")
        self.assertEqual(reviewed["status"], "FAIL")
        self.assertEqual(reviewed["total_credits"], 3)
        self.assertEqual(reviewed["actions"], [])
        revoked = self.service.add_review(sid, "DEMO-ONCE", "113-2", "revoke", "虛構承辦人")
        self.assertEqual(revoked["status"], "MANUAL_REVIEW")
        self.assertEqual(len(revoked["actions"]), 1)

    def test_csv_preserves_flags_actions_and_anomaly_rows(self):
        rows = {r["student_id"]: r for r in csv.DictReader(io.StringIO(self.service.export_csv()))}
        row = rows["DEMO-FLAGS"]
        self.assertEqual(set(row["flag_types"].split("；")), {"overflow", "repeatable", "substitution_pending"})
        self.assertIn("虛構系辦", row["actions"])
        self.assertIn("外系總上限", row["flag_messages"])
        self.assertEqual(rows["DEMO-CLEAN"]["flag_types"], "")
        self.service.load_data(roster=("r.csv", "student_id\nDEMO-FLAGS\nDEMO-ABSENT\n"))
        rows = list(csv.DictReader(io.StringIO(self.service.export_csv())))
        self.assertTrue(any(r["status"] == "ROSTER_NO_RECORDS" for r in rows))

    def test_printed_report_contains_flags_actions_and_escapes_office(self):
        from dataclasses import replace
        kb = self.service.kb
        kb.courses["DEMO-OLD"] = replace(kb.courses["DEMO-OLD"], substitution_office="<script>虛構單位</script>")
        html = self.service.report_html("DEMO-FLAGS")
        for text in ("疑似誤判提示", "需辦理事項", "DEMO-OLD", "外系總上限", "D1"):
            self.assertIn(text, html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

if __name__ == "__main__":
    unittest.main()
