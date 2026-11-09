"""監控者模式：解析結果分流（只看例外）與批次確認＋隨機抽查。（A 組）

承辦人從「逐份核對」改為「監控者」：
- 每份解析結果依檢查結果自動分桶，人只處理失敗、需修正、需核對三桶。
- 「建議放行」（兩模型一致、原文核對與合計都沒有問題、無錯誤無警示、沒有經過自我修正）的份，由監控者
  按一次批次確認；確認前伺服器隨機抽出至少 1 份（約 10%）由監控者逐列核對，人不能挑選樣本。
- 抽查份有任何修改，本批取消，其餘全部改為逐份核對，批次放行停用到監控者手動重新開啟。
- 按下確認的一定是有名字的人；沒有無人放行（見 docs/TEAM_PLAN.md〈AI 輸入與啟用驗收〉）。

抽查是放行前的閘門，不是準確率量測：樣本太小，準確率以虛構基準集（benchmark.py）另行量測。
"""
from __future__ import annotations

import math
import secrets
import uuid
from datetime import datetime

from . import dashboard, dashboard_batch as db
from .records import RecordsError

SAMPLE_RATE = 0.1
PENDING, FAILED, FIX, REVIEW, SAMPLING, HELD, RELEASE, CONFIRMED = (
    "pending", "failed", "fix", "review", "sampling", "held", "release", "confirmed")
BUCKETS = {PENDING: "處理中", FAILED: "失敗", FIX: "需修正", REVIEW: "需核對", SAMPLING: "抽查中",
           HELD: "等待抽查結果", RELEASE: "建議放行", CONFIRMED: "已確認"}
# 抽查結果 → 本批狀態
SAMPLING_STATE, READY, CANCELLED, DONE = "sampling", "ready", "cancelled", "done"


def triage(job: dict) -> str:
    """依 DashboardBatch 的工作摘要分桶（純函式）。"""
    state = job["state"]
    if state in db.PENDING:
        return PENDING
    if state in (db.FAILED, db.STOPPED):
        return FAILED
    if state == db.CONFIRMED:
        return CONFIRMED
    if job["errors"]:
        return FIX
    # 曾自我修正代表 AI 第一次讀錯過，保守起見仍請人核對
    if job["warnings"] or not job["cross_checked"] or job["flagged_rows"] or job["corrected"]:
        return REVIEW
    return RELEASE


def sample_size(n: int) -> int:
    return min(n, max(1, math.ceil(n * SAMPLE_RATE))) if n else 0


def edited(counts: dict) -> bool:
    """確認時的列與 AI 擷取不同（修改欄位、新增或刪除列）即算抽查發現錯誤。"""
    return bool(counts["fields_changed"] or counts["rows_added"] or counts["rows_removed"])


class MonitorMixin:
    """需搭配 OfficeMixin（self._batch()、self.dashboard_confirm、self._save_state、self.batch_uid）。"""

    # ---------- 狀態 ----------
    def _monitor(self) -> dict:
        return self.__dict__.setdefault("_monitor_state", {"release": None, "enabled": True, "note": "",
                                                           "demoted": {}, "last": None})

    def _monitor_reset(self) -> None:
        self.__dict__["_monitor_state"] = {"release": None, "enabled": True, "note": "", "demoted": {}, "last": None}

    def bucket(self, job: dict) -> str:
        m = self._monitor()
        if job["state"] != db.DONE:
            return triage(job)
        r = m["release"]
        sid = job["student_id"]
        if r and r["status"] in (SAMPLING_STATE, READY):
            if sid in r["sampled"]:
                return SAMPLING
            if sid in r["held"]:
                return HELD
        return m["demoted"].get(sid) or triage(job)

    def monitor_view(self, status: dict) -> dict:
        """批次狀態加上分桶與批次放行資訊（畫面用）。"""
        for j in status["jobs"]:
            j["bucket"] = self.bucket(j)
        counts = {b: sum(j["bucket"] == b for j in status["jobs"]) for b in BUCKETS}
        m = self._monitor()
        return {**status, "buckets": counts, "bucket_labels": BUCKETS,
                "release": {"enabled": m["enabled"], "note": m["note"], "active": m["release"],
                            "last": m["last"], "sample_rate": SAMPLE_RATE}}

    # ---------- 批次確認＋抽查 ----------
    def release_start(self, reviewer: str) -> dict:
        if not reviewer.strip():
            raise ValueError("請填寫監控者姓名")
        m = self._monitor()
        if not m["enabled"]:
            raise ValueError(f"批次放行已停用：{m['note']}；請先重新開啟")
        if m["release"] and m["release"]["status"] in (SAMPLING_STATE, READY):
            raise ValueError("已有一批正在抽查，請先完成或取消")
        jobs = self._batch().status()["jobs"]
        candidates = sorted(j["student_id"] for j in jobs if self.bucket(j) == RELEASE)
        if not candidates:
            raise ValueError("目前沒有建議放行的份")
        sampled = sorted(secrets.SystemRandom().sample(candidates, sample_size(len(candidates))))
        m["release"] = {"id": uuid.uuid4().hex[:12], "by": reviewer.strip(),
                        "started_at": datetime.now().isoformat(timespec="seconds"), "candidates": len(candidates),
                        "sampled": sampled, "held": [s for s in candidates if s not in sampled],
                        "results": {}, "status": SAMPLING_STATE}
        self._save_state()
        return self.dashboard_batch_status()

    def _after_confirm(self, sid: str, counts: dict) -> str:
        """確認成功後呼叫；回傳確認方式（single／sampled），抽查份有修改時取消本批。"""
        m = self._monitor()
        m["demoted"].pop(sid, None)
        r = m["release"]
        if not r or r["status"] != SAMPLING_STATE or sid not in r["sampled"]:
            return "single"
        r["results"][sid] = {"edited": edited(counts)}
        if r["results"][sid]["edited"]:
            r["status"] = CANCELLED
            m["enabled"] = False
            m["note"] = f"抽查 {sid} 發現需要修改，本批 {r['candidates']} 份改為逐份核對"
            for other in r["held"] + [s for s in r["sampled"] if s not in r["results"]]:
                m["demoted"][other] = REVIEW
            self._log_release(r, "cancelled_by_sample")
            m["last"], m["release"] = _summary(r, "cancelled_by_sample"), None
        elif len(r["results"]) == len(r["sampled"]):
            r["status"] = READY
        return "sampled"

    def release_finish(self, reviewer: str) -> dict:
        """抽查全數無修改後，其餘份逐份走確認（伺服器重新檢查；個別未通過的退回需修正）。"""
        if not reviewer.strip():
            raise ValueError("請填寫監控者姓名")
        m = self._monitor()
        r = m["release"]
        if not r or r["status"] != READY:
            raise ValueError("抽查尚未全部完成，不能放行其餘份")
        batch = self._batch()
        released, failed = [], []
        for sid in r["held"]:
            job = batch.jobs.get(sid)
            if not job or job.state != db.DONE or not job.draft:
                continue
            d = job.draft
            try:
                self.dashboard_confirm(d["draft_id"], d["rows"], reviewer, d.get("cohort"), mode="batch")
                released.append(sid)
            except (RecordsError, KeyError, ValueError):
                m["demoted"][sid] = FIX
                failed.append(sid)
        r.update(status=DONE, released=released, failed=failed, finished_by=reviewer.strip())
        self._log_release(r, "released")
        m["last"], m["release"] = _summary(r, "released"), None
        self._save_state()
        return self.dashboard_batch_status()

    def release_cancel(self, reviewer: str) -> dict:
        """監控者自行取消本批（不是抽查發現錯誤）：其餘份回到建議放行，批次放行仍可用。"""
        if not reviewer.strip():
            raise ValueError("請填寫監控者姓名")
        m = self._monitor()
        r = m["release"]
        if not r or r["status"] not in (SAMPLING_STATE, READY):
            raise ValueError("目前沒有進行中的批次放行")
        self._log_release(r, "cancelled_by_monitor")
        m["last"], m["release"] = _summary(r, "cancelled_by_monitor"), None
        self._save_state()
        return self.dashboard_batch_status()

    def release_reopen(self, reviewer: str) -> dict:
        if not reviewer.strip():
            raise ValueError("請填寫監控者姓名")
        m = self._monitor()
        m["enabled"], m["note"] = True, ""
        self._save_state()
        return self.dashboard_batch_status()

    def _log_release(self, r: dict, outcome: str) -> None:
        """抽查人數與結果記入執行紀錄（驗證證據）；只有計數，不記學號。"""
        dashboard.log_run({
            "type": "sample", "at": datetime.now().isoformat(timespec="seconds"), "batch_id": self.batch_uid(),
            "release_id": r["id"], "outcome": outcome, "candidates": r["candidates"], "sampled": len(r["sampled"]),
            "sampled_checked": len(r["results"]), "sampled_edited": sum(x["edited"] for x in r["results"].values()),
            "released": len(r.get("released", [])), "release_failed": len(r.get("failed", [])),
            "sample_rate": SAMPLE_RATE,
        })


def _summary(r: dict, outcome: str) -> dict:
    return {"outcome": outcome, "candidates": r["candidates"], "sampled": len(r["sampled"]),
            "sampled_edited": sum(x["edited"] for x in r["results"].values()),
            "released": len(r.get("released", [])), "failed": list(r.get("failed", [])),
            "at": datetime.now().isoformat(timespec="seconds")}
