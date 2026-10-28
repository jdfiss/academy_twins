"""自然語言 What-if（academic_twin/ask.py，B 組）：以替身模型測檢核、有界自我修正、轉手動與軌跡。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import ask, llm

COURSES = {"IM2001": "資料結構", "IM3001": "作業系統"}
PLANNED = {"IM2001": "115-1", "IM3001": "115-2"}
TERMS = ask.term_labels("115-1", 113)


def reply(**data):
    base = {"supported": True, "reason": "", "skip": [], "away_start": "", "away_count": 0, "away_credits": -1}
    return llm.LLMResult({**base, **data}, "fake-model", "2026-10-28T00:00:00")


class FakeLLM:
    """依序回傳預先準備的回應，並記下每次收到的提示。"""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt, schema, *args, **kwargs):
        self.prompts.append(prompt)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class AskTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "ask_runs.jsonl"
        patcher = mock.patch.object(ask, "EVAL_LOG", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_ask(self, *replies, question="如果我大三下去交換一年會延畢嗎？"):
        fake = FakeLLM(*replies)
        with mock.patch.object(ask.llm, "generate_json", fake):
            return ask.parse(question, COURSES, PLANNED, TERMS), fake

    def test_term_labels_mark_next_term_and_grade(self):
        self.assertEqual(list(TERMS)[:3], ["115-1", "115-2", "116-1"])
        self.assertIn("下一學期", TERMS["115-1"])
        self.assertIn("大三上", TERMS["115-1"])
        self.assertIn("大三下", TERMS["115-2"])
        self.assertNotIn("大", ask.term_labels("115-1", None)["115-1"])

    def test_away_year_becomes_two_consecutive_terms(self):
        res, _ = self.run_ask(reply(away_start="115-2", away_count=2))
        self.assertEqual(res["status"], "proposed")
        self.assertEqual(res["params"]["away"], ["115-2", "116-1"])
        self.assertEqual(res["params"]["away_credits"], ask.DEFAULT_AWAY_CREDITS)
        self.assertTrue(res["params"]["away_credits_assumed"])
        self.assertIn("採預設值", res["conditions"][0])
        self.assertFalse(res["trace"]["corrected"])

    def test_skip_and_away_together(self):
        res, _ = self.run_ask(reply(skip=[{"code": "IM3001", "term": "115-2"}],
                                    away_start="116-1", away_count=1, away_credits=12))
        self.assertEqual(res["params"], {"skip": ["IM3001@115-2"], "away": ["116-1"],
                                         "away_credits": 12, "away_credits_assumed": False})
        self.assertEqual(res["conditions"][0], "115-2 不修 IM3001 作業系統")

    def test_conditions_are_written_by_program_not_ai(self):
        # AI 的 reason 在 supported=true 時不採用；確認文字只依檢核後的參數產生
        res, _ = self.run_ask(reply(reason="你一定會延畢", skip=[{"code": "IM2001", "term": "115-1"}]))
        self.assertEqual(res["conditions"], ["115-1 不修 IM2001 資料結構"])
        self.assertNotIn("延畢", json.dumps(res["conditions"], ensure_ascii=False))

    def test_self_correction_once_feeds_back_reasons(self):
        res, fake = self.run_ask(reply(skip=[{"code": "IM9999", "term": "115-1"}]),
                                 reply(skip=[{"code": "IM2001", "term": "115-1"}]))
        self.assertEqual(res["status"], "proposed")
        self.assertTrue(res["trace"]["corrected"])
        self.assertEqual(len(fake.prompts), 2)
        self.assertIn("IM9999", fake.prompts[1])
        self.assertIn("沒有通過程式檢核", fake.prompts[1])
        self.assertEqual([s["errors"] for s in res["trace"]["steps"] if s["step"] == "check"],
                         [["unknown_course"], []])

    def test_still_failing_after_one_retry_goes_manual(self):
        bad = reply(away_start="199-1", away_count=1)
        res, fake = self.run_ask(bad, bad)
        self.assertEqual(len(fake.prompts), 2)  # 只修正 1 次，不無限重試
        self.assertEqual(res["status"], "manual")
        self.assertIsNone(res["params"])
        self.assertTrue(res["trace"]["to_manual"])
        self.assertTrue(res["errors"])

    def test_checks_reject_bad_values(self):
        cases = {
            "bad_away_count": reply(away_start="115-1", away_count=5),
            "bad_away_credits": reply(away_start="115-1", away_count=1, away_credits=40),
            "duplicate_course": reply(skip=[{"code": "IM2001", "term": "115-1"}, {"code": "IM2001", "term": "115-2"}]),
            "bad_term": reply(skip=[{"code": "IM2001", "term": "大三上"}]),
            "empty": reply(),
        }
        for code, data in cases.items():
            with self.subTest(code):
                _, errors = ask.check(data.data, COURSES, TERMS)
                self.assertIn(code, [c for c, _ in errors])
        _, errors = ask.check(reply(away_start="118-2", away_count=2).data, COURSES, TERMS)
        self.assertIn("bad_away_count", [c for c, _ in errors])  # 超出可選學期

    def test_unsupported_question_returns_reason(self):
        res, _ = self.run_ask(reply(supported=False, reason="目前不支援加修輔系的模擬"), question="我想修輔系")
        self.assertEqual(res["status"], "unsupported")
        self.assertEqual(res["reason"], "目前不支援加修輔系的模擬")
        self.assertIsNone(res["params"])

    def test_unsupported_without_reason_is_retried(self):
        res, fake = self.run_ask(reply(supported=False), reply(supported=False, reason="沒說是哪一學期"))
        self.assertEqual(len(fake.prompts), 2)
        self.assertEqual(res["status"], "unsupported")

    def test_llm_failure_falls_back_to_manual(self):
        res, fake = self.run_ask(llm.LLMError("未設定 GEMINI_API_KEY"))
        self.assertEqual(len(fake.prompts), 1)
        self.assertEqual(res["status"], "manual")
        self.assertIn("手動", res["reason"])
        self.assertEqual(res["trace"]["steps"], [{"step": "model", "ok": False, "error": "llm_unavailable"}])

    def test_empty_or_long_question_rejected_without_calling_model(self):
        for q in ("  ", "問" * (ask.MAX_QUESTION + 1)):
            with self.subTest(len(q)), self.assertRaises(ValueError):
                self.run_ask(question=q)
        self.assertFalse(self.log.exists())

    def test_trace_log_has_no_question_or_course_codes(self):
        question = "如果 IM2001 在 115-1 不修呢"
        self.run_ask(reply(skip=[{"code": "IM9999", "term": "115-1"}]),
                     reply(skip=[{"code": "IM2001", "term": "115-1"}]), question=question)
        text = self.log.read_text(encoding="utf-8")
        entry = json.loads(text)
        for key in ("model", "prompt_version", "at", "steps", "corrected", "to_manual", "wait_seconds", "status"):
            self.assertIn(key, entry)
        for secret in (question, "IM2001", "IM9999", "資料結構"):
            self.assertNotIn(secret, text)

    def test_prompt_sends_no_student_id_or_grades(self):
        _, fake = self.run_ask(reply(away_start="115-1", away_count=1))
        self.assertIn("IM2001", fake.prompts[0])
        self.assertNotIn("grade", fake.prompts[0])


class ServiceAskTest(unittest.TestCase):
    def test_proposal_uses_planned_courses_and_does_not_simulate(self):
        from academic_twin.api import AcademicTwinService
        service = AcademicTwinService()
        base = service.simulate("S002")["base"]
        first = next(t for t in base["terms"] if t["courses"])
        code = first["courses"][0]["code"]
        fake = FakeLLM(reply(skip=[{"code": code, "term": first["term"]}]))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ask, "EVAL_LOG", Path(tmp) / "log.jsonl"), \
                mock.patch.object(ask.llm, "generate_json", fake):
            res = service.ask("S002", "如果這門課不修呢")
        self.assertEqual(res["params"]["skip"], [f"{code}@{first['term']}"])
        self.assertNotIn("S002", fake.prompts[0])
        self.assertIn(first["term"], fake.prompts[0])
        self.assertNotIn("scenario", res)
        # 確認後交給 simulate 計算，與手動選項同一個入口
        sim = service.simulate("S002", skip=res["params"]["skip"])
        self.assertIsNotNone(sim["scenario"])


if __name__ == "__main__":
    unittest.main()
