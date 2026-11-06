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
