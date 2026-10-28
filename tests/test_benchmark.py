"""虛構基準集與量測指標。以替身 LLM 測試，不連網、不送出任何資料；不啟動瀏覽器。（A 組）"""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import benchmark, dashboard, demo_dashboard, llm

SAMPLES = Path(__file__).parent.parent / "data" / "samples"
CODE_OF = {v: k for k, v in dashboard.FORMATS["demo-v1"].items() if v}


def ans(code, term="114-1", name=None, credits="3", grade="80", category=""):
    return {"student_id": "DEMO-T", "code": code, "name": name or f"課{code}", "credits": credits, "grade": grade,
            "term": term, "category": category}


def ai_row(r, **change):
    """把答案列轉成 AI 擷取的列（含規則代碼與來源），change 可模擬讀錯。"""
    row = {"rule_code": CODE_OF.get(r["category"], "A01"), "term": r["term"], "code": r["code"], "name": r["name"],
           "credits": r["credits"], "grade": r["grade"], "source_page": 1, "source_text": r["code"],
           "category": r["category"]}
    row.update(change)
    return row


def extraction(rows, model):
    return dashboard.Extraction(rows, model=model, generated_at="2026-10-27T00:00:00")


ANSWER = [ans("A1"), ans("B1"), ans("C1"), ans("D1"), ans("E1", "114-2")]


class ScoreRunTest(unittest.TestCase):
    def test_all_correct(self):
        rows = [ai_row(a) for a in ANSWER]
        s = benchmark.score_run(extraction(rows, "p"), extraction(rows, "c"), ANSWER)
        self.assertEqual(s["primary"]["field_accuracy"], 1.0)
        self.assertTrue(s["primary"]["fully_correct"])
        self.assertEqual(s["cross"], {"rows": 5, "flagged": 0, "error_rows": 0, "error_flagged": 0, "agreed": 5,
                                      "agreed_errors": 0, "missing_rows": 0, "missing_caught": 0, "only_check": 0})
        summary = benchmark.aggregate([({"case_id": "x", "split": "開發", "tags": ["baseline"]}, 1, s)])["overall"]
        self.assertIsNone(summary["cross_check"]["catch_rate"])  # 沒有錯誤列，抓錯率無分母
        self.assertEqual(summary["cross_check"]["agreed_error_rate"], 0.0)
        self.assertEqual(summary["cross_check"]["review_share"], 0.0)

    def test_all_wrong_in_the_same_way_is_not_caught(self):
        rows = [ai_row(a, grade="18") for a in ANSWER]  # 兩模型犯同樣的錯：一致不代表正確
        s = benchmark.score_run(extraction(rows, "p"), extraction(rows, "c"), ANSWER)
        o = benchmark.aggregate([({"case_id": "x", "split": "開發", "tags": []}, 1, s)])["overall"]
        self.assertEqual(o["primary"]["field_correct"], 25)
        self.assertEqual(o["cross_check"]["catch_rate"], 0.0)
        self.assertEqual(o["cross_check"]["agreed_error_rate"], 1.0)
        self.assertIsNone(o["cross_check"]["flag_precision"])

    def test_all_wrong_differently_is_all_caught(self):
        p = [ai_row(a, grade="18") for a in ANSWER]
        c = [ai_row(a, grade="81") for a in ANSWER]
        s = benchmark.score_run(extraction(p, "p"), extraction(c, "c"), ANSWER)
        self.assertEqual((s["cross"]["error_rows"], s["cross"]["error_flagged"], s["cross"]["agreed"]), (5, 5, 0))
        o = benchmark.aggregate([({"case_id": "x", "split": "開發", "tags": []}, 1, s)])["overall"]
        self.assertEqual(o["cross_check"]["catch_rate"], 1.0)
        self.assertIsNone(o["cross_check"]["agreed_error_rate"])
        self.assertEqual(o["cross_check"]["review_share"], 1.0)
        self.assertEqual(o["check"]["field_correct"], 25)

    def test_mixed_errors_missing_and_extra_rows(self):
        a, b, c, d, e = ANSWER
        extra = ai_row(ans("X9"))
        primary = [ai_row(a), ai_row(b, grade="8"), ai_row(c, name="錯名"), ai_row(e), extra]
        check = [ai_row(a), ai_row(b), ai_row(c, name="錯名"), ai_row(d), ai_row(e)]
        s = benchmark.score_run(extraction(primary, "p"), extraction(check, "c"), ANSWER)
        self.assertEqual(s["cross_status"], "ok")
        self.assertEqual(s["cross"], {"rows": 5, "flagged": 2, "error_rows": 3, "error_flagged": 2, "agreed": 3,
                                      "agreed_errors": 1, "missing_rows": 1, "missing_caught": 1, "only_check": 1})
        self.assertEqual((s["primary"]["field_correct"], s["primary"]["field_total"]), (22, 30))
        self.assertEqual((s["primary"]["missing_rows"], s["primary"]["extra_rows"]), (1, 1))
        self.assertEqual((s["check"]["field_correct"], s["check"]["missing_rows"]), (29, 0))
        cc = benchmark.aggregate([({"case_id": "x", "split": "開發", "tags": []}, 1, s)])["overall"]["cross_check"]
        self.assertEqual(cc["catch_rate"], round(2 / 3, 4))
        self.assertEqual(cc["agreed_error_rate"], round(1 / 3, 4))
        self.assertEqual(cc["review_share"], 0.4)
        self.assertEqual(cc["flag_precision"], 1.0)
        self.assertEqual(cc["catch_rate_with_missing"], 0.75)

    def test_duplicate_row_counts_as_error(self):
        self.assertEqual(benchmark.row_errors([ai_row(ANSWER[0]), ai_row(ANSWER[0])], ANSWER), [False, True])

    def test_fullwidth_punctuation_is_not_an_error(self):
        a = ans("F1", name="會計學（上）")
        self.assertEqual(benchmark.row_errors([ai_row(a, name="會計學(上)")], [a]), [False])

    def test_zero_rows(self):
        s = benchmark.score_run(extraction([], "p"), extraction([], "c"), [])
        o = benchmark.aggregate([({"case_id": "x", "split": "開發", "tags": []}, 1, s)])["overall"]
        self.assertIsNone(o["primary"]["field_accuracy"])
        for k in ("catch_rate", "agreed_error_rate", "review_share", "flag_precision", "missing_caught_rate"):
            self.assertIsNone(o["cross_check"][k], k)
        s = benchmark.score_run(extraction([], "p"), extraction([], "c"), ANSWER)  # 擷取不到任何列
        o = benchmark.aggregate([({"case_id": "x", "split": "開發", "tags": []}, 1, s)])["overall"]
        self.assertEqual(o["primary"]["field_accuracy"], 0.0)
        self.assertEqual((o["primary"]["missing_rows"], o["cross_check"]["missing_rows"]), (5, 5))
        self.assertEqual(o["cross_check"]["missing_caught"], 0)

    def test_second_model_failure_is_not_cross_checked(self):
        rows = [ai_row(a) for a in ANSWER]
        s = benchmark.score_run(extraction(rows, "p"), "第二模型擷取失敗：503", ANSWER)
        self.assertEqual((s["cross_status"], s["check"], s["cross"]), ("unavailable", None, None))
        o = benchmark.aggregate([({"case_id": "x", "split": "開發", "tags": []}, 1, s)])["overall"]
        self.assertEqual(o["check_failed"], 1)
        self.assertEqual(o["cross_check"]["unchecked_rows"], 5)
        self.assertEqual(o["cross_check"]["status_runs"], {"unavailable": 1})
        self.assertIsNone(o["cross_check"]["review_share"])
        self.assertEqual(o["primary"]["field_accuracy"], 1.0)

    def test_same_model_after_fallback_is_not_cross_checked(self):
        rows = [ai_row(a) for a in ANSWER]
        s = benchmark.score_run(extraction(rows, "same"), extraction(rows, "same"), ANSWER)
        self.assertEqual(s["cross_status"], "same_model")
        self.assertIsNone(s["cross"])


class RunTest(unittest.TestCase):
    """端到端：替身 LLM 依 model 參數回傳正確或刻意出錯的結果，經 extract_pair 再評分。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        pdf = Path(self.tmp.name) / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        self.cases = [
            {"meta": {"case_id": "c1", "split": "開發", "tags": ["retake"], "case_set": "t", "seed": 1, "rows": 5},
             "pdf": pdf, "answer": ANSWER},
            {"meta": {"case_id": "c2", "split": "驗證", "tags": ["retake", "failed"], "case_set": "t", "seed": 1,
                      "rows": 5}, "pdf": pdf, "answer": ANSWER},
        ]
        env = mock.patch.dict("os.environ", {"GEMINI_MODEL": "fake-primary", "GEMINI_CHECK_MODEL": "fake-check"})
        env.start()
        self.addCleanup(env.stop)

    def fake_llm(self, primary_rows=None, check_fails=False, primary_fails=False):
        def generate_json(prompt, schema, attachments=(), model=None, **kw):
            if model is None:  # 主模型
                if primary_fails:
                    raise llm.LLMError("primary down")
                rows = primary_rows if primary_rows is not None else [ai_row(a) for a in ANSWER]
                return llm.LLMResult({"rows": rows, "sections": []}, "fake-primary", "2026-10-27T00:00:00")
            if check_fails:
                raise llm.LLMError("check down")
            return llm.LLMResult({"rows": [ai_row(a) for a in ANSWER], "sections": []}, model, "2026-10-27T00:00:00")
        return mock.patch.object(llm, "generate_json", side_effect=generate_json)

    def test_run_groups_by_tag_split_and_run(self):
        wrong = [ai_row(a, grade="1") if i == 0 else ai_row(a) for i, a in enumerate(ANSWER)]
        with self.fake_llm(wrong), mock.patch("sys.stderr"):
            report = benchmark.run(self.cases, runs=2)
        o = report["overall"]
        self.assertEqual((o["runs"], o["cases"], o["primary_failed"]), (4, 2, 0))
        self.assertEqual(o["primary"]["models"], {"fake-primary": 4})
        self.assertEqual(o["check"]["models"], {"fake-check": 4})
        self.assertEqual((o["cross_check"]["error_rows"], o["cross_check"]["error_flagged"]), (4, 4))
        self.assertEqual(o["cross_check"]["catch_rate"], 1.0)
        self.assertEqual(report["by_tag"]["retake"]["runs"], 4)
        self.assertEqual(report["by_tag"]["failed"]["runs"], 2)
        self.assertEqual(set(report["by_split"]), {"開發", "驗證"})
        self.assertEqual(set(report["by_run"]), {"1", "2"})
        self.assertEqual(report["configured_models"]["check"], "fake-check")
        self.assertEqual(report["prompt_version"], dashboard.PROMPT_VERSION)
        self.assertIn("虛構", report["disclaimer"])

    def test_check_failure_keeps_primary_results(self):
        with self.fake_llm(check_fails=True), mock.patch("sys.stderr"):
            o = benchmark.run(self.cases, runs=1)["overall"]
        self.assertEqual((o["check_failed"], o["primary"]["field_accuracy"]), (2, 1.0))
        self.assertEqual(o["cross_check"]["status_runs"], {"unavailable": 2})
        self.assertEqual(o["cross_check"]["unchecked_rows"], 10)

    def test_primary_failure_is_counted_not_fatal(self):
        with self.fake_llm(primary_fails=True), mock.patch("sys.stderr"):
            o = benchmark.run(self.cases, runs=1)["overall"]
        self.assertEqual((o["runs"], o["primary_failed"], o["primary"]["runs"]), (2, 2, 0))
        self.assertIsNone(o["primary"]["field_accuracy"])

    def test_report_files_have_numbers_only(self):
        with self.fake_llm(), mock.patch("sys.stderr"):
            report = benchmark.run(self.cases, runs=1)
        js, md = benchmark.write(report, Path(self.tmp.name) / "eval")
        text = js.read_text(encoding="utf-8") + md.read_text(encoding="utf-8")
        self.assertIn("虛構", md.read_text(encoding="utf-8"))
        self.assertIn("抓錯率", md.read_text(encoding="utf-8"))
        for content in ("課A1", "114-1"):  # 不寫課程內容
            self.assertNotIn(content, text)

    def test_missing_key_exits(self):
        with mock.patch.object(llm, "api_key", side_effect=llm.LLMError("未設定 GEMINI_API_KEY")), \
                mock.patch.object(benchmark, "run") as run:
            with self.assertRaises(SystemExit) as ctx:
                benchmark.main(["--runs", "1"])
        self.assertIn("GEMINI_API_KEY", str(ctx.exception.code))
        run.assert_not_called()


class BenchCasesTest(unittest.TestCase):
    """案例產生器：同種子同輸出；只測 HTML 與答案，不啟動瀏覽器。"""

    def test_same_seed_same_output(self):
        a, b = demo_dashboard.bench_cases(), demo_dashboard.bench_cases()
        self.assertEqual(a, b)
        self.assertEqual([demo_dashboard.render_html(c["student_id"], c["rows"], c["cohort"]) for c in a],
                         [demo_dashboard.render_html(c["student_id"], c["rows"], c["cohort"]) for c in b])
        self.assertNotEqual(a, demo_dashboard.bench_cases(seed=demo_dashboard.BENCH_SEED + 1))

    def test_cases_cover_scenarios_and_align(self):
        cases = demo_dashboard.bench_cases()
        self.assertTrue(20 <= len(cases) <= 30)
        tags = {t for c in cases for t in c["tags"]}
        self.assertEqual(tags, set(demo_dashboard.TAGS))
        for c in cases:
            with self.subTest(case=c["case_id"]):
                keys = [(r["code"], r["term"]) for r in c["rows"]]
                self.assertEqual(len(keys), len(set(keys)))  # 答案可一對一對齊
                self.assertTrue(c["student_id"].startswith("DEMO-"))
                self.assertTrue(all(r["student_id"] == c["student_id"] for r in c["rows"]))
                rows, t = c["rows"], set(c["tags"])
                if "retake" in t:
                    self.assertGreater(len(keys), len({k[0] for k in keys}))
                if "grade_boundary" in t:
                    self.assertTrue({"59", "60"} <= {r["grade"] for r in rows})
                if "long_multipage" in t:
                    self.assertGreaterEqual(len(rows), 70)
                if "substitution" in t:
                    self.assertTrue(any(r["category"] == "抵免" for r in rows))
                if "other_cohort" in t:
                    self.assertNotEqual(c["cohort"], 114)
                    self.assertIn(f"入學學年度：{c['cohort']}",
                                  demo_dashboard.render_html(c["student_id"], rows, c["cohort"]))

    def test_committed_files_match_generator(self):
        loaded = {c["meta"]["case_id"]: c for c in benchmark.load_cases()}
        cases = demo_dashboard.bench_cases()
        self.assertEqual(set(loaded), {c["case_id"] for c in cases})
        for c in cases:
            with self.subTest(case=c["case_id"]):
                path = SAMPLES / "bench" / f"{c['case_id']}_answer.csv"
                self.assertEqual(path.read_text(encoding="utf-8"), demo_dashboard.answer_csv(c["rows"]))
                meta = loaded[c["case_id"]]["meta"]
                self.assertEqual({k: v for k, v in meta.items() if k != "pages"},
                                 {k: v for k, v in demo_dashboard.case_meta(c).items() if k != "pages"})
                self.assertTrue(meta["fictional"])
                self.assertTrue(loaded[c["case_id"]]["pdf"].is_file())

    def test_original_demos_unchanged(self):
        for demo_id in demo_dashboard.DEMOS:
            stem = demo_id.lower().replace("-", "_")
            path = SAMPLES / f"dashboard_{stem}_answer.csv"
            self.assertEqual(path.read_text(encoding="utf-8"),
                             demo_dashboard.answer_csv(demo_dashboard.demo_records(demo_id)))
            html = demo_dashboard.render_html(demo_id, demo_dashboard.demo_records(demo_id))
            self.assertIn(f"入學學年度：{demo_dashboard.KB.cohort}", html)


if __name__ == "__main__":
    unittest.main()
