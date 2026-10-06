"""Course Graph：先修關係 DAG（正式擋修＋建議先修）。

回答：
- 這門課沒修，後面會卡到哪些課？
- 哪些課是瓶頸（擋住最多後續課、位在最長先修鏈上）？
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from .knowledge_base import KnowledgeBase


@dataclass(frozen=True)
class Bottleneck:
    code: str
    blocked: int  # 直接或間接依賴它的課數
    depth: int  # 從它往下最長的先修鏈長度（含自己）


class CourseGraph:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
        self.children: dict[str, list[str]] = {c: [] for c in kb.courses}
        for course in kb.courses.values():
            for p in self.parents(course.code):
                self.children[p].append(course.code)
        self.order = self._topological_order()
        self.depth = cache(self._depth)

    def parents(self, code: str) -> tuple[str, ...]:
        course = self.kb.courses[code]
        return course.prereqs + course.soft_prereqs

    def is_soft(self, parent: str, child: str) -> bool:
        return parent not in self.kb.courses[child].prereqs

    def _topological_order(self) -> list[str]:
        indegree = {c: len(self.parents(c)) for c in self.kb.courses}
        queue = [c for c, d in indegree.items() if d == 0]
        order = []
        while queue:
            c = queue.pop(0)
            order.append(c)
            for child in self.children[c]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(order) != len(indegree):
            cyclic = sorted(c for c, d in indegree.items() if d > 0)
            raise ValueError(f"先修關係有循環：{'、'.join(cyclic)}")
        return order

    def descendants(self, code: str) -> list[str]:
        """沒修 code 會卡到的所有後續課（依修課順序排列）。"""
        seen: set[str] = set()
        stack = list(self.children[code])
        while stack:
            c = stack.pop()
            if c not in seen:
                seen.add(c)
                stack.extend(self.children[c])
        return [c for c in self.order if c in seen]

    def ancestors(self, code: str) -> list[str]:
        """修 code 之前必須先修完的所有課（依修課順序排列）。"""
        seen: set[str] = set()
        stack = list(self.parents(code))
        while stack:
            c = stack.pop()
            if c not in seen:
                seen.add(c)
                stack.extend(self.parents(c))
        return [c for c in self.order if c in seen]

    def _depth(self, code: str) -> int:
        return 1 + max((self.depth(c) for c in self.children[code]), default=0)

    def bottlenecks(self, top: int = 5) -> list[Bottleneck]:
        ranked = sorted(
            (Bottleneck(c, len(self.descendants(c)), self.depth(c)) for c in self.kb.courses),
            key=lambda b: (b.depth, b.blocked),
            reverse=True,
        )
        return [b for b in ranked if b.blocked][:top]
