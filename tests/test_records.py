import unittest

from academic_twin.api import STUDENTS_PATH, AcademicTwinService
from academic_twin.records import RecordsError, parse_records

HEADER = "student_id,code,name,credits,grade,term,category\n"


class ParseRecordsTest(unittest.TestCase):
    def errors(self, text: str) -> list[str]:
        with self.assertRaises(RecordsError) as ctx:
            parse_records(text)
        return ctx.exception.errors

    def test_sample_file_is_valid(self):
        students = parse_records(STUDENTS_PATH.read_text(encoding="utf-8-sig"))
        self.assertIn("S001", students)

    def test_accepts_bom_missing_category_and_blank_lines(self):
        students = parse_records("﻿student_id,code,name,credits,grade,term\nS1,IM1001,計概,3,80,114-1\n,,,,,\n")
        self.assertEqual(students["S1"][0].category, "")

    def test_missing_columns_listed(self):
        [msg] = self.errors("student_id,code,name\nS1,IM1001,計概\n")
        self.assertIn("credits", msg)
        self.assertIn("term", msg)

    def test_every_bad_row_reported_with_line_number(self):
        errors = self.errors(HEADER
                             + "S1,IM1001,計概,3,80,114-1,\n"
                             + "S1,IM1002,程設,三,80,114-1,\n"
                             + ",IM1003,微積分,3,120,1141,\n")
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[0].startswith("第 3 行"))
        self.assertIn("學分", errors[0])
        self.assertTrue(errors[1].startswith("第 4 行"))
        for part in ("student_id 空白", "成績", "學期"):
            self.assertIn(part, errors[1])

    def test_empty_file_rejected(self):
        self.assertTrue(self.errors(HEADER))


class LoadDataTest(unittest.TestCase):
    def test_upload_replaces_records_and_reaudits(self):
        service = AcademicTwinService()
        service.load_data(records=("new.csv", HEADER + "S005,IM1001,計概,3,80,114-1,\n"))
        self.assertEqual([r["id"] for r in service.batch()], ["S005"])
        self.assertEqual(service.data_source()["records"], "new.csv")

    def test_bad_upload_keeps_current_data(self):
        service = AcademicTwinService()
        before = service.batch()
        with self.assertRaises(RecordsError) as ctx:
            service.load_data(records=("bad.csv", HEADER + "S1,IM1001,計概,x,80,114-1,\n"),
                              roster=("bad_roster.csv", "學號\nS1\n"))
        self.assertEqual(len(ctx.exception.errors), 2)  # 兩份的錯誤都回報
        self.assertEqual(service.batch(), before)
        self.assertEqual(service.data_source()["records"], STUDENTS_PATH.name)

    def test_roster_only_upload_keeps_records(self):
        service = AcademicTwinService()
        service.load_data(roster=("r.csv", "student_id\nS001\n"))
        self.assertEqual([r["id"] for r in service.batch()], ["S001"])
        self.assertIn("S002", {a["id"] for a in service.roster_anomalies()})


if __name__ == "__main__":
    unittest.main()
