"""系辦端 Demo 腳本（docs/DEMO_OFFICE.md）的預期數字（A 組）。

以評審模式（ACADEMIC_TWIN_REPLAY=only）播放 data/replay/ 的真實錄製結果，照腳本從頭跑一次：
儀表板分流、抽查放行、DEMO-D07 逐份核對、收件匣擬稿、採用覆核建議後重新擬稿、AI 草擬規則。
不呼叫模型。改了提示、模型名稱或示範資料使錄製失效，或使旁白數字改變，這裡會失敗，
提醒先重錄或更新腳本再錄影。只用虛構示範批次，不是實證數據。
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import dashboard, inbox, office_state, rule_draft, rule_versions
from academic_twin.api import AcademicTwinService

ROOT = Path(__file__).resolve().parent.parent
PDFS = sorted((ROOT / "data" / "samples" / "demo_batch").glob("DEMO-D0[3-8].pdf"))
REVIEWER = "系辦"


def wait_parsed(service, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = service.dashboard_batch_status()["jobs"]
        if jobs and all(j["state"] not in ("queued", "running") for j in jobs):
            return
        time.sleep(0.1)
    raise AssertionError("示範批次解析逾時")


class DemoOfficeScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for patcher in (mock.patch.dict(os.environ, {"ACADEMIC_TWIN_REPLAY": "only", "ACADEMIC_TWIN_RECORD": ""}),
                        mock.patch.object(office_state, "STATE_DIR", d / "office_state"),
                        mock.patch.object(dashboard, "EVAL_LOG", d / "dashboard_runs.jsonl"),
                        mock.patch.object(inbox, "EVAL_LOG", d / "inbox_runs.jsonl"),
                        mock.patch.object(rule_draft, "EVAL_LOG", d / "rule_draft_runs.jsonl"),
                        mock.patch.object(rule_draft, "DRAFT_DIR", d / "rule_drafts")):
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        kb, extra = rule_versions.startup_paths()
        s = cls.service = AcademicTwinService(kb, reviews_path=d / "decisions.json", extra_kb_paths=extra)
        cls.addClassCleanup(lambda: s.__dict__.get("_dashboard_batch") and s._dashboard_batch.close())

        # 0:10 載入示範批次、丟入 6 份 PDF
        s.load_demo_batch()
        for p in PDFS:
            s.dashboard_batch_add(p.name, p.read_bytes())
        wait_parsed(s)
        cls.parsed = s.dashboard_batch_status()

        # 0:50 批次確認：隨機抽查（不修改直接確認）→ 放行其餘
        started = s.release_start(REVIEWER)["release"]["active"]
        cls.release = started
        for sid in started["sampled"]:
            d = s.dashboard_batch_draft(sid)
            s.dashboard_confirm(d["draft_id"], d["rows"], REVIEWER, d.get("cohort"))
        cls.finished = s.release_finish(REVIEWER)

        # 1:05 例外那份：DEMO-D07 逐份核對後確認警示
        cls.d07 = s.dashboard_batch_draft("DEMO-D07")
        s.dashboard_confirm(cls.d07["draft_id"], cls.d07["rows"], REVIEWER, cls.d07.get("cohort"),
                            acknowledge_warnings=True)
        cls.after_confirm = s.dashboard_batch_status()

        # 1:20 收件匣：匯入示範信件 → AI 擬稿
        s.inbox_load_demo()
        s.inbox_process()
        s.inbox_wait()
        cls.inbox = {m["subject"]: m for m in s.inbox_list()["messages"]}

    def mail(self, subject):
        return {m["subject"]: m for m in self.service.inbox_list()["messages"]}[subject]

    def test_all_parsing_is_replayed(self):
        jobs = self.parsed["jobs"]
        self.assertEqual(len(jobs), 6)
        self.assertTrue(all(j["state"] == "done" and j["replay"] for j in jobs), "評審模式應全部重播成功")

    def test_triage(self):
        """6 份：5 份建議放行、1 份需核對（DEMO-D07），沒有失敗或需修正。"""
        buckets = {j["student_id"]: j["bucket"] for j in self.parsed["jobs"]}
        self.assertEqual(buckets, {"DEMO-D03": "release", "DEMO-D04": "release", "DEMO-D05": "release",
                                   "DEMO-D06": "release", "DEMO-D07": "review", "DEMO-D08": "release"})
        self.assertTrue(all(j["source_check"]["ok"] for j in self.parsed["jobs"]))

    def test_release_samples_one_and_releases_four(self):
        self.assertEqual((self.release["candidates"], len(self.release["sampled"])), (5, 1))
        last = self.finished["release"]["last"]
        self.assertEqual((last["outcome"], last["sampled_edited"], last["released"], last["failed"]),
                         ("released", 0, 4, []))
        self.assertEqual(self.after_confirm["buckets"]["confirmed"], 6)

    def test_d07_needs_review_because_cross_check_unavailable(self):
        """D07 不是兩模型不一致：主模型改用備援後與第二模型相同，無法交叉驗證。旁白須照實說。"""
        self.assertEqual(self.d07["cross_check"]["status"], "same_model")
        self.assertEqual([i["level"] for i in self.d07["issues"]], ["warning"])
        self.assertIn("未交叉驗證", self.d07["issues"][0]["message"])

    def test_audit_results(self):
        status = {sid: self.service.student(sid)["status"] for sid in
                  ("DEMO-D03", "DEMO-D04", "DEMO-D05", "DEMO-D06", "DEMO-D07", "DEMO-D08")}
        self.assertEqual(status, {"DEMO-D03": "MANUAL_REVIEW", "DEMO-D04": "PASS", "DEMO-D05": "PASS",
                                  "DEMO-D06": "FAIL", "DEMO-D07": "MANUAL_REVIEW", "DEMO-D08": "PASS"})

    def test_inbox_drafts(self):
        states = {s: m["state"] for s, m in self.inbox.items()}
        self.assertEqual(sum(v == "drafted" for v in states.values()), 7)
        self.assertEqual({s for s, v in states.items() if v == "manual"}, {"英檢其實有過", "專案申請", "離校手續"})
        self.assertTrue(all(m["grounded"] and not m["error"] for m in self.inbox.values()))
        ok = self.inbox["我可以畢業了嗎"]
        self.assertEqual((ok["type"], ok["batch_ok"], ok["proposals"]), ("progress", True, 0))
        self.assertEqual(self.inbox["英檢其實有過"]["type"], "correction")

    def test_z_proposal_then_redraft(self):
        """轉學抵免：帶覆核建議、不能批次核准 → 核准建議（重審）→ 重新擬稿（也已錄製）。
        D03 變為 FAIL、100 學分，與學生端腳本（docs/DEMO_STUDENT.md）銜接。名稱帶 z：最後執行。"""
        m = self.inbox["轉學抵免的課"]
        self.assertEqual((m["type"], m["proposals"], m["batch_ok"]), ("course", 1, False))
        self.service.inbox_apply_proposal(m["id"], 0, REVIEWER, True)
        self.assertTrue(self.mail("轉學抵免的課")["stale"])
        self.service.inbox_process(m["id"])
        self.service.inbox_wait()
        after = self.mail("轉學抵免的課")
        self.assertEqual((after["state"], after["stale"], after["batch_ok"], after["error"]),
                         ("drafted", False, True, ""))
        d03 = self.service.student("DEMO-D03")
        self.assertEqual((d03["status"], d03["total_credits"]), ("FAIL", 100))

    def test_rule_draft_is_replayed(self):
        pdf = (ROOT / "data" / "sources" / "im_114_table.pdf").read_bytes()
        draft = self.service.rule_draft_extract(pdf, 114)
        self.assertEqual(draft["replay"]["reason"], "評審模式")


if __name__ == "__main__":
    unittest.main()
