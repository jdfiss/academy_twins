"""儀表板批次解析：名單比對、背景佇列、失敗重試、停止全部（AI 停用）。以假的 AI 回應測試，不連網。"""
import dataclasses
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import dashboard, dashboard_batch, llm
from academic_twin.api import AcademicTwinService
from tests.test_dashboard import fake_response, pdf_pages, two_models

PDF = b"%PDF-1.7 fake"
ROSTER = "student_id,english_passed\nS001,Y\nDEMO-D02,\nDEMO-D03,\n"


class DashboardBatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(dashboard, "EVAL_LOG", Path(tmp.name) / "runs.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = AcademicTwinService()
        self.service.load_data(roster=("roster.csv", ROSTER))

    def run_batch(self, *filenames):
        with two_models(fake_response()):
            out = [self.service.dashboard_batch_add(f, PDF) for f in filenames]
            self.service._batch().wait()
        return out

    def job(self, sid):
        return next(j for j in self.service.dashboard_batch_status()["jobs"] if j["student_id"] == sid)

    def test_filename_not_on_roster_is_not_parsed_and_listed_as_anomaly(self):
        with mock.patch.object(llm, "generate_json") as generate:
            r = self.service.dashboard_batch_add("X999.pdf", PDF)
        generate.assert_not_called()
        self.assertEqual(r["unmatched"]["student_id"], "X999")
        self.assertEqual(self.service.dashboard_batch_status()["jobs"], [])
        anomalies = [a for a in self.service.roster_anomalies() if a["kind"] == "dashboard_not_on_roster"]
        self.assertEqual([a["id"] for a in anomalies], ["X999"])
        self.assertIn("ROSTER_DASHBOARD_NOT_ON_ROSTER", self.service.export_csv())

    def test_filename_must_be_student_id_pdf(self):
        for bad in ("DEMO-D02.csv", ".pdf", "", "DEMO,D02.pdf"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                self.service.dashboard_batch_add(bad, PDF)
        self.assertEqual(dashboard_batch.student_id_from_filename("C:\\scan\\DEMO-D02.PDF"), "DEMO-D02")

    def test_parsed_jobs_wait_for_confirmation_then_enter_audit(self):
        self.run_batch("DEMO-D02.pdf", "DEMO-D03.pdf")
        status = self.service.dashboard_batch_status()
        self.assertEqual(status["counts"]["done"], 2)
        job = self.job("DEMO-D02")
        self.assertEqual(job["rows"], 17)
        self.assertTrue(job["cross_checked"])
        self.assertNotIn("DEMO-D02", self.service.students)  # 確認前不進入審查

        draft = self.service.dashboard_batch_draft("DEMO-D02")
        with pdf_pages(fake_response()):
            s = self.service.dashboard_confirm(draft["draft_id"], draft["rows"], "承辦人", acknowledge_warnings=True)
        self.assertEqual(s["id"], "DEMO-D02")
        self.assertEqual(self.job("DEMO-D02")["state"], "confirmed")
        self.assertEqual(self.job("DEMO-D03")["state"], "done")
        with self.assertRaises(KeyError):
            self.service.dashboard_batch_draft("DEMO-D02")

    def test_failure_is_isolated_and_can_be_retried(self):
        def generate(*args, model=None, **kwargs):
            raise llm.LLMError("503 過載")
        with mock.patch.object(llm, "generate_json", side_effect=generate):
            self.service.dashboard_batch_add("DEMO-D02.pdf", PDF)
            self.service._batch().wait()
        job = self.job("DEMO-D02")
        self.assertEqual(job["state"], "failed")
        self.assertIn("503", job["error"])
        self.assertNotIn("DEMO-D02", self.service.students)

        with two_models(fake_response()):
            self.service.dashboard_batch_retry("DEMO-D02")
            self.service._batch().wait()
        self.assertEqual(self.job("DEMO-D02")["state"], "done")
        with self.assertRaises(ValueError):  # 已完成的不能重試
            self.service.dashboard_batch_retry("DEMO-D02")

    def test_same_student_cannot_be_queued_twice(self):
        release = threading.Event()
        response = fake_response()

        def generate(*args, model=None, **kwargs):
            release.wait(5)
            return dataclasses.replace(response, model=model or response.model)
        with mock.patch.object(llm, "generate_json", side_effect=generate), pdf_pages(response):
            self.service.dashboard_batch_add("DEMO-D02.pdf", PDF)
            with self.assertRaises(ValueError):
                self.service.dashboard_batch_add("DEMO-D02.pdf", PDF)
            release.set()
            self.service._batch().wait()
        self.assertEqual(self.job("DEMO-D02")["state"], "done")

    def test_stop_all_keeps_running_result_stops_queue_and_pauses_ai(self):
        started, release = threading.Event(), threading.Event()
        response = fake_response()

        def generate(*args, model=None, **kwargs):
            started.set()
            release.wait(5)
            return dataclasses.replace(response, model=model or response.model)
        self.service._dashboard_batch = dashboard_batch.DashboardBatch(self.service.dashboard_extract, max_workers=1)
        with mock.patch.object(llm, "generate_json", side_effect=generate), pdf_pages(response):
            self.service.dashboard_batch_add("DEMO-D02.pdf", PDF)
            self.service.dashboard_batch_add("DEMO-D03.pdf", PDF)
            started.wait(5)
            status = self.service.dashboard_batch_stop()
            self.assertTrue(status["paused"])
            self.assertEqual(self.job("DEMO-D03")["state"], "stopped")
            release.set()
            self.service._batch().wait()
        self.assertEqual(self.job("DEMO-D02")["state"], "done")  # 已送出的呼叫結果保留
        self.assertEqual(self.job("DEMO-D03")["state"], "stopped")

        with mock.patch.object(llm, "generate_json") as generate:  # 停用期間系辦端 AI 一律不呼叫
            for call in (lambda: self.service.dashboard_extract(PDF, "DEMO-D02"),
                         lambda: self.service.rule_draft_extract(PDF, 114)):
                with self.assertRaises(llm.LLMError):
                    call()
            for call in (lambda: self.service.dashboard_batch_retry("DEMO-D03"),
                         lambda: self.service.dashboard_batch_add("S001.pdf", PDF)):
                with self.assertRaises(ValueError):
                    call()
        generate.assert_not_called()

        self.service.dashboard_batch_resume()
        with two_models(response):
            self.service.dashboard_batch_retry()
            self.service._batch().wait()
        self.assertEqual(self.job("DEMO-D03")["state"], "done")

    def test_workers_setting(self):
        for raw, want in (("", 3), ("5", 5)):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {"DASHBOARD_WORKERS": raw}):
                self.assertEqual(dashboard_batch.workers(), want)
        for raw in ("0", "-1", "abc"):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {"DASHBOARD_WORKERS": raw}), \
                    self.assertRaises(ValueError):
                dashboard_batch.workers()


if __name__ == "__main__":
    unittest.main()
