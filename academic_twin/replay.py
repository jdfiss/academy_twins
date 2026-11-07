"""評審模式：預先錄下的 AI 回應重播。（A 組）

比賽評審沒有金鑰、或決賽現場網路不穩時，系辦端仍能展示同一條流程。做法：
- 錄製：環境變數 ACADEMIC_TWIN_RECORD=1 時，真實模型的回應依「提示＋模型＋附件」的雜湊存到
  data/replay/<雜湊>.json。**只對虛構文件錄製**（data/samples/ 產生的示範 PDF），錄下的檔案進版控。
- 重播：ACADEMIC_TWIN_REPLAY 決定何時使用錄製結果——
    （預設）fallback  先呼叫真實模型；失敗（沒有金鑰、過載、逾時）且有錄製結果時改用重播
    only              評審模式：完全不呼叫模型，只用錄製結果；沒錄過的文件一律失敗
    off               不使用重播
- 只有錄過的同一份檔案、同一版提示才對得上；評審上傳其他文件不會得到任何結果，不會編造。
- 重播結果一律標示「重播」與原本的模型、產生時間，畫面與執行軌跡都看得到。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import llm

ROOT = Path(__file__).parent.parent
REPLAY_DIR = ROOT / "data" / "replay"
MODES = ("fallback", "only", "off")


def mode() -> str:
    raw = os.environ.get("ACADEMIC_TWIN_REPLAY", "").strip().lower() or "fallback"
    if raw not in MODES:
        raise ValueError(f"ACADEMIC_TWIN_REPLAY 須為 {'、'.join(MODES)} 之一，目前為「{raw}」")
    return raw


def disable() -> None:
    """量測用的命令列（基準量測、單份量測、規則草擬量測）呼叫：一律即時呼叫模型，
    錄製結果不能算成模型表現。"""
    os.environ["ACADEMIC_TWIN_REPLAY"] = "off"


def recording() -> bool:
    return os.environ.get("ACADEMIC_TWIN_RECORD", "").strip() == "1"


def key(prompt: str, model: str | None, attachments) -> str:
    h = hashlib.sha256()
    for part in (prompt.encode("utf-8"), (model or "primary").encode("utf-8")):
        h.update(len(part).to_bytes(8, "big") + part)
    for a in attachments:
        h.update(a.mime_type.encode("utf-8") + hashlib.sha256(a.data).digest())
    return h.hexdigest()[:32]


def generate_json(prompt: str, schema: dict, attachments=(), *, model: str | None = None,
                  purpose: str = "") -> llm.LLMResult:
    """與 llm.generate_json 相同介面；依模式改用錄製結果。重播時 result.replay 帶原本的模型與時間。

    purpose：用途代號（dashboard／inbox／rule_draft），只用於 token 用量紀錄。
    """
    m = mode()
    path = REPLAY_DIR / f"{key(prompt, model, attachments)}.json"
    if m == "only":
        return _load(path, "評審模式只能重播預先錄製的虛構文件，這份文件沒有錄製結果")
    try:
        result = llm.generate_json(prompt, schema, attachments, model=model)
    except llm.LLMError as e:
        if m == "fallback" and path.exists():
            return _load(path, "", reason=str(e)[:200])
        raise
    log_usage(result, purpose, m)
    if recording() and not result.fallback_from:  # 備援模型的回應不能存成主模型的錄製結果
        _save(path, result)
    return result


# ---------- token 用量（估算費用用；只記計數，不記提示或回應內容） ----------
USAGE_LOG = ROOT / "data" / "eval" / "llm_usage.jsonl"
_USAGE_LOCK = threading.Lock()


def usage_entry(result: llm.LLMResult, purpose: str, m: str) -> dict | None:
    """把模型回傳的 usageMetadata 整理成一筆紀錄；沒有用量資料（例如測試替身）時回傳 None。"""
    u = result.usage or {}
    if not u:
        return None
    return {"at": datetime.now().isoformat(timespec="seconds"), "purpose": purpose or "other",
            "model": result.model, "fallback": bool(result.fallback_from), "measure": m == "off",
            "input_tokens": int(u.get("promptTokenCount") or 0),
            "output_tokens": int(u.get("candidatesTokenCount") or 0),
            "thinking_tokens": int(u.get("thoughtsTokenCount") or 0),
            "total_tokens": int(u.get("totalTokenCount") or 0)}


def log_usage(result: llm.LLMResult, purpose: str, m: str) -> None:
    entry = usage_entry(result, purpose, m)
    if entry is None:
        return
    with _USAGE_LOCK:  # 批次解析與收件匣都是多執行緒
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def usage_summary(entries: list[dict]) -> dict:
    """依用途與模型彙總：呼叫次數、平均輸入／輸出（含思考）token。費用由使用者以當時的價目表換算。"""
    groups: dict[tuple, list[dict]] = {}
    for e in entries:
        groups.setdefault((e.get("purpose", "other"), e.get("model", "")), []).append(e)
    rows = []
    for (purpose, model), es in sorted(groups.items()):
        n = len(es)
        rows.append({"purpose": purpose, "model": model, "calls": n,
                     "avg_input": round(sum(e["input_tokens"] for e in es) / n),
                     "avg_output": round(sum(e["output_tokens"] + e["thinking_tokens"] for e in es) / n)})
    return {"rows": rows, "calls": len(entries)}


def _save(path: Path, result: llm.LLMResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": result.model, "generated_at": result.generated_at,
                                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                                "fallback_from": result.fallback_from, "usage": result.usage or {},
                                "data": result.data},
                               ensure_ascii=False, indent=1), encoding="utf-8")


def _load(path: Path, missing: str, reason: str = "") -> llm.LLMResult:
    if not path.exists():
        raise llm.LLMError(missing)
    saved = json.loads(path.read_text(encoding="utf-8"))
    result = llm.LLMResult(saved["data"], saved["model"], saved["generated_at"],
                           fallback_from=saved.get("fallback_from", ""))
    result.replay = {"model": saved["model"], "generated_at": saved["generated_at"],
                     "reason": reason or "評審模式"}
    return result


def main() -> None:
    """python -m academic_twin.replay --usage：彙總 data/eval/llm_usage.jsonl 的 token 用量。"""
    if "--usage" not in sys.argv[1:]:
        print("用法：python -m academic_twin.replay --usage")
        return
    if not USAGE_LOG.is_file():
        print(f"尚無用量紀錄（{USAGE_LOG}）：真實模型呼叫後才會產生")
        return
    entries = [json.loads(line) for line in USAGE_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    s = usage_summary(entries)
    print(f"共 {s['calls']} 次真實模型呼叫（不含重播）；輸出含思考 token")
    for r in s["rows"]:
        print(f"  {r['purpose']:<10} {r['model']:<24} {r['calls']:>4} 次  平均輸入 {r['avg_input']:>6}  平均輸出 {r['avg_output']:>6}")


if __name__ == "__main__":
    main()
