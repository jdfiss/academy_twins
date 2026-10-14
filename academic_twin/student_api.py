"""學生端 API：學業分身試算、修課路徑與 What-if、課程影響範圍。（B 組）"""
from __future__ import annotations

from .planner import Plan, Scenario, Term, explain_delay, plan


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
