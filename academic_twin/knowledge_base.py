"""Curriculum Knowledge Base：把應修科目表／規章（JSON）載入成結構化資料。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Course:
    code: str
    name: str
    credits: int
    type: str  # required / elective / general / selective(必選)
    terms: tuple[str, ...]
    prereqs: tuple[str, ...]  # 正式擋修
    soft_prereqs: tuple[str, ...] = ()  # 建議先修（非擋修，排課時仍遵守）
    field: str | None = None


@dataclass(frozen=True)
class Source:
    doc: str
    ref: str


@dataclass(frozen=True)
class Requirement:
    id: str
    kind: str  # all_of / n_of / credits_from / category_credits / category_count / total_credits
    title: str
    source: Source
    courses: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    n: int = 0
    min_credits: int = 0
    course_type: str | None = None
    # 預設一門課只能計入一條規則；總學分與類別規則例外
    exclusive: bool = True


@dataclass(frozen=True)
class CreditPolicy:
    """總學分怎麼算：外系學分上限、各類別超修可認列的上限、完全不採計的類別。"""
    outside_cap: int | None = None
    category_caps: dict[str, int] = field(default_factory=dict)
    excluded: tuple[str, ...] = ()
    source: Source | None = None


@dataclass
class KnowledgeBase:
    program: str
    cohort: int
    passing_grade: int
    dept_prefixes: tuple[str, ...]
    max_credits_per_term: int
    sources: dict[str, str]
    courses: dict[str, Course]
    requirements: list[Requirement] = field(default_factory=list)
    categories: dict[str, str] = field(default_factory=dict)  # 類別 → 上層類別（如 通識核心 → 通識）
    credit_policy: CreditPolicy = field(default_factory=CreditPolicy)
    assumptions: list[str] = field(default_factory=list)

    def cite(self, source: Source) -> str:
        return f"{self.sources.get(source.doc, source.doc)} {source.ref}"

    def in_category(self, category: str, target: str) -> bool:
        """category 是否屬於 target（含上層類別）。"""
        while category:
            if category == target:
                return True
            category = self.categories.get(category, "")
        return False


def load(path: str | Path) -> KnowledgeBase:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    courses = {
        c["code"]: Course(
            code=c["code"],
            name=c["name"],
            credits=c["credits"],
            type=c["type"],
            terms=tuple(c.get("terms", [])),
            prereqs=tuple(c.get("prereqs", [])),
            soft_prereqs=tuple(c.get("soft_prereqs", [])),
            field=c.get("field"),
        )
        for c in raw["courses"]
    }

    requirements = []
    for r in raw["requirements"]:
        kind = r["kind"]
        requirements.append(
            Requirement(
                id=r["id"],
                kind=kind,
                title=r["title"],
                source=Source(**r["source"]),
                courses=tuple(r.get("courses", [])),
                categories=tuple(r.get("categories", [])),
                n=r.get("n", 0),
                min_credits=r.get("min_credits", 0),
                course_type=r.get("course_type"),
                exclusive=r.get("exclusive", kind in ("all_of", "n_of", "credits_from")),
            )
        )

    policy_raw = raw.get("credit_policy", {})
    policy = CreditPolicy(
        outside_cap=policy_raw.get("outside_cap"),
        category_caps=policy_raw.get("category_caps", {}),
        excluded=tuple(policy_raw.get("excluded", [])),
        source=Source(**policy_raw["source"]) if "source" in policy_raw else None,
    )

    kb = KnowledgeBase(
        program=raw["program"],
        cohort=raw["cohort"],
        passing_grade=raw.get("passing_grade", 60),
        dept_prefixes=tuple(raw.get("dept_prefixes", [])),
        max_credits_per_term=raw.get("max_credits_per_term", 25),
        sources=raw.get("sources", {}),
        courses=courses,
        requirements=requirements,
        categories=raw.get("categories", {}),
        credit_policy=policy,
        assumptions=raw.get("assumptions", []),
    )
    _validate(kb)
    return kb


def _validate(kb: KnowledgeBase) -> None:
    """規則庫自我檢查：引用到不存在的課號／類別就直接報錯，避免規則悄悄失效。"""
    errors = []
    for c in kb.courses.values():
        for p in c.prereqs + c.soft_prereqs:
            if p not in kb.courses:
                errors.append(f"{c.code} 的先修 {p} 不在課程清單")
    for r in kb.requirements:
        for code in r.courses:
            if code not in kb.courses:
                errors.append(f"{r.id} 引用的 {code} 不在課程清單")
        for cat in r.categories:
            if cat not in kb.categories:
                errors.append(f"{r.id} 引用的類別 {cat} 未定義")
        if r.source.doc not in kb.sources:
            errors.append(f"{r.id} 的來源 {r.source.doc} 未定義")
    policy = kb.credit_policy
    for cat in list(policy.category_caps) + list(policy.excluded):
        if cat not in kb.categories:
            errors.append(f"學分認列規則引用的類別 {cat} 未定義")
    if errors:
        raise ValueError("規則庫有誤：\n" + "\n".join(errors))
