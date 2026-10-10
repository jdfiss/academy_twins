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


if __name__ == "__main__":
    unittest.main()
