"""規則草稿：假的 LLM 回應與虛構確認人，不連網、不啟用規則。（A 組）"""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import knowledge_base, llm, rule_draft
from academic_twin.api import AcademicTwinService

PDF = b"%PDF-1.7 fake public regulation"
# 引文為公開 PDF 的視讀示例；此測試不是承辦人已核定的實證答案。
QUOTES = {
    "R2": "會計學 MT1031 / MT1032\n企業概論 MT1021\n統計學 MT2041 / MT2042\n經濟學 MT1011 / MT1012",
    "R4": "國文 5",
    "R10": "必修科目計 100學分，最低畢業學分 128 學分。",
}


def fake_response(**extra):
    rows = [copy.deepcopy(r) for r in rule_draft.baseline()["requirements"] if r["id"] in rule_draft.TARGETS]
    for r in rows:
        r.update(applicable_year=114, source_page=1, source_quote=QUOTES[r["id"]],
                 applicability="114 學年度入學新生適用", exceptions="來源未載明其他例外，需人工確認",
                 review_notes="請核對清單、門檻與遺漏；其餘要求不在本次草擬範圍")
    return llm.LLMResult({"requirements": rows, "unreadable": "", **extra}, "fake-model", "2026-10-21T00:00:00")


class RuleDraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "drafts"
        self.log = Path(self.tmp.name) / "runs.jsonl"
        for name, value in (("DRAFT_DIR", self.out), ("EVAL_LOG", self.log)):
            patcher = mock.patch.object(rule_draft, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = AcademicTwinService()
        self.active_bytes = rule_draft.BASELINE_PATH.read_bytes()
        self.version = self.service.rule_version
        self.report = self.service.student("S003")

    def draft(self, response=None):
        with mock.patch.object(llm, "generate_json", return_value=response or fake_response()) as generate:
            d = self.service.rule_draft_extract(PDF, 114)
        args = generate.call_args.args
        self.assertIs(args[1], rule_draft.SCHEMA)
        self.assertEqual(args[2][0], llm.Attachment("application/pdf", PDF))
        return d

    def assert_active_unchanged(self):
        self.assertEqual(rule_draft.BASELINE_PATH.read_bytes(), self.active_bytes)
        self.assertEqual(self.service.rule_version, self.version)
        self.assertEqual(self.service.student("S003"), self.report)

    def test_faithful_draft_has_no_errors_and_all_same(self):
        d = self.draft()
        self.assertEqual(d["issues"], [])
        self.assertEqual({r["id"] for r in d["diff"]}, set(rule_draft.TARGETS))
        self.assertTrue(all(r["status"] == "same" for r in d["diff"]))
        self.assertFalse(d["active"])
        self.assertEqual(d["label"], "AI 草稿、未啟用")

    def test_missing_source_page_or_quote_is_error_and_blocks_confirm(self):
        for field, value in (("source_page", 0), ("source_quote", "  ")):
            response = fake_response()
            response.data["requirements"][0][field] = value
            d = self.draft(response)
            self.assertTrue(any("來源" in i["message"] for i in d["issues"]))
            with self.assertRaises(ValueError):
                rule_draft.confirm(d["draft_id"], d["requirements"], "虛構確認人", True)
        self.assert_active_unchanged()

    def test_source_types_and_missing_fields_are_errors(self):
        for field, value in (("source_page", True), ("source_page", 1.5), ("source_page", None),
                             ("source_quote", None), ("source", {}), ("applicable_year", 112)):
            with self.subTest(field=field, value=value):
                response = fake_response()
                response.data["requirements"][0][field] = value
                self.assertTrue(self.draft(response)["issues"])
        response = fake_response()
        del response.data["requirements"][0]["source_quote"]
        self.assertTrue(self.draft(response)["issues"])

    def test_structural_errors_are_rejected(self):
        for field, value in (("min_credits", "128"), ("min_credits", -1), ("min_credits", True),
                             ("kind", "invented"), ("title", ""), ("exceptions", ""),
                             ("unknown_condition", "不支援的條件不可靜默忽略")):
            with self.subTest(field=field, value=value):
                response = fake_response()
                row = next(r for r in response.data["requirements"] if r["id"] == "R10")
                row[field] = value
                self.assertTrue(self.draft(response)["issues"])

    def test_uses_existing_knowledge_base_validation(self):
        response = fake_response()
        response.data["requirements"][0]["courses"] = ["NO-SUCH-COURSE"]
        with mock.patch.object(knowledge_base, "from_dict", wraps=knowledge_base.from_dict) as validate:
            d = self.draft(response)
        validate.assert_called()
        self.assertTrue(any("不在課程清單" in i["message"] for i in d["issues"]))

    def test_unknown_category_and_source_are_errors(self):
        for key, value in (("categories", ["虛構類別"]), ("source", {"doc": "OTHER", "ref": "位置"})):
            response = fake_response()
            row = next(r for r in response.data["requirements"] if r["id"] == "R4")
            row[key] = value
            self.assertTrue(self.draft(response)["issues"])

    def test_changed_threshold_missing_extra_are_human_review_not_errors(self):
        rows = fake_response().data["requirements"]
        rows = [r for r in rows if r["id"] != "R2"]
        next(r for r in rows if r["id"] == "R10")["min_credits"] = 130
        rows.append({**copy.deepcopy(rows[0]), "id": "EXTRA"})
        d = self.draft(fake_response(requirements=rows))
        self.assertEqual(d["issues"], [])
        changes = {r["id"]: r for r in d["diff"]}
        self.assertEqual(changes["R2"]["status"], "missing_in_draft")
        self.assertEqual(changes["EXTRA"]["status"], "extra_in_draft")
        self.assertEqual(changes["R10"]["status"], "different")
        self.assertEqual(changes["R10"]["fields"], [
            {"field": "min_credits", "before": 128, "after": 130, "change": "changed"}])
        self.assertTrue(all(changes[k]["label"] == "需人工確認" for k in ("R2", "EXTRA", "R10")))

    def test_diff_includes_nested_source_and_course_list(self):
        rows = fake_response().data["requirements"]
        rows[0]["source"]["ref"] = "人工修正位置"
        rows[0]["courses"].pop()
        d = self.draft(fake_response(requirements=rows))
        change = next(r for r in d["diff"] if r["id"] == "R2")
        self.assertEqual({f["field"] for f in change["fields"]}, {"source.ref", "courses"})

    def test_draft_only_written_under_separate_directory(self):
        d = self.draft()
        path = self.out / (d["draft_id"] + ".json")
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "draft")
        self.assertEqual(len(list(self.out.iterdir())), 1)
        self.assert_active_unchanged()

    def test_confirm_preserves_original_and_records_reviewer_timestamp_and_counts(self):
        d = self.draft()
        rows = copy.deepcopy(d["requirements"])
        rows[0]["title"] += "（已核對）"
        rows[0]["source_page"] = 2
        original = (self.out / (d["draft_id"] + ".json")).read_bytes()
        confirmed = self.service.rule_draft_confirm(d["draft_id"], rows, "虛構確認人", True)
        saved = json.loads((self.out / (d["draft_id"] + ".confirmed.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "confirmed")
        self.assertFalse(saved["active"])
        self.assertEqual(saved["requirements"], rows)
        self.assertEqual(saved["confirmation"]["reviewer"], "虛構確認人")
        self.assertTrue(saved["confirmation"]["confirmed_at"])
        self.assertEqual(saved["confirmation"]["edited_field_count"], 2)
        self.assertEqual((self.out / (d["draft_id"] + ".json")).read_bytes(), original)
        self.assertFalse(confirmed["active"])
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["edited_field_count"], 2)
        text = json.dumps(entry, ensure_ascii=False)
        for private in ("虛構確認人", QUOTES["R2"], rows[0]["title"], "requirements\""):
            self.assertNotIn(private, text)
        self.assert_active_unchanged()

    def test_confirm_requires_reviewer_and_explicit_acknowledgment(self):
        d = self.draft()
        for reviewer, ack in (("", True), ("  ", True), ("虛構確認人", False), ("虛構確認人", 1)):
            with self.subTest(reviewer=reviewer, ack=ack), self.assertRaises(ValueError):
                self.service.rule_draft_confirm(d["draft_id"], d["requirements"], reviewer, ack)
        self.assertFalse(list(self.out.glob("*.confirmed.json")))

    def test_confirm_revalidates_edited_fields(self):
        d = self.draft()
        rows = copy.deepcopy(d["requirements"])
        rows[0]["courses"] = ["INVALID"]
        with self.assertRaises(ValueError):
            self.service.rule_draft_confirm(d["draft_id"], rows, "虛構確認人", True)
        self.assertFalse(list(self.out.glob("*.confirmed.json")))
        self.assert_active_unchanged()

    def test_invalid_draft_can_be_corrected_before_confirming(self):
        response = fake_response()
        response.data["requirements"][0]["source_quote"] = ""
        d = self.draft(response)
        rows = copy.deepcopy(d["requirements"])
        rows[0]["source_quote"] = QUOTES[rows[0]["id"]]
        checked = self.service.rule_draft_check(d["draft_id"], rows)
        self.assertEqual(checked["issues"], [])
        saved = self.service.rule_draft_confirm(d["draft_id"], rows, "虛構確認人", True)
        self.assertEqual(saved["confirmation"]["edited_field_count"], 1)

    def test_repeated_confirm_cannot_overwrite_record(self):
        d = self.draft()
        self.service.rule_draft_confirm(d["draft_id"], d["requirements"], "虛構甲", True)
        with self.assertRaises(ValueError):
            self.service.rule_draft_confirm(d["draft_id"], d["requirements"], "虛構乙", True)

    def test_unreadable_empty_or_malformed_response_is_not_silent_success(self):
        for data in ({"requirements": [], "unreadable": ""}, {"requirements": {}, "unreadable": ""},
                     {"requirements": [None], "unreadable": ""}, [], {"requirements": [], "unexpected": 1}):
            with self.subTest(data=data):
                response = llm.LLMResult(data, "fake", "t")
                self.assertTrue(self.draft(response)["issues"])
        d = self.draft(fake_response(unreadable="影像不清楚"))
        self.assertTrue(d["issues"])
        with self.assertRaises(ValueError):
            self.service.rule_draft_confirm(d["draft_id"], d["requirements"], "虛構確認人", True)

    def test_invalid_top_level_response_remains_blocked_on_review(self):
        d = self.draft(fake_response(unexpected="不可靜默略過"))
        self.assertTrue(self.service.rule_draft_check(d["draft_id"], d["requirements"])["issues"])
        with self.assertRaises(ValueError):
            self.service.rule_draft_confirm(d["draft_id"], d["requirements"], "虛構確認人", True)

    def test_duplicate_ids_are_errors(self):
        response = fake_response()
        response.data["requirements"].append(copy.deepcopy(response.data["requirements"][0]))
        self.assertTrue(any("重複" in i["message"] for i in self.draft(response)["issues"]))

    def test_rejects_non_pdf_and_other_year_before_llm(self):
        with mock.patch.object(llm, "generate_json") as generate:
            for pdf, year in ((b"not pdf", 114), (PDF, 112), (PDF, True), (PDF, "114")):
                with self.assertRaises(ValueError):
                    rule_draft.run_draft(pdf, year)
            generate.assert_not_called()

    def test_llm_failure_leaves_active_rules_and_audits_unchanged_and_logs_counts(self):
        with mock.patch.object(llm, "generate_json", side_effect=llm.LLMError("503 sensitive detail")):
            with self.assertRaises(llm.LLMError):
                self.service.rule_draft_extract(PDF)
        self.assertFalse(self.out.exists())
        entry = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(entry["type"], "draft_failed")
        self.assertEqual(entry["errors"], 1)
        self.assertNotIn("sensitive detail", json.dumps(entry))
        self.assert_active_unchanged()

    def test_compare_counts_correct_wrong_missing_extra(self):
        answer = fake_response().data["requirements"]
        rows = copy.deepcopy(answer)
        del rows[0]
        rows[0]["min_credits"] += 1
        rows.append({**copy.deepcopy(answer[0]), "id": "EXTRA"})
        self.assertEqual(rule_draft.compare(rows, answer), {
            "correct": 1, "wrong": 1, "missing": 1, "extra": 1, "answer_count": 3, "draft_count": 3})

    def test_compare_ignores_wording_of_review_text(self):
        answer = fake_response().data["requirements"]
        rows = copy.deepcopy(answer)
        for r in rows:
            r["review_notes"] += "（措辭不同）"
            r["exceptions"] = "未載明，需人工確認"
        self.assertEqual(rule_draft.compare(rows, answer)["correct"], len(answer))

    def test_measurement_includes_source_errors_and_duplicates(self):
        answer = fake_response().data["requirements"]
        rows = copy.deepcopy(answer)
        rows[0]["source_page"] = True
        rows.append(copy.deepcopy(rows[1]))
        result = rule_draft.compare(rows, answer)
        self.assertEqual((result["correct"], result["wrong"], result["extra"]), (2, 1, 1))

    def test_answer_must_be_human_confirmed_not_existing_json(self):
        with mock.patch.object(llm, "generate_json") as generate:
            with self.assertRaises(ValueError):
                rule_draft.run_draft(PDF, 114, rule_draft.BASELINE_PATH)
            generate.assert_not_called()
        d = self.draft()
        with mock.patch.object(llm, "generate_json") as generate, self.assertRaises(ValueError):
            rule_draft.run_draft(PDF, 114, self.out / (d["draft_id"] + ".json"))
        generate.assert_not_called()

    def test_confirmed_answer_measurement_is_counts_only(self):
        d = self.draft()
        self.service.rule_draft_confirm(d["draft_id"], d["requirements"], "虛構確認人", True)
        answer = self.out / (d["draft_id"] + ".confirmed.json")
        with mock.patch.object(llm, "generate_json", return_value=fake_response()):
            rule_draft.run_draft(PDF, 114, answer)
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["before_correction"]["correct"], 3)
        self.assertEqual(entry["before_correction"]["wrong"], 0)
        self.assertIn("answer_version", entry)
        for text in ("虛構確認人", "source_quote", str(answer), QUOTES["R10"]):
            self.assertNotIn(text, json.dumps(entry, ensure_ascii=False))

        with mock.patch.object(llm, "generate_json", return_value=fake_response(requirements={})):
            bad = rule_draft.run_draft(PDF, 114, answer)
        self.assertTrue(bad["issues"])
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["before_correction"]["missing"], 3)
        with mock.patch.object(llm, "generate_json", side_effect=llm.LLMError("逾時")):
            with self.assertRaises(llm.LLMError):
                rule_draft.run_draft(PDF, 114, answer)
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["before_correction"]["missing"], 3)
        self.assert_active_unchanged()

    def test_review_after_restart_and_changed_baseline(self):
        d = self.draft()
        service = AcademicTwinService()
        context = rule_draft.baseline()
        next(r for r in context["requirements"] if r["id"] == "R10")["min_credits"] = 129
        with mock.patch.object(rule_draft, "baseline", return_value=context):
            checked = service.rule_draft_check(d["draft_id"], d["requirements"])
        self.assertTrue(checked["baseline_changed"])
        self.assertEqual(next(r for r in checked["diff"] if r["id"] == "R10")["status"], "different")
        self.assert_active_unchanged()

    def test_invalid_draft_identifier_cannot_escape_storage(self):
        for draft_id in ("../curriculum_im_114", "A" * 32, "a" * 31):
            with self.assertRaises(ValueError):
                self.service.rule_draft_check(draft_id, [])
        with self.assertRaises(KeyError):
            self.service.rule_draft_check("a" * 32, [])

    def test_cli_saves_only_draft_and_reports_bad_structure_as_failure(self):
        pdf = Path(self.tmp.name) / "public.pdf"
        pdf.write_bytes(PDF)
        for response, code in ((fake_response(), 0), (fake_response(requirements=[]), 1)):
            with mock.patch.object(llm, "generate_json", return_value=response), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(rule_draft.main([str(pdf), "--year", "114"]), code)
                self.assertIn("AI 草稿、未啟用", output.getvalue())
        self.assertFalse(list(self.out.glob("*.confirmed.json")))
        self.assert_active_unchanged()

    def test_office_routes_draft_check_confirm_without_network(self):
        import web
        payload = {"data": "JVBERi0xLjc=", "year": 114}
        def post(action, body):
            handler = web.Handler.__new__(web.Handler)
            handler.path = "/api/rule-drafts/" + action
            raw = json.dumps(body).encode("utf-8")
            handler.headers = {"Content-Length": str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            handler._json = mock.Mock()
            with mock.patch.object(web, "service", self.service):
                handler.do_POST()
            return handler._json.call_args
        with mock.patch.object(llm, "generate_json", return_value=fake_response()):
            result = post("extract", payload)
        d = result.args[0]
        checked = post("check", {"draft_id": d["draft_id"], "requirements": d["requirements"]}).args[0]
        self.assertEqual(checked["issues"], [])
        confirmed = post("confirm", {"draft_id": d["draft_id"], "requirements": d["requirements"],
                                    "reviewer": "虛構確認人", "acknowledge": True}).args[0]
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertFalse(confirmed["active"])
        self.assertEqual(post("confirm", {"requirements": "bad"}).args[1], 400)
        self.assertEqual(post("extract", {"data": "bad base64"}).args[1], 400)
        self.assert_active_unchanged()


if __name__ == "__main__":
    unittest.main()
