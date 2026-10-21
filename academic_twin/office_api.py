"""系辦端 API：批次審查、名單異常、匯出、人工覆核、審查明細。（A 組）"""
from __future__ import annotations

import csv
import io
import uuid
from dataclasses import asdict, replace
from datetime import datetime

from . import dashboard, report
from .records import REQUIRED, RecordsError, parse_records
from .rule_engine import CATEGORY, classify, reasons


class OfficeMixin:
    """需搭配 api.CoreService 使用（self.kb、self.students、self._report…）。"""

    def data_source(self) -> dict:
        return {
            "records": self.records_name,
            "records_count": sum(len(v) for v in self.students.values()),
            "students": len(self.students),
            "roster": self.roster_name,
            "roster_count": self.roster.roster_size,
        }

    # ---------- 畢審 ----------
    def batch(self) -> list[dict]:
        rows = []
        for sid in self.roster.to_audit:
            report = self._report(sid)
            t = report.transcript
            rows.append({
                "id": sid,
                "status": report.status.value,
                "total_credits": report.total_credits,
                "total_required": next((r.need for r in report.results
                                        if r.requirement.kind == "total_credits"), None),
                "satisfied": sum(r.satisfied for r in report.results),
                "requirements": len(report.results),
                "reasons": reasons(report),
                "reviewed": len(t.reviewed),
                "flags": [{"type": f.type, "message": f.message, "credits": f.credits} for f in report.flags],
                "action_count": len(report.actions),
                "actions": [{"title": a.title, "office": a.office} for a in report.actions],
            })
        return rows

    def roster_anomalies(self) -> list[dict]:
        """名單比對異常；與 batch() 合起來涵蓋名單上每位學生。"""
        r = self.roster
        return (
            [{"id": sid, "kind": "no_records", "message": "在應屆名單上，但沒有任何修課紀錄，未審查"} for sid in r.no_records]
            + [{"id": sid, "kind": "not_on_roster", "message": "有修課紀錄，但不在應屆名單，未審查"} for sid in r.not_on_roster]
            + [{"id": sid, "kind": "other_cohort",
                "message": f"名單入學年度為 {c}，系統只有 {self.kb.cohort} 學年度規則庫，未審查"}
               for sid, c in r.other_cohort.items()]
            + [{"id": sid, "kind": "duplicate", "message": "應屆名單重複列出"} for sid in r.duplicates]
            + [{"id": None, "kind": "blank", "message": f"名單第 {line} 行學號空白"} for line in r.blank_rows]
        )

    EXPORT_FIELDS = ["student_id", "status", "total_credits", "satisfied", "requirements", "reasons",
                     "reviewed", "rule_version", "audited_at", "flag_types", "flag_messages", "actions"]

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
                "rule_version": self.rule_version, "audited_at": stamp,
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
        raw = classify(self.kb, self.students[sid])
        if (code, term) not in {(r.code, r.term) for r in raw.review + raw.dept_unverified}:
            raise ValueError(f"{code}（{term}）不是待覆核的紀錄；規則庫能判定的紀錄不能人工改判")
        if action == CATEGORY and category not in self.kb.categories:
            raise ValueError(f"類別「{category}」不在規則庫")
        self.reviews.add(sid, code, term, action, reviewer, self.rule_version, category, note)
        return self.student(sid)

    # ---------- AI 解析儀表板（結果待承辦人確認，確認前不進入審查） ----------
    def dashboard_extract(self, pdf: bytes, student_id: str) -> dict:
        """AI 擷取儀表板為草稿；草稿暫存於記憶體，只有確認後才會寫入修課紀錄。"""
        sid = _student_id(student_id)
        ext = dashboard.extract(pdf)
        drafts = self.__dict__.setdefault("_dashboard_drafts", {})
        draft_id = uuid.uuid4().hex
        drafts[draft_id] = (sid, ext)
        return {"draft_id": draft_id, "student_id": sid, "exists": sid in self.students,
                **self._draft_view(ext, dashboard.check(ext, self.kb))}

    def dashboard_confirm(self, draft_id: str, rows: list[dict], reviewer: str, cohort: int | None = None,
                          acknowledge_warnings: bool = False) -> dict:
        """承辦人修正並確認後，重新檢查，再以該生的紀錄取代或新增，並記錄修正量（不含課程內容）。"""
        drafts = self.__dict__.get("_dashboard_drafts", {})
        if draft_id not in drafts:
            raise KeyError("找不到這份解析草稿，可能已確認或伺服器已重開，請重新解析")
        if not reviewer.strip():
            raise ValueError("請填寫確認人")
        sid, ext = drafts[draft_id]
        fields = dashboard.FIELDS + ["rule_code", "source_page", "source_text"]
        edited = replace(ext, rows=[{k: r.get(k, "") for k in fields} for r in rows])
        issues = dashboard.check(edited, self.kb)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise RecordsError([_issue_text(i) for i in errors])
        if any(i.level == "warning" for i in issues) and not acknowledge_warnings:
            raise RecordsError(["仍有警示未確認；請逐項對照原文後勾選「已確認警示」"]
                               + [_issue_text(i) for i in issues if i.level == "warning"])
        records = parse_records(dashboard.to_records_csv(sid, edited.rows))[sid]
        self._replace_student(sid, records, cohort)
        del drafts[draft_id]
        dashboard.log_run({
            "type": "confirm", "model": ext.model, "prompt_version": ext.prompt_version, "format": ext.format,
            "generated_at": ext.generated_at, "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            **_edit_counts(ext.rows, edited.rows), "warnings_acknowledged": acknowledge_warnings,
        })
        return self.student(sid)

    def _draft_view(self, ext: dashboard.Extraction, issues: list[dashboard.Issue]) -> dict:
        return {"rows": ext.rows, "issues": [asdict(i) for i in issues], "cohort": ext.cohort,
                "declared_required": ext.declared_required, "declared_earned": ext.declared_earned,
                "model": ext.model, "generated_at": ext.generated_at, "prompt_version": ext.prompt_version,
                "format": ext.format, "fallback_from": ext.fallback_from, "categories": sorted(set(dashboard.FORMATS[ext.format].values()) - {""})}

    def _replace_student(self, sid: str, records: list, cohort: int | None) -> None:
        """以確認後的紀錄取代該生既有紀錄並加入名單；經由 load_data 重新檢查，失敗時資料不變。"""
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=REQUIRED + ["category"], lineterminator="\n")
        writer.writeheader()
        for other, recs in {**self.students, sid: records}.items():
            for r in recs:
                writer.writerow({"student_id": other, "code": r.code, "name": r.name, "credits": r.credits,
                                 "grade": f"{r.grade:g}", "term": r.term, "category": r.category})
        roster = self._roster_parsed
        cohorts = {**roster.cohorts, **({sid: cohort} if cohort else {})}
        ids = roster.ids + ([] if sid in roster.ids else [sid])
        rout = io.StringIO()
        rw = csv.writer(rout, lineterminator="\n")
        rw.writerow(["student_id", "english_passed", "cohort"])
        for i in ids:
            english = roster.english_passed.get(i)
            rw.writerow([i, "" if english is None else ("Y" if english else "N"), cohorts.get(i, "")])
        source = f"{self.records_name}＋AI 解析 {sid}" if self.records_name else f"AI 解析 {sid}"
        self.load_data((source, out.getvalue()), (self.roster_name or "應屆名單", rout.getvalue()))

    def report_html(self, sid: str, audited_at: datetime | None = None) -> str:
        """單一學生審查明細（可列印）。"""
        return report.render(self.student(sid), self.kb.program, self.kb.cohort, self.rule_version, audited_at)


def _student_id(value: str) -> str:
    sid = (value or "").strip()
    if not sid or any(c in sid for c in ",\n\r\""):
        raise ValueError("請填寫學號（去識別化代號），不可含逗號、引號或換行")
    return sid


def _issue_text(i: dashboard.Issue) -> str:
    return (f"第 {i.row + 1} 列：" if i.row is not None else "") + i.message


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
