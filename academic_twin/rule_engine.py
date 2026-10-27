"""Rule Engine：拿學生修課紀錄逐條比對畢業規則。

設計原則：
- 判定完全由規則決定，不經過 LLM
- 每一條結果都附上規章出處，可回溯
- 規則庫無法判斷的東西不猜：類別不明的紀錄丟給人工覆核；
  不在規則庫的本系課號，規則庫已確認認列方式時算本系選修，否則暫以本系選修計並標成 Warning
- 系辦的覆核決定只作用在上述兩種紀錄；規則庫能判斷的紀錄不能被覆核改掉
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections import Counter
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


# 覆核決定的採計方式（category 另指定規則庫類別）
DEPT_ELECTIVE, OUTSIDE, EXCLUDE, CATEGORY = "dept_elective", "outside", "exclude", "category"


@dataclass(frozen=True)
class Decision:
    action: str  # DEPT_ELECTIVE / OUTSIDE / EXCLUDE / CATEGORY
    category: str = ""  # action 為 CATEGORY 時使用


Decisions = dict[tuple[str, str], Decision]  # (課號, 學期) → 決定


@dataclass
class RequirementResult:
    requirement: Requirement
    citation: str
    satisfied: bool
    used: list[str]
    missing: list[str] = field(default_factory=list)
    have: int = 0  # 已達成數量（門數或學分，依規則種類）
    need: int = 0
    pending: bool = False  # 修課紀錄無法判斷、需外部資料確認（例如英檢是否已通過）
    detail: str = ""  # 附加條件的說明（例如「大一體育 1／2 學期」）

    @property
    def shortfall(self) -> int:
        return max(self.need - self.have, 0)


@dataclass
class Transcript:
    """修課紀錄分類後的結果。"""
    passed: list[str] = field(default_factory=list)  # 規則庫內通過的課號；允許重複修習時保留各次
    categorized: list[Record] = field(default_factory=list)  # 有類別的課（國文、通識…）
    dept_elective: list[Record] = field(default_factory=list)  # 本系課號、不在規則庫，依規則算本系選修
    dept_unverified: list[Record] = field(default_factory=list)  # 本系課號、不在規則庫，認列方式未確認
    outside: list[Record] = field(default_factory=list)  # 外系課程
    review: list[Record] = field(default_factory=list)  # 類別不明，需人工判斷
    failed: list[Record] = field(default_factory=list)
    dept_confirmed: list[Record] = field(default_factory=list)  # 系辦確認的本系選修
    excluded: list[Record] = field(default_factory=list)  # 系辦決定不採計
    reviewed: list[tuple[Record, Decision]] = field(default_factory=list)  # 套用了覆核決定的紀錄
    attempted: set[str] = field(default_factory=set)  # 有修課紀錄的課號（含不及格），必選課用
    repeated: list[Record] = field(default_factory=list)  # 已採計的可重複修習，第二次起的紀錄
    duplicate_review: list[Record] = field(default_factory=list)  # 未標示可重複修習，待覆核的第二次起紀錄


# 校務系統可能誤判的情況（見 docs/TEAM_PLAN.md 交接格式）
OVERFLOW, REPEATABLE, SUBSTITUTION_PENDING = "overflow", "repeatable", "substitution_pending"
FLAG_KINDS = (OVERFLOW, REPEATABLE, SUBSTITUTION_PENDING)


@dataclass(frozen=True)
class Flag:
    """校務系統可能誤判、但不一定是真的缺修的項目；只提示，不改變判定。"""
    type: str  # FLAG_KINDS 之一
    message: str  # 簡短事實描述；給學生看的白話說明由學生端組成
    requirement_ids: tuple[str, ...] = ()
    courses: tuple[str, ...] = ()
    credits: int = 0


# 需辦理事項的種類
SUBSTITUTION, TRANSFER_CREDIT, MANUAL_RECOGNITION = "substitution", "transfer_credit", "manual_recognition"


@dataclass(frozen=True)
class Action:
    """學生需找承辦單位辦理的事項（抵修、抵免、人工認列…）。"""
    type: str
    title: str
    office: str = "待確認"  # 承辦單位或承辦人
    requirement_ids: tuple[str, ...] = ()
    courses: tuple[str, ...] = ()
    from_flag: str | None = None  # 由哪一種 flag 推得


@dataclass
class AuditReport:
    status: Status
    results: list[RequirementResult]
    transcript: Transcript
    total_credits: int
    credit_breakdown: dict[str, int]
    # 由審查核心（A 組）產生，兩端呈現；提示本身不改變判定。
    flags: list[Flag] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)

    @property
    def passed_courses(self) -> list[str]:
        return self.transcript.passed


def classify(kb: KnowledgeBase, records: list[Record], decisions: Decisions | None = None) -> Transcript:
    t = Transcript()
    decisions = decisions or {}
    seen: set[str] = set()
    offerings: set[tuple[str, tuple[int, ...]]] = set()
    for r in records:
        offering = r.code, tuple(int(p) for p in r.term.split("-"))
        if offering in offerings:
            raise ValueError(f"{r.code}（{r.term}）修課紀錄重複，請先確認資料")
        offerings.add(offering)
    repeatable = set(kb.repeatable_courses)
    counted: Counter[str] = Counter()
    # 先保留最早的通過紀錄，輸入列順序不能決定哪次修習送人工覆核。
    for r in sorted(records, key=lambda r: (tuple(int(p) for p in r.term.split("-")), r.code)):
        t.attempted.add(r.code)
        if r.grade < kb.passing_grade:
            t.failed.append(r)
            continue
        repeated = r.code in seen
        seen.add(r.code)
        category = kb.category_of(r.code, r.category)
        if category != r.category:
            r = replace(r, category=category)
        if repeated and r.code not in repeatable:
            decision = decisions.get((r.code, r.term))
            if decision:
                _apply(kb, t, r, decision)
            else:
                t.review.append(r)
                t.duplicate_review.append(r)
            continue
        if r.code in kb.courses:
            t.passed.append(r.code)
        elif r.category and r.category in kb.categories:
            t.categorized.append(r)
        elif not r.category and r.code.startswith(kb.dept_prefixes) and kb.dept_prefix_is_elective:
            t.dept_elective.append(r)
        elif r.category or r.code.startswith(kb.dept_prefixes):
            decision = decisions.get((r.code, r.term))
            if decision:
                _apply(kb, t, r, decision)
            else:
                (t.review if r.category else t.dept_unverified).append(r)
        else:
            t.outside.append(r)
        if r not in t.review and r not in t.excluded:
            counted[r.code] += 1
            if counted[r.code] > 1:
                t.repeated.append(r)
    # 之後重修通過的課，不再列為不及格
    t.failed = [r for r in t.failed if r.code not in seen]
    return t


def _apply(kb: KnowledgeBase, t: Transcript, r: Record, d: Decision) -> None:
    if d.action == CATEGORY:
        if d.category not in kb.categories:
            raise ValueError(f"覆核決定的類別「{d.category}」不在規則庫")
        t.categorized.append(Record(r.code, r.name, r.credits, r.grade, r.term, d.category))
    elif d.action == DEPT_ELECTIVE:
        t.dept_confirmed.append(r)
    elif d.action == OUTSIDE:
        t.outside.append(r)
    elif d.action == EXCLUDE:
        t.excluded.append(r)
    else:
        raise ValueError(f"未知的覆核決定：{d.action}")
    t.reviewed.append((r, d))


def root_category(kb: KnowledgeBase, category: str) -> str:
    while kb.categories.get(category):
        category = kb.categories[category]
    return category


def dept_elective_credits(kb: KnowledgeBase, t: Transcript) -> int:
    """本系選修學分：計入本系選修的必選課、本系選修課（含覆核確認與未核對）。"""
    return (sum(kb.courses[c].credits for c in t.passed if kb.courses[c].counts_as == "dept_elective")
            + sum(r.credits for r in t.dept_elective + t.dept_confirmed + t.dept_unverified))


def credit_breakdown(kb: KnowledgeBase, t: Transcript) -> dict[str, int]:
    """依學分認列規則算出每一塊可採計的學分。"""
    policy = kb.credit_policy
    breakdown = {"院系必修": sum(kb.courses[c].credits for c in t.passed if kb.courses[c].counts_as is None)}
    unverified = sum(r.credits for r in t.dept_unverified)
    if dept_elective_credits(kb, t) - unverified:
        breakdown["本系選修"] = dept_elective_credits(kb, t) - unverified
    if unverified:
        breakdown["本系選修（未核對）"] = unverified

    outside_pool = (sum(r.credits for r in t.outside)
                    + sum(kb.courses[c].credits for c in t.passed if kb.courses[c].counts_as == "outside"))
    for cat, counted, _, overflow in _category_allocation(kb, t):
        if counted:
            breakdown[cat] = counted
        outside_pool += overflow

    if policy.outside_cap is not None:
        outside_pool = min(outside_pool, policy.outside_cap)
    if outside_pool:
        breakdown["外系認列"] = outside_pool
    return breakdown


def category_overflow(kb: KnowledgeBase, t: Transcript) -> list[tuple[str, int, int]]:
    """有最低學分要求的類別超修多少學分、其中可移入外系學分池多少（扣類別上限）；總表呈現用。"""
    required = {req.categories[0] for req in kb.requirements if req.kind == "category_credits"}
    return [(cat, excess, overflow) for cat, _, excess, overflow in _category_allocation(kb, t)
            if cat in required and excess > 0]


def _category_allocation(kb: KnowledgeBase, t: Transcript) -> list[tuple[str, int, int, int]]:
    """類別的採計、超出要求與可移入外系學分池數量；總學分、flag 與總表共用同一份計算。"""
    by_root: dict[str, int] = {}
    for r in t.categorized:
        root = root_category(kb, r.category)
        by_root[root] = by_root.get(root, 0) + r.credits
    required = {req.categories[0]: req.min_credits for req in kb.requirements if req.kind == "category_credits"}
    allocations = []
    for cat, credits in by_root.items():
        if cat in kb.credit_policy.excluded:
            continue
        counted = min(credits, required.get(cat, 0))
        excess = credits - counted
        overflow = min(excess, kb.credit_policy.category_caps.get(cat, excess))
        allocations.append((cat, counted, excess, overflow))
    return allocations


def _related_requirements(kb: KnowledgeBase, records: list[Record]) -> tuple[str, ...]:
    """提示引用受影響的要求；判定仍由逐條審查產生。"""
    codes = {r.code for r in records}
    categories = {r.category for r in records if r.category}
    dept = any((r.code in kb.courses and kb.courses[r.code].counts_as == "dept_elective")
               or (r.code not in kb.courses and not r.category and r.code.startswith(kb.dept_prefixes))
               for r in records)
    return tuple(req.id for req in kb.requirements
                 if req.kind == "total_credits" or codes.intersection(req.courses)
                 or (req.kind == "credits_from" and any(
                     r.code in kb.courses and (not req.courses or r.code in req.courses)
                     and (req.course_type is None or kb.courses[r.code].type == req.course_type)
                     for r in records))
                 or (req.kind == "dept_elective_credits" and dept)
                 or any(kb.in_category(cat, target) for cat in categories for target in req.categories))


def _detect_flags(kb: KnowledgeBase, t: Transcript,
                  results: list[RequirementResult]) -> tuple[list[Flag], list[Action]]:
    flags: list[Flag] = []
    actions: list[Action] = []
    for cat, _, _, overflow in _category_allocation(kb, t):
        if overflow <= 0:
            continue
        records = [r for r in t.categorized if root_category(kb, r.category) == cat]
        ids = _related_requirements(kb, records)
        codes = tuple(sorted({r.code for r in records}))
        flags.append(Flag(OVERFLOW, f"{cat}有 {overflow} 學分可納入外系學分池，仍受外系總上限與人工確認限制",
                          ids, codes, overflow))
        actions.append(Action(MANUAL_RECOGNITION, f"確認{cat}超修學分的人工認列", requirement_ids=ids,
                              courses=codes, from_flag=OVERFLOW))
    for code in sorted({r.code for r in t.repeated}):
        extra = [r for r in t.repeated if r.code == code]
        credits = sum(kb.courses[code].credits if code in kb.courses else r.credits for r in extra)
        ids = _related_requirements(kb, extra)
        flags.append(Flag(REPEATABLE, f"{code}允許重複修習，第二次起已按規則採計 {credits} 學分，需確認校務系統認列",
                          ids, (code,), credits))
        actions.append(Action(MANUAL_RECOGNITION, f"確認 {code} 重複修習認列", requirement_ids=ids,
                              courses=(code,), from_flag=REPEATABLE))
    missing = {code for result in results for code in result.missing}
    for code in sorted(missing):
        course = kb.courses[code]
        alternatives = tuple(c for c in course.replacement_courses if c in t.passed)
        if not course.discontinued or not alternatives:
            continue
        ids = tuple(r.requirement.id for r in results if code in r.missing)
        codes = (code,) + alternatives
        flags.append(Flag(SUBSTITUTION_PENDING,
                          f"{code}已停開，已通過替代課程{'、'.join(alternatives)}，尚待辦理抵修；原要求仍列缺課",
                          ids, codes, course.credits))
        actions.append(Action(SUBSTITUTION, f"確認並辦理 {code} 抵修", course.substitution_office,
                              ids, codes, SUBSTITUTION_PENDING))
    for r in t.duplicate_review:
        actions.append(Action(MANUAL_RECOGNITION, f"確認 {r.code}（{r.term}）重複通過是否採計",
                              requirement_ids=_related_requirements(kb, [r]), courses=(r.code,)))
    return flags, actions


def audit(kb: KnowledgeBase, records: list[Record], decisions: Decisions | None = None,
          english_passed: bool | None = None) -> AuditReport:
    """english_passed：語言中心英檢審核結果（名單提供）；None 表示不知道。"""
    t = classify(kb, records, decisions)
    breakdown = credit_breakdown(kb, t)
    total = sum(breakdown.values())
    consumed: Counter[str] = Counter()
    results = [_evaluate(kb, req, t, consumed, total, english_passed) for req in kb.requirements]

    # 類別不明的紀錄可能改變其他規則結果，所以優先人工判斷；
    # 待確認的外部條件（英檢）補不了其他缺漏，所以有明確缺漏時直接 Fail
    if t.review:
        status = Status.MANUAL_REVIEW
    elif not all(r.satisfied for r in results if not r.pending):
        status = Status.FAIL
    elif any(r.pending for r in results):
        status = Status.MANUAL_REVIEW
    elif t.dept_unverified:
        status = Status.WARNING
    else:
        status = Status.PASS

    flags, actions = _detect_flags(kb, t, results)
    return AuditReport(status, results, t, total, breakdown, flags, actions)


def reasons(report: AuditReport) -> list[str]:
    """批次清單上的一行原因：需人工判斷的紀錄、未符合的規則（待確認的另外標示）、未核對的本系課。"""
    t = report.transcript
    out = [f"{r.requirement.id} {r.requirement.title}" + ("（待確認）" if r.pending else "")
           for r in report.results if not r.satisfied]
    if t.review:
        out.insert(0, f"{len(t.review)} 筆紀錄需人工判斷")
    if t.dept_unverified and not out:
        out.append(f"{len(t.dept_unverified)} 門本系選修未在規則庫核對")
    return out


def _evaluate(kb: KnowledgeBase, req: Requirement, t: Transcript, consumed: Counter[str],
              total: int, english_passed: bool | None = None) -> RequirementResult:
    # 排他採計以修習次數扣除；同課號的另一次合法修習仍可供後續學分規則使用。
    available = list(t.passed)
    if req.exclusive:
        remaining = consumed.copy()
        available = []
        for code in t.passed:
            if remaining[code]:
                remaining[code] -= 1
            else:
                available.append(code)
    cite = kb.cite(req.source)

    if req.kind == "all_of":
        used = [c for c in req.courses if c in available]
        missing = [c for c in req.courses if c not in used]
        result = RequirementResult(req, cite, not missing, used, missing, have=len(used), need=len(req.courses))

    elif req.kind == "taken_all":  # 必選課：有修過即可，不需及格；學分另依 counts_as 計入選修
        used = [c for c in req.courses if c in t.attempted]
        missing = [c for c in req.courses if c not in used]
        result = RequirementResult(req, cite, not missing, used, missing, have=len(used), need=len(req.courses))

    elif req.kind == "dept_elective_credits":
        have = dept_elective_credits(kb, t)
        result = RequirementResult(req, cite, have >= req.min_credits, [], have=have, need=req.min_credits)

    elif req.kind == "english_threshold":  # 英檢通過，或指定類別（進修英文）及格達一定學分
        matched = [r for r in t.categorized if any(kb.in_category(r.category, c) for c in req.categories)]
        have = sum(r.credits for r in matched)
        satisfied = english_passed is True or have >= req.min_credits
        result = RequirementResult(req, cite, satisfied, [r.code for r in matched], have=have,
                                   need=req.min_credits, pending=not satisfied and english_passed is None)

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
        ok, detail = have >= need, ""
        subs = [c for c, parent in kb.categories.items() if parent in req.categories]
        if req.kind == "category_count" and subs:  # 例如通識核心列出已修的領域
            found = sorted({next((s for s in subs if kb.in_category(r.category, s)), "類別未細分")
                            for r in matched})
            detail = "已修：" + "、".join(found) if found else ""
        if req.name_contains:
            named = sum(req.name_contains in r.name for r in matched)
            ok = ok and named >= req.name_min
            detail = f"{req.name_contains} {named}／{req.name_min}"
        result = RequirementResult(req, cite, ok, used, have=have, need=need, detail=detail)

    elif req.kind == "total_credits":
        result = RequirementResult(req, cite, total >= req.min_credits, available,
                                   have=total, need=req.min_credits)

    else:
        raise ValueError(f"未知的規則種類：{req.kind}（{req.id}）")

    if req.exclusive:
        consumed.update(result.used)
    return result
