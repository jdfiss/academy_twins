"""把引擎結果轉成 JSON 友善的 dict，給網頁介面（之後也給 LLM 層）使用。

兩組分工（docs/TEAM_PLAN.md）：
- 本檔 CoreService：載入資料、審查、交接格式 student()（共用，A 組維護）
- office_api.OfficeMixin：系辦端（A 組）
- student_api.StudentMixin：學生端（B 組）
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import knowledge_base
from .graph import CourseGraph
from .knowledge_base import KnowledgeBase
from .office_api import OfficeMixin
from .planner import Term
from .records import RecordsError, parse_records
from .roster import check_roster, parse_roster
from .reviews import ACTIONS, ReviewEntry, ReviewStore
from .rule_engine import AuditReport, Record, audit
from .student_api import StudentMixin

ROOT = Path(__file__).parent.parent
KB_PATH = ROOT / "data" / "curriculum_im_114.json"
STUDENTS_PATH = ROOT / "data" / "students" / "sample_students.csv"
ROSTER_PATH = ROOT / "data" / "students" / "sample_roster.csv"
REVIEWS_PATH = ROOT / "data" / "reviews" / "decisions.json"  # 含學生資料，不進版控


@dataclass(frozen=True)
class RuleSet:
    """一個入學年度的規則庫、課程圖與規則版本。"""
    kb: KnowledgeBase
    graph: CourseGraph
    rule_version: str


def load_ruleset(path: Path) -> RuleSet:
    kb = knowledge_base.load(path)
    # 規則版本：學年度 + 規則庫檔案雜湊，規則庫一改版本就不同
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return RuleSet(kb, CourseGraph(kb), f"{kb.cohort}-{digest}")


class CoreService:
    def __init__(self, kb_path: Path = KB_PATH, students_path: Path = STUDENTS_PATH,
                 roster_path: Path = ROSTER_PATH, reviews_path: Path | None = None,
                 extra_kb_paths: Iterable[Path] = ()):
        """reviews_path 為 None 時覆核紀錄只存在記憶體；網頁與命令列傳 REVIEWS_PATH。

        kb_path 是預設年度的規則庫（名單未填入學年度時使用）；extra_kb_paths 是其他年度的規則庫。
        每位學生依名單 cohort 欄使用自己年度的規則庫，沒有該年度規則庫時列入名單異常、不審查。
        """
        default = load_ruleset(kb_path)
        self.rulesets: dict[int, RuleSet] = {default.kb.cohort: default}
        for path in extra_kb_paths:
            rs = load_ruleset(path)
            if rs.kb.cohort in self.rulesets:
                raise ValueError(f"規則庫 {path} 的學年度 {rs.kb.cohort} 已載入過，同一年度只能有一份規則庫")
            self.rulesets[rs.kb.cohort] = rs
        # 預設年度；規則庫頁（meta）與尚未改用 ruleset() 的功能使用
        self.kb, self.graph, self.rule_version = default.kb, default.graph, default.rule_version
        self.reviews = ReviewStore(reviews_path)
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
        self.roster = check_roster(roster_parsed.ids, students, roster_parsed.blank_rows,
                                   roster_parsed.cohorts, self.kb.cohort, set(self.rulesets))

    def use_ruleset(self, path: Path) -> RuleSet:
        """換用某年度的規則庫版本（啟用或還原規則版本後呼叫）；只能換已載入的年度。"""
        rs = load_ruleset(path)
        if rs.kb.cohort not in self.rulesets:
            raise ValueError(f"沒有載入 {rs.kb.cohort} 學年度規則庫，不能換用")
        self.rulesets[rs.kb.cohort] = rs
        if rs.kb.cohort == self.kb.cohort:
            self.kb, self.graph, self.rule_version = rs.kb, rs.graph, rs.rule_version
        return rs

    # ---------- 共用 ----------
    def ruleset(self, sid: str) -> RuleSet:
        """學生適用的規則庫：依名單的入學學年度；名單未填或不在名單時用預設年度。不套用其他年度。"""
        cohort = self._roster_parsed.cohorts.get(sid, self.kb.cohort)
        if cohort not in self.rulesets:
            raise KeyError(f"沒有 {cohort} 學年度規則庫，學生 {sid} 無法審查")
        return self.rulesets[cohort]

    def course(self, code: str, kb: KnowledgeBase | None = None) -> dict:
        c = (kb or self.kb).courses[code]
        return {"code": code, "name": c.name, "credits": c.credits, "type": c.type, "terms": list(c.terms)}

    def _report(self, sid: str) -> AuditReport:
        if sid not in self.students:
            raise KeyError(f"找不到學生 {sid}")
        return audit(self.ruleset(sid).kb, self.students[sid], self.reviews.decisions(sid),
                     self._roster_parsed.english_passed.get(sid))

    def _next_term(self, sid: str) -> Term:
        return max(Term.parse(r.term) for r in self.students[sid]).next()

    # ---------- 規則庫 ----------
    def meta(self) -> dict:
        kb = self.kb
        return {
            "program": kb.program,
            "cohort": kb.cohort,
            "cohorts": sorted(self.rulesets),
            "max_credits_per_term": kb.max_credits_per_term,
            "sources": kb.sources,
            "assumptions": kb.assumptions,
            "categories": list(kb.categories),
            "review_actions": ACTIONS,
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

    def _review_entry(self, e: ReviewEntry, rule_version: str) -> dict:
        """rule_version：該生目前適用的規則版本；與覆核時的版本不同就標示建議再確認。"""
        return {"code": e.code, "term": e.term, "action": e.action, "category": e.category, "label": e.label,
                "reviewer": e.reviewer, "note": e.note, "decided_at": e.decided_at, "rule_version": e.rule_version,
                "outdated": e.rule_version != rule_version}

    # ---------- 交接格式（兩端共用；欄位變更需兩組同意，見 docs/TEAM_PLAN.md） ----------
    def student(self, sid: str, trial: bool = False) -> dict:
        """單一學生的審查結果。trial：學生端試算為 True，畫面據此標示「試算」。"""
        report = self._report(sid)
        t = report.transcript
        rs = self.ruleset(sid)
        kb = rs.kb
        current = self.reviews.current(sid)

        def rec(r: Record) -> dict:
            return {"code": r.code, "name": r.name, "credits": r.credits, "grade": r.grade,
                    "term": r.term, "category": r.category}

        return {
            "id": sid,
            "status": report.status.value,
            "cohort": str(kb.cohort),
            "rule_version": rs.rule_version,
            "trial": trial,
            "total_credits": report.total_credits,
            "credit_breakdown": report.credit_breakdown,
            "next_term": str(self._next_term(sid)),
            "requirements": [
                {
                    "id": r.requirement.id,
                    "kind": r.requirement.kind,
                    "title": r.requirement.title,
                    "satisfied": r.satisfied,
                    "pending": r.pending,
                    "detail": r.detail,
                    "have": r.have,
                    "need": r.need,
                    "shortfall": r.shortfall,
                    "missing": [self.course(c, kb) for c in r.missing],
                    "citation": r.citation,
                    "system_code": r.requirement.system_code or None,
                    "regulation_term": r.requirement.regulation_term or None,
                }
                for r in report.results
            ],
            "issues": {
                "review": [rec(r) for r in t.review],
                "dept_unverified": [rec(r) for r in t.dept_unverified],
                "failed": [rec(r) for r in t.failed],
            },
            "reviews": [
                {**rec(r), **self._review_entry(current[(r.code, r.term)], rs.rule_version)}
                for r, _ in t.reviewed
            ],
            "review_history": [self._review_entry(e, rs.rule_version) for e in reversed(self.reviews.history(sid))],
            "records": [rec(r) for r in sorted(self.students[sid], key=lambda r: (r.term, r.code))],
            "remaining_courses": [
                self.course(c, kb) for c in kb.courses if c not in t.passed
            ],
            "flags": [
                {"type": f.type, "requirement_ids": list(f.requirement_ids), "courses": list(f.courses),
                 "credits": f.credits, "message": f.message}
                for f in report.flags
            ],
            "actions": [
                {"type": a.type, "title": a.title, "office": a.office, "requirement_ids": list(a.requirement_ids),
                 "courses": list(a.courses), "from_flag": a.from_flag}
                for a in report.actions
            ],
        }


class AcademicTwinService(OfficeMixin, StudentMixin, CoreService):
    """網頁與命令列使用的完整服務。"""
