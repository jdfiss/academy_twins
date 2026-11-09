"""系辦端 API：批次審查、名單異常、匯出、人工覆核、審查明細、監控者模式與工作狀態存檔。（A 組）"""
from __future__ import annotations

import csv
import io
import threading
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from . import (cross_check, dashboard, dashboard_batch, llm, office_state, pdf_text, replay, report, rule_diff,
               rule_draft, rule_versions)
from .inbox import InboxMixin
from .metrics import compute as compute_metrics
from .monitor import MonitorMixin
from .records import RecordsError, parse_records
from .roster import parse_roster
from .rule_engine import CATEGORY, category_overflow, classify, reasons

_BATCH_LOCK = threading.Lock()
DEMO_ROSTER = Path(__file__).parent.parent / "data" / "students" / "demo_batch_roster.csv"


class OfficeMixin(MonitorMixin, InboxMixin):
    """需搭配 api.CoreService 使用（self.ruleset()、self.students、self._report…）。"""

    _state_dir: Path | None = None  # 設定後（restore_office_state）每次狀態改變就存檔；測試預設不存檔

    # ---------- 工作狀態存檔（伺服器重開後接續） ----------
    def load_data(self, records: tuple[str, str] | None = None, roster: tuple[str, str] | None = None) -> None:
        super().load_data(records, roster)
        self._save_state()

    def batch_uid(self) -> str:
        """目前這一批的代號（換示範批次時更新）；執行軌跡據以區分「本批次」與累計指標。"""
        return self.__dict__.setdefault("_batch_uid", uuid.uuid4().hex[:12])

    def restore_office_state(self, directory: Path = office_state.STATE_DIR) -> str:
        """啟用存檔並接續上次的狀態；回傳略過的原因（空字串表示正常）。失敗時以目前資料啟動。"""
        self._state_dir = None  # 接續期間不存檔
        state, reason = office_state.load(directory)
        if state:
            try:
                self._restore(state)
            except Exception as e:  # 規則庫或格式改變導致接不上：保留檔案供檢查，以目前資料啟動
                (directory / "state.json").replace(directory / "state.bad.json")
                reason = f"教務處端存檔無法接續，已改名為 state.bad.json：{e}"
        self._state_dir = directory
        self._save_state()
        return reason

    def _restore(self, state: dict) -> None:
        data = state["data"]
        if data["students"]:
            self.load_data((data["records_name"], data["records_csv"]), (data["roster_name"], data["roster_csv"]))
        else:
            self.students, self.records_name = {}, data["records_name"]
            self.load_data(roster=(data["roster_name"], data["roster_csv"]))
        self.__dict__["_batch_uid"] = state["batch_uid"]
        self.__dict__["_dashboard_drafts"] = {
            draft_id: (d["sid"], office_state.extraction_from_dict(d["ext"]),
                       d["other"] if isinstance(d["other"], str) else office_state.extraction_from_dict(d["other"]))
            for draft_id, d in state["drafts"].items()}
        self._batch().restore(state["batch"])
        self.__dict__["_monitor_state"] = state["monitor"]
        self._inbox_restore(state.get("inbox"))

    def _save_state(self) -> None:
        if not self._state_dir:
            return
        with _BATCH_LOCK:
            batch = self.__dict__.get("_dashboard_batch")
        drafts = list(self.__dict__.get("_dashboard_drafts", {}).items())
        office_state.save({
            "data": {"records_name": self.records_name, "students": len(self.students),
                     "records_csv": office_state.records_csv(self.students),
                     "roster_name": self.roster_name, "roster_csv": office_state.roster_csv(self._roster_parsed)},
            "batch_uid": self.batch_uid(),
            "batch": batch.to_state() if batch else {"jobs": [], "unmatched": [], "paused": False},
            "drafts": {draft_id: {"sid": sid, "ext": office_state.extraction_to_dict(ext),
                                  "other": other if isinstance(other, str) else office_state.extraction_to_dict(other)}
                       for draft_id, (sid, ext, other) in drafts},
            "monitor": self._monitor(),
            **self._inbox_state(),
        }, self._state_dir)

    def office_metrics(self) -> dict:
        """監控台指標：本批次與累計（只有彙總數字），加上本月 AI token 用量與預算。"""
        try:
            budget = replay.budget_status()
        except llm.LLMError as e:  # 預算設定錯誤時仍顯示其他指標
            budget = {"error": str(e)}
        return {**compute_metrics(self.batch_uid()), "ai_budget": budget}

    def data_source(self) -> dict:
        return {
            "records": self.records_name,
            "records_count": sum(len(v) for v in self.students.values()),
            "students": len(self.students),
            "roster": self.roster_name,
            "roster_count": self.roster.roster_size,
            "replay_mode": _replay_mode(),  # only＝評審模式（只重播預錄的虛構文件結果）
        }

    def load_demo_batch(self) -> dict:
        """比賽示範起點：應屆名單換成 DEMO-D03–D08（虛構），修課紀錄清空、待儀表板解析確認後才進入審查；
        同時清掉上一輪的解析佇列與草稿。示例 S001–S005 不會出現在名單異常。"""
        text = DEMO_ROSTER.read_text(encoding="utf-8-sig")
        parse_roster(text, DEMO_ROSTER.name)  # 先檢查，失敗時現有資料完全不變
        self.students, self.records_name = {}, "示範批次（待解析儀表板）"
        with _BATCH_LOCK:
            old = self.__dict__.pop("_dashboard_batch", None)
            self._dashboard_drafts = {}
            self._batch_uid = uuid.uuid4().hex[:12]
            self._monitor_reset()
            self.inbox_reset()  # 示範信件的寄件人是新批次的學生
            if old:  # 承辦人停用 AI 的狀態保留，不因換批次而恢復
                old.close()
                self._dashboard_batch = dashboard_batch.DashboardBatch(self.dashboard_extract, paused=old.paused,
                                                                       on_change=self._save_state)
        self.load_data(roster=(DEMO_ROSTER.name, text))  # 最後才存檔，存到的是新批次
        return self.data_source()

    # ---------- 畢審 ----------
    def batch(self) -> list[dict]:
        rows = []
        for sid in self.roster.to_audit:
            report = self._report(sid)
            t = report.transcript
            rs = self.ruleset(sid)
            rows.append({
                "id": sid,
                "cohort": rs.kb.cohort,
                "rule_version": rs.rule_version,
                "status": report.status.value,
                "total_credits": report.total_credits,
                "total_required": next((r.need for r in report.results
                                        if r.requirement.kind == "total_credits"), None),
                "satisfied": sum(r.satisfied for r in report.results),
                "requirements": len(report.results),
                "reasons": reasons(report),
                "reviewed": len(t.reviewed),
                "overflow": [{"category": cat, "excess": excess, "movable": movable}
                             for cat, excess, movable in category_overflow(rs.kb, t)],
                "flags": [{"type": f.type, "message": f.message, "credits": f.credits} for f in report.flags],
                "action_count": len(report.actions),
                "actions": [{"title": a.title, "office": a.office} for a in report.actions],
            })
        return rows

    def roster_anomalies(self) -> list[dict]:
        """名單比對異常；與 batch() 合起來涵蓋名單上每位學生。"""
        r = self.roster
        parsed = {j["student_id"] for j in self._batch().status()["jobs"] if j["state"] == dashboard_batch.DONE}
        return (
            [{"id": sid, "kind": "dashboard_pending", "message": "儀表板已解析，待確認後才審查"}
             for sid in r.no_records if sid in parsed]
            + [{"id": sid, "kind": "no_records", "message": "在應屆名單上，但沒有任何修課紀錄，未審查"}
               for sid in r.no_records if sid not in parsed]
            + [{"id": sid, "kind": "not_on_roster", "message": "有修課紀錄，但不在應屆名單，未審查"} for sid in r.not_on_roster]
            + [{"id": sid, "kind": "other_cohort",
                "message": f"名單入學年度為 {c}，系統沒有該學年度規則庫"
                           f"（現有 {'、'.join(map(str, sorted(self.rulesets)))}），未審查，不套用其他年度"}
               for sid, c in r.other_cohort.items()]
            + [{"id": sid, "kind": "duplicate", "message": "應屆名單重複列出"} for sid in r.duplicates]
            + [{"id": None, "kind": "blank", "message": f"名單第 {line} 行學號空白"} for line in r.blank_rows]
            + [{"id": u["student_id"], "kind": "dashboard_not_on_roster",
                "message": f"上傳了儀表板 {u['filename']}，但學號不在應屆名單，未解析"}
               for u in self._batch().unmatched]
        )

    EXPORT_FIELDS = ["student_id", "status", "total_credits", "satisfied", "requirements", "reasons",
                     "reviewed", "overflow", "rule_version", "audited_at", "flag_types", "flag_messages", "actions"]

    def export_csv(self, audited_at: datetime | None = None) -> str:
        """批次審查結果 + 名單異常匯出成 CSV；每列附規則版本與審查時間。"""
        stamp = (audited_at or datetime.now()).isoformat(timespec="seconds")
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=self.EXPORT_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in self.batch():
            writer.writerow({
                "student_id": row["id"], "status": row["status"], "total_credits": row["total_credits"],
                "satisfied": row["satisfied"], "requirements": row["requirements"],
                "reasons": "；".join(row["reasons"]) or "全部符合", "reviewed": row["reviewed"],
                "overflow": "；".join(_overflow_text(o) for o in row["overflow"]),
                "rule_version": row["rule_version"], "audited_at": stamp,
                "flag_types": "；".join(dict.fromkeys(f["type"] for f in row["flags"])),
                "flag_messages": "；".join(f["message"] for f in row["flags"]),
                "actions": "；".join(f"{a['title']}（{a['office']}）" for a in row["actions"]),
            })
        for a in self.roster_anomalies():
            writer.writerow({
                "student_id": a["id"] or "", "status": f"ROSTER_{a['kind'].upper()}", "reasons": a["message"],
                "rule_version": self.rule_version, "audited_at": stamp,
            })
        return out.getvalue()

    # ---------- 人工覆核 ----------
    def add_review(self, sid: str, code: str, term: str, action: str, reviewer: str,
                   category: str = "", note: str = "") -> dict:
        """記下覆核決定並回傳重新審查後的學生資料。只接受引擎無法判定的紀錄。"""
        if sid not in self.students:
            raise KeyError(f"找不到學生 {sid}")
        rs = self.ruleset(sid)
        raw = classify(rs.kb, self.students[sid])
        if (code, term) not in {(r.code, r.term) for r in raw.review + raw.dept_unverified}:
            raise ValueError(f"{code}（{term}）不是待覆核的紀錄；規則庫能判定的紀錄不能人工改判")
        if action == CATEGORY and category not in rs.kb.categories:
            raise ValueError(f"類別「{category}」不在規則庫")
        self.reviews.add(sid, code, term, action, reviewer, rs.rule_version, category, note)
        return self.student(sid)

    # ---------- AI 解析儀表板（結果待承辦人確認，確認前不進入審查） ----------
    def dashboard_extract(self, pdf: bytes, student_id: str) -> dict:
        """AI 擷取儀表板為草稿；草稿暫存於記憶體，只有確認後才會寫入修課紀錄。

        兩個模型同時擷取並逐列比對，不一致處列為警示；第二模型失敗時照常使用主模型並標示未交叉驗證。
        主模型結果有錯誤等級的檢查問題時，把檢查理由回饋給主模型重新擷取 1 次（有界自我修正）；
        仍有錯誤就交給承辦人逐列修正。每次執行留下執行軌跡（只記計數與代號，不記學號與課程內容）。
        """
        sid = _student_id(student_id)
        self._require_ai()
        began = time.monotonic()
        trace = {"type": "extract_trace", "at": datetime.now().isoformat(timespec="seconds"),
                 "batch_id": self.batch_uid(),
                 "prompt_version": dashboard.PROMPT_VERSION, "format": dashboard.FORMAT, "model": None,
                 "steps": [], "corrected": False, "to_manual": False}

        def finish(status: str) -> dict:
            trace.update(status=status, wait_seconds=round(time.monotonic() - began, 2))
            dashboard.log_run(trace)
            return trace

        try:
            ext, other = cross_check.extract_pair(pdf)
        except llm.LLMError:
            trace["steps"].append({"step": "extract", "ok": False, "error": "llm_unavailable"})
            trace["to_manual"] = True
            finish("failed")
            raise
        trace["model"] = ext.model
        trace["replay"] = ext.replay  # 評審模式重播：原本的模型與產生時間
        trace["steps"].append({"step": "extract", "ok": True, "model": ext.model, "rows": len(ext.rows),
                               "fallback": bool(ext.fallback_from), "replay": bool(ext.replay)})
        trace["steps"].append({"step": "cross_check", "ok": not isinstance(other, str),
                               "model": other.model if not isinstance(other, str) else cross_check.check_model()})
        ext.pages = pdf_text.extract_pages(pdf)  # 不送給 AI；check 據以核對每列原文與頁碼
        ext = self._self_correct(pdf, ext, trace)
        if ext.pages is not None:  # 只記警示數，畫面據以畫出「原文核對」這一關（不列入 steps，steps 只記 AI 呼叫與檢查）
            src = dashboard.check_sources(ext.rows, ext.pages)
            trace["source_check"] = {"ok": not src, "warnings": len(src), "available": ext.pages.ok}
        cmp = cross_check.recheck(ext.rows, other, ext.model)
        drafts = self.__dict__.setdefault("_dashboard_drafts", {})
        draft_id = uuid.uuid4().hex
        drafts[draft_id] = (sid, ext, other)
        issues = self._dashboard_issues(ext, ext.cohort) + cross_check.issues(cmp)
        # 轉人工：最後仍有任何錯誤（含入學年度沒有規則庫這類不回饋給模型的錯誤），確認會被擋下
        trace["to_manual"] = any(i.level == "error" for i in issues)
        trace["warnings"] = sum(i.level == "warning" for i in issues)
        finish("draft")
        self._save_state()
        return {"draft_id": draft_id, "student_id": sid, "exists": sid in self.students,
                **self._draft_view(ext, issues), "cross_check": _cross_check_view(cmp, len(ext.rows)),
                "trace": trace}

    def _self_correct(self, pdf: bytes, ext: dashboard.Extraction, trace: dict) -> dashboard.Extraction:
        """有界自我修正：有錯誤時只把檢查理由回饋給主模型重新擷取 1 次，走同一道檢查。

        修正結果要同時符合兩點才採用，否則保留第一次：錯誤變少，而且第一次沒有錯誤的列全部還在
        （不能靠刪掉有問題的列讓錯誤變少，違反「不能靜默略過錯誤列」）。步驟寫入 trace。
        """
        errors = self._retryable_errors(ext)
        trace["steps"].append({"step": "check", "ok": not errors, "errors": len(errors)})
        if not errors:
            return ext
        try:
            self._require_ai()  # 「停止全部」後不再送出新的模型呼叫
            retry = dashboard.extract(pdf, feedback=[_issue_text(i) for i in errors])
        except dashboard_batch.AIPaused:
            trace["steps"].append({"step": "retry", "ok": False, "error": "ai_paused",
                                   "prompt_version": dashboard.RETRY_VERSION})
            return ext
        except llm.LLMError:
            trace["steps"].append({"step": "retry", "ok": False, "error": "llm_unavailable",
                                   "prompt_version": dashboard.RETRY_VERSION})
            return ext
        retry.pages = ext.pages
        again = self._retryable_errors(retry)
        bad_rows = {i.row for i in errors if i.row is not None}
        kept_rows = {_row_key(r) for i, r in enumerate(ext.rows) if i not in bad_rows}
        dropped = len(kept_rows - {_row_key(r) for r in retry.rows})
        adopt = len(again) < len(errors) and not dropped
        trace["steps"].append({"step": "retry", "ok": True, "model": retry.model, "rows": len(retry.rows),
                               "prompt_version": dashboard.RETRY_VERSION})
        trace["steps"].append({"step": "check", "ok": not again, "errors": len(again), "dropped_rows": dropped,
                               "kept": "retry" if adopt else "first"})
        if not adopt:
            return ext
        trace["corrected"] = not again
        trace["model"], trace["replay"] = retry.model, retry.replay  # 草稿改用修正後的擷取
        return retry

    def _retryable_errors(self, ext: dashboard.Extraction) -> list[dashboard.Issue]:
        """可回饋給模型的錯誤：擷取內容本身的問題。入學年度沒有規則庫不回饋，免得模型改寫年度去配合。
        原文核對只產生警示，這裡略過（pages=None），不重複逐列定位。"""
        kb = self.rulesets[ext.cohort].kb if ext.cohort in self.rulesets else self.kb
        return [i for i in dashboard.check(replace(ext, pages=None), kb) if i.level == "error"]

    def dashboard_confirm(self, draft_id: str, rows: list[dict], reviewer: str, cohort: int | None = None,
                          acknowledge_warnings: bool = False, mode: str | None = None) -> dict:
        """承辦人修正並確認後，重新檢查，再以該生的紀錄取代或新增，並記錄修正量（不含課程內容）。

        mode：single 逐份、sampled 批次放行的抽查份（自動判斷）、batch 抽查通過後放行的其餘份。
        """
        drafts = self.__dict__.get("_dashboard_drafts", {})
        if draft_id not in drafts:
            raise KeyError("找不到這份解析草稿，可能已確認或伺服器已重開，請重新解析")
        if not reviewer.strip():
            raise ValueError("請填寫確認人")
        sid, ext, other = drafts[draft_id]
        fields = dashboard.FIELDS + ["rule_code", "source_page", "source_text"]
        edited = replace(ext, rows=[{k: r.get(k, "") for k in fields} for r in rows])
        cohort = cohort or self._roster_parsed.cohorts.get(sid)
        cmp = cross_check.recheck(edited.rows, other, ext.model)
        issues = self._dashboard_issues(edited, cohort) + cross_check.issues(cmp)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise RecordsError([_issue_text(i) for i in errors])
        if any(i.level == "warning" for i in issues) and not acknowledge_warnings:
            raise RecordsError(["仍有警示未確認；請逐項對照原文後勾選「已確認警示」"]
                               + [_issue_text(i) for i in issues if i.level == "warning"])
        records = parse_records(dashboard.to_records_csv(sid, edited.rows))[sid]
        self._replace_student(sid, records, cohort)
        del drafts[draft_id]
        counts = _edit_counts(ext.rows, edited.rows)
        mode = mode or self._after_confirm(sid, counts)
        self._batch().mark_confirmed(draft_id)  # 會觸發存檔
        initial = cross_check.recheck(ext.rows, other, ext.model)
        dashboard.log_run({
            "type": "confirm", "mode": mode, "batch_id": self.batch_uid(), "model": ext.model, "prompt_version": ext.prompt_version, "format": ext.format,
            "generated_at": ext.generated_at, "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            **counts, "warnings_acknowledged": acknowledge_warnings,
            "cross_check": {**initial.summary(len(ext.rows)), **_edits_by_flag(ext.rows, edited.rows, initial),
                            "flagged_after_edit": len(cmp.flagged_rows) + len(cmp.only_check)},
        })
        return self.student(sid)

    # ---------- 儀表板批次解析（背景排隊；每份結果仍走上方的確認流程） ----------
    def _batch(self) -> dashboard_batch.DashboardBatch:
        with _BATCH_LOCK:  # 網頁伺服器多執行緒，避免同時建出兩個佇列
            if "_dashboard_batch" not in self.__dict__:
                self._dashboard_batch = dashboard_batch.DashboardBatch(self.dashboard_extract,
                                                                       on_change=self._save_state)
            return self._dashboard_batch

    def _require_ai(self) -> None:
        with _BATCH_LOCK:
            batch = self.__dict__.get("_dashboard_batch")
        if batch is not None and batch.paused:
            raise dashboard_batch.AIPaused("教務處端 AI 已由承辦人停用，請按「恢復 AI」後再試")

    def dashboard_batch_add(self, filename: str, pdf: bytes) -> dict:
        """檔名「<學號>.pdf」；學號須在應屆名單上才排入解析。"""
        _student_id(dashboard_batch.student_id_from_filename(filename))
        return self._batch().add(filename, pdf, set(self._roster_parsed.ids))

    def dashboard_batch_status(self) -> dict:
        return self.monitor_view(self._batch().status())

    def dashboard_batch_draft(self, sid: str) -> dict:
        return self._batch().draft(sid)

    def dashboard_batch_retry(self, sid: str | None = None) -> dict:
        self._batch().retry(sid)
        return self.dashboard_batch_status()

    def dashboard_batch_stop(self) -> dict:
        """停止全部，兼作系辦端 AI 停用開關（批次、單份解析與規則草擬都暫停）。"""
        self._batch().stop()
        return self.dashboard_batch_status()

    def dashboard_batch_resume(self) -> dict:
        self._batch().resume()
        return self.dashboard_batch_status()

    def _dashboard_issues(self, ext: dashboard.Extraction, cohort: int | None) -> list[dashboard.Issue]:
        """依入學年度的規則庫檢查；沒有該年度規則庫時為錯誤，不能送入審查，也不套用其他年度。"""
        year = cohort or self.kb.cohort
        if year not in self.rulesets:
            return [dashboard.Issue("error", f"入學學年度 {year} 沒有規則庫（現有 "
                                             f"{'、'.join(map(str, sorted(self.rulesets)))}），不能送入審查；"
                                             "請核對文件年度，系統不套用其他年度")] + dashboard.check(ext, self.kb)
        return dashboard.check(ext, self.rulesets[year].kb)

    def _draft_view(self, ext: dashboard.Extraction, issues: list[dashboard.Issue]) -> dict:
        return {"rows": ext.rows, "issues": [asdict(i) for i in issues], "cohort": ext.cohort,
                "declared_required": ext.declared_required, "declared_earned": ext.declared_earned,
                "model": ext.model, "generated_at": ext.generated_at, "prompt_version": ext.prompt_version,
                "format": ext.format, "fallback_from": ext.fallback_from, "replay": ext.replay,
                "cohorts": sorted(self.rulesets),
                "categories": sorted(set(dashboard.FORMATS[ext.format].values()) - {""})}

    def _replace_student(self, sid: str, records: list, cohort: int | None) -> None:
        """以確認後的紀錄取代該生既有紀錄並加入名單；經由 load_data 重新檢查，失敗時資料不變。"""
        roster = self._roster_parsed
        cohorts = {**roster.cohorts, **({sid: cohort} if cohort else {})}
        ids = roster.ids + ([] if sid in roster.ids else [sid])
        source = f"{self.records_name}＋AI 解析 {sid}" if self.records_name else f"AI 解析 {sid}"
        self.load_data((source, office_state.records_csv({**self.students, sid: records})),
                       (self.roster_name or "應屆名單", office_state.roster_csv(roster, ids, cohorts)))

    # ---------- AI 草擬規則（獨立存檔，確認也不啟用） ----------
    def rule_draft_extract(self, pdf: bytes, year: int = 114) -> dict:
        """公開應修科目表產生少量草稿；不接觸 self.kb 或審查資料。"""
        self._require_ai()
        return rule_draft.run_draft(pdf, year)

    def rule_draft_check(self, draft_id: str, requirements: list[dict]) -> dict:
        return rule_draft.review_draft(draft_id, requirements)

    def rule_draft_confirm(self, draft_id: str, requirements: list[dict], reviewer: str,
                           acknowledge: bool = False) -> dict:
        """重新檢查並保存確認版本；後續由年度規則流程另外審核啟用。"""
        return rule_draft.confirm(draft_id, requirements, reviewer, acknowledge)

    # ---------- 規則版本（已確認草稿經承辦人明確啟用；可還原） ----------
    def rule_versions_status(self) -> dict:
        latest = {e["year"]: e for e in rule_versions.history()}
        return {
            "years": [{"year": year, "rule_version": rs.rule_version,
                       "activated": latest[year] if latest.get(year, {}).get("type") == "activate" else None}
                      for year, rs in sorted(self.rulesets.items())],
            "history": list(reversed(rule_versions.history())),
            "confirmed_drafts": [{**d, "preview": self._rule_preview(d)} for d in rule_versions.confirmed_drafts()],
        }

    def _rule_preview(self, draft: dict) -> dict:
        """啟用前影響預覽：該年度目前使用的規則 vs 套用草稿後的版本，以虛構案例由引擎各審查一次。

        只預覽、不啟用；同一份草稿對同一規則版本只搜尋一次。失敗時回傳錯誤訊息，不影響啟用流程。
        """
        if draft["year"] not in self.rulesets:
            return {"error": f"沒有載入 {draft['year']} 學年度規則庫，無法預覽"}
        rs = self.rulesets[draft["year"]]
        cache = self.__dict__.setdefault("_rule_previews", {})
        key = (draft["draft_id"], rs.rule_version)
        if key not in cache:
            try:
                cache[key] = rule_diff.preview_draft(draft["draft_id"], rs.kb)
            except Exception as e:  # 預覽失敗只顯示訊息，不能讓規則版本頁整個打不開
                return {"error": str(e.args[0] if e.args else e)}
        return cache[key]

    def rule_activate(self, draft_id: str, activated_by: str, acknowledge: bool = False) -> dict:
        """啟用後立即以新版本審查該年度學生；覆核紀錄因規則版本不同會標示建議再確認。"""
        entry = rule_versions.activate(draft_id, activated_by, acknowledge)
        self.use_ruleset(rule_versions.active_paths()[entry["year"]])
        return self.rule_versions_status()

    def rule_revert(self, year: int, reverted_by: str) -> dict:
        entry = rule_versions.revert(year, reverted_by)
        self.use_ruleset(rule_versions.active_paths()[entry["year"]])
        return self.rule_versions_status()

    def report_html(self, sid: str, audited_at: datetime | None = None) -> str:
        """單一學生審查明細（可列印）。"""
        rs = self.ruleset(sid)
        return report.render(self.student(sid), rs.kb.program, rs.kb.cohort, rs.rule_version, audited_at)


def _replay_mode() -> str:
    """環境變數設錯時回報 invalid，不讓已套用的上傳因為顯示資料來源失敗而被誤報成未套用。"""
    try:
        return replay.mode()
    except ValueError:
        return "invalid"


def _row_key(r: dict) -> tuple[str, str]:
    return str(r.get("code", "")).strip().upper(), str(r.get("term", "")).strip()


def _student_id(value: str) -> str:
    sid = (value or "").strip()
    if not sid or any(c in sid for c in ",\n\r\""):
        raise ValueError("請填寫學號（去識別化代號），不可含逗號、引號或換行")
    return sid


def _issue_text(i: dashboard.Issue) -> str:
    return (f"第 {i.row + 1} 列：" if i.row is not None else "") + i.message


def _cross_check_view(cmp: cross_check.Comparison, total_rows: int) -> dict:
    return {**cmp.summary(total_rows), "differences": cmp.differences, "only_primary": cmp.only_primary,
            "only_check": [{k: r.get(k, "") for k in ("code", "term", "name")} for r in cmp.only_check]}


def _edits_by_flag(before: list[dict], after: list[dict], cmp: cross_check.Comparison) -> dict:
    """人工修正分別落在交叉驗證標示列與未標示列的數量，用來量測標示是否抓到真正要改的地方（不含課程內容）。"""
    def key(r):
        return str(r.get("code", "")).strip(), str(r.get("term", "")).strip()
    new = {key(r): r for r in after}
    fields = [k for k in dashboard.FIELDS if k not in ("code", "term")]
    out = {"edited_rows_flagged": 0, "edited_rows_unflagged": 0, "rows_flagged": len(cmp.flagged_rows),
           "rows_added_reported_by_check": 0}
    for i, r in enumerate(before):
        changed = key(r) not in new or any(str(r.get(f, "")).strip() != str(new[key(r)].get(f, "")).strip()
                                           for f in fields)
        if changed:
            out["edited_rows_flagged" if i in cmp.flagged_rows else "edited_rows_unflagged"] += 1
    old_keys = {key(r) for r in before}
    reported = {key(r) for r in cmp.only_check}
    out["rows_added_reported_by_check"] = sum(1 for k in new if k not in old_keys and k in reported)
    return out


def _edit_counts(before: list[dict], after: list[dict]) -> dict:
    """人工修正量：以（科目代碼, 學期）對齊，計算修改的欄位與增刪的列數。"""
    def key(r):
        return str(r.get("code", "")).strip(), str(r.get("term", "")).strip()
    old = {key(r): r for r in before}
    new = {key(r): r for r in after}
    fields = [k for k in dashboard.FIELDS if k not in ("code", "term")]
    changed = sum(1 for k in old.keys() & new.keys() for f in fields
                  if str(old[k].get(f, "")).strip() != str(new[k].get(f, "")).strip())
    return {"rows_before": len(before), "rows_after": len(after), "rows_added": len(new.keys() - old.keys()),
            "rows_removed": len(old.keys() - new.keys()), "fields_changed": changed}


def _overflow_text(o: dict) -> str:
    return f"{o['category']}超修 {o['excess']} 學分（可移入外系 {o['movable']}）"
