import json
"""評審模式：預錄 AI 回應的錄製與重播。以假的 AI 回應測試，不連網。（A 組）"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import dashboard, llm, replay
from academic_twin.api import AcademicTwinService
from tests.test_dashboard import fake_response, pdf_pages, two_models

PDF = b"%PDF-1.7 fictional demo"
ATTACH = [llm.Attachment("application/pdf", PDF)]
NL = "\n"


class ReplayTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(replay, "REPLAY_DIR", Path(tmp.name) / "replay"),
                        mock.patch.object(dashboard, "EVAL_LOG", Path(tmp.name) / "runs.jsonl")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dir = Path(tmp.name) / "replay"

    def env(self, **values):
        return mock.patch.dict("os.environ", {"ACADEMIC_TWIN_REPLAY": "", "ACADEMIC_TWIN_RECORD": "", **values})

    def record(self):
        with self.env(ACADEMIC_TWIN_RECORD="1"), \
                mock.patch.object(llm, "generate_json", return_value=fake_response()):
            replay.generate_json("提示", {}, ATTACH)

    def test_live_call_is_not_saved_unless_recording(self):
        with self.env(), mock.patch.object(llm, "generate_json", return_value=fake_response()):
            result = replay.generate_json("提示", {}, ATTACH)
        self.assertIsNone(getattr(result, "replay", None))
        self.assertFalse(self.dir.exists())

    def test_judge_mode_replays_without_calling_model(self):
        self.record()
        with self.env(ACADEMIC_TWIN_REPLAY="only"), mock.patch.object(llm, "generate_json") as live:
            result = replay.generate_json("提示", {}, ATTACH)
        live.assert_not_called()
        self.assertEqual(result.data, fake_response().data)
        self.assertEqual(result.replay["model"], "fake-model")
        self.assertEqual(result.replay["generated_at"], "2026-10-21T00:00:00")

    def test_judge_mode_rejects_unrecorded_document(self):
        self.record()
        other = [llm.Attachment("application/pdf", b"%PDF-1.7 something else")]
        for prompt, attachments in (("提示", other), ("改過的提示", ATTACH)):
            with self.subTest(prompt=prompt), self.env(ACADEMIC_TWIN_REPLAY="only"), \
                    mock.patch.object(llm, "generate_json") as live, self.assertRaises(llm.LLMError):
                replay.generate_json(prompt, {}, attachments)
            live.assert_not_called()

    def test_fallback_replays_only_when_live_call_fails(self):
        self.record()
        with self.env(), mock.patch.object(llm, "generate_json", side_effect=llm.LLMError("沒有金鑰")):
            result = replay.generate_json("提示", {}, ATTACH)
        self.assertIn("沒有金鑰", result.replay["reason"])
        with self.env(ACADEMIC_TWIN_REPLAY="off"), \
                mock.patch.object(llm, "generate_json", side_effect=llm.LLMError("沒有金鑰")), \
                self.assertRaises(llm.LLMError):
            replay.generate_json("提示", {}, ATTACH)

    def test_fallback_model_response_is_not_recorded(self):
        """審查 #2：主模型逾時改用備援時，不能把備援的回應存成主模型的錄製結果。"""
        response = fake_response()
        response.fallback_from = "Gemini（primary）等待逾時"
        with self.env(ACADEMIC_TWIN_RECORD="1"), mock.patch.object(llm, "generate_json", return_value=response):
            replay.generate_json("提示", {}, ATTACH)
        self.assertFalse(self.dir.exists())

    def test_measurement_entry_points_disable_replay(self):
        """審查 #6：量測命令列一律即時呼叫，主模型失敗時不能拿錄製結果充數。"""
        self.record()
        with self.env():
            replay.disable()
            with mock.patch.object(llm, "generate_json", side_effect=llm.LLMError("503")), \
                    self.assertRaises(llm.LLMError):
                replay.generate_json("提示", {}, ATTACH)

    def test_live_call_logs_token_usage_counts_only(self):
        log = Path(self.dir.parent) / "usage.jsonl"
        response = fake_response()
        response.usage = {"promptTokenCount": 1200, "candidatesTokenCount": 300, "thoughtsTokenCount": 50,
                          "totalTokenCount": 1550}
        with self.env(ACADEMIC_TWIN_RECORD="1"), mock.patch.object(replay, "USAGE_LOG", log),                 mock.patch.object(llm, "generate_json", return_value=response):
            replay.generate_json("提示含個資以外的內容", {}, ATTACH, purpose="dashboard")
        entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual((e["purpose"], e["input_tokens"], e["output_tokens"], e["thinking_tokens"]),
                         ("dashboard", 1200, 300, 50))
        self.assertNotIn("提示", json.dumps(e, ensure_ascii=False))  # 只記計數
        saved = json.loads(next(self.dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["usage"]["promptTokenCount"], 1200)  # 錄製檔也保留用量
        self.assertEqual(replay.usage_summary(entries)["rows"][0]["avg_output"], 350)

    def test_failed_call_usage_is_logged_and_counts_toward_budget(self):
        """逾時、重複迴圈、截斷的呼叫也計費：失敗與備援前的用量都要記入，否則每月預算會低估。"""
        log = Path(self.dir.parent) / "usage.jsonl"
        error = llm.LLMError("截斷", usage=[("m1", {"totalTokenCount": 700})])
        response = fake_response()
        response.usage = {"totalTokenCount": 100}
        response.failed_usage = [("m1", {"totalTokenCount": 300})]
        with self.env(ACADEMIC_TWIN_REPLAY="off"), mock.patch.object(replay, "USAGE_LOG", log):
            with mock.patch.object(llm, "generate_json", side_effect=error), self.assertRaises(llm.LLMError):
                replay.generate_json("提示", {}, ATTACH, purpose="dashboard")
            with mock.patch.object(llm, "generate_json", return_value=response):
                replay.generate_json("提示", {}, ATTACH, purpose="dashboard")
            entries = replay.read_usage()
        self.assertEqual([(e["total_tokens"], e.get("failed", False)) for e in entries],
                         [(700, True), (300, True), (100, False)])
        self.assertEqual(replay.month_usage(entries), 1100)

    def test_replay_and_stub_results_do_not_log_usage(self):
        log = Path(self.dir.parent) / "usage.jsonl"
        self.record()  # 替身沒有 usage
        with mock.patch.object(replay, "USAGE_LOG", log), self.env(ACADEMIC_TWIN_REPLAY="only"):
            replay.generate_json("提示", {}, ATTACH)
        self.assertFalse(log.exists())

    def test_monthly_budget_stops_live_calls(self):
        log = Path(self.dir.parent) / "usage.jsonl"
        month = replay.datetime.now().strftime("%Y-%m")
        log.write_text(json.dumps({"at": f"{month}-01T00:00:00", "total_tokens": 1000}) + NL
                       + json.dumps({"at": "2000-01-01T00:00:00", "total_tokens": 10**9}) + NL, encoding="utf-8")
        with mock.patch.object(replay, "USAGE_LOG", log), mock.patch.object(llm, "generate_json") as live:
            with self.env(ACADEMIC_TWIN_TOKEN_BUDGET="1000"), self.assertRaises(llm.LLMError) as e:
                replay.generate_json("提示", {}, ATTACH)
            self.assertIn("預算", str(e.exception))
            live.assert_not_called()
            live.return_value = fake_response()
            with self.env(ACADEMIC_TWIN_TOKEN_BUDGET="1001"):  # 只算本月，舊月份不計
                replay.generate_json("提示", {}, ATTACH)
            with self.env(ACADEMIC_TWIN_TOKEN_BUDGET="0"):  # 0＝不設上限
                replay.generate_json("提示", {}, ATTACH)
            self.assertEqual(live.call_count, 2)

    def test_budget_exceeded_falls_back_to_recording(self):
        log = Path(self.dir.parent) / "usage.jsonl"
        self.record()
        month = replay.datetime.now().strftime("%Y-%m")
        log.write_text(json.dumps({"at": f"{month}-02T00:00:00", "total_tokens": 50}) + NL, encoding="utf-8")
        with mock.patch.object(replay, "USAGE_LOG", log), self.env(ACADEMIC_TWIN_TOKEN_BUDGET="10"), \
                mock.patch.object(llm, "generate_json") as live:
            result = replay.generate_json("提示", {}, ATTACH)
        live.assert_not_called()
        self.assertIn("預算", result.replay["reason"])

    def test_invalid_mode_is_rejected(self):
        with self.env(ACADEMIC_TWIN_REPLAY="maybe"), self.assertRaises(ValueError):
            replay.mode()

    def test_office_flow_marks_replay_in_draft_and_trace(self):
        service = AcademicTwinService()
        with self.env(ACADEMIC_TWIN_RECORD="1"), two_models(fake_response()):
            service.dashboard_extract(PDF, "DEMO-D02")
        with self.env(ACADEMIC_TWIN_REPLAY="only"), pdf_pages(fake_response()), \
                mock.patch.object(llm, "generate_json") as live:
            draft = service.dashboard_extract(PDF, "DEMO-D02")
        live.assert_not_called()
        self.assertEqual(draft["replay"]["model"], "fake-model")
        self.assertTrue(draft["trace"]["replay"])
        self.assertEqual(draft["cross_check"]["status"], "ok")  # 第二模型也是重播
        self.assertEqual(service.data_source()["replay_mode"], "fallback")
        service.dashboard_confirm(draft["draft_id"], draft["rows"], "虛構確認人")  # 重播草稿走同一套確認
        self.assertIn("DEMO-D02", service.students)


if __name__ == "__main__":
    unittest.main()
