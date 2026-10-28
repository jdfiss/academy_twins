"""儀表板批次解析：一次上傳多份 PDF，在背景排隊交給 AI 擷取，結果仍逐份由承辦人確認。（A 組）

- 檔名須為「<學號>.pdf」，先對應屆名單；學號不在名單上的不解析，列入名單異常。
- 同時處理的份數由環境變數 DASHBOARD_WORKERS 指定，預設 DEFAULT_WORKERS。
- 失敗的由承辦人按重試；「停止全部」讓佇列中的不再開始，並暫停系辦端的 AI 呼叫，直到承辦人恢復。
  已送出的模型呼叫無法中斷，回來的結果照常保留為待確認草稿。
- 解析結果只是草稿（與單份解析相同），確認前不進入審查。狀態只在記憶體，伺服器重開後需重新上傳。
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

DEFAULT_WORKERS = 3
QUEUED, RUNNING, DONE, FAILED, STOPPED, CONFIRMED = "queued", "running", "done", "failed", "stopped", "confirmed"
PENDING = (QUEUED, RUNNING)


def workers() -> int:
    raw = os.environ.get("DASHBOARD_WORKERS", "").strip()
    if not raw:
        return DEFAULT_WORKERS
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"DASHBOARD_WORKERS 必須是正整數，目前為「{raw}」")
    return int(raw)


def student_id_from_filename(filename: str) -> str:
    """「<學號>.pdf」取出學號；副檔名不是 PDF 或學號空白時丟 ValueError。"""
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    stem, dot, ext = name.rpartition(".")
    if not dot or ext.lower() != "pdf" or not stem.strip():
        raise ValueError(f"「{name or '（無檔名）'}」不是「學號.pdf」格式，未加入佇列")
    return stem.strip()


@dataclass
class Job:
    student_id: str
    filename: str
    pdf: bytes | None
    state: str = QUEUED
    queued_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    error: str = ""
    seconds: float | None = None  # AI 擷取等待秒數
    draft: dict | None = None  # dashboard_extract 的回傳（待確認草稿）

    def summary(self) -> dict:
        d = self.draft or {}
        issues = d.get("issues", [])
        cc = d.get("cross_check") or {}
        return {"student_id": self.student_id, "filename": self.filename, "state": self.state,
                "queued_at": self.queued_at, "error": self.error, "seconds": self.seconds,
                "draft_id": d.get("draft_id"), "rows": len(d.get("rows", [])),
                "errors": sum(i["level"] == "error" for i in issues),
                "warnings": sum(i["level"] == "warning" for i in issues),
                "cross_checked": cc.get("status") == "ok",
                "flagged_rows": cc.get("flagged_rows", 0) + cc.get("missing_in_primary", 0)}


class DashboardBatch:
    """extract(pdf, student_id) 為單份解析（OfficeMixin.dashboard_extract），回傳待確認草稿。"""

    def __init__(self, extract: Callable[[bytes, str], dict], max_workers: int | None = None):
        self._extract = extract
        self.max_workers = max_workers or workers()
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="dashboard-batch")
        self._lock = threading.Lock()
        self._futures = []
        self.jobs: dict[str, Job] = {}
        self.unmatched: list[dict] = []  # 檔名學號不在應屆名單，未解析
        self.paused = False

    def add(self, filename: str, pdf: bytes, roster_ids: set[str]) -> dict:
        """加入佇列；學號不在名單上時不解析，記為名單異常並回傳 {"unmatched": ...}。"""
        sid = student_id_from_filename(filename)
        with self._lock:
            if sid not in roster_ids:
                entry = {"student_id": sid, "filename": filename}
                self.unmatched = [u for u in self.unmatched if u["student_id"] != sid] + [entry]
                return {"unmatched": entry}
            if self.paused:
                raise ValueError("AI 已停用，請先按「恢復 AI」再加入佇列")
            old = self.jobs.get(sid)
            if old and old.state in PENDING:
                raise ValueError(f"{sid} 已在佇列中，請等這一份處理完")
            job = self.jobs[sid] = Job(sid, filename, pdf)
            self.unmatched = [u for u in self.unmatched if u["student_id"] != sid]
            self._submit(job)
            return job.summary()

    def retry(self, sid: str | None = None) -> list[dict]:
        """重試失敗或已停止的一份（sid）或全部；AI 停用中不能重試。"""
        with self._lock:
            if self.paused:
                raise ValueError("AI 已停用，請先按「恢復 AI」再重試")
            if sid is not None and sid not in self.jobs:
                raise KeyError(f"佇列中沒有 {sid}")
            targets = [self.jobs[sid]] if sid is not None else list(self.jobs.values())
            for job in targets:
                if job.state in (FAILED, STOPPED) and job.pdf is not None:
                    job.state, job.error, job.seconds = QUEUED, "", None
                    self._submit(job)
                elif sid is not None:
                    raise ValueError(f"{sid} 目前是「{job.state}」，只有失敗或已停止的可以重試")
        return self.status()["jobs"]

    def stop(self) -> None:
        """佇列中的不再開始，並暫停 AI；執行中的模型呼叫無法中斷，回來的結果仍保留。"""
        with self._lock:
            self.paused = True
            for job in self.jobs.values():
                if job.state == QUEUED:
                    job.state, job.error = STOPPED, "已由承辦人停止"

    def resume(self) -> None:
        with self._lock:
            self.paused = False

    def mark_confirmed(self, draft_id: str) -> None:
        with self._lock:
            for job in self.jobs.values():
                if job.draft and job.draft.get("draft_id") == draft_id:
                    job.state = CONFIRMED

    def draft(self, sid: str) -> dict:
        with self._lock:
            job = self.jobs.get(sid)
            if not job or job.state != DONE:
                raise KeyError(f"{sid} 沒有待確認的解析結果")
            return job.draft

    def status(self) -> dict:
        with self._lock:
            jobs = [j.summary() for j in self.jobs.values()]
            counts = {s: sum(j["state"] == s for j in jobs) for s in (QUEUED, RUNNING, DONE, FAILED, STOPPED, CONFIRMED)}
            return {"paused": self.paused, "workers": self.max_workers, "counts": counts, "jobs": jobs,
                    "unmatched": list(self.unmatched)}

    def wait(self) -> None:
        """等目前送出的工作都結束（測試與命令列用）。"""
        while True:
            with self._lock:
                pending = [f for f in self._futures if not f.done()]
                self._futures = pending
            if not pending:
                return
            for f in pending:
                f.result()

    def _submit(self, job: Job) -> None:
        self._futures.append(self._pool.submit(self._run, job))

    def _run(self, job: Job) -> None:
        with self._lock:
            if job.state != QUEUED or self.paused:
                return
            job.state = RUNNING
        started = time.monotonic()
        try:
            draft, error = self._extract(job.pdf, job.student_id), ""
        except Exception as e:  # 單份失敗只記在該份，不影響其他份與既有資料
            draft, error = None, str(e.args[0] if e.args else e)[:300] or type(e).__name__
        with self._lock:
            job.seconds = round(time.monotonic() - started, 1)
            if draft is None:
                job.state, job.error = FAILED, error
            else:
                job.state, job.draft, job.pdf = DONE, draft, None  # 成功後不再保留 PDF
