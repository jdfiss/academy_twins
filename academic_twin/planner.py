"""Path Planner：從現在的狀態往後排課，估最早畢業學期，並做 What-if。

排課方式（MVP，貪婪法）：
- 每學期只排「有開課、先修已通過」的課，學分不超過上限
- 先排先修鏈最長的課（關鍵路徑優先），避免瓶頸課拖到後面
- 剩下的空檔用「其他學分」（國文、外文、通識、本系選修…）補滿總學分
- 先修同時遵守正式擋修與建議先修
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .graph import CourseGraph
from .knowledge_base import KnowledgeBase
from .rule_engine import AuditReport

MAX_TERMS = 16  # 排超過這麼多學期還排不完，視為不可行


@dataclass(frozen=True, order=True)
class Term:
    year: int
    sem: int  # 1 = 上學期（fall），2 = 下學期（spring）

    @classmethod
    def parse(cls, s: str) -> "Term":
        year, sem = s.split("-")
        return cls(int(year), int(sem))

    def next(self) -> "Term":
        return Term(self.year, 2) if self.sem == 1 else Term(self.year + 1, 1)

    @property
    def season(self) -> str:
        return "fall" if self.sem == 1 else "spring"

    def __str__(self) -> str:
        return f"{self.year}-{self.sem}"


@dataclass
class Scenario:
    """What-if 條件。"""
    skip: dict[str, Term] = field(default_factory=dict)  # 某門課在某學期不修
    away: set[Term] = field(default_factory=set)  # 交換／休學：該學期不能修本系課
    away_credits: int = 0  # 交換學期可抵的自由學分


@dataclass
class Plan:
    start: Term
    terms: dict[Term, list[str]]  # 每學期排的課號
    free: dict[Term, int]  # 每學期排的自由學分
    targets: list[str]  # 為了畢業必須再修的課
    unplaceable: list[str]  # 排不進去的課（先修缺漏、從不開課等）

    @property
    def feasible(self) -> bool:
        return not self.unplaceable

    @property
    def graduation(self) -> Term | None:
        return max(self.terms) if self.terms else None

    def term_of(self, code: str) -> Term | None:
        return next((t for t, cs in self.terms.items() if code in cs), None)


def targets_from_audit(kb: KnowledgeBase, graph: CourseGraph, report: AuditReport) -> tuple[list[str], int]:
    """從畢審結果推出：還必須修哪些課，以及還缺多少自由學分。"""
    passed = set(report.passed_courses)
    targets: list[str] = []

    def reachable(c: str) -> bool:
        return all(a in passed or a in targets for a in graph.ancestors(c))

    def by_ease(codes):
        return sorted(codes, key=lambda c: (len([a for a in graph.ancestors(c) if a not in passed]), c))

    for r in report.results:
        req = r.requirement
        if r.satisfied or req.kind == "total_credits":
            continue
        if req.kind == "all_of":
            targets += [c for c in r.missing if c not in targets]
        elif req.kind == "n_of":
            options = [c for c in by_ease(r.missing) if c not in targets and reachable(c)]
            targets += options[: r.shortfall]
        elif req.kind == "credits_from":
            pool = [c for c in kb.courses
                    if c not in passed and c not in targets
                    and (not req.courses or c in req.courses)
                    and (req.course_type is None or kb.courses[c].type == req.course_type)]
            need = r.shortfall
            for c in by_ease(pool):
                if need <= 0:
                    break
                if reachable(c):
                    targets.append(c)
                    need -= kb.courses[c].credits

    # 國文、通識、本系選修等沒有指定課號的學分，排課時以「其他學分」補齊
    total = next((r for r in report.results if r.requirement.kind == "total_credits"), None)
    total_short = total.shortfall if total else 0
    category_short = sum(r.shortfall for r in report.results if r.requirement.kind == "category_credits")
    free_needed = max(total_short - sum(kb.courses[c].credits for c in targets), category_short)
    return targets, free_needed


def min_terms_from_audit(report: AuditReport) -> int:
    """體育這類「每學期一門」的要求，至少還要幾個學期。"""
    return max((r.shortfall for r in report.results if r.requirement.kind == "category_count"), default=0)


def plan(kb: KnowledgeBase, graph: CourseGraph, report: AuditReport, start: Term,
         scenario: Scenario | None = None) -> Plan:
    scenario = scenario or Scenario()
    targets, free_left = targets_from_audit(kb, graph, report)
    done = set(report.passed_courses)
    remaining = set(targets)
    cap = kb.max_credits_per_term

    # 關鍵路徑優先：在「還沒修的目標課」之間，往下鏈越長越先排
    def remaining_depth(c: str) -> int:
        return 1 + max((remaining_depth(x) for x in graph.children[c] if x in targets), default=0)

    min_terms = min_terms_from_audit(report)
    terms: dict[Term, list[str]] = {}
    free: dict[Term, int] = {}
    term = start
    for i in range(MAX_TERMS):
        if not remaining and free_left <= 0 and i >= min_terms:
            break
        taken: list[str] = []
        load = 0
        if term in scenario.away:
            got = min(scenario.away_credits, free_left)
            free_left -= got
            free[term] = got
        else:
            available = [
                c for c in remaining
                if term.season in kb.courses[c].terms
                and all(p in done for p in graph.parents(c))
                and scenario.skip.get(c) != term
            ]
            for c in sorted(available, key=lambda c: (-remaining_depth(c), c)):
                if load + kb.courses[c].credits <= cap:
                    taken.append(c)
                    load += kb.courses[c].credits
            got = min(cap - load, free_left)
            free_left -= got
            free[term] = got
        terms[term] = taken
        remaining -= set(taken)
        done |= set(taken)
        term = term.next()

    return Plan(start, terms, free, targets, unplaceable=sorted(remaining))


def explain_delay(kb: KnowledgeBase, base: Plan, alt: Plan) -> list[str]:
    """比較兩個計畫，列出被往後推的課與畢業時間的變化。"""
    lines = []
    moved = []
    for c in base.targets:
        before, after = base.term_of(c), alt.term_of(c)
        if before and (after is None or after > before):
            moved.append((before, c, after))
    for before, c, after in sorted(moved):
        name = f"{c} {kb.courses[c].name}"
        lines.append(f"{name}：{before} → {after if after else '排不進去'}")

    g0, g1 = base.graduation, alt.graduation
    if not alt.feasible:
        lines.append(f"⚠ 此方案在 {MAX_TERMS} 學期內排不完：{'、'.join(alt.unplaceable)}")
    elif g0 and g1 and g1 > g0:
        delay = (g1.year - g0.year) * 2 + (g1.sem - g0.sem)
        lines.append(f"最早畢業：{g0} → {g1}（延後 {delay} 學期）")
    else:
        lines.append(f"最早畢業不受影響：仍為 {g1}")
    return lines
