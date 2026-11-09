"""監控者模式：分流、批次確認＋隨機抽查、工作狀態存檔。以假的 AI 回應測試，不連網。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import dashboard, dashboard_batch as db, monitor
from academic_twin.api import AcademicTwinService
from tests.test_dashboard import fake_response, two_models

PDF = b"%PDF-1.7 fake"
IDS = [f"DEMO-M{i:02d}" for i in range(1, 12)]  # 11 份 → 抽 2 份


def job(**kw):
    base = {"student_id": "X", "state": db.DONE, "errors": 0, "warnings": 0, "cross_checked": True,
            "flagged_rows": 0, "corrected": False}
    return {**base, **kw}


class TriageTest(unittest.TestCase):
    def test_buckets(self):
        cases = [
            (job(state=db.QUEUED), monitor.PENDING), (job(state=db.RUNNING), monitor.PENDING),
            (job(state=db.FAILED), monitor.FAILED), (job(state=db.STOPPED), monitor.FAILED),
            (job(state=db.CONFIRMED), monitor.CONFIRMED),
            (job(errors=1, warnings=3), monitor.FIX),
            (job(warnings=1), monitor.REVIEW), (job(cross_checked=False), monitor.REVIEW),
            (job(flagged_rows=1), monitor.REVIEW), (job(corrected=True), monitor.REVIEW),
            (job(), monitor.RELEASE),
        ]
        for summary, bucket in cases:
            with self.subTest(summary=summary):
                self.assertEqual(monitor.triage(summary), bucket)

    def test_sample_size_is_at_least_one_and_about_ten_percent(self):
        for n, k in [(0, 0), (1, 1), (6, 1), (10, 1), (11, 2), (25, 3)]:
            self.assertEqual(monitor.sample_size(n), k, n)

    def test_edited(self):
        none = {"fields_changed": 0, "rows_added": 0, "rows_removed": 0}
        self.assertFalse(monitor.edited(none))
        for k in none:
            self.assertTrue(monitor.edited({**none, k: 1}))


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "runs.jsonl"
        patcher = mock.patch.object(dashboard, "EVAL_LOG", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = AcademicTwinService()
        self.service.load_data(roster=("roster.csv", "student_id,cohort\n" + "".join(f"{s},114\n" for s in IDS)))
        with two_models(fake_response()):
            for sid in IDS:
                self.service.dashboard_batch_add(f"{sid}.pdf", PDF)
            self.service._batch().wait()

    def status(self):
        return self.service.dashboard_batch_status()

    def buckets(self):
        return {j["student_id"]: j["bucket"] for j in self.status()["jobs"]}

    def confirm(self, sid, change=None):
        draft = self.service.dashboard_batch_draft(sid)
        rows = [dict(r) for r in draft["rows"]]
        if change:
            change(rows)
        return self.service.dashboard_confirm(draft["draft_id"], rows, "監控者", 114, True)

    def logs(self, kind):
        return [e for e in map(json.loads, self.log.read_text(encoding="utf-8").splitlines()) if e["type"] == kind]

    def test_clean_jobs_are_suggested_for_release(self):
        self.assertEqual(set(self.buckets().values()), {monitor.RELEASE})
        self.assertEqual(self.status()["buckets"][monitor.RELEASE], len(IDS))

    def test_start_requires_reviewer_and_candidates(self):
        with self.assertRaises(ValueError):
            self.service.release_start(" ")
        for sid in IDS:
            self.confirm(sid)
        with self.assertRaisesRegex(ValueError, "沒有建議放行"):
            self.service.release_start("監控者")

    def test_server_picks_random_sample_and_holds_the_rest(self):
        st = self.service.release_start("監控者")
        active = st["release"]["active"]
        self.assertEqual(len(active["sampled"]), 2)
        self.assertEqual(sorted(active["sampled"] + active["held"]), IDS)
        b = self.buckets()
        self.assertEqual(sorted(s for s, x in b.items() if x == monitor.SAMPLING), active["sampled"])
        self.assertEqual(sum(x == monitor.HELD for x in b.values()), len(IDS) - 2)
        with self.assertRaisesRegex(ValueError, "正在抽查"):
            self.service.release_start("監控者")

    def test_unedited_sample_releases_the_rest(self):
        active = self.service.release_start("監控者")["release"]["active"]
        with self.assertRaisesRegex(ValueError, "尚未全部完成"):
            self.service.release_finish("監控者")
        for sid in active["sampled"]:
            self.confirm(sid)
        self.assertEqual(self.status()["release"]["active"]["status"], monitor.READY)
        st = self.service.release_finish("監控者")
        self.assertEqual(set(self.buckets().values()), {monitor.CONFIRMED})
        self.assertEqual(st["release"]["last"]["released"], len(IDS) - 2)
        self.assertIsNone(st["release"]["active"])
        self.assertTrue(set(IDS) <= set(self.service.students))
        modes = [e["mode"] for e in self.logs("confirm")]
        self.assertEqual((modes.count("sampled"), modes.count("batch")), (2, len(IDS) - 2))
        [sample] = self.logs("sample")
        self.assertEqual((sample["outcome"], sample["sampled"], sample["sampled_edited"], sample["released"]),
                         ("released", 2, 0, len(IDS) - 2))
        self.assertNotIn("DEMO-M", json.dumps(sample))  # 只記計數

    def test_edited_sample_cancels_batch_and_disables_release(self):
        active = self.service.release_start("監控者")["release"]["active"]

        def change(rows):
            rows[0]["grade"] = "61"
        self.confirm(active["sampled"][0], change)
        st = self.status()
        self.assertFalse(st["release"]["enabled"])
        self.assertIn(active["sampled"][0], st["release"]["note"])
        self.assertIsNone(st["release"]["active"])
        b = self.buckets()
        self.assertEqual(b[active["sampled"][0]], monitor.CONFIRMED)
        for sid in active["held"] + active["sampled"][1:]:
            self.assertEqual(b[sid], monitor.REVIEW)  # 全部改回逐份核對
        with self.assertRaisesRegex(ValueError, "已停用"):
            self.service.release_start("監控者")
        [sample] = self.logs("sample")
        self.assertEqual((sample["outcome"], sample["sampled_edited"]), ("cancelled_by_sample", 1))
        self.service.release_reopen("監控者")
        self.assertTrue(self.status()["release"]["enabled"])
        self.assertEqual(self.buckets()[active["held"][0]], monitor.REVIEW)  # 已改逐份的不會又變回放行

    def test_monitor_cancel_returns_jobs_to_release(self):
        self.service.release_start("監控者")
        self.service.release_cancel("監控者")
        st = self.status()
        self.assertTrue(st["release"]["enabled"])
        self.assertEqual(set(self.buckets().values()), {monitor.RELEASE})
        self.assertEqual(self.logs("sample")[0]["outcome"], "cancelled_by_monitor")

    def test_held_job_failing_recheck_goes_to_fix(self):
        active = self.service.release_start("監控者")["release"]["active"]
        broken = active["held"][0]
        draft_id = self.service.dashboard_batch_draft(broken)["draft_id"]
        del self.service._dashboard_drafts[draft_id]  # 例如草稿已失效
        for sid in active["sampled"]:
            self.confirm(sid)
        st = self.service.release_finish("監控者")
        self.assertEqual(st["release"]["last"]["failed"], [broken])
        self.assertEqual(self.buckets()[broken], monitor.FIX)

    def test_confirm_works_while_ai_is_stopped(self):
        self.service.dashboard_batch_stop()
        active = self.service.release_start("監控者")["release"]["active"]
        for sid in active["sampled"]:
            self.confirm(sid)
        self.service.release_finish("監控者")
        self.assertEqual(set(self.buckets().values()), {monitor.CONFIRMED})

    def test_parsed_but_unconfirmed_is_listed_as_pending(self):
        kinds = {a["id"]: a["kind"] for a in self.service.roster_anomalies()}
        self.assertEqual(kinds[IDS[0]], "dashboard_pending")
        self.confirm(IDS[0])
        self.assertNotIn(IDS[0], {a["id"] for a in self.service.roster_anomalies()})

    def test_demo_batch_resets_release(self):
        self.service.release_start("監控者")
        uid = self.service.batch_uid()
        self.service.load_demo_batch()
        st = self.status()
        self.assertIsNone(st["release"]["active"])
        self.assertEqual(st["jobs"], [])
        self.assertNotEqual(self.service.batch_uid(), uid)


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "state"
        patcher = mock.patch.object(dashboard, "EVAL_LOG", Path(tmp.name) / "runs.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self):
        s = AcademicTwinService()
        self.assertEqual(s.restore_office_state(self.dir), "")
        return s

    def test_drafts_jobs_and_release_survive_restart(self):
        s = self.service()
        s.load_data(roster=("roster.csv", "student_id,cohort\nDEMO-P1,114\nDEMO-P2,114\nDEMO-P3,114\n"))
        with two_models(fake_response()):
            for sid in ("DEMO-P1", "DEMO-P2"):
                s.dashboard_batch_add(f"{sid}.pdf", PDF)
            s._batch().wait()
        draft = s.dashboard_batch_draft("DEMO-P1")
        s.dashboard_confirm(draft["draft_id"], draft["rows"], "監控者", 114)
        active = s.release_start("監控者")["release"]["active"]
        uid = s.batch_uid()

        again = self.service()
        self.assertEqual(sorted(x for x in again.students if x.startswith("DEMO")), ["DEMO-P1"])
        self.assertEqual(again._roster_parsed.ids, ["DEMO-P1", "DEMO-P2", "DEMO-P3"])
        self.assertEqual(again.batch_uid(), uid)
        st = again.dashboard_batch_status()
        self.assertEqual(st["release"]["active"]["sampled"], active["sampled"])
        self.assertEqual({j["student_id"]: j["state"] for j in st["jobs"]},
                         {"DEMO-P1": db.CONFIRMED, "DEMO-P2": db.DONE})
        # 草稿接得上：原文文字也還在，確認時照常核對
        d2 = again.dashboard_batch_draft("DEMO-P2")
        sid, ext, _ = again._dashboard_drafts[d2["draft_id"]]
        self.assertTrue(ext.pages.ok)
        again.dashboard_confirm(d2["draft_id"], d2["rows"], "監控者", 114)
        self.assertIn("DEMO-P2", again.students)
        self.assertEqual(again.dashboard_batch_status()["release"]["active"]["status"], monitor.READY)

    def test_running_job_becomes_failed_and_cannot_retry_without_pdf(self):
        s = self.service()
        s.load_data(roster=("roster.csv", "student_id\nDEMO-P1\n"))
        batch = s._batch()
        batch.jobs["DEMO-P1"] = db.Job("DEMO-P1", "DEMO-P1.pdf", PDF, db.RUNNING)
        s._save_state()
        again = self.service()
        [j] = again.dashboard_batch_status()["jobs"]
        self.assertEqual((j["state"], j["error"]), (db.FAILED, db.RESTARTED))
        with self.assertRaisesRegex(ValueError, "重新上傳"):
            again.dashboard_batch_retry("DEMO-P1")

    def test_corrupt_state_is_skipped(self):
        self.dir.mkdir(parents=True)
        (self.dir / "state.json").write_text("{not json", encoding="utf-8")
        s = AcademicTwinService()
        self.assertIn("無法讀取", s.restore_office_state(self.dir))
        self.assertEqual(json.loads((self.dir / "state.json").read_text(encoding="utf-8"))["version"], 1)

    def test_state_that_cannot_be_applied_is_kept_aside(self):
        s = self.service()
        state = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        state["data"]["roster_csv"] = "no_id_column\nx\n"
        (self.dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        reason = AcademicTwinService().restore_office_state(self.dir)
        self.assertIn("state.bad.json", reason)
        self.assertTrue((self.dir / "state.bad.json").exists())

    def test_demo_batch_is_saved(self):
        s = self.service()
        s.load_demo_batch()
        again = self.service()
        self.assertEqual(again.students, {})
        self.assertEqual(again.records_name, "示範批次（待解析儀表板）")
        self.assertIn("DEMO-D03", again._roster_parsed.ids)


if __name__ == "__main__":
    unittest.main()
