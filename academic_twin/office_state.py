"""系辦端工作狀態存檔：伺服器重開後，名單、修課紀錄、解析佇列、待確認草稿與批次放行都能接續。（A 組）

- 存在 data/office_state/state.json（不進版控：含修課紀錄與草稿內容）。
- 每次狀態改變就整份覆寫（先寫暫存檔再取代，寫到一半當機也不會留下半份檔案）。
- PDF 不存：成功解析後本來就丟棄；重開時還在排隊或解析中的工作改為失敗，請承辦人重新上傳。
- 存檔損毀或版本不符時略過並回報原因，伺服器照常以示例資料啟動，不會因此無法開啟。
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import dashboard, pdf_text
from .records import REQUIRED
from .roster import Roster

ROOT = Path(__file__).parent.parent
STATE_DIR = ROOT / "data" / "office_state"
VERSION = 1
_LOCK = threading.Lock()


def records_csv(students: dict) -> str:
    """修課紀錄（學號 → Record 清單）轉回 CSV；格式與上傳的修課紀錄相同。"""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=REQUIRED + ["category"], lineterminator="\n")
    writer.writeheader()
    for sid, recs in students.items():
        for r in recs:
            writer.writerow({"student_id": sid, "code": r.code, "name": r.name, "credits": r.credits,
                             "grade": f"{r.grade:g}", "term": r.term, "category": r.category})
    return out.getvalue()


def roster_csv(roster: Roster, ids: list[str] | None = None, cohorts: dict[str, int] | None = None) -> str:
    """名單轉回 CSV（保留順序與重複；學號空白的列不保留）。"""
    cohorts = roster.cohorts if cohorts is None else cohorts
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["student_id", "english_passed", "cohort"])
    for sid in roster.ids if ids is None else ids:
        english = roster.english_passed.get(sid)
        writer.writerow([sid, "" if english is None else ("Y" if english else "N"), cohorts.get(sid, "")])
    return out.getvalue()


def extraction_to_dict(ext: dashboard.Extraction) -> dict:
    return asdict(ext)  # pages（PdfText）一併轉成 dict，確認時才能照常核對原文


def extraction_from_dict(data: dict) -> dashboard.Extraction:
    data = dict(data)
    pages = data.pop("pages", None)
    ext = dashboard.Extraction(**data)
    ext.pages = pdf_text.PdfText.from_dict(pages) if pages is not None else None
    return ext


def save(state: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "state.json"
    tmp = path.with_suffix(".tmp")
    body = json.dumps({"version": VERSION, "saved_at": datetime.now().isoformat(timespec="seconds"), **state},
                      ensure_ascii=False)
    with _LOCK:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)


def load(directory: Path) -> tuple[dict | None, str]:
    """回傳 (狀態, 略過原因)；沒有存檔時為 (None, "")。"""
    path = directory / "state.json"
    if not path.exists():
        return None, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"教務處端存檔無法讀取，已略過：{e}"
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return None, "教務處端存檔版本不符，已略過"
    return data, ""


def clear(directory: Path) -> None:
    with _LOCK:
        (directory / "state.json").unlink(missing_ok=True)
