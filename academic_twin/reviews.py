"""人工覆核紀錄：系辦對無法自動判定的修課紀錄做的決定。

只增不改：每次決定或撤銷都追加一筆，保留誰、何時、依哪個規則版本、為什麼。
同一學生同一課號同一學期以最新一筆為準；最新一筆是撤銷就視為沒有決定。
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .rule_engine import CATEGORY, DEPT_ELECTIVE, EXCLUDE, OUTSIDE, Decision, Decisions

REVOKE = "revoke"
ACTIONS = {DEPT_ELECTIVE: "本系選修", OUTSIDE: "外系認列", EXCLUDE: "不採計", CATEGORY: "歸入類別", REVOKE: "撤銷決定"}


@dataclass(frozen=True)
class ReviewEntry:
    student_id: str
    code: str
    term: str
    action: str
    category: str
    reviewer: str
    note: str
    decided_at: str
    rule_version: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.student_id, self.code, self.term

    @property
    def label(self) -> str:
        return f"歸入「{self.category}」" if self.action == CATEGORY else ACTIONS[self.action]


class ReviewStore:
    def __init__(self, path: Path | None):
        """path 為 None 時只存在記憶體（測試用）。"""
        self.path = path
        self._lock = threading.Lock()
        self.entries: list[ReviewEntry] = []
        if path and path.exists():
            self.entries = [ReviewEntry(**e) for e in json.loads(path.read_text(encoding="utf-8"))]

    def add(self, student_id: str, code: str, term: str, action: str, reviewer: str, rule_version: str,
            category: str = "", note: str = "", now: datetime | None = None) -> ReviewEntry:
        if action not in ACTIONS:
            raise ValueError(f"未知的覆核決定：{action}")
        if action == CATEGORY and not category:
            raise ValueError("歸入類別時需指定類別")
        if not reviewer.strip():
            raise ValueError("請填寫覆核人")
        entry = ReviewEntry(student_id, code, term, action, category if action == CATEGORY else "",
                            reviewer.strip(), note.strip(),
                            (now or datetime.now()).isoformat(timespec="seconds"), rule_version)
        with self._lock:
            self.entries.append(entry)
            self._save()
        return entry

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(e) for e in self.entries], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def current(self, student_id: str) -> dict[tuple[str, str], ReviewEntry]:
        """該生每筆紀錄目前有效的決定（不含已撤銷）。"""
        latest: dict[tuple[str, str], ReviewEntry] = {}
        for e in self.entries:
            if e.student_id == student_id:
                latest[(e.code, e.term)] = e
        return {k: e for k, e in latest.items() if e.action != REVOKE}

    def decisions(self, student_id: str) -> Decisions:
        return {k: Decision(e.action, e.category) for k, e in self.current(student_id).items()}

    def history(self, student_id: str) -> list[ReviewEntry]:
        return [e for e in self.entries if e.student_id == student_id]
