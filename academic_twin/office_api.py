"""系辦端 API：批次審查、名單異常、匯出、人工覆核、審查明細。（A 組）"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from . import report
from .rule_engine import CATEGORY, category_overflow, classify, reasons


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
                "overflow": [{"category": cat, "excess": excess, "movable": movable}
                             for cat, excess, movable in category_overflow(self.kb, t)],
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

    def report_html(self, sid: str, audited_at: datetime | None = None) -> str:
        """單一學生審查明細（可列印）。"""
        return report.render(self.student(sid), self.kb.program, self.kb.cohort, self.rule_version, audited_at)


def _overflow_text(o: dict) -> str:
    return f"{o['category']}超修 {o['excess']} 學分（可移入外系 {o['movable']}）"
