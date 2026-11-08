"""學生端 API：學業分身試算、修課路徑與 What-if、課程影響範圍。（B 組）"""
from __future__ import annotations

import json

from .ask import parse as parse_question, term_labels
from .planner import Plan, Scenario, Term, explain_delay, plan
from .rule_engine import AuditReport, Record, audit

# 本學期修課：學校選課系統沒有公開介接，改由學生自行輸入；只用於試算，伺服器不保存
MAX_CURRENT_COURSES = 15


class StudentMixin:
    """需搭配 api.CoreService 使用（self.kb、self.graph、self._report…）。"""

    def twin_ids(self) -> list[str]:
        """可試算的學生代號；身分驗證前暫時列出全部，之後改為只回傳登入者本人。"""
        return sorted(self.students)

    def twin(self, sid: str) -> dict:
        """學生端看到的審查結果：與系辦端同一份判定，標為試算，不含覆核歷程（承辦人內部紀錄）。"""
        data = self.student(sid, trial=True)
        del data["review_history"]
        return data

    def course_impact(self, code: str) -> dict:
        if code not in self.kb.courses:
            raise KeyError(f"規則庫沒有 {code}")
        return {
            **self.course(code),
            "ancestors": [self.course(c) for c in self.graph.ancestors(code)],
            "descendants": [self.course(c) for c in self.graph.descendants(code)],
            "soft": any(self.graph.is_soft(p, code) for p in self.graph.parents(code)),
        }

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

    # ---------- 本學期修課（試算） ----------
    def _current_records(self, sid: str, current) -> list[tuple[Record, bool]]:
        """把學生輸入的本學期課程轉成 (紀錄, 是否算進去)。

        current 為 JSON 字串或 list，每項 {code, name, credits, category, counted}。
        規則庫有的課，課名與學分以規則庫為準；其他課（通識、外系）用學生填的課名、學分、類別。
        """
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except json.JSONDecodeError as e:
                raise ValueError(f"本學期修課格式錯誤：{e}") from e
        if not isinstance(current, list):
            raise ValueError("本學期修課應為清單")
        if len(current) > MAX_CURRENT_COURSES:
            raise ValueError(f"本學期修課最多 {MAX_CURRENT_COURSES} 門")
        kb = self.ruleset(sid).kb
        term = str(self._next_term(sid))
        passed = set(self._report(sid).passed_courses)
        result: list[tuple[Record, bool]] = []
        seen: set[str] = set()
        for item in current:
            if not isinstance(item, dict):
                raise ValueError("本學期修課每一項應為物件")
            code = str(item.get("code") or "").strip()
            if not code or code in seen:
                raise ValueError(f"本學期修課課號空白或重複：{code or '（空白）'}")
            seen.add(code)
            if code in passed:
                raise ValueError(f"{code} 已經修過並通過，不用列入本學期")
            category = str(item.get("category") or "").strip()
            if category and category not in kb.categories:
                raise ValueError(f"類別「{category}」不在規則庫")
            if code in kb.courses:
                course = kb.courses[code]
                name, credits = course.name, course.credits
            else:
                name = str(item.get("name") or "").strip()
                try:
                    credits = int(item.get("credits"))
                except (TypeError, ValueError):
                    credits = -1
                if not name or not 0 <= credits <= 12:
                    raise ValueError(f"{code}：請填課名與 0–12 的學分")
            result.append((Record(code, name, credits, kb.passing_grade, term, category), item.get("counted") is not False))
        return result

    def _trial_report(self, sid: str, current=None) -> tuple[AuditReport, Term, dict | None]:
        """規劃用的審查結果與起始學期。

        學生有輸入本學期修課時，勾選的課假設及格，交給審查引擎重新審查（學生端不自行計算），
        規劃從下一學期開始；未勾選的課（打算停修、可能不及格）視同沒修，由規劃器重新排入。
        正式審查結果（twin、系辦端）不受影響。
        """
        start = self._next_term(sid)
        entries = self._current_records(sid, current) if current else []
        if not entries:
            return self._report(sid), start, None
        kb = self.ruleset(sid).kb
        assumed = [r for r, counted in entries if counted]
        report = audit(kb, self.students[sid] + assumed, self.reviews.decisions(sid),
                       self._roster_parsed.english_passed.get(sid))
        return report, start.next(), {
            "term": str(start),
            "courses": [{"code": r.code, "name": r.name, "credits": r.credits, "category": r.category,
                         "counted": counted} for r, counted in entries],
            "official_total_credits": self._report(sid).total_credits,
            "trial_total_credits": report.total_credits,
        }

    def simulate(self, sid: str, skip: list[str] | None = None, away: list[str] | None = None,
                 away_credits: int = 15, current=None) -> dict:
        """current：學生輸入的本學期修課（見 _current_records）；勾選的課假設及格。"""
        report, start, current = self._trial_report(sid, current)
        base = plan(self.kb, self.graph, report, start)
        result = {"start": str(start), "current": current, "base": self._plan_dict(base), "scenario": None}

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

    def ask(self, sid: str, question: str, current=None) -> dict:
        """自然語言 What-if：AI 把問題轉成 simulate 參數，回傳待學生確認的提案，不在這裡模擬。"""
        report, start, _ = self._trial_report(sid, current)
        base = plan(self.kb, self.graph, report, start)
        planned = {c: str(t) for t, codes in base.terms.items() for c in codes}
        try:
            cohort = self.ruleset(sid).kb.cohort
        except KeyError:
            cohort = None
        return parse_question(question, {c: self.kb.courses[c].name for c in planned}, planned,
                              term_labels(str(start), cohort))

