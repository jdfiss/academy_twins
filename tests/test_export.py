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

if __name__ == "__main__":
    unittest.main()
