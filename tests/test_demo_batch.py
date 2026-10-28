"""示範批次（DEMO-D03 與 5 位示範學生）：檔案與產生器一致、每位的審查結果符合設計、主角的 What-if 故事成立。

只用虛構資料；審查結果由規則引擎產生，這裡驗證的是「示範資料設計得對」，不是實際準確率。
"""
import csv
import io
import unittest

from academic_twin import demo_dashboard as demo, pdf_text
from academic_twin.api import AcademicTwinService
from academic_twin.evaluate import parse_answers
from academic_twin.rule_engine import OVERFLOW
from tests.test_pdf_text import row_text

OUT = demo.DEMO_BATCH_OUT
ROSTER_IDS = [sid for sid, spec in demo.DEMO_BATCH.items() if spec.get("roster", True)]
# 承辦人覆核（設計中的示範操作）：D03 的抵免列認列為本系選修，D07 第二次通過不採計
REVIEWS = [("DEMO-D03", "XFER-01", "114-1", "dept_elective"), ("DEMO-D07", "IM9001", "117-2", "exclude")]


def answer_rows(sid: str) -> list[dict]:
    return list(csv.DictReader((OUT / f"{sid}_answer.csv").read_text(encoding="utf-8").splitlines()))


def confirmed_service() -> AcademicTwinService:
    """等同承辦人把名單上 6 份儀表板都照答案確認送入審查。"""
    out = io.StringIO()
    rows = [r for sid in ROSTER_IDS for r in answer_rows(sid)]
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    service = AcademicTwinService()
    service.load_data(("demo_batch.csv", out.getvalue()), ("demo_batch_roster.csv", demo.demo_batch_roster()))
    return service


class DemoBatchFilesTest(unittest.TestCase):
    def test_committed_files_match_generator(self):
        for sid in demo.DEMO_BATCH:
            with self.subTest(sid=sid):
                self.assertEqual(answer_rows(sid), [{k: r[k] for k in answer_rows(sid)[0]}
                                                    for r in demo.demo_batch_records(sid)])
                self.assertTrue((OUT / f"{sid}.pdf").is_file())
        self.assertEqual(demo.DEMO_BATCH_ROSTER.read_text(encoding="utf-8"), demo.demo_batch_roster())
        self.assertEqual(demo.DEMO_BATCH_ANSWERS.read_text(encoding="utf-8"), demo.demo_batch_answers())

    def test_pdf_text_contains_every_answer_row(self):
        for sid in demo.DEMO_BATCH:
            text = pdf_text.extract_pages((OUT / f"{sid}.pdf").read_bytes())
            with self.subTest(sid=sid):
                self.assertTrue(text.ok, text.reason)
                self.assertIn(f"學號：{sid}", text.pages[0])
                self.assertIn("虛構示範文件", pdf_text.normalize(text.pages[0]))
                for r in answer_rows(sid):
                    self.assertTrue(any(pdf_text.normalize(row_text(r)) in pdf_text.normalize(p) for p in text.pages),
                                    row_text(r))

    def test_fictional_and_separate_from_benchmark(self):
        self.assertNotIn("DEMO-D09", demo.demo_batch_roster())  # 示範「學號不在名單」
        bench_ids = {c["student_id"] for c in demo.bench_cases()}
        self.assertFalse(bench_ids & set(demo.DEMO_BATCH))
        for row in parse_answers(demo.demo_batch_answers()).values():
            self.assertEqual((row["source"], row["confirmed"]), ("fictional", False))


class DemoBatchAuditTest(unittest.TestCase):
    def setUp(self):
        self.service = confirmed_service()

    def test_before_review_d03_and_d07_need_manual_review(self):
        status = {b["id"]: b["status"] for b in self.service.batch()}
        self.assertEqual(status["DEMO-D03"], "MANUAL_REVIEW")
        self.assertEqual(status["DEMO-D07"], "MANUAL_REVIEW")

    def test_results_after_review_match_designed_answers(self):
        for sid, code, term, action in REVIEWS:
            self.service.add_review(sid, code, term, action, "示範承辦人")
        answers = parse_answers(demo.DEMO_BATCH_ANSWERS.read_text(encoding="utf-8"))
        self.assertEqual(set(answers), set(ROSTER_IDS))
        for b in self.service.batch():
            want = answers[b["id"]]
            report = self.service._report(b["id"])
            with self.subTest(sid=b["id"]):
                self.assertEqual(b["status"], want["status"])
                self.assertEqual({r.requirement.id for r in report.results if not r.satisfied}, want["unmet"])
                self.assertEqual({f["type"] for f in b["flags"]}, want["flags"])

    def test_d03_story_flag_and_exchange_delay(self):
        self.service.add_review("DEMO-D03", "XFER-01", "114-1", "dept_elective", "示範承辦人")
        s = self.service.twin("DEMO-D03")
        self.assertEqual(s["status"], "FAIL")
        self.assertEqual([f["type"] for f in s["flags"]], [OVERFLOW])
        self.assertIn("國文", s["flags"][0]["message"])
        self.assertLessEqual({"IM4001", "IM4048"}, {c["code"] for c in s["remaining_courses"]})  # 只在上學期開
        base = self.service.simulate("DEMO-D03", skip=[], away=[], away_credits=15)
        self.assertEqual(base["base"]["graduation"], "117-2")
        away = self.service.simulate("DEMO-D03", skip=[], away=["117-1"], away_credits=15)
        self.assertEqual(away["scenario"]["graduation"], "118-1")
        self.assertEqual(away["scenario"]["delay_terms"], 1)


if __name__ == "__main__":
    unittest.main()
