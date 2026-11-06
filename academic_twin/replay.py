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


def recording() -> bool:
    return os.environ.get("ACADEMIC_TWIN_RECORD", "").strip() == "1"


def key(prompt: str, model: str | None, attachments) -> str:
    h = hashlib.sha256()
    for part in (prompt.encode("utf-8"), (model or "primary").encode("utf-8")):
        h.update(len(part).to_bytes(8, "big") + part)
    for a in attachments:
        h.update(a.mime_type.encode("utf-8") + hashlib.sha256(a.data).digest())
    return h.hexdigest()[:32]


def generate_json(prompt: str, schema: dict, attachments=(), *, model: str | None = None) -> llm.LLMResult:
    """與 llm.generate_json 相同介面；依模式改用錄製結果。重播時 result.replay 帶原本的模型與時間。"""
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
    if recording():
        _save(path, result)
    return result


def _save(path: Path, result: llm.LLMResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": result.model, "generated_at": result.generated_at,
                                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                                "fallback_from": result.fallback_from, "data": result.data},
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
