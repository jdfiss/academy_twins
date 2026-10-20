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
    # 通過後學分計入哪一塊：None = 院系必修；dept_elective = 本系選修；outside = 外系選修（必選課用）
    counts_as: str | None = None
    # 停開與替代關係只用來提示待辦，不代表學生已獲准抵修。
    discontinued: bool = False
    replacement_courses: tuple[str, ...] = ()
    substitution_office: str = "待確認"


@dataclass(frozen=True)
class Source:
    doc: str
    ref: str


@dataclass(frozen=True)
class Requirement:
    id: str
    kind: str  # all_of / taken_all / n_of / credits_from / category_credits / category_count /
    #            dept_elective_credits / english_threshold / total_credits
    title: str
    source: Source
    courses: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    n: int = 0
    min_credits: int = 0
    course_type: str | None = None
    # category_count 附加條件：課名含 name_contains 的至少 name_min 門（例如體育須含 2 學期大一體育）
    name_contains: str = ""
    name_min: int = 0
    # 預設一門課只能計入一條規則；總學分與類別規則例外
    exclusive: bool = True
    # 校務系統畢審儀表板的規則代碼（全校共用）與本系章則用語；尚未對照時留空
    system_code: str = ""
    regulation_term: str = ""


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
    # 課號開頭 → 類別（如 CC01 → 人文與思想）；最長的開頭優先
    code_categories: dict[str, str] = field(default_factory=dict)
    credit_policy: CreditPolicy = field(default_factory=CreditPolicy)
    assumptions: list[str] = field(default_factory=list)
    # True：本系課號不在規則庫一律算本系選修（已確認規則）；False：暫算本系選修但標 Warning 待核對
    dept_prefix_is_elective: bool = False
    # 可含不在課程清單的課號（例如依類別採計的體育）；各次修習須能以學期區分。
    repeatable_courses: tuple[str, ...] = ()

    def cite(self, source: Source) -> str:
        return f"{self.sources.get(source.doc, source.doc)} {source.ref}"

    def category_of(self, code: str, category: str = "") -> str:
        """紀錄的類別：依課號推得的類別比紀錄上的更細（或紀錄沒填）時採課號的，否則照紀錄。"""
        prefixes = [p for p in self.code_categories if code.startswith(p)]
        if not prefixes:
            return category
        derived = self.code_categories[max(prefixes, key=len)]
        return derived if not category or self.in_category(derived, category) else category

    def in_category(self, category: str, target: str) -> bool:
        """category 是否屬於 target（含上層類別）。"""
        while category:
            if category == target:
                return True
            category = self.categories.get(category, "")
        return False


def _course_codes(value: object, label: str) -> tuple[str, ...]:
    """清單須明確列出課號，避免把字串拆成字元或誤用布林值。"""
    if (not isinstance(value, list)
            or any(not isinstance(c, str) or not c.strip() or c != c.strip() for c in value)
            or len(value) != len(set(value))):
        raise ValueError(f"規則庫有誤：{label} 必須是不重複的非空白課號陣列")
    return tuple(value)


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
            counts_as=c.get("counts_as"),
            discontinued=c.get("discontinued", False),
            replacement_courses=_course_codes(c.get("replacement_courses", []), f"{c['code']} 的替代課程"),
            substitution_office=c.get("substitution_office", "待確認"),
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
                name_contains=r.get("name_contains", ""),
                name_min=r.get("name_min", 0),
                exclusive=r.get("exclusive", kind in ("all_of", "taken_all", "n_of", "credits_from")),
                system_code=r.get("system_code", ""),
                regulation_term=r.get("regulation_term", ""),
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
        code_categories=raw.get("code_categories", {}),
        credit_policy=policy,
        assumptions=raw.get("assumptions", []),
        dept_prefix_is_elective=raw.get("dept_prefix_is_elective", False),
        repeatable_courses=_course_codes(raw.get("repeatable_courses", []), "repeatable_courses"),
    )
    _validate(kb)
    return kb


def _validate(kb: KnowledgeBase) -> None:
    """規則庫自我檢查：引用到不存在的課號／類別就直接報錯，避免規則悄悄失效。"""
    errors = []
    for c in kb.courses.values():
        if not isinstance(c.discontinued, bool):
            errors.append(f"{c.code} 的 discontinued 必須為布林值")
        if not isinstance(c.substitution_office, str) or not c.substitution_office.strip():
            errors.append(f"{c.code} 的抵修承辦單位不得空白；未確認時請填「待確認」")
        if c.replacement_courses and not c.discontinued:
            errors.append(f"{c.code} 尚未標為停開，不可設定停開替代課程")
        for replacement in c.replacement_courses:
            if replacement == c.code or replacement not in kb.courses:
                errors.append(f"{c.code} 的替代課程 {replacement} 必須存在且不同於原課程")
        if c.counts_as not in (None, "dept_elective", "outside"):
            errors.append(f"{c.code} 的 counts_as「{c.counts_as}」不是 dept_elective 或 outside")
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
    for prefix, cat in kb.code_categories.items():
        if cat not in kb.categories:
            errors.append(f"課號開頭 {prefix} 對應的類別 {cat} 未定義")
    policy = kb.credit_policy
    for cat in list(policy.category_caps) + list(policy.excluded):
        if cat not in kb.categories:
            errors.append(f"學分認列規則引用的類別 {cat} 未定義")
    if errors:
        raise ValueError("規則庫有誤：\n" + "\n".join(errors))
