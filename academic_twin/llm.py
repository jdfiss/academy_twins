"""呼叫 Gemini 的共用模組（只用標準函式庫）。兩組的 AI 功能共用；變更需通知 B 組。

AI 只負責提案（解析文件、草擬規則、白話說明），輸出一律視為待確認，
不得直接改變審查判定（見 docs/TEAM_PLAN.md〈AI 三層分工〉）。

金鑰讀取順序：環境變數 GEMINI_API_KEY → repo 根目錄 .env（已列入 .gitignore，不進版控）。
模型可用環境變數 GEMINI_MODEL 指定；主模型持續過載時改用備援模型（GEMINI_FALLBACK_MODELS，逗號分隔）。
每次呼叫結果都附實際使用的模型名稱與時間，供實證紀錄。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_FALLBACKS = "gemini-2.5-flash"
RETRYABLE = (429, 500, 502, 503, 504)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(RuntimeError):
    """AI 服務無法使用或回應無法解析；呼叫端應保留既有功能，不中斷審查。"""


@dataclass(frozen=True)
class Attachment:
    mime_type: str  # 例如 application/pdf
    data: bytes


@dataclass
class LLMResult:
    data: object  # 依 schema 解析後的 JSON
    model: str
    generated_at: str
    usage: dict = field(default_factory=dict)


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    env = ROOT / ".env"
    if not key and env.is_file():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "GEMINI_API_KEY":
                key = value.strip().strip("'\"")
    if not key:
        raise LLMError("未設定 GEMINI_API_KEY（環境變數或 .env），AI 功能暫停使用，其餘功能不受影響")
    return key


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def fallback_models() -> list[str]:
    raw = os.environ.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def generate_json(prompt: str, schema: dict, attachments: list[Attachment] = (), *,
                  model: str | None = None, timeout: float = 180, retries: int = 3) -> LLMResult:
    """送出提示（可附 PDF 等檔案），要求依 schema 回傳 JSON。

    temperature 設為 0 讓結果盡量可重現；服務暫時失敗（429、5xx、連線逾時）時以 2、4、8 秒間隔重試，
    仍失敗就依序改用備援模型。指定 model 時不使用備援。
    """
    models = [model] if model else [model_name(), *[m for m in fallback_models() if m != model_name()]]
    for i, name in enumerate(models):
        try:
            return _call(name, prompt, schema, attachments, timeout, retries)
        except _Unavailable as e:
            if i == len(models) - 1:
                raise LLMError(str(e)) from e


class _Unavailable(LLMError):
    """服務暫時無法使用（過載、逾時），可改用備援模型。"""


def _call(model: str, prompt: str, schema: dict, attachments, timeout: float, retries: int) -> LLMResult:
    parts = [{"inline_data": {"mime_type": a.mime_type, "data": base64.b64encode(a.data).decode("ascii")}}
             for a in attachments]
    parts.append({"text": prompt})
    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": schema},
    }).encode("utf-8")
    request = urllib.request.Request(API.format(model=model), data=body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": api_key()})

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in RETRYABLE and attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            error = _Unavailable if e.code in RETRYABLE else LLMError
            raise error(f"Gemini（{model}）回應錯誤 {e.code}：{detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise _Unavailable(f"無法連線 Gemini（{model}）：{e}") from e

    try:
        candidate = payload["candidates"][0]
        text = "".join(p.get("text", "") for p in candidate["content"]["parts"])
        data = json.loads(text)
    except (KeyError, IndexError, ValueError) as e:
        reason = payload.get("candidates", [{}])[0].get("finishReason") if payload.get("candidates") else None
        raise LLMError(f"Gemini 回應無法解析為 JSON（finishReason：{reason}）") from e
    return LLMResult(data, model, datetime.now().isoformat(timespec="seconds"), payload.get("usageMetadata", {}))
