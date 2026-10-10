"""把引擎結果轉成 JSON 友善的 dict，給網頁介面（之後也給 LLM 層）使用。"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime
from pathlib import Path

from . import knowledge_base
from .graph import CourseGraph
from .planner import Plan, Scenario, Term, explain_delay, plan
from .records import RecordsError, parse_records
from .roster import check_roster, parse_roster
from .rule_engine import AuditReport, Record, audit

ROOT = Path(__file__).parent.parent
KB_PATH = ROOT / "data" / "curriculum_im_114.json"
STUDENTS_PATH = ROOT / "data" / "students" / "sample_students.csv"
ROSTER_PATH = ROOT / "data" / "students" / "sample_roster.csv"


class AcademicTwinService:
    def __init__(self, kb_path: Path = KB_PATH, students_path: Path = STUDENTS_PATH,
                 roster_path: Path = ROSTER_PATH):
        self.kb = knowledge_base.load(kb_path)
        # 規則版本：學年度 + 規則庫檔案雜湊，規則庫一改版本就不同
        digest = hashlib.sha256(kb_path.read_bytes()).hexdigest()[:12]
        self.rule_version = f"{self.kb.cohort}-{digest}"
        self.graph = CourseGraph(self.kb)
        self.load_data(
            (students_path.name, students_path.read_text(encoding="utf-8-sig")),
            (roster_path.name, roster_path.read_text(encoding="utf-8-sig")),
        )

    def load_data(self, records: tuple[str, str] | None = None, roster: tuple[str, str] | None = None) -> None:
        """換掉修課紀錄及／或應屆名單（各為 (檔名, CSV 文字)）。

        兩份都檢查完才一起套用：任何一份有錯就丟 RecordsError，現有資料保持不變。
        """
        errors: list[str] = []
        students, records_name = getattr(self, "students", None), getattr(self, "records_name", None)
        roster_parsed, roster_name = getattr(self, "_roster_parsed", None), getattr(self, "roster_name", None)
        if records:
            try:
                students, records_name = parse_records(records[1]), records[0]
            except RecordsError as e:
                errors += [f"修課紀錄 {records[0]}：{msg}" for msg in e.errors]
        if roster:
            try:
                roster_parsed, roster_name = parse_roster(roster[1], roster[0]), roster[0]
            except ValueError as e:
                errors.append(str(e))
        if errors:
            raise RecordsError(errors)
        self.students, self.records_name = students, records_name
        self._roster_parsed, self.roster_name = roster_parsed, roster_name
        self.roster = check_roster(roster_parsed[0], students, roster_parsed[1])

    def data_source(self) -> dict:
        return {
            "records": self.records_name,
            "records_count": sum(len(v) for v in self.students.values()),
            "students": len(self.students),
            "roster": self.roster_name,
            "roster_count": len(self.roster.to_audit) + len(self.roster.no_records),
        }

    # ---------- 共用 ----------
    def course(self, code: str) -> dict:
        c = self.kb.courses[code]
        return {"code": code, "name": c.name, "credits": c.credits, "type": c.type, "terms": list(c.terms)}

    def _report(self, sid: str) -> AuditReport:
        if sid not in self.students:
            raise KeyError(f"找不到學生 {sid}")
        return audit(self.kb, self.students[sid])

    def _next_term(self, sid: str) -> Term:
        return max(Term.parse(r.term) for r in self.students[sid]).next()

    # ---------- 規則庫 ----------
    def meta(self) -> dict:
        kb = self.kb
        return {
            "program": kb.program,
            "cohort": kb.cohort,
            "max_credits_per_term": kb.max_credits_per_term,
            "sources": kb.sources,
            "assumptions": kb.assumptions,
            "courses": [self.course(c) for c in self.graph.order],
            "edges": [
                {"from": p, "to": c, "soft": self.graph.is_soft(p, c)}
                for c in kb.courses for p in self.graph.parents(c)
            ],
            "bottlenecks": [
                {**self.course(b.code), "depth": b.depth, "blocked": b.blocked}
                for b in self.graph.bottlenecks(top=6)
            ],
        }

    def course_impact(self, code: str) -> dict:
        if code not in self.kb.courses:
            raise KeyError(f"規則庫沒有 {code}")
        return {
            **self.course(code),
            "ancestors": [self.course(c) for c in self.graph.ancestors(code)],
            "descendants": [self.course(c) for c in self.graph.descendants(code)],
            "soft": any(self.graph.is_soft(p, code) for p in self.graph.parents(code)),
        }

    # ---------- 畢審 ----------
    def batch(self) -> list[dict]:
        rows = []
        for sid in self.roster.to_audit:
            report = self._report(sid)
            t = report.transcript
            reasons = [f"{r.requirement.id} {r.requirement.title}" for r in report.results if not r.satisfied]
            if t.review:
                reasons.insert(0, f"{len(t.review)} 筆紀錄需人工判斷")
            if t.dept_unverified and not reasons:
                reasons.append(f"{len(t.dept_unverified)} 門本系選修未在規則庫核對")
            rows.append({
                "id": sid,
                "status": report.status.value,
                "total_credits": report.total_credits,
                "satisfied": sum(r.satisfied for r in report.results),
                "requirements": len(report.results),
                "reasons": reasons,
            })
        return rows

    def roster_anomalies(self) -> list[dict]:
        """名單比對異常；與 batch() 合起來涵蓋名單上每位學生。"""
        r = self.roster
        return (
            [{"id": sid, "kind": "no_records", "message": "在應屆名單上，但沒有任何修課紀錄，未審查"} for sid in r.no_records]
            + [{"id": sid, "kind": "not_on_roster", "message": "有修課紀錄，但不在應屆名單，未審查"} for sid in r.not_on_roster]
            + [{"id": sid, "kind": "duplicate", "message": "應屆名單重複列出"} for sid in r.duplicates]
            + [{"id": None, "kind": "blank", "message": f"名單第 {line} 行學號空白"} for line in r.blank_rows]
        )

    EXPORT_FIELDS = ["student_id", "status", "total_credits", "satisfied", "requirements", "reasons",
                     "rule_version", "audited_at"]

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
                "reasons": "；".join(row["reasons"]) or "全部符合",
                "rule_version": self.rule_version, "audited_at": stamp,
            })
        for a in self.roster_anomalies():
            writer.writerow({
                "student_id": a["id"] or "", "status": f"ROSTER_{a['kind'].upper()}", "reasons": a["message"],
                "rule_version": self.rule_version, "audited_at": stamp,
            })
        return out.getvalue()

    def student(self, sid: str) -> dict:
        report = self._report(sid)
        t = report.transcript
        kb = self.kb

        def rec(r: Record) -> dict:
            return {"code": r.code, "name": r.name, "credits": r.credits, "grade": r.grade,
                    "term": r.term, "category": r.category}

        return {
            "id": sid,
            "status": report.status.value,
            "total_credits": report.total_credits,
            "credit_breakdown": report.credit_breakdown,
            "next_term": str(self._next_term(sid)),
            "requirements": [
                {
                    "id": r.requirement.id,
                    "kind": r.requirement.kind,
                    "title": r.requirement.title,
                    "satisfied": r.satisfied,
                    "have": r.have,
                    "need": r.need,
                    "shortfall": r.shortfall,
                    "missing": [self.course(c) for c in r.missing],
                    "citation": r.citation,
                }
                for r in report.results
            ],
            "issues": {
                "review": [rec(r) for r in t.review],
                "dept_unverified": [rec(r) for r in t.dept_unverified],
                "failed": [rec(r) for r in t.failed],
            },
            "records": [rec(r) for r in sorted(self.students[sid], key=lambda r: (r.term, r.code))],
            "remaining_courses": [
                self.course(c) for c in kb.courses if c not in t.passed
            ],
        }

    # ---------- 路徑模擬 ----------
    def _plan_dict(self, p: Plan) -> dict:
        return {
            "feasible": p.feasible,
            "graduation": str(p.graduation) if p.graduation else None,
            "terms": [
                {
                    "term": str(term),
                    "courses": [self.course(c) for c in codes],
                    "other_credits": p.free.get(term, 0),
                    "credits": sum(self.kb.courses[c].credits for c in codes) + p.free.get(term, 0),
                }
                for term, codes in p.terms.items()
            ],
            "unplaceable": [self.course(c) for c in p.unplaceable],
        }

    def simulate(self, sid: str, skip: list[str] | None = None, away: list[str] | None = None,
                 away_credits: int = 15) -> dict:
        report = self._report(sid)
        start = self._next_term(sid)
        base = plan(self.kb, self.graph, report, start)
        result = {"start": str(start), "base": self._plan_dict(base), "scenario": None}

        skip_map = {}
        for item in skip or []:
            code, _, term = item.partition("@")
            if code not in self.kb.courses or not term:
                raise ValueError(f"skip 格式錯誤：{item}（應為 課號@學期）")
            skip_map[code] = Term.parse(term)
        away_terms = {Term.parse(t) for t in away or []}

        if skip_map or away_terms:
            scenario = Scenario(skip=skip_map, away=away_terms, away_credits=away_credits)
            alt = plan(self.kb, self.graph, report, start, scenario)
            g0, g1 = base.graduation, alt.graduation
            result["scenario"] = {
                **self._plan_dict(alt),
                "conditions": [f"{t} 不修 {c} {self.kb.courses[c].name}" for c, t in skip_map.items()]
                              + [f"{t} 出國交換／不修本系課" for t in sorted(away_terms)],
                "impact": explain_delay(self.kb, base, alt),
                "delay_terms": ((g1.year - g0.year) * 2 + (g1.sem - g0.sem)) if (g0 and g1 and alt.feasible) else None,
            }
        return result
