"""AI 解析儀表板：檢查、量測與確認流程。以假的 AI 回應測試，不連網、不送出任何資料。"""
import contextlib
import csv
import dataclasses
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from academic_twin import dashboard, llm, pdf_text
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


def pdf_pages(response):
    """假 PDF 的逐頁文字：以 AI 回應宣稱的原文組成第 1 頁，讓原文核對通過（核對本身另有測試）。"""
    text = "\n".join(r.get("source_text") or "" for r in response.data.get("rows", []))
    return mock.patch.object(pdf_text, "extract_pages", return_value=pdf_text.PdfText([text]))


@contextlib.contextmanager
def two_models(response):
    """兩個模型讀到相同內容的替身：主模型回原名稱，指定 model 時回該名稱（交叉驗證一致）。"""
    def generate(*args, model=None, **kwargs):
        return dataclasses.replace(response, model=model or response.model)
    with mock.patch.object(llm, "generate_json", side_effect=generate), pdf_pages(response):
        yield


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
        with two_models(response):
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
        # 改成與兩個模型都不同的值，交叉驗證會再提示一次，須勾選已確認警示
        with self.assertRaises(RecordsError):
            self.service.dashboard_confirm(draft["draft_id"], rows, "王助教")
        self.service.dashboard_confirm(draft["draft_id"], rows, "王助教", acknowledge_warnings=True)
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((entry["fields_changed"], entry["rows_added"], entry["rows_removed"]), (1, 0, 0))
        self.assertEqual((entry["cross_check"]["edited_rows_flagged"], entry["cross_check"]["edited_rows_unflagged"]),
                         (0, 1))
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

    # ---------- 有界自我修正與執行軌跡 ----------
    def scripted(self, first, retry):
        """主模型第一次回 first、自我修正那次回 retry；第二模型都回正確答案。記下每次呼叫的提示。"""
        prompts = []

        def generate(prompt, *args, model=None, **kwargs):
            prompts.append((prompt, model))
            if model:
                return dataclasses.replace(fake_response(), model=model)
            return retry if dashboard.RETRY.split("{")[0] in prompt else first
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(llm, "generate_json", side_effect=generate))
        stack.enter_context(pdf_pages(fake_response()))
        return stack, prompts

    def bad_response(self):
        rows = fake_response().data["rows"]
        rows[0] = {**rows[0], "grade": "優"}
        return fake_response(rows)

    def traces(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["type"] == "extract_trace"]

    def test_self_correction_fixes_error_once(self):
        stack, prompts = self.scripted(self.bad_response(), fake_response())
        with stack:
            draft = self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        self.assertFalse(any(i["level"] == "error" for i in draft["issues"]))
        self.assertTrue(draft["trace"]["corrected"])
        self.assertFalse(draft["trace"]["to_manual"])
        retries = [p for p, m in prompts if m is None and "上一次擷取" in p]
        self.assertEqual(len(retries), 1)
        self.assertIn("成績", retries[0])  # 回饋的是檢查理由
        self.assertNotIn("DEMO-D02", retries[0])  # 不加入學號
        self.assertEqual([s["step"] for s in draft["trace"]["steps"]],
                         ["extract", "cross_check", "check", "retry", "check"])

    def test_self_correction_still_failing_goes_to_manual(self):
        stack, prompts = self.scripted(self.bad_response(), self.bad_response())
        with stack:
            draft = self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        self.assertTrue(any(i["level"] == "error" for i in draft["issues"]))
        self.assertTrue(draft["trace"]["to_manual"])
        self.assertFalse(draft["trace"]["corrected"])
        self.assertEqual(sum(1 for p, m in prompts if m is None), 2)  # 只重來 1 次

    def test_worse_retry_is_not_used(self):
        worse = fake_response(rows=[])
        stack, _ = self.scripted(self.bad_response(), worse)
        with stack:
            draft = self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        self.assertEqual(len(draft["rows"]), len(answer_rows()))
        self.assertEqual(draft["trace"]["steps"][-1]["kept"], "first")

    def test_retry_that_drops_good_rows_is_not_used(self):
        """審查 #1：修正版刪掉有錯的列、錯誤變少，但也刪掉了沒問題的列，不能採用。"""
        bad = self.bad_response()
        dropped = fake_response(rows=fake_response().data["rows"][5:])  # 少了第 1–5 列（第 2–5 列原本沒問題）
        stack, _ = self.scripted(bad, dropped)
        with stack:
            draft = self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        self.assertEqual(len(draft["rows"]), len(answer_rows()))
        last = draft["trace"]["steps"][-1]
        self.assertEqual((last["kept"], last["dropped_rows"]), ("first", 4))
        self.assertFalse(draft["trace"]["corrected"])
        self.assertTrue(draft["trace"]["to_manual"])

    def test_retry_may_drop_only_the_bad_row(self):
        """有錯的那一列被修正版拿掉（例如 AI 多抄的列）可以採用；其他列都要在。"""
        rows = fake_response().data["rows"]
        extra = rows + [{**rows[0], "code": "XX999", "grade": "優", "source_text": "XX999"}]
        stack, _ = self.scripted(fake_response(extra), fake_response())
        with stack:
            draft = self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        self.assertTrue(draft["trace"]["corrected"])
        self.assertEqual(len(draft["rows"]), len(answer_rows()))

    def test_retry_is_skipped_when_ai_is_stopped(self):
        """審查 #3：擷取途中按了「停止全部」，自我修正不得再呼叫模型。"""
        prompts = []
        batch = self.service._batch()

        def generate(prompt, *args, model=None, **kwargs):
            prompts.append(prompt)
            batch.paused = True  # 第一次擷取進行中被停止
            return dataclasses.replace(self.bad_response(), model=model or "fake-model")
        with mock.patch.object(llm, "generate_json", side_effect=generate), pdf_pages(fake_response()):
            draft = self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        self.assertFalse(any("上一次擷取" in p for p in prompts))
        self.assertEqual(draft["trace"]["steps"][-1], {"step": "retry", "ok": False, "error": "ai_paused",
                                                       "prompt_version": dashboard.RETRY_VERSION})

    def test_trace_to_manual_counts_cohort_error(self):
        """審查 #10：入學年度沒有規則庫不回饋模型，但確認會被擋，軌跡要標示轉人工。"""
        draft = self.extract(fake_response(cohort=99))
        self.assertTrue(draft["trace"]["to_manual"])

    def test_adopted_retry_model_is_traced(self):
        fixed = dataclasses.replace(fake_response(), model="backup-model")
        stack, _ = self.scripted(self.bad_response(), fixed)
        with stack:
            draft = self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        self.assertEqual((draft["trace"]["model"], draft["model"]), ("backup-model", "backup-model"))

    def test_clean_extraction_does_not_retry(self):
        draft = self.extract(fake_response())
        self.assertNotIn("retry", [s["step"] for s in draft["trace"]["steps"]])
        self.assertFalse(draft["trace"]["to_manual"])

    def test_missing_cohort_rules_is_not_fed_back(self):
        draft = self.extract(fake_response(cohort=99))
        self.assertTrue(any(i["level"] == "error" and "規則庫" in i["message"] for i in draft["issues"]))
        self.assertNotIn("retry", [s["step"] for s in draft["trace"]["steps"]])

    def test_trace_is_logged_with_counts_only(self):
        stack, _ = self.scripted(self.bad_response(), fake_response())
        with stack:
            self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        [trace] = self.traces()
        text = json.dumps(trace, ensure_ascii=False)
        self.assertNotIn("DEMO-D02", text)
        for r in answer_rows()[:3]:
            self.assertNotIn(r["code"], text)
        self.assertNotIn("優", text)
        for key in ("model", "prompt_version", "at", "corrected", "to_manual", "wait_seconds", "steps"):
            self.assertIn(key, trace)

    def test_llm_failure_is_traced(self):
        with mock.patch.object(llm, "generate_json", side_effect=llm.LLMError("503")), \
                self.assertRaises(llm.LLMError):
            self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")
        [trace] = self.traces()
        self.assertEqual((trace["status"], trace["to_manual"]), ("failed", True))

    def test_source_text_without_spaces_is_not_flagged(self):
        """備援模型把原文欄位黏在一起（2026-11-06 實測 DEMO-D07）時，正確的學分與成績不應被誤報。"""
        row = {"code": "IM1001", "name": "計算機概論I", "credits": "3", "grade": "89"}
        self.assertEqual(dashboard._fields_not_in_source(row, "114-1IM1001計算機概論I389"), [])
        self.assertEqual(dashboard._fields_not_in_source(
            {"code": "SL-1", "name": "服務學習(一)", "credits": "0", "grade": "80"}, "114-1SL-1服務學習(一)080"), [])
        # 讀錯的值仍要抓到：黏在一起的數字裡剛好含有該數字也不算（審查指出 8、38 會被 389 蓋過）
        glued = "114-1IM1001計算機概論I389"
        for grade in ("8", "38", "75", "9"):
            with self.subTest(grade=grade):
                self.assertIn(f"成績「{grade}」", dashboard._fields_not_in_source({**row, "grade": grade}, glued))
        # 學期、課號裡的數字不算數
        self.assertEqual(dashboard._fields_not_in_source({**row, "credits": "4", "grade": "11"}, glued),
                         ["學分「4」", "成績「11」"])
        # 有空白分隔時照舊逐一比對
        self.assertEqual(dashboard._fields_not_in_source(row, "114-1 IM1001 計算機概論I 3 89"), [])

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


    def test_temperature_follows_model_generation(self):
        """Gemini 3 以上不指定溫度（溫度 0 會陷入重複迴圈，2026-11-06 實測 DEMO-D07）；2.x 維持 0。"""
        sent = {}

        def capture(request, timeout=None):
            sent[json.loads(request.data)["generationConfig"].get("temperature", "default")] = True
            return ok_response()
        for model, expected in (("gemini-3.5-flash", "default"), ("gemini-2.5-flash", 0)):
            sent.clear()
            with self.subTest(model=model), mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k"}), \
                    mock.patch.object(llm.urllib.request, "urlopen", side_effect=capture):
                llm.generate_json("p", {}, model=model)
                self.assertEqual(list(sent), [expected])

    def test_explicit_temperature_overrides_model_default(self):
        """收件匣固定溫度 0（inbox-v3 人工檢查的條件），即使是 Gemini 3 也照送；同時帶單次輸出上限。"""
        sent = []

        def capture(request, timeout=None):
            sent.append(json.loads(request.data)["generationConfig"])
            return ok_response()
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k"}), \
                mock.patch.object(llm.urllib.request, "urlopen", side_effect=capture):
            llm.generate_json("p", {}, model="gemini-3.5-flash-lite", temperature=0)
        self.assertEqual(sent[0]["temperature"], 0)
        self.assertEqual(sent[0]["maxOutputTokens"], llm.DEFAULT_MAX_OUTPUT_TOKENS)

    def test_aborted_call_usage_is_kept(self):
        """重複迴圈中斷的呼叫也已計費：用量帶到備援結果的 failed_usage；沒有備援時帶在 LLMError.usage。"""
        usage = {"totalTokenCount": 5000}

        def fake(request, timeout=None):
            if "/primary:" in request.full_url:
                return sse({**text_chunk("段落標題重複出現。" * 10), "usageMetadata": usage})
            return ok_response()
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "primary",
                                            "GEMINI_FALLBACK_MODELS": "backup"}), \
                mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake):
            result = llm.generate_json("p", {})
            self.assertEqual(result.model, "backup")
            self.assertEqual(result.failed_usage, [("primary", usage)])
            with self.assertRaises(llm.LLMError) as e:
                llm.generate_json("p", {}, model="primary")
            self.assertEqual(e.exception.usage, [("primary", usage)])


def sse(*chunks: dict) -> io.BytesIO:
    """串流（SSE）回應：每段一行 data:。"""
    return io.BytesIO(b"".join(b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\r\n\r\n" for c in chunks))


def text_chunk(text: str, finish: str | None = None) -> dict:
    cand = {"content": {"parts": [{"text": text}]}}
    return {"candidates": [{**cand, **({"finishReason": finish} if finish else {})}]}


def ok_response(*args, **kwargs):
    return sse(text_chunk("{"), text_chunk("}", "STOP"))


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

    def test_repetition_loop_switches_to_fallback_early(self):
        """輸出陷入重複迴圈（2026-11-06 實測 gemini-3.5-flash 解析 DEMO-D07）時立即中斷，改用備援模型。"""
        read = []

        def fake(request, timeout):
            model = "primary" if "/primary:" in request.full_url else "backup"
            self.calls.append((model, timeout))
            if model == "backup":
                return ok_response()
            chunks = [text_chunk('{"rows": [')] + [text_chunk(" Section Code A01 Title 院系必修 Earned 75")] * 50

            class Stream(io.BytesIO):
                def __iter__(inner):
                    for line in sse(*chunks).read().split(b"\r\n\r\n"):
                        read.append(line)
                        yield line
            return Stream()
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake):
            result = llm.generate_json("p", {})
        self.assertEqual(result.model, "backup")
        self.assertIn("重複迴圈", result.fallback_from)
        self.assertLess(len(read), 20)  # 偵測到就停，不會讀完 50 段

    def test_total_deadline_is_checked_while_streaming(self):
        """串流持續有資料時，單次讀取不會逾時；仍要依總等待時間中斷。"""
        def fake(request, timeout):
            model = "primary" if "/primary:" in request.full_url else "backup"
            if model == "backup":
                return ok_response()

            class Slow(io.BytesIO):
                def __iter__(inner):
                    for i in range(100):
                        self.now += 5  # 每段 5 秒
                        yield b"data: " + json.dumps(text_chunk(f'"row{i}",')).encode() + b"\r\n"
            return Slow()
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake):
            result = llm.generate_json("p", {})
        self.assertEqual(result.model, "backup")
        self.assertIn("逾時", result.fallback_from)
        self.assertLessEqual(self.now, llm.DEFAULT_TIMEOUT + 5)

    def test_stream_joins_chunks_and_skips_thoughts(self):
        def fake(request, timeout):
            self.assertIn(":streamGenerateContent?alt=sse", request.full_url)
            return sse({"candidates": [{"content": {"parts": [{"text": "想一想", "thought": True}]}}]},
                       text_chunk('{"a": '), {**text_chunk("1}", "STOP"), "usageMetadata": {"candidatesTokenCount": 3}})
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake):
            result = llm.generate_json("p", {})
        self.assertEqual((result.data, result.usage), ({"a": 1}, {"candidatesTokenCount": 3}))

    def test_error_inside_stream_is_reported(self):
        with mock.patch.object(llm.urllib.request, "urlopen",
                               side_effect=lambda r, timeout: sse({"error": {"code": 400, "message": "bad"}})),                 self.assertRaises(llm.LLMError) as ctx:
            llm.generate_json("p", {}, model="primary")
        self.assertIn("bad", str(ctx.exception))

    def test_loop_detector_ignores_normal_json(self):
        rows = "".join(f'{{"term": "114-1", "code": "IM10{i:02d}", "name": "課程{i}", "credits": "3", "grade": "8{i % 10}"}},'
                       for i in range(40))
        for text in (rows, " " * 500, "0" * 500, "ab" * 300):
            with self.subTest(text=text[:20]):
                self.assertFalse(llm.looping(text[-llm.LOOP_WINDOW:]))
        self.assertTrue(llm.looping("院系必修 Title: " * 8))

    def test_invalid_timeout_setting_is_rejected(self):
        for raw in ("abc", "0", "-5"):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {"GEMINI_TIMEOUT": raw}):
                with self.assertRaises(llm.LLMError):
                    llm.generate_json("p", {})


if __name__ == "__main__":
    unittest.main()


class DashboardCohortTest(unittest.TestCase):
    """儀表板確認依入學年度的規則庫審查；沒有該年度規則庫時不能送入審查，資料不變。

    112 學年度規則庫為測試中由 114 改出的虛構版本，只驗證切換機制。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(dashboard, "EVAL_LOG", Path(self.tmp.name) / "runs.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)
        from academic_twin.api import KB_PATH
        raw = json.loads(KB_PATH.read_text(encoding="utf-8"))
        raw["cohort"] = 112
        next(r for r in raw["requirements"] if r["kind"] == "total_credits")["min_credits"] = 120
        self.kb_112 = Path(self.tmp.name) / "curriculum_fake_112.json"
        self.kb_112.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def extract(self, service, cohort):
        with two_models(fake_response(cohort=cohort)):
            return service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")

    def test_missing_cohort_rules_blocks_confirm_and_keeps_data(self):
        service = AcademicTwinService()
        draft = self.extract(service, 112)
        self.assertTrue(any(i["level"] == "error" and "112" in i["message"] for i in draft["issues"]))
        self.assertEqual(draft["cohorts"], [114])
        before = dict(service.students)
        with self.assertRaises(RecordsError) as ctx:
            service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教", 112, True)
        self.assertTrue(any("不套用其他年度" in m for m in ctx.exception.errors))
        self.assertEqual(service.students, before)
        self.assertNotIn("DEMO-D02", service.roster.other_cohort)

    def test_confirm_audits_with_own_cohort_rules(self):
        service = AcademicTwinService(extra_kb_paths=[self.kb_112])
        draft = self.extract(service, 112)
        self.assertFalse([i for i in draft["issues"] if i["level"] == "error"])
        s = service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教", 112)
        need = next(r["need"] for r in s["requirements"] if r["kind"] == "total_credits")
        self.assertEqual((s["cohort"], need), ("112", 120))
        self.assertIn("DEMO-D02", [r["id"] for r in service.batch()])

    def test_blank_cohort_keeps_roster_cohort(self):
        roster = Path(self.tmp.name) / "roster.csv"
        roster.write_text("student_id,cohort\nS001,\nDEMO-D02,112\n", encoding="utf-8")
        service = AcademicTwinService(roster_path=roster, extra_kb_paths=[self.kb_112])
        draft = self.extract(service, None)
        s = service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教", None)
        self.assertEqual(s["cohort"], "112")

    def test_web_loads_every_cohort_rule_file(self):
        from academic_twin import rule_versions
        d = Path(self.tmp.name)
        for year in (114, 112):
            (d / f"curriculum_im_{year}.json").write_text(json.dumps({"cohort": year}), encoding="utf-8")
        (d / "curriculum_demo_flags.json").write_text("{}", encoding="utf-8")
        default, extra = rule_versions.startup_paths(d / "curriculum_im_114.json", d / "versions")
        self.assertEqual((default.name, [p.name for p in extra]),
                         ("curriculum_im_114.json", ["curriculum_im_112.json"]))


class PageLimitTest(unittest.TestCase):
    def test_oversized_pdf_is_not_sent_to_model(self):
        pdf = b"%PDF-1.7 " + b"<< /Type /Page >> " * 11 + b"<< /Type /Pages >>"
        self.assertEqual(dashboard.page_count(pdf), 11)
        with mock.patch.object(llm, "generate_json") as live, self.assertRaises(ValueError) as e:
            dashboard.extract(pdf)
        live.assert_not_called()
        self.assertIn("11 頁", str(e.exception))


class LLMOutputCapTest(unittest.TestCase):
    """費用控制：每次呼叫都帶輸出 token 上限（含思考）。不連網。"""

    def test_request_carries_max_output_tokens(self):
        bodies = []

        def fake(request, timeout):
            bodies.append(json.loads(request.data))
            return ok_response()
        for env, expected in (({}, llm.DEFAULT_MAX_OUTPUT_TOKENS), ({"GEMINI_MAX_OUTPUT_TOKENS": "4096"}, 4096)):
            with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k", "GEMINI_MAX_OUTPUT_TOKENS": "", **env}), \
                    mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake):
                llm.generate_json("p", {}, model="m")
            self.assertEqual(bodies[-1]["generationConfig"]["maxOutputTokens"], expected)
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k", "GEMINI_MAX_OUTPUT_TOKENS": "-1"}), \
                self.assertRaises(llm.LLMError):
            llm.generate_json("p", {}, model="m")


def ncu_response(cohort=114):
    """校務系統版型（ncu-v1）的假 AI 回應：照真實儀表板的寫法抄錄，課程與成績皆為虛構。"""
    rows = [
        ("11201", "1141", "LN1005", "大一英文:管理課群", "3", "85.00"),
        ("12101", "1141", "IM1001", "計算機概論Ｉ", "3", "80.00"),
        ("12101", "1151", "IM3003", "作業研究 Ⅰ", "3", "未到(Not Entered)"),
        ("18101", "1141", "CR0001", "操行", "0", "85.00"),
        ("18401", "1141", "SC0003", "服務學習課程", "0", "勞動服務通過(Pass)"),
    ]
    rows = [{"rule_code": c, "term": t, "code": k, "name": n, "credits": cr, "grade": g, "source_page": 1,
             "source_text": f"{t} {k} {n} {cr} {g} 通過"} for c, t, k, n, cr, g in rows]
    sections = [{"rule_code": "11000", "declared_earned": 3}, {"rule_code": "11201", "declared_earned": 3},
                {"rule_code": "12000", "declared_earned": 3}, {"rule_code": "19120", "declared_earned": 9}]
    data = {"cohort": cohort, "declared_required": 128, "declared_earned": 6, "sections": sections, "rows": rows}
    return llm.LLMResult(data, "fake-model", "2026-11-10T00:00:00")


class NcuFormatTest(unittest.TestCase):
    """校務系統版型：學期、文字成績與 5 位數規則代碼由程式換算，不靠 AI。"""

    setUp = DashboardTest.setUp
    extract = DashboardTest.extract

    def ncu_extract(self, response=None):
        with mock.patch.object(llm, "generate_json", return_value=response or ncu_response()):
            return dashboard.extract(b"%PDF-1.7 fake")

    def test_detects_format_and_normalizes(self):
        ext = self.ncu_extract()
        self.assertEqual(ext.format, "ncu-v1")
        self.assertEqual([r["term"] for r in ext.rows], ["114-1", "114-1", "115-1", "114-1", "114-1"])
        self.assertEqual([r["grade"] for r in ext.rows][2:], [dashboard.PENDING, "85.00", dashboard.PASS])
        self.assertEqual(ext.rows[0]["category"], "外文")
        self.assertIn("未到(Not Entered)", ext.rows[2]["source_text"])  # 原文保留

    def test_demo_format_unchanged(self):
        with mock.patch.object(llm, "generate_json", return_value=fake_response()):
            self.assertEqual(dashboard.extract(b"%PDF-1.7 fake").format, "demo-v1")

    def test_clean_ncu_extraction_has_no_errors_only_explanations(self):
        issues = dashboard.check(self.ncu_extract(), self.service.kb)
        self.assertEqual([i.message for i in issues if i.level == "error"], [])
        messages = " ".join(i.message for i in issues)
        self.assertIn("1 列尚無成績", messages)
        self.assertIn("操行", messages)
        self.assertNotIn("規則代碼 11000", messages)  # 上層段落含下層代碼的列，合計相符
        self.assertNotIn("19120", messages)  # 特殊檢核不是修課列合計

    def test_section_total_counts_lower_codes(self):
        response = ncu_response()
        response.data["sections"][0]["declared_earned"] = 6
        messages = [i.message for i in dashboard.check(self.ncu_extract(response), self.service.kb)]
        self.assertTrue(any("規則代碼 11000 文件標示實得 6 學分" in m for m in messages))

    def test_unknown_code_and_text_still_errors(self):
        response = ncu_response()
        response.data["rows"][0].update(rule_code="18301", grade="缺考")
        messages = [i.message for i in dashboard.check(self.ncu_extract(response), self.service.kb)
                    if i.level == "error"]
        self.assertTrue(any("18301" in m for m in messages))
        self.assertTrue(any("缺考" in m for m in messages))

    def test_review_rows_and_pass_grade(self):
        ext = self.ncu_extract()
        rows = dashboard.review_rows(ext.format, ext.rows)
        self.assertEqual([r["code"] for r in rows], ["LN1005", "IM1001", "SC0003"])
        text = dashboard.to_records_csv("DEMO-X", rows, 60)
        self.assertIn("SC0003,服務學習課程,0,60,114-1,服務學習", text)

    def test_confirm_flow_reviews_only_graded_rows(self):
        draft = self.extract(ncu_response(), student_id="DEMO-N01")
        self.assertEqual(draft["format"], "ncu-v1")
        self.assertFalse([i for i in draft["issues"] if i["level"] == "error"])
        self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教", draft["cohort"],
                                       acknowledge_warnings=True)
        self.assertEqual(sorted(r.code for r in self.service.students["DEMO-N01"]), ["IM1001", "LN1005", "SC0003"])

    def test_cohort_without_rules_still_blocked(self):
        draft = self.extract(ncu_response(cohort=113), student_id="DEMO-N02")
        self.assertTrue(any(i["level"] == "error" and "113" in i["message"] for i in draft["issues"]))
