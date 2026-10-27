"""系辦端 API：批次審查、名單異常、匯出、人工覆核、審查明細。（A 組）"""
from __future__ import annotations

import csv
import io
import uuid
from dataclasses import asdict, replace
from datetime import datetime

from . import cross_check, dashboard, pdf_text, report, rule_diff, rule_draft, rule_versions
from .records import REQUIRED, RecordsError, parse_records
from .rule_engine import CATEGORY, category_overflow, classify, reasons


class OfficeMixin:
    """需搭配 api.CoreService 使用（self.ruleset()、self.students、self._report…）。"""

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
        return (
            [{"id": sid, "kind": "no_records", "message": "在應屆名單上，但沒有任何修課紀錄，未審查"} for sid in r.no_records]
            + [{"id": sid, "kind": "not_on_roster", "message": "有修課紀錄，但不在應屆名單，未審查"} for sid in r.not_on_roster]
            + [{"id": sid, "kind": "other_cohort",
                "message": f"名單入學年度為 {c}，系統沒有該學年度規則庫"
                           f"（現有 {'、'.join(map(str, sorted(self.rulesets)))}），未審查，不套用其他年度"}
               for sid, c in r.other_cohort.items()]
            + [{"id": sid, "kind": "duplicate", "message": "應屆名單重複列出"} for sid in r.duplicates]
            + [{"id": None, "kind": "blank", "message": f"名單第 {line} 行學號空白"} for line in r.blank_rows]
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
        """
        sid = _student_id(student_id)
        ext, other = cross_check.extract_pair(pdf)
        ext.pages = pdf_text.extract_pages(pdf)  # 不送給 AI；check 據以核對每列原文與頁碼
        cmp = cross_check.recheck(ext.rows, other, ext.model)
        drafts = self.__dict__.setdefault("_dashboard_drafts", {})
        draft_id = uuid.uuid4().hex
        drafts[draft_id] = (sid, ext, other)
        issues = self._dashboard_issues(ext, ext.cohort) + cross_check.issues(cmp)
        return {"draft_id": draft_id, "student_id": sid, "exists": sid in self.students,
                **self._draft_view(ext, issues), "cross_check": _cross_check_view(cmp, len(ext.rows))}

    def dashboard_confirm(self, draft_id: str, rows: list[dict], reviewer: str, cohort: int | None = None,
                          acknowledge_warnings: bool = False) -> dict:
        """承辦人修正並確認後，重新檢查，再以該生的紀錄取代或新增，並記錄修正量（不含課程內容）。"""
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
        initial = cross_check.recheck(ext.rows, other, ext.model)
        dashboard.log_run({
            "type": "confirm", "model": ext.model, "prompt_version": ext.prompt_version, "format": ext.format,
            "generated_at": ext.generated_at, "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            **_edit_counts(ext.rows, edited.rows), "warnings_acknowledged": acknowledge_warnings,
            "cross_check": {**initial.summary(len(ext.rows)), **_edits_by_flag(ext.rows, edited.rows, initial),
                            "flagged_after_edit": len(cmp.flagged_rows) + len(cmp.only_check)},
        })
        return self.student(sid)

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
                "format": ext.format, "fallback_from": ext.fallback_from, "cohorts": sorted(self.rulesets),
                "categories": sorted(set(dashboard.FORMATS[ext.format].values()) - {""})}

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

    # ---------- AI 草擬規則（獨立存檔，確認也不啟用） ----------
    def rule_draft_extract(self, pdf: bytes, year: int = 114) -> dict:
        """公開應修科目表產生少量草稿；不接觸 self.kb 或審查資料。"""
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
