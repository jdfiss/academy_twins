"""Rule Engine：拿學生修課紀錄逐條比對畢業規則。

設計原則：
- 判定完全由規則決定，不經過 LLM
- 每一條結果都附上規章出處，可回溯
- 規則庫無法判斷的東西不猜：類別不明的紀錄丟給人工覆核；
  不在規則庫的本系課號暫以本系選修計，但標成 Warning 讓系辦確認
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .knowledge_base import KnowledgeBase, Requirement


class Status(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass(frozen=True)
class Record:
    code: str
    name: str
    credits: int
    grade: float
    term: str  # 例如 "113-1"
    category: str = ""  # 國文／外文／通識／體育…；本系或院訂課程留空


@dataclass
class RequirementResult:
    requirement: Requirement
    citation: str
    satisfied: bool
    used: list[str]
    missing: list[str] = field(default_factory=list)
    have: int = 0  # 已達成數量（門數或學分，依規則種類）
    need: int = 0

    @property
    def shortfall(self) -> int:
        return max(self.need - self.have, 0)


@dataclass
class Transcript:
    """修課紀錄分類後的結果。"""
    passed: list[str] = field(default_factory=list)  # 規則庫內的課號
    categorized: list[Record] = field(default_factory=list)  # 有類別的課（國文、通識…）
    dept_unverified: list[Record] = field(default_factory=list)  # 本系課號但不在規則庫
    outside: list[Record] = field(default_factory=list)  # 外系課程
    review: list[Record] = field(default_factory=list)  # 類別不明，需人工判斷
    failed: list[Record] = field(default_factory=list)


@dataclass
class AuditReport:
    status: Status
    results: list[RequirementResult]
    transcript: Transcript
    total_credits: int
    credit_breakdown: dict[str, int]

    @property
    def passed_courses(self) -> list[str]:
        return self.transcript.passed


def classify(kb: KnowledgeBase, records: list[Record]) -> Transcript:
    t = Transcript()
    seen: set[str] = set()
    for r in records:
        if r.grade < kb.passing_grade:
            t.failed.append(r)
            continue
        if r.code in seen:  # 重複通過同一課號只算一次
            continue
        seen.add(r.code)
        if r.code in kb.courses:
            t.passed.append(r.code)
        elif r.category:
            (t.categorized if r.category in kb.categories else t.review).append(r)
        elif r.code.startswith(kb.dept_prefixes):
            t.dept_unverified.append(r)
        else:
            t.outside.append(r)
    # 之後重修通過的課，不再列為不及格
    t.failed = [r for r in t.failed if r.code not in seen]
    return t


def root_category(kb: KnowledgeBase, category: str) -> str:
    while kb.categories.get(category):
        category = kb.categories[category]
    return category


def credit_breakdown(kb: KnowledgeBase, t: Transcript) -> dict[str, int]:
    """依學分認列規則算出每一塊可採計的學分。"""
    policy = kb.credit_policy
    breakdown = {"規則庫課程": sum(kb.courses[c].credits for c in t.passed)}
    if t.dept_unverified:
        breakdown["本系選修（未核對）"] = sum(r.credits for r in t.dept_unverified)

    by_root: dict[str, int] = {}
    for r in t.categorized:
        root = root_category(kb, r.category)
        by_root[root] = by_root.get(root, 0) + r.credits

    required = {req.categories[0]: req.min_credits for req in kb.requirements if req.kind == "category_credits"}
    outside_pool = sum(r.credits for r in t.outside)
    for cat, credits in by_root.items():
        if cat in policy.excluded:
            continue
        counted = min(credits, required.get(cat, 0))
        if counted:
            breakdown[cat] = counted
        excess = credits - counted
        outside_pool += min(excess, policy.category_caps.get(cat, excess))

    if policy.outside_cap is not None:
        outside_pool = min(outside_pool, policy.outside_cap)
    if outside_pool:
        breakdown["外系認列"] = outside_pool
    return breakdown


def audit(kb: KnowledgeBase, records: list[Record]) -> AuditReport:
    t = classify(kb, records)
    breakdown = credit_breakdown(kb, t)
    total = sum(breakdown.values())
    consumed: set[str] = set()
    results = [_evaluate(kb, req, t, consumed, total) for req in kb.requirements]

    if t.review:
        status = Status.MANUAL_REVIEW
    elif not all(r.satisfied for r in results):
        status = Status.FAIL
    elif t.dept_unverified:
        status = Status.WARNING
    else:
        status = Status.PASS

    return AuditReport(status, results, t, total, breakdown)


def _evaluate(kb: KnowledgeBase, req: Requirement, t: Transcript, consumed: set[str],
              total: int) -> RequirementResult:
    available = [c for c in t.passed if not (req.exclusive and c in consumed)]
    cite = kb.cite(req.source)

    if req.kind == "all_of":
        used = [c for c in req.courses if c in available]
        missing = [c for c in req.courses if c not in used]
        result = RequirementResult(req, cite, not missing, used, missing, have=len(used), need=len(req.courses))

    elif req.kind == "n_of":
        used = [c for c in req.courses if c in available][: req.n]
        missing = [c for c in req.courses if c not in used] if len(used) < req.n else []
        result = RequirementResult(req, cite, len(used) >= req.n, used, missing, have=len(used), need=req.n)

    elif req.kind == "credits_from":
        pool = [c for c in available
                if (not req.courses or c in req.courses)
                and (req.course_type is None or kb.courses[c].type == req.course_type)]
        used, have = [], 0
        for c in pool:
            if have >= req.min_credits:
                break
            used.append(c)
            have += kb.courses[c].credits
        result = RequirementResult(req, cite, have >= req.min_credits, used, have=have, need=req.min_credits)

    elif req.kind in ("category_credits", "category_count"):
        matched = [r for r in t.categorized if any(kb.in_category(r.category, c) for c in req.categories)]
        used = [r.code for r in matched]
        if req.kind == "category_credits":
            have, need = sum(r.credits for r in matched), req.min_credits
        else:
            have, need = len(matched), req.n
        result = RequirementResult(req, cite, have >= need, used, have=have, need=need)

    elif req.kind == "total_credits":
        result = RequirementResult(req, cite, total >= req.min_credits, available,
                                   have=total, need=req.min_credits)

    else:
        raise ValueError(f"未知的規則種類：{req.kind}（{req.id}）")

    if req.exclusive:
        consumed.update(result.used)
    return result
