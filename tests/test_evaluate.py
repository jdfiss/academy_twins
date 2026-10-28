import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from academic_twin import evaluate
from academic_twin.api import ROOT, CoreService

RULES = ROOT / "data" / "curriculum_demo_flags.json"
RECORDS = ROOT / "data" / "students" / "demo_flags_records.csv"
ROSTER = ROOT / "data" / "students" / "demo_flags_roster.csv"
ANSWERS = ROOT / "data" / "students" / "demo_flags_answers.csv"
HEADER = "student_id,status,unmet,flags,split,source,confirmed\n"


def service() -> CoreService:
    return CoreService(RULES, RECORDS, ROSTER)


class AnswerParseTest(unittest.TestCase):
    def errors(self, body: str) -> list[str]:
        with self.assertRaises(evaluate.AnswerError) as ctx:
            evaluate.parse_answers(HEADER + body)
        return ctx.exception.errors

    def test_demo_answers_parse(self):
        answers = evaluate.parse_answers(ANSWERS.read_text(encoding="utf-8-sig"))
        self.assertEqual(answers["DEMO-FLAGS"]["unmet"], {"D1"})
        self.assertEqual(answers["DEMO-CLEAN"]["status"], "PASS")
        self.assertFalse(answers["DEMO-MISSING"]["confirmed"])

    def test_missing_column_rejected(self):
        with self.assertRaises(evaluate.AnswerError):
            evaluate.parse_answers("student_id,status\nA,通過\n")

    def test_invalid_rows_listed_with_line_numbers(self):
        errors = self.errors("A,也許,,,開發,虛構,Y\n"
                             "B,通過,R1,,開發,虛構,Y\n"
                             "C,未通過,,,開發,虛構,Y\n"
                             "D,通過,,typo,開發,虛構,Y\n"
                             "D,通過,,,開發,虛構,Y\n"
                             ",通過,,,開發,虛構,Y\n")
        self.assertEqual(len(errors), 6)
        self.assertTrue(errors[0].startswith("第 2 行"))
        self.assertIn("重複", errors[4])

    def test_bad_split_source_confirmed(self):
        self.assertEqual(len(self.errors("A,通過,,,x,y,z\n")), 3)


class AuditEvaluateTest(unittest.TestCase):
    def run_eval(self, body: str):
        return evaluate.evaluate_audit(service(), evaluate.parse_answers(HEADER + body))

    def test_demo_case_set_groups(self):
        summary, details = evaluate.evaluate_audit(service(), evaluate.parse_answers(
            ANSWERS.read_text(encoding="utf-8-sig")))
        groups = summary["groups"]
        self.assertEqual(set(groups), {"fictional/dev/confirmed", "fictional/holdout/confirmed",
                                       "fictional/holdout/unconfirmed"})
        dev = groups["fictional/dev/confirmed"]
        self.assertEqual((dev["status"]["agree"], dev["fully_agree"]), (2, 2))
        self.assertEqual(dev["flags"]["repeatable"], {"tp": 1, "fp": 0, "fn": 0, "tn": 1})
        # 引擎列人工覆核的案例算「未定」，不算一致也不算不一致
        held = groups["fictional/holdout/confirmed"]["status"]
        self.assertEqual((held["agree"], held["disagree"], held["undecided"]), (0, 0, 1))
        self.assertEqual(held["matrix"], {"MANUAL_REVIEW->FAIL": 1})
        self.assertEqual(details, ["DEMO-REVIEW：引擎 MANUAL_REVIEW，承辦人 FAIL"])

    def test_disagreements_and_flag_errors_counted(self):
        summary, details = self.run_eval("DEMO-CLEAN,未通過,D1,repeatable,開發,虛構,Y\n"
                                         "DEMO-FLAGS,未通過,D1,overflow,開發,虛構,Y\n")
        g = summary["groups"]["fictional/dev/confirmed"]
        self.assertEqual(g["status"]["disagree"], 1)
        self.assertEqual(g["requirements"]["by_requirement"]["D1"], {"agree": 1, "disagree": 1, "undecided": 0})
        self.assertEqual(g["flags"]["repeatable"], {"tp": 0, "fp": 1, "fn": 1, "tn": 0})
        self.assertEqual(g["flags"]["substitution_pending"]["fp"], 1)
        self.assertEqual(g["fully_agree"], 0)
        self.assertEqual(summary["audited_without_answer"], 2)
        self.assertEqual(len(details), 2)

    def test_unaudited_answers_counted_by_reason(self):
        summary, _ = self.run_eval("NOBODY,通過,,,開發,實務,Y\n")
        self.assertEqual(summary["groups"]["real/dev/confirmed"]["not_audited"]["not_on_roster"], 1)

    def test_unknown_requirement_rejected(self):
        with self.assertRaises(evaluate.AnswerError) as ctx:
            self.run_eval("DEMO-CLEAN,未通過,R99,,開發,虛構,Y\n")
        self.assertIn("R99", ctx.exception.errors[0])

    def test_summary_has_no_student_ids(self):
        result, _ = evaluate.run_audit(RULES, RECORDS, ROSTER, ANSWERS)
        text = json.dumps(result, ensure_ascii=False)
        for sid in ("DEMO-FLAGS", "DEMO-CLEAN", "DEMO-REVIEW", "DEMO-MISSING", "虛構"):
            self.assertNotIn(sid, text)
        self.assertEqual(len(result["case_set"]), 12)


class TimingTest(unittest.TestCase):
    def test_medians_and_pairs(self):
        result = evaluate.evaluate_timing(
            "case_id,method,minutes,order\n"
            "C1,原流程,20,1\nC1,輔助流程,8,2\n"
            "C2,輔助流程,6,3\nC2,原流程,14,4\n"
            "C3,原流程,30,5\n")
        self.assertEqual(result["original"], {"n": 3, "median": 20.0, "min": 14.0, "max": 30.0})
        self.assertEqual(result["assisted"]["median"], 7.0)
        self.assertEqual(result["paired"]["n"], 2)
        self.assertEqual(result["paired"]["median"], 10.0)
        self.assertEqual(result["paired"]["original_first"], 1)

    def test_invalid_timing_rejected(self):
        with self.assertRaises(evaluate.AnswerError) as ctx:
            evaluate.evaluate_timing("case_id,method,minutes,order\n"
                                     "C1,原流程,-1,1\nC1,其他,5,x\nC2,原流程,5,1\nC2,原流程,6,2\n")
        # 負的分鐘數；method 與 order 都錯；C2 原流程重複
        self.assertEqual(len(ctx.exception.errors), 4)


class CliTest(unittest.TestCase):
    def test_cli_writes_summary_and_rejects_bad_answers(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "result.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = evaluate.main(["audit", "--rules", str(RULES), "--records", str(RECORDS),
                                      "--roster", str(ROSTER), "--answers", str(ANSWERS), "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["type"], "audit")

            bad = Path(d) / "bad.csv"
            bad.write_text(HEADER + "DEMO-CLEAN,也許,,,開發,虛構,Y\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = evaluate.main(["audit", "--rules", str(RULES), "--records", str(RECORDS),
                                      "--roster", str(ROSTER), "--answers", str(bad)])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
