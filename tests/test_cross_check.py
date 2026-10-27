"""雙模型交叉驗證：比對、警示與系辦確認流程。以假的 AI 回應測試，不連網、不送出任何資料。"""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import cross_check, dashboard, llm
from academic_twin.api import AcademicTwinService
from academic_twin.records import RecordsError
from tests.test_dashboard import answer_rows, fake_response

CHECK = "check-model"


def models(primary, check=None, primary_name="main-model"):
    """主模型（model=None）回 primary；第二模型回 check，check 為例外時丟出。"""
    def generate(*args, model=None, **kwargs):
        if model is None:
            return dataclasses.replace(primary, model=primary_name)
        if isinstance(check, Exception):
            raise check
        return dataclasses.replace(check or primary, model=model)
    return mock.patch.object(llm, "generate_json", side_effect=generate)


def rows_with(**changes):
    """依標準答案產生 AI 回應的列；changes 以列索引改寫欄位，值為 None 表示刪除該列。"""
    rows = [dict(r) for r in fake_response().data["rows"]]
    for i, change in sorted(changes.items(), key=lambda kv: -int(kv[0][1:])):
        if change is None:
            del rows[int(i[1:])]
        else:
            rows[int(i[1:])].update(change)
    return fake_response(rows)


class CompareTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"GEMINI_CHECK_MODEL": CHECK})
        patcher.start()
        self.addCleanup(patcher.stop)

    def pair(self, primary, check=None):
        with models(primary, check):
            ext, other = cross_check.extract_pair(b"%PDF-1.7 fake")
        return ext, cross_check.recheck(ext.rows, other, ext.model)

    def test_identical_extractions_agree(self):
        ext, cmp = self.pair(fake_response())
        self.assertEqual((cmp.status, cmp.differences, cmp.only_primary, cmp.only_check), ("ok", [], [], []))
        self.assertEqual(cross_check.issues(cmp), [])
        self.assertEqual(cmp.summary(len(ext.rows))["agreed_rows"], len(ext.rows))

    def test_field_difference_is_flagged_on_that_row(self):
        _, cmp = self.pair(fake_response(), rows_with(r0={"grade": "58"}))
        self.assertEqual(cmp.differences, [{"row": 0, "field": "grade", "primary": answer_rows()[0]["grade"],
                                            "check": "58"}])
        [issue] = cross_check.issues(cmp)
        self.assertEqual((issue.level, issue.row), ("warning", 0))
        self.assertIn("成績", issue.message)

    def test_normalized_values_are_not_differences(self):
        first = answer_rows()[0]
        _, cmp = self.pair(fake_response(), rows_with(r0={"grade": first["grade"] + ".0",
                                                          "code": " " + first["code"].lower() + " "}))
        self.assertEqual((cmp.differences, cmp.only_primary), ([], []))

    def test_extra_row_in_primary_and_missing_row_in_primary(self):
        _, cmp = self.pair(rows_with(r1=None), rows_with(r2=None))
        missing = answer_rows()[1]
        self.assertEqual([r["code"] for r in cmp.only_check], [missing["code"]])  # 主模型可能漏列
        self.assertEqual(len(cmp.only_primary), 1)  # 主模型可能多列
        messages = [i.message for i in cross_check.issues(cmp)]
        self.assertTrue(any("可能漏列" in m and missing["code"] in m for m in messages))
        self.assertTrue(any("可能是多列" in m for m in messages))

    def test_duplicate_keys_pair_in_order(self):
        rows = [dict(r) for r in fake_response().data["rows"]]
        dup = {**rows[0], "grade": "40"}
        _, cmp = self.pair(fake_response(rows + [dup]), fake_response(rows + [dup]))
        self.assertEqual((cmp.differences, cmp.only_primary, cmp.only_check), ([], [], []))

    def test_check_model_failure_keeps_primary_and_warns(self):
        ext, cmp = self.pair(fake_response(), llm.LLMError("503 overloaded"))
        self.assertEqual(len(ext.rows), len(answer_rows()))
        self.assertEqual(cmp.status, "unavailable")
        [issue] = cross_check.issues(cmp)
        self.assertIn("未交叉驗證", issue.message)
        self.assertEqual(cmp.summary(len(ext.rows))["agreed_rows"], 0)

    def test_primary_failure_propagates(self):
        def generate(*args, model=None, **kwargs):
            if model is None:
                raise llm.LLMError("503")
            return fake_response()
        with mock.patch.object(llm, "generate_json", side_effect=generate), self.assertRaises(llm.LLMError):
            cross_check.extract_pair(b"%PDF-1.7 fake")

    def test_same_model_after_fallback_is_not_called_agreement(self):
        with models(fake_response(), primary_name=CHECK):
            ext, other = cross_check.extract_pair(b"%PDF-1.7 fake")
        cmp = cross_check.recheck(ext.rows, other, ext.model)
        self.assertEqual(cmp.status, "same_model")
        self.assertIn("未交叉驗證", cross_check.issues(cmp)[0].message)

    def test_check_model_env(self):
        self.assertEqual(cross_check.check_model(), CHECK)
        with mock.patch.dict("os.environ", {"GEMINI_CHECK_MODEL": ""}):
            self.assertEqual(cross_check.check_model(), cross_check.DEFAULT_CHECK_MODEL)


class OfficeFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "runs.jsonl"
        for patcher in (mock.patch.object(dashboard, "EVAL_LOG", self.log),
                        mock.patch.dict("os.environ", {"GEMINI_CHECK_MODEL": CHECK})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = AcademicTwinService()

    def extract(self, primary, check=None):
        with models(primary, check):
            return self.service.dashboard_extract(b"%PDF-1.7 fake", "DEMO-D02")

    def test_draft_reports_cross_check_summary_and_row_warning(self):
        draft = self.extract(fake_response(), rows_with(r0={"credits": "2"}))
        cc = draft["cross_check"]
        self.assertEqual((cc["status"], cc["check_model"], cc["flagged_rows"]), ("ok", CHECK, 1))
        self.assertEqual(cc["agreed_rows"], len(draft["rows"]) - 1)
        self.assertEqual(cc["differences"][0]["field"], "credits")
        self.assertTrue(any(i["row"] == 0 and "兩個模型不一致" in i["message"] for i in draft["issues"]))

    def test_disagreement_blocks_until_acknowledged_and_does_not_change_judgement(self):
        draft = self.extract(fake_response(), rows_with(r0={"grade": "30"}))
        before = dict(self.service.students)
        with self.assertRaises(RecordsError):
            self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教")
        self.assertEqual(self.service.students, before)  # 未確認前不動審查資料
        student = self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教",
                                                 acknowledge_warnings=True)
        self.assertEqual(student["id"], "DEMO-D02")  # 採用承辦人送出的列，判定仍由引擎產生

    def test_correcting_to_check_model_value_clears_warning(self):
        draft = self.extract(rows_with(r0={"grade": "30"}), fake_response())
        rows = [dict(r) for r in draft["rows"]]
        rows[0]["grade"] = answer_rows()[0]["grade"]
        self.service.dashboard_confirm(draft["draft_id"], rows, "王助教")  # 不需勾選警示
        cc = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])["cross_check"]
        self.assertEqual((cc["edited_rows_flagged"], cc["edited_rows_unflagged"], cc["flagged_after_edit"]),
                         (1, 0, 0))

    def test_row_added_from_check_model_report_is_counted(self):
        missing = fake_response().data["rows"][1]
        draft = self.extract(rows_with(r1=None), fake_response())
        rows = [dict(r) for r in draft["rows"]] + [{**missing, "category": ""}]
        self.service.dashboard_confirm(draft["draft_id"], rows, "王助教")
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["cross_check"]["rows_added_reported_by_check"], 1)
        self.assertNotIn(missing["code"], json.dumps(entry, ensure_ascii=False))  # 紀錄只有數字

    def test_check_model_failure_still_allows_confirm_after_ack(self):
        draft = self.extract(fake_response(), llm.LLMError("timeout"))
        self.assertEqual(draft["cross_check"]["status"], "unavailable")
        with self.assertRaises(RecordsError):
            self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教")
        self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "王助教", acknowledge_warnings=True)
        cc = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])["cross_check"]
        self.assertEqual(cc["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
