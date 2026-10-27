"""規則啟用前的影響預覽（差異測試）：找出新、舊規則判定不同的虛構學生。（A 組）

    python -m academic_twin.rule_diff <舊規則庫.json> <新規則庫.json>
    python -m academic_twin.rule_diff --draft <已確認草稿編號>     # 目前啟用版本 vs 套用草稿後

承辦人看不懂 JSON 規則，但看得懂案例：本模組依兩版規則的條件產生虛構修課紀錄
（門檻剛好、差 1 學分、少 1 門、修過但不及格、停開改修替代課，再加固定種子的隨機組合），
交給現有規則引擎 rule_engine.audit 分別以兩版規則審查，列出判定不同的最小代表案例。

- 判定只在規則引擎：本模組不計算學分、不判斷是否符合；補足總學分也是先由引擎算出總學分再補差額。
- 全部案例為虛構（課號 ZZ 開頭或規則庫內課號、固定學期），不讀取任何學生資料。
- 只在搜尋範圍內找差異：找不到時只能說「在搜尋範圍內未發現差異」，不代表兩版規則必然判定相同。
- 只預覽，不啟用；啟用仍須承辦人在規則版本流程明確操作（rule_versions.activate）。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from . import knowledge_base, rule_draft, rule_versions
from .knowledge_base import KnowledgeBase
from .rule_engine import AuditReport, Record, audit

TERM = "113-1"
SEED = 114
RANDOM_CASES = 150
MAX_RANDOM_EDITS = 4
LABEL = "虛構案例，由規則引擎依兩版規則各審查一次"
BASE_CASE = "每個案例都從「完整修畢的虛構學生」改起：每條要求剛好達到兩版中較高的門檻，總學分以虛構選修補足"


@dataclass(frozen=True)
class Edit:
    """相對於「完整修畢學生」的一項修改；可單獨拿掉，用來縮成最小案例。"""
    kind: str  # drop / fail / substitute / credits / count / dept / total
    codes: tuple[str, ...] = ()
    category: str = ""
    value: int = 0
    named: int = 0
    label: str = ""


class _Cases:
    """依兩版規則條件產生虛構修課紀錄；只組資料，不判定。"""

    def __init__(self, old: KnowledgeBase, new: KnowledgeBase):
        self.old, self.new = old, new
        self.courses = {**new.courses, **old.courses}
        reqs = list(old.requirements) + list(new.requirements)
        required: dict[str, None] = {}
        self.taken_all: set[str] = set()
        for r in reqs:
            codes = r.courses
            if r.kind == "credits_from" and not codes:
                kb = old if r in old.requirements else new
                codes = tuple(c for c, course in kb.courses.items()
                              if r.course_type is None or course.type == r.course_type)
            required.update(dict.fromkeys(codes))
            if r.kind == "taken_all":
                self.taken_all.update(r.courses)
        self.required = list(required)

        # 每個類別、本系選修、總學分在兩版規則中出現的門檻；完整學生取較高者。
        self.cat_thresholds: dict[str, set[int]] = {}
        self.count_thresholds: dict[str, set[int]] = {}
        self.name_rule: dict[str, tuple[str, set[int]]] = {}
        self.dept_thresholds: set[int] = set()
        self.total_thresholds: set[int] = set()
        for r in reqs:
            if r.kind in ("category_credits", "english_threshold") and r.categories:
                self.cat_thresholds.setdefault(r.categories[0], set()).add(r.min_credits)
            elif r.kind == "category_count" and r.categories:
                self.count_thresholds.setdefault(r.categories[0], set()).add(r.n)
                if r.name_contains:
                    self.name_rule.setdefault(r.categories[0], (r.name_contains, set()))[1].add(r.name_min)
            elif r.kind == "dept_elective_credits":
                self.dept_thresholds.add(r.min_credits)
            elif r.kind == "total_credits":
                self.total_thresholds.add(r.min_credits)

        self.pass_grade = max(80, old.passing_grade, new.passing_grade)
        self.fail_grade = max(0, min(old.passing_grade, new.passing_grade) - 1)
        self.dept_prefix = next((p for p in old.dept_prefixes if p in new.dept_prefixes), "")
        self.dept_filler = bool(self.dept_prefix) and old.dept_prefix_is_elective and new.dept_prefix_is_elective
        taken = set(self.courses)
        prefixes = (old.dept_prefixes + new.dept_prefixes + tuple(old.code_categories)
                    + tuple(new.code_categories))
        self.synthetic = next(p for p in ("ZZ", "ZY", "ZX", "ZW", "QQ")
                              if not any(p.startswith(x) or x.startswith(p) for x in prefixes)
                              and not any(c.startswith(p) for c in taken))
        self.dept_codes = [code for code in (f"{self.dept_prefix}9{i:03d}" for i in range(1000))
                           if code not in taken] if self.dept_prefix else []

    def baseline(self) -> dict:
        """完整修畢學生：每條要求都剛好達到兩版中較高的門檻。"""
        return {
            "courses": {c: self.pass_grade for c in self.required},
            "cats": {cat: max(t) for cat, t in self.cat_thresholds.items()},
            "counts": {cat: (max(t), max(self.name_rule[cat][1]) if cat in self.name_rule else 0)
                       for cat, t in self.count_thresholds.items()},
            "dept": max(self.dept_thresholds, default=0),
            "total": max(self.total_thresholds, default=0),
            "filler": "dept" if self.dept_filler else "outside",
        }

    def boundary_edits(self) -> list[Edit]:
        edits = []
        for code in self.required:
            edits.append(Edit("drop", (code,), label=f"未修 {self._course(code)}"))
            if code in self.taken_all:
                edits.append(Edit("fail", (code,), label=f"{self._course(code)} 修過但不及格"))
        for r in list(self.old.requirements) + list(self.new.requirements):
            if r.kind == "n_of" and len(r.courses) >= r.n:  # 清單中剛好修 n 門、n-1 門
                for keep in (r.n, r.n - 1):
                    if 0 <= keep < len(r.courses):
                        codes = r.courses[keep:]
                        edits.append(Edit("drop", codes, label=f"{r.id} 清單只修 {keep} 門（未修 "
                                          + "、".join(codes) + "）"))
        for kb in (self.old, self.new):
            for course in kb.courses.values():
                for rep in course.replacement_courses:
                    if course.discontinued and course.code in self.required:
                        edits.append(Edit("substitute", (course.code, rep),
                                          label=f"未修停開課 {self._course(course.code)}，改修替代課 {self._course(rep)}"))
        for cat, thresholds in self.cat_thresholds.items():
            for v in _boundaries(thresholds):
                edits.append(Edit("credits", category=cat, value=v, label=f"{cat} {v} 學分"))
        for cat, thresholds in self.count_thresholds.items():
            name, name_mins = self.name_rule.get(cat, ("", {0}))
            for v in _boundaries(thresholds):
                named = min(v, max(name_mins))
                edits.append(Edit("count", category=cat, value=v, named=named,
                                  label=f"{cat} {v} 門" + (f"（課名含「{name}」{named} 門）" if name else "")))
            if name:
                for m in _boundaries(name_mins):
                    edits.append(Edit("count", category=cat, value=max(thresholds), named=m,
                                      label=f"{cat} {max(thresholds)} 門（課名含「{name}」{m} 門）"))
        if self.dept_prefix:
            for v in _boundaries(self.dept_thresholds):
                edits.append(Edit("dept", value=v, label=f"本系選修 {v} 學分"))
        for v in _boundaries(self.total_thresholds):
            edits.append(Edit("total", value=v, label=f"總學分補到 {v}"))
        return list(dict.fromkeys(edits))

    def _course(self, code: str) -> str:
        c = self.courses.get(code)
        return f"{code} {c.name}（{c.credits} 學分）" if c else code

    def records(self, edits: tuple[Edit, ...]) -> list[Record] | None:
        """套用修改後的修課紀錄；總學分目標達不到（其他紀錄已超過）時回傳 None。"""
        spec = self.baseline()
        spec["courses"] = dict(spec["courses"])
        for e in edits:
            if e.kind == "drop":
                for code in e.codes:
                    spec["courses"].pop(code, None)
            elif e.kind == "fail":
                spec["courses"][e.codes[0]] = self.fail_grade
            elif e.kind == "substitute":
                spec["courses"].pop(e.codes[0], None)
                spec["courses"][e.codes[1]] = self.pass_grade
            elif e.kind == "credits":
                spec["cats"] = {**spec["cats"], e.category: e.value}
            elif e.kind == "count":
                spec["counts"] = {**spec["counts"], e.category: (e.value, e.named)}
            elif e.kind == "dept":
                spec["dept"], spec["filler"] = e.value, "outside"  # 不再用本系選修補總學分
            elif e.kind == "total":
                spec["total"] = e.value
        serial = iter(range(10_000))
        out = [Record(code, self.courses[code].name, self.courses[code].credits, grade, TERM)
               for code, grade in spec["courses"].items()]
        for cat, credits in spec["cats"].items():
            out += [Record(f"{self.synthetic}{next(serial):04d}", f"虛構{cat}課程", c, self.pass_grade, TERM, cat)
                    for c in _chunks(credits)]
        for cat, (n, named) in spec["counts"].items():
            name = self.name_rule.get(cat, ("", set()))[0]
            out += [Record(f"{self.synthetic}{next(serial):04d}", (f"{name}（虛構）" if i < named else f"虛構{cat}"),
                           0, self.pass_grade, TERM, cat) for i in range(n)]
        dept = iter(self.dept_codes)
        out += [Record(next(dept), "虛構本系選修", c, self.pass_grade, TERM) for c in _chunks(spec["dept"])]
        if spec["total"]:
            # 總學分由引擎計算；只依引擎結果補差額，不自行加總。
            gap = spec["total"] - audit(self.old, out).total_credits
            if gap < 0:
                return None if any(e.kind == "total" for e in edits) else out
            if spec["filler"] == "dept":
                out += [Record(next(dept), "虛構本系選修（補總學分）", c, self.pass_grade, TERM) for c in _chunks(gap)]
            else:
                out += [Record(f"{self.synthetic}{next(serial):04d}", "虛構外系選修（補總學分）", c, self.pass_grade, TERM)
                        for c in _chunks(gap)]
        return out


def _boundaries(thresholds: set[int]) -> list[int]:
    """門檻剛好與差 1。"""
    return sorted({v for t in thresholds for v in (t, t - 1) if v >= 0})


def _chunks(credits: int, size: int = 3) -> list[int]:
    return [size] * (credits // size) + ([credits % size] if credits % size else [])


def _key(records: list[Record]) -> tuple:
    return tuple(sorted((r.code, r.name, r.credits, r.grade, r.category) for r in records))


def _state(report: AuditReport) -> dict:
    return {r.requirement.id: r for r in report.results}


def _signature(old: AuditReport, new: AuditReport) -> tuple:
    """判定差異的分組依據：哪些要求的符合／待確認改變，以及整體狀態。

    只存在於一版的要求，另一版視為不限制：新增的要求只有在案例未符合時才算差異。
    """
    a, b = _state(old), _state(new)

    def same(x, y) -> bool:
        return {x, y} <= {"absent", "satisfied"} or x == y

    changed = tuple(sorted(
        (rid, _verdict(a.get(rid)), _verdict(b.get(rid))) for rid in a.keys() | b.keys()
        if not same(_verdict(a.get(rid)), _verdict(b.get(rid)))))
    if changed:
        return changed
    if old.status != new.status:
        return (("status", old.status.value, new.status.value),)
    return ()


def _verdict(result) -> str:
    if result is None:
        return "absent"
    return "pending" if result.pending else "satisfied" if result.satisfied else "unsatisfied"


VERDICT_TEXT = {"absent": "無此要求", "pending": "待確認", "satisfied": "符合", "unsatisfied": "未符合"}


def search(old: KnowledgeBase, new: KnowledgeBase, seed: int = SEED, random_cases: int = RANDOM_CASES) -> dict:
    """以虛構案例比較兩版規則；回傳搜尋數量、依變動要求分組的最小代表案例。"""
    gen = _Cases(old, new)
    seen: dict[tuple, tuple] = {}  # 紀錄 → 差異分組

    def evaluate(edits: tuple[Edit, ...]):
        recs = gen.records(edits)
        if recs is None:
            return None, None, None
        k = _key(recs)
        if k not in seen:
            o, n = audit(old, recs), audit(new, recs)
            seen[k] = (_signature(o, n), o, n)
        sig, o, n = seen[k]
        return sig, recs, (o, n)

    boundary = gen.boundary_edits()
    candidates: list[tuple[Edit, ...]] = [()] + [(e,) for e in boundary]
    rng = random.Random(seed)
    for _ in range(random_cases if boundary else 0):
        k = rng.randint(2, min(MAX_RANDOM_EDITS, len(boundary))) if len(boundary) >= 2 else 1
        candidates.append(tuple(rng.sample(boundary, k)))

    groups: dict[tuple, dict] = {}
    for order, edits in enumerate(candidates):
        sig, recs, reports = evaluate(edits)
        if not sig:
            continue
        # 縮成最小案例：逐一拿掉修改，差異分組不變就不需要那項修改。
        for e in list(edits):
            trial = tuple(x for x in edits if x != e)
            if evaluate(trial)[0] == sig:
                edits = trial
        sig, recs, reports = evaluate(edits)
        rank = (len(edits), len(recs), order)
        group = groups.setdefault(sig, {"found": 0, "best": None})
        group["found"] += 1
        if group["best"] is None or rank < group["best"][0]:
            group["best"] = (rank, edits, recs, reports)

    ordered = sorted(groups.items(), key=lambda kv: (len(kv[0]), kv[0]))  # 單一要求的差異先列
    differences = [_describe(gen, sig, g) for sig, g in ordered]
    changed_reqs = _changed_requirements(old, new)
    observed = {rid for d in differences for rid in d["requirement_ids"]}
    searched = len(seen)
    return {
        "label": LABEL,
        "base_case": BASE_CASE,
        "searched": searched,
        "boundary_cases": len(boundary) + 1,
        "random_cases": len(candidates) - len(boundary) - 1,
        "seed": seed,
        "difference_count": len(differences),
        "differences": differences,
        "changed_requirements": changed_reqs,
        "changed_without_observed_difference": [r for r in changed_reqs if r["id"] not in observed],
        "message": (f"在 {searched} 個虛構案例中找到 {len(differences)} 種判定差異" if differences
                    else f"在搜尋範圍內未發現差異（共 {searched} 個虛構案例）；這不代表兩版規則必然判定相同"),
        "scope": "依兩版規則條件的邊界值（門檻剛好、差 1 學分、少 1 門、修過不及格、停開改修替代課）"
                 f"與固定種子 {seed} 的隨機組合；不含覆核決定、英檢名單結果、重複修習與跨學期情境",
    }


def _changed_requirements(old: KnowledgeBase, new: KnowledgeBase) -> list[dict]:
    a = {r.id: r for r in old.requirements}
    b = {r.id: r for r in new.requirements}
    out = []
    for rid in sorted(a.keys() | b.keys()):
        if a.get(rid) != b.get(rid):
            out.append({"id": rid, "title": (b.get(rid) or a.get(rid)).title,
                        "change": "added" if rid not in a else "removed" if rid not in b else "changed"})
    return out


def _describe(gen: _Cases, sig: tuple, group: dict) -> dict:
    _, edits, recs, (old_report, new_report) = group["best"]
    a, b = _state(old_report), _state(new_report)
    changes = []
    key_codes: dict[str, None] = {}
    for rid, before, after in sig:
        if rid == "status":
            continue
        ra, rb = a.get(rid), b.get(rid)
        title = (rb or ra).requirement.title
        changes.append({"id": rid, "title": title, "old": _result(ra), "new": _result(rb),
                        "reason": f"{rid} {title}：舊規則{_phrase(ra)}，新規則{_phrase(rb)}"})
        for r in (ra, rb):
            if r is not None:
                key_codes.update(dict.fromkeys(r.requirement.courses))
    for e in edits:
        key_codes.update(dict.fromkeys(e.codes))
    grades = {r.code: r.grade for r in recs}
    return {
        "requirement_ids": [c["id"] for c in changes] or ["status"],
        "changes": changes,
        "old_status": old_report.status.value,
        "new_status": new_report.status.value,
        "case": {
            "edits": [e.label for e in edits] or ["每條要求都剛好達到兩版中較高的門檻"],
            "records": len(recs),
            "total_credits": {"old": old_report.total_credits, "new": new_report.total_credits},
            "key_courses": [{"code": c, "name": gen.courses[c].name, "credits": gen.courses[c].credits,
                             "grade": grades.get(c)} for c in key_codes if c in gen.courses],
        },
        "cases_found": group["found"],
    }


def _result(r) -> dict | None:
    if r is None:
        return None
    return {"satisfied": r.satisfied, "pending": r.pending, "have": r.have, "need": r.need,
            "missing": list(r.missing)}


def _phrase(r) -> str:
    if r is None:
        return "沒有這條要求（不限制）"
    return f"{VERDICT_TEXT[_verdict(r)]}（{r.have}／{r.need}）"


def preview_draft(draft_id: str, active: KnowledgeBase, kb_dir: Path | None = None, **options) -> dict:
    """已確認草稿啟用前預覽：目前啟用的規則 vs 以草稿取代正式規則庫要求後的版本（與啟用時相同的組法）。"""
    path = rule_draft._path(draft_id, confirmed=True)
    if not path.is_file():
        raise KeyError("找不到這份已確認草稿")
    draft = json.loads(path.read_text(encoding="utf-8"))
    bases = rule_versions.base_paths(kb_dir)
    if draft["year"] not in bases:
        raise ValueError(f"沒有 {draft['year']} 學年度正式規則庫，無法預覽")
    base = json.loads(bases[draft["year"]].read_text(encoding="utf-8"))
    merged, replaced, added = rule_versions.build(draft, base)
    return {**search(active, knowledge_base.from_dict(merged), **options), "replaced": replaced, "added": added}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="規則啟用前影響預覽：找出新舊規則判定不同的虛構學生")
    parser.add_argument("paths", nargs="*", type=Path, help="舊規則庫 新規則庫")
    parser.add_argument("--draft", help="已確認草稿編號；與該年度目前啟用的規則比較")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    try:
        if args.draft:
            year = json.loads(rule_draft._path(args.draft, True).read_text(encoding="utf-8"))["year"]
            result = preview_draft(args.draft, knowledge_base.load(rule_versions.active_paths()[year]), seed=args.seed)
        elif len(args.paths) == 2:
            result = search(*(knowledge_base.load(p) for p in args.paths), seed=args.seed)
        else:
            parser.error("請給兩個規則庫檔案，或 --draft 草稿編號")
    except (OSError, KeyError, ValueError) as exc:
        print(f"無法預覽：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
