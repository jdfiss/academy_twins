"""規則版本啟用：已確認的 AI 規則草稿 → 新規則版本 → 承辦人明確啟用，可隨時還原。（A 組）

    python -m academic_twin.rule_versions                         # 各年度目前啟用的版本與紀錄
    python -m academic_twin.rule_versions activate <草稿編號> --by 姓名
    python -m academic_twin.rule_versions revert 114 --by 姓名

新版本 = 該年度正式規則庫 data/curriculum_im_<年度>.json，把草稿中的要求依編號取代（或新增）；
其餘要求、課程與採計政策不變。版本檔存在 data/rule_versions/（不進版控），不覆寫正式規則庫檔案；
要成為正式規則庫，仍須以 PR 修改 curriculum_im_<年度>.json 並經審查。
啟用與還原都寫入 activations.jsonl，只增不改；目前啟用的版本由紀錄推得。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import knowledge_base, rule_draft

ROOT = Path(__file__).parent.parent
KB_PATH = ROOT / "data" / "curriculum_im_114.json"
VERSIONS_DIR = ROOT / "data" / "rule_versions"
LOG_NAME = "activations.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def base_paths(kb_dir: Path | None = None) -> dict[int, Path]:
    """各年度的正式規則庫檔案。"""
    kb_dir = kb_dir or KB_PATH.parent
    paths: dict[int, Path] = {}
    for path in sorted(kb_dir.glob("curriculum_im_*.json")):
        cohort = json.loads(path.read_text(encoding="utf-8"))["cohort"]
        if cohort in paths:
            raise ValueError(f"{path.name} 與 {paths[cohort].name} 都是 {cohort} 學年度規則庫")
        paths[cohort] = path
    return paths


def history(versions_dir: Path | None = None) -> list[dict]:
    versions_dir = versions_dir or VERSIONS_DIR
    log = versions_dir / LOG_NAME
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def active_paths(kb_dir: Path | None = None, versions_dir: Path | None = None) -> dict[int, Path]:
    """各年度目前啟用的規則庫：最後一筆紀錄為啟用時用該版本檔，否則用正式規則庫檔案。"""
    versions_dir = versions_dir or VERSIONS_DIR
    paths = base_paths(kb_dir)
    for entry in history(versions_dir):
        if entry["year"] not in paths:
            continue
        if entry["type"] == "activate":
            paths[entry["year"]] = versions_dir / entry["file"]
        elif entry["type"] == "revert":
            paths[entry["year"]] = base_paths(kb_dir)[entry["year"]]
    return paths


def startup_paths(kb_path: Path | None = None, versions_dir: Path | None = None) -> tuple[Path, list[Path]]:
    """（預設年度規則庫, 其他年度規則庫）；網頁啟動時使用，學生依名單年度取用。"""
    kb_path = kb_path or KB_PATH
    paths = active_paths(kb_path.parent, versions_dir)
    default = json.loads(kb_path.read_text(encoding="utf-8"))["cohort"]
    return paths.pop(default), [paths[year] for year in sorted(paths)]


def confirmed_drafts(draft_dir: Path | None = None) -> list[dict]:
    """已確認、可啟用的草稿摘要；不含引文或規則內容。"""
    draft_dir = draft_dir or rule_draft.DRAFT_DIR
    out = []
    for path in sorted(draft_dir.glob("*.confirmed.json")):
        draft = json.loads(path.read_text(encoding="utf-8"))
        conf = draft.get("confirmation", {})
        out.append({"draft_id": draft["draft_id"], "year": draft["year"], "reviewer": conf.get("reviewer"),
                    "confirmed_at": conf.get("confirmed_at"),
                    "requirement_ids": [r.get("id") for r in draft.get("requirements", [])]})
    return sorted(out, key=lambda d: d["confirmed_at"] or "", reverse=True)


def build(draft: dict, base: dict) -> tuple[dict, list[str], list[str]]:
    """以草稿要求取代或新增到正式規則庫；回傳（新規則庫, 取代的編號, 新增的編號）。"""
    merged = copy.deepcopy(base)
    index = {r["id"]: i for i, r in enumerate(merged["requirements"])}
    replaced, added = [], []
    for row in draft["requirements"]:
        rule = rule_draft._rule(row)
        if rule["id"] in index:
            merged["requirements"][index[rule["id"]]] = rule
            replaced.append(rule["id"])
        else:
            merged["requirements"].append(rule)
            added.append(rule["id"])
    knowledge_base.from_dict(merged)  # 與載入正式規則庫相同的驗證；有錯丟 ValueError／KeyError
    return merged, replaced, added


def activate(draft_id: str, activated_by: str, acknowledge: bool = False, kb_dir: Path | None = None,
             versions_dir: Path | None = None) -> dict:
    """承辦人明確同意後，以已確認草稿產生並啟用新規則版本。"""
    kb_dir, versions_dir = kb_dir or KB_PATH.parent, versions_dir or VERSIONS_DIR
    if not isinstance(activated_by, str) or not activated_by.strip():
        raise ValueError("請填寫啟用人")
    if acknowledge is not True:
        raise ValueError("請先確認：啟用後該年度學生改以新版本審查，既有覆核會標示建議再確認")
    path = rule_draft._path(draft_id, confirmed=True)
    if not path.is_file():
        raise KeyError("找不到這份已確認草稿；草稿須先由承辦人確認存檔才能啟用")
    draft = json.loads(path.read_text(encoding="utf-8"))
    if draft.get("state") != "confirmed" or not draft.get("confirmation", {}).get("reviewer"):
        raise ValueError("草稿尚未確認，不能啟用")
    year = draft["year"]
    bases = base_paths(kb_dir)
    if year not in bases:
        raise ValueError(f"沒有 {year} 學年度正式規則庫，不能啟用")
    base = json.loads(bases[year].read_text(encoding="utf-8"))
    if rule_draft._hash(base) != draft["confirmation"].get("baseline_sha256"):
        raise ValueError(f"{bases[year].name} 在草稿確認後已變更，請重新草擬或檢查後再確認")
    merged, replaced, added = build(draft, base)

    content = json.dumps({**merged, "_activation": {
        "note": "由已確認 AI 規則草稿產生的規則版本；正式規則庫檔案未變更",
        "base": bases[year].name, "draft_id": draft_id, "replaced": replaced, "added": added,
    }}, ensure_ascii=False, indent=2) + "\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    name = f"{year}-{digest}.json"
    versions_dir.mkdir(parents=True, exist_ok=True)
    if not (versions_dir / name).exists():
        (versions_dir / name).write_text(content, encoding="utf-8")
    entry = {"type": "activate", "year": year, "file": name, "draft_id": draft_id, "replaced": replaced,
             "added": added, "by": activated_by.strip(), "at": _now()}
    _append(versions_dir, entry)
    return entry


def revert(year: int, reverted_by: str, kb_dir: Path | None = None, versions_dir: Path | None = None) -> dict:
    """還原為正式規則庫檔案。"""
    versions_dir = versions_dir or VERSIONS_DIR
    if not isinstance(reverted_by, str) or not reverted_by.strip():
        raise ValueError("請填寫還原人")
    bases = base_paths(kb_dir)
    if year not in bases or active_paths(kb_dir, versions_dir)[year] == bases[year]:
        raise ValueError(f"{year} 學年度目前使用正式規則庫，不需還原")
    entry = {"type": "revert", "year": year, "by": reverted_by.strip(), "at": _now()}
    _append(versions_dir, entry)
    return entry


def _append(versions_dir: Path, entry: dict) -> None:
    with (versions_dir / LOG_NAME).open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="規則版本啟用與還原")
    sub = parser.add_subparsers(dest="command")
    a = sub.add_parser("activate", help="以已確認草稿啟用新規則版本")
    a.add_argument("draft_id")
    a.add_argument("--by", required=True, help="啟用人")
    r = sub.add_parser("revert", help="還原為正式規則庫")
    r.add_argument("year", type=int)
    r.add_argument("--by", required=True, help="還原人")
    args = parser.parse_args(argv)
    try:
        if args.command == "activate":
            print(json.dumps(activate(args.draft_id, args.by, acknowledge=True), ensure_ascii=False))
        elif args.command == "revert":
            print(json.dumps(revert(args.year, args.by), ensure_ascii=False))
    except (KeyError, ValueError) as e:
        print(f"未變更：{e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    bases = base_paths()
    for year, path in sorted(active_paths().items()):
        print(f"{year} 學年度：{'正式規則庫 ' if path == bases[year] else '啟用版本 '}{path.name}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
