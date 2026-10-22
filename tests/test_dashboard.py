"""AI 解析儀表板：檢查、量測與確認流程。以假的 AI 回應測試，不連網、不送出任何資料。"""
import csv
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from academic_twin import dashboard, llm
from academic_twin.api import AcademicTwinService
from academic_twin.records import RecordsError

SAMPLES = Path(__file__).parent.parent / "data" / "samples"
ANSWER = SAMPLES / "dashboard_demo_d02_answer.csv"


def answer_rows():
    return list(csv.DictReader(ANSWER.read_text(encoding="utf-8").splitlines()))


def fake_response(rows=None, **extra):
    """依標準答案組出 AI 回應；rows 可改寫以模擬擷取錯誤。"""
    code_of = {v: k for k, v in dashboard.FORMATS["demo-v1"].items() if v}
    rows = rows if rows is not None else [
        {"rule_code": code_of.get(r["category"], "A01"), "term": r["term"], "code": r["code"], "name": r["name"],
         "credits": r["credits"], "grade": r["grade"], "source_page": 1,
         "source_text": f"{r['term']} {r['code']} {r['name']} {r['credits']} {r['grade']}"} for r in answer_rows()]
    data = {"cohort": 114, "declared_required": 128, "declared_earned": None, "sections": [], "rows": rows, **extra}
    return llm.LLMResult(data, "fake-model", "2026-10-21T00:00:00")


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "runs.jsonl"
        patcher = mock.patch.object(dashboard, "EVAL_LOG", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = AcademicTwinService()

    def extract(self, response, student_id="DEMO-D02"):
        with mock.patch.object(llm, "generate_json", return_value=response):
            return self.service.dashboard_extract(b"%PDF-1.7 fake", student_id)

    def test_faithful_extraction_matches_answer(self):
        with mock.patch.object(llm, "generate_json", return_value=fake_response()):
            ext = dashboard.extract(b"%PDF-1.7 fake")
        self.assertEqual(dashboard.check(ext, self.service.kb), [])
        result = dashboard.compare(ext.rows, answer_rows())
        self.assertTrue(result["fully_correct"])
        self.assertEqual(result["field_accuracy"], 1.0)

    def test_rejects_non_pdf(self):
        with self.assertRaises(ValueError):
            dashboard.extract(b"not a pdf")

    def test_compare_counts_missing_extra_and_wrong_fields(self):
        rows = [dict(r) for r in answer_rows()]
        rows[0]["grade"] = "70"
        del rows[1]
        rows.append({**rows[2], "code": "EXTRA-1"})
        result = dashboard.compare(rows, answer_rows())
        self.assertEqual((result["missing_rows"], result["extra_rows"]), (1, 1))
        self.assertEqual(result["wrong_by_field"], {"grade": 1})
        self.assertFalse(result["fully_correct"])

    def test_compare_ignores_fullwidth_punctuation_only(self):
        rows = [dict(r) for r in answer_rows()]
        rows[0]["name"] = rows[0]["name"] + "（上）"
        answer = [dict(r) for r in answer_rows()]
        answer[0]["name"] = answer[0]["name"] + "(上)"
        self.assertTrue(dashboard.compare(rows, answer)["fully_correct"])

    def test_check_flags_bad_rows(self):
        rows = fake_response().data["rows"]
        rows[0] = {**rows[0], "grade": "優", "source_text": ""}
        rows[1] = {**rows[1], "rule_code": "X99"}
        rows.append(dict(rows[2]))  # 重複列
        with mock.patch.object(llm, "generate_json", return_value=fake_response(rows)):
            ext = dashboard.extract(b"%PDF-1.7 fake")
        messages = [(i.row, i.message) for i in dashboard.check(ext, self.service.kb) if i.level == "error"]
        self.assertTrue(any(r == 0 and "成績" in m for r, m in messages))
        self.assertTrue(any(r == 0 and "來源" in m for r, m in messages))
        self.assertTrue(any(r == 1 and "X99" in m for r, m in messages))
        self.assertTrue(any(r == len(rows) - 1 and "重複" in m for r, m in messages))

    def test_declared_total_mismatch_warns_possible_missing_rows(self):
        ext = self.extract(fake_response(declared_earned=999))
        warnings = [i for i in ext["issues"] if i["level"] == "warning"]
        self.assertTrue(any("漏列" in i["message"] for i in warnings))

    def test_manual_source_requires_positive_integer_page_and_nonblank_text(self):
        for page, text in ((None, "原文"), (0, "原文"), (-1, "原文"), (1.5, "原文"),
                           (True, "原文"), (1, "  ")):
            with self.subTest(page=page, text=text):
                response = fake_response()
                response.data["rows"][0].update(source_page=page, source_text=text)
                draft = self.extract(response)
                self.assertTrue(any(i["row"] == 0 and i["level"] == "error" and "來源" in i["message"]
                                    for i in draft["issues"]))

    def test_new_warning_details_are_returned_and_draft_can_be_corrected(self):
        draft = self.extract(fake_response())
        rows = [dict(r) for r in draft["rows"]]
        known = next(r for r in rows if r["code"] in self.service.kb.courses)
        known["credits"] = str(self.service.kb.courses[known["code"]].credits + 1)
        with self.assertRaises(RecordsError) as ctx:
            self.service.dashboard_confirm(draft["draft_id"], rows, "虛構確認人")
        self.assertTrue(any("警示" in message for message in ctx.exception.errors))
        self.assertTrue(any("學分" in message and "規則庫" in message for message in ctx.exception.errors))
        self.assertNotIn("DEMO-D02", self.service.students)
        self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "虛構確認人")

    def test_empty_or_unreadable_document_is_error(self):
        ext = self.extract(fake_response(rows=[], unreadable="掃描影像"))
        errors = [i["message"] for i in ext["issues"] if i["level"] == "error"]
        self.assertTrue(any("無法辨識" in m for m in errors))
        self.assertTrue(any("不能當成沒有修課紀錄" in m for m in errors))

    def test_confirm_adds_student_and_matches_csv_path(self):
        draft = self.extract(fake_response())
        self.assertNotIn("DEMO-D02", self.service.students)  # 確認前不進入審查
        s = self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教", draft["cohort"])
        csv_path = self.service.student("S003")  # 同一批虛構課程經 CSV 匯入的結果
        for key in ("status", "total_credits", "credit_breakdown"):
            self.assertEqual(s[key], csv_path[key])
        self.assertIn("DEMO-D02", [r["id"] for r in self.service.batch()])

    def test_confirm_blocks_errors_and_unacknowledged_warnings(self):
        draft = self.extract(fake_response(declared_earned=999))
        bad = [dict(r) for r in draft["rows"]]
        bad[0]["credits"] = "三"
        with self.assertRaises(RecordsError):
            self.service.dashboard_confirm(draft["draft_id"], bad, "王助教")
        with self.assertRaises(ValueError):
            self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教")
        with self.assertRaises(ValueError):
            self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "", acknowledge_warnings=True)
        self.assertNotIn("DEMO-D02", self.service.students)
        self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教", acknowledge_warnings=True)
        self.assertIn("DEMO-D02", self.service.students)

    def test_confirm_replaces_existing_student_records(self):
        draft = self.extract(fake_response(), student_id="S001")
        self.assertTrue(draft["exists"])
        self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教")
        self.assertEqual(len(self.service.students["S001"]), len(answer_rows()))
        with self.assertRaises(KeyError):  # 草稿確認後即失效
            self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教")

    def test_confirm_log_has_counts_only(self):
        draft = self.extract(fake_response())
        rows = [dict(r) for r in draft["rows"]]
        rows[0]["grade"] = "90"
        self.service.dashboard_confirm(draft["draft_id"], rows, "王助教")
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((entry["fields_changed"], entry["rows_added"], entry["rows_removed"]), (1, 0, 0))
        text = json.dumps(entry, ensure_ascii=False)
        self.assertNotIn("DEMO-D02", text)
        self.assertNotIn(rows[0]["code"], text)

    def test_invalid_student_id_rejected(self):
        with self.assertRaises(ValueError):
            self.extract(fake_response(), student_id="a,b")

    def test_llm_failure_propagates_without_changing_data(self):
        before = dict(self.service.students)
        with mock.patch.object(llm, "generate_json", side_effect=llm.LLMError("503")):
            with self.assertRaises(llm.LLMError):
                self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        self.assertEqual(self.service.students, before)

    def test_draft_shows_fallback_reason(self):
        response = fake_response()
        response.fallback_from = "Gemini（primary）等待逾時，90 秒內未回應"
        draft = self.extract(response)
        self.assertEqual(draft["fallback_from"], response.fallback_from)

    def test_demo_answer_files_are_fictional(self):
        for path in SAMPLES.glob("dashboard_*_answer.csv"):
            ids = {r["student_id"] for r in csv.DictReader(path.read_text(encoding="utf-8").splitlines())}
            self.assertTrue(all(i.startswith("DEMO-") for i in ids), path)


class LLMClientTest(unittest.TestCase):
    def test_missing_key_raises_llm_error(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": ""}), \
                mock.patch.object(llm, "ROOT", Path(tempfile.gettempdir()) / "no-such-dir"):
            with self.assertRaises(llm.LLMError):
                llm.api_key()

    def test_falls_back_when_primary_unavailable(self):
        calls = []

        def fake_call(model, *args):
            calls.append(model)
            if model == "primary":
                raise llm._Unavailable("503")
            return llm.LLMResult({}, model, "t")

        with mock.patch.dict("os.environ", {"GEMINI_MODEL": "primary", "GEMINI_FALLBACK_MODELS": "backup"}), \
                mock.patch.object(llm, "_call", side_effect=fake_call):
            self.assertEqual(llm.generate_json("p", {}).model, "backup")
        self.assertEqual(calls, ["primary", "backup"])


def ok_response(*args, **kwargs):
    return io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}).encode())


class LLMTimeoutTest(unittest.TestCase):
    """以假的 urlopen 與時鐘模擬等待；不連網。"""

    def setUp(self):
        self.now = 0.0
        self.sleeps, self.calls = [], []
        for patcher in (
                mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "primary",
                                               "GEMINI_FALLBACK_MODELS": "backup", "GEMINI_TIMEOUT": ""}),
                mock.patch.object(llm.time, "monotonic", side_effect=lambda: self.now),
                mock.patch.object(llm.time, "sleep", side_effect=self.sleep)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def urlopen(self, primary_error):
        def fake(request, timeout):
            model = "primary" if "/primary:" in request.full_url else "backup"
            self.calls.append((model, timeout))
            if model == "primary":
                raise primary_error()
            return ok_response()
        return mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake)

    def test_timeout_switches_to_fallback_without_retry(self):
        for error in (TimeoutError, lambda: urllib.error.URLError(TimeoutError("timed out"))):
            with self.subTest(error=error):
                self.calls.clear()
                with self.urlopen(error):
                    result = llm.generate_json("p", {})
                self.assertEqual(result.model, "backup")
                self.assertEqual(self.calls, [("primary", llm.DEFAULT_TIMEOUT), ("backup", llm.FALLBACK_TIMEOUT)])
                self.assertEqual(self.sleeps, [])
                self.assertIn("primary", result.fallback_from)
                self.assertIn("逾時", result.fallback_from)

    def test_overload_retries_stay_within_budget(self):
        busy = lambda: urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"busy"))
        with mock.patch.dict("os.environ", {"GEMINI_TIMEOUT": "10"}), self.urlopen(busy):
            result = llm.generate_json("p", {})
        # 第 1 次後等 2 秒、第 2 次後等 4 秒；再等 8 秒會超過 10 秒，就不再重試
        self.assertEqual(self.sleeps, [2, 4])
        self.assertEqual(self.calls, [("primary", 10), ("primary", 8), ("primary", 4), ("backup", llm.FALLBACK_TIMEOUT)])
        self.assertEqual(result.model, "backup")
        self.assertIn("503", result.fallback_from)

    def test_all_models_fail_lists_each_reason(self):
        def fake(request, timeout):
            raise TimeoutError()
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake):
            with self.assertRaises(llm.LLMError) as ctx:
                llm.generate_json("p", {})
        self.assertIn("primary", str(ctx.exception))
        self.assertIn("backup", str(ctx.exception))

    def test_primary_success_has_no_fallback_reason(self):
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=ok_response):
            self.assertEqual(llm.generate_json("p", {}).fallback_from, "")

    def test_invalid_timeout_setting_is_rejected(self):
        for raw in ("abc", "0", "-5"):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {"GEMINI_TIMEOUT": raw}):
                with self.assertRaises(llm.LLMError):
                    llm.generate_json("p", {})


if __name__ == "__main__":
    unittest.main()
