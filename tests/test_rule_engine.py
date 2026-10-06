import unittest
from pathlib import Path

from academic_twin import knowledge_base
from academic_twin.rule_engine import Record, Status, audit

KB = knowledge_base.load(Path(__file__).parent / "fixtures" / "curriculum_sample.json")
ALL_REQUIRED = ["IM1001", "IM1002", "IM1003", "IM1004", "IM1005", "IM2001",
                "IM2002", "IM2003", "IM2004", "IM3001", "IM3002", "GE0001", "GE0002", "GE0003"]


def rec(code, grade=80, credits=None, term="113-1"):
    credits = credits if credits is not None else (KB.courses[code].credits if code in KB.courses else 3)
    return Record(code, code, credits, grade, term)


def result(report, rid):
    return next(r for r in report.results if r.requirement.id == rid)


class RuleEngineTest(unittest.TestCase):
    def graduating(self, electives, free=0):
        records = [rec(c) for c in ALL_REQUIRED + electives]
        if free:
            records.append(rec("FREE", credits=free))
        return records

    def test_complete_record_passes(self):
        electives = ["IM3101", "IM3102", "IM3201", "IM3301", "IM3302", "IM3303"]
        report = audit(KB, self.graduating(electives, free=128 - 57))
        self.assertEqual(report.status, Status.PASS)

    def test_core_elective_not_double_counted(self):
        # 只修 4 門選修：2 門被 R3 吃掉，R4 只剩 6 學分
        report = audit(KB, self.graduating(["IM3101", "IM3102", "IM3201", "IM3301"], free=200))
        self.assertTrue(result(report, "R3").satisfied)
        r4 = result(report, "R4")
        self.assertFalse(r4.satisfied)
        self.assertEqual(r4.have, 6)
        self.assertEqual(report.status, Status.FAIL)

    def test_failed_then_retaken_counts_once(self):
        report = audit(KB, [rec("IM1002", grade=50, term="111-1"), rec("IM1002", grade=70, term="112-1")])
        self.assertIn("IM1002", result(report, "R1").used)
        self.assertEqual(report.transcript.failed, [])
        self.assertEqual(report.total_credits, 3)

    def test_failed_course_not_counted(self):
        report = audit(KB, [rec("IM1002", grade=59)])
        self.assertIn("IM1002", result(report, "R1").missing)
        self.assertEqual(len(report.transcript.failed), 1)

    def test_unknown_dept_code_counted_but_unverified(self):
        report = audit(KB, [rec("IM0999")])
        self.assertEqual([r.code for r in report.transcript.dept_unverified], ["IM0999"])
        self.assertEqual(result(report, "R5").have, 3)

    def test_unverified_dept_course_turns_pass_into_warning(self):
        electives = ["IM3101", "IM3102", "IM3201", "IM3301", "IM3302", "IM3303"]
        records = self.graduating(electives, free=128 - 57 - 3) + [rec("IM0999")]
        self.assertEqual(audit(KB, records).status, Status.WARNING)

    def test_unknown_category_needs_manual_review(self):
        report = audit(KB, [Record("X1", "抵免", 3, 80, "113-1", "抵免")])
        self.assertEqual(report.status, Status.MANUAL_REVIEW)

    def test_other_dept_course_counts_as_free_credits(self):
        report = audit(KB, [rec("EC1001", credits=3)])
        self.assertEqual(report.status, Status.FAIL)
        self.assertEqual(result(report, "R5").have, 3)

    def test_every_result_has_citation(self):
        report = audit(KB, [])
        for r in report.results:
            self.assertTrue(r.citation.strip())

    def test_invalid_kb_reference_rejected(self):
        import json, tempfile
        raw = json.loads((Path(__file__).parent / "fixtures" / "curriculum_sample.json").read_text(encoding="utf-8"))
        raw["requirements"][0]["courses"].append("NOPE")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(raw, f)
        with self.assertRaises(ValueError):
            knowledge_base.load(f.name)
        Path(f.name).unlink()


if __name__ == "__main__":
    unittest.main()
