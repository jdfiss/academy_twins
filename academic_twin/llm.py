"""呼叫 Gemini 的共用模組（只用標準函式庫）。兩組的 AI 功能共用；變更需通知 B 組。

AI 只負責提案（解析文件、草擬規則、白話說明），輸出一律視為待確認，
不得直接改變審查判定（見 docs/TEAM_PLAN.md〈AI 三層分工〉）。

金鑰讀取順序：環境變數 GEMINI_API_KEY → repo 根目錄 .env（已列入 .gitignore，不進版控）。
模型可用環境變數 GEMINI_MODEL 指定；主模型持續過載或逾時時改用備援模型（GEMINI_FALLBACK_MODELS，逗號分隔）。
每個模型有總等待上限（含重試）：主模型預設 90 秒，可用 GEMINI_TIMEOUT（秒）調整；備援模型固定 120 秒。
以串流接收回應：總等待時間逐段檢查（不只單次讀取），輸出陷入重複迴圈時立即中斷並改用備援模型。
每次呼叫結果都附實際使用的模型名稱與時間，供實證紀錄。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_FALLBACKS = "gemini-2.5-flash"
DEFAULT_TIMEOUT = 90  # 主模型總等待秒數（含重試）
FALLBACK_TIMEOUT = 120  # 每個備援模型的總等待秒數
RETRYABLE = (429, 500, 502, 503, 504)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
# 重複迴圈：輸出結尾同一段文字（8–80 字、至少 4 種字元）連續出現 6 次以上。正常 JSON 每列內容不同，
# 不會整段連續重複。2026-11-06 實測 gemini-3.5-flash 解析 DEMO-D07 時會無限重複段落標題直到逾時。
LOOP_RE = re.compile(r"(.{8,80}?)\1{5,}", re.S)
LOOP_WINDOW = 2000


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
    fallback_from: str = ""  # 改用備援時，記錄前面模型失敗的原因


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


def primary_timeout() -> float:
    raw = os.environ.get("GEMINI_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        value = 0
    if value <= 0:
        raise LLMError(f"GEMINI_TIMEOUT 必須是正的秒數，目前為「{raw}」")
    return value


def generate_json(prompt: str, schema: dict, attachments: list[Attachment] = (), *,
                  model: str | None = None, timeout: float | None = None, retries: int = 3) -> LLMResult:
    """送出提示（可附 PDF 等檔案），要求依 schema 回傳 JSON。

    溫度依模型世代決定（見 temperature()）。timeout 是單一模型的總等待秒數（含重試），
    未指定時主模型用 primary_timeout()，備援模型用 FALLBACK_TIMEOUT。
    過載或伺服器錯誤（429、5xx）在時限內以 2、4、8 秒間隔重試；等待逾時不重試，直接改用備援模型。
    指定 model 時不使用備援。全部失敗時，錯誤訊息列出每個模型的原因。
    """
    models = [model] if model else [model_name(), *[m for m in fallback_models() if m != model_name()]]
    failures = []
    for i, name in enumerate(models):
        budget = timeout or (primary_timeout() if i == 0 else FALLBACK_TIMEOUT)
        try:
            result = _call(name, prompt, schema, attachments, budget, retries)
        except _Unavailable as e:
            failures.append(str(e))
            continue
        result.fallback_from = "；".join(failures)
        return result
    raise LLMError("；".join(failures))


class _Unavailable(LLMError):
    """服務暫時無法使用（過載、逾時），可改用備援模型。"""


def temperature(model: str) -> float | None:
    """Gemini 2.x 用 0，讓結果盡量可重現；Gemini 3 以上回傳 None（不指定，使用預設 1.0）。

    Google 對 Gemini 3 系列的建議是維持預設溫度，調低可能陷入重複迴圈。2026-11-06 實測：
    gemini-3.5-flash 在溫度 0 解析虛構儀表板 DEMO-D07 時，第 36 秒起無限重複段落標題
    （連 3 次逾時，等 440 秒仍未結束）；改用預設溫度 36 秒正常完成。輸出因此不再完全可重現，
    仍由程式檢查、交叉驗證、自我修正與人工確認把關。
    """
    m = re.match(r"gemini-(\d+)", model)
    return None if m and int(m.group(1)) >= 3 else 0


def _wait(seconds: float, deadline: float) -> bool:
    """重試前等待；等完會超過時限就不等，回傳 False。"""
    if time.monotonic() + seconds >= deadline:
        return False
    time.sleep(seconds)
    return True


def _call(model: str, prompt: str, schema: dict, attachments, budget: float, retries: int) -> LLMResult:
    parts = [{"inline_data": {"mime_type": a.mime_type, "data": base64.b64encode(a.data).decode("ascii")}}
             for a in attachments]
    parts.append({"text": prompt})
    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {**({} if temperature(model) is None else {"temperature": temperature(model)}),
                             "responseMimeType": "application/json", "responseSchema": schema},
    }).encode("utf-8")
    request = urllib.request.Request(API.format(model=model), data=body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": api_key()})

    # urlopen 的 timeout 是單次讀取的上限；不用串流時回應一次送完，實際上接近整次等待時間。
    deadline = time.monotonic() + budget
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable(f"Gemini（{model}）{budget:g} 秒內未完成")
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                text, finish, usage = _read_stream(response, model, budget, deadline)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            e.close()
            if e.code in RETRYABLE and attempt < retries and _wait(2 ** (attempt + 1), deadline):
                continue
            error = _Unavailable if e.code in RETRYABLE else LLMError
            raise error(f"Gemini（{model}）回應錯誤 {e.code}：{detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                raise _Unavailable(f"Gemini（{model}）等待逾時，{budget:g} 秒內未回應") from e
            if attempt < retries and _wait(2 ** (attempt + 1), deadline):
                continue
            raise _Unavailable(f"無法連線 Gemini（{model}）：{e}") from e

    try:
        data = json.loads(text)
    except ValueError as e:
        raise LLMError(f"Gemini 回應無法解析為 JSON（finishReason：{finish}）") from e
    return LLMResult(data, model, datetime.now().isoformat(timespec="seconds"), usage)


def _read_stream(response, model: str, budget: float, deadline: float) -> tuple[str, str | None, dict]:
    """逐段讀取串流（SSE）回應，回傳（文字, finishReason, usageMetadata）。

    每收到一段就檢查：超過總等待時間、或輸出陷入重複迴圈，都丟 _Unavailable 讓呼叫端改用備援模型。
    """
    parts: list[str] = []
    tail, finish, usage = "", None, {}
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            chunk = json.loads(line[5:])
        except ValueError as e:
            raise LLMError(f"Gemini（{model}）串流回應格式錯誤") from e
        if "error" in chunk:
            raise LLMError(f"Gemini（{model}）回應錯誤：{str(chunk['error'])[:300]}")
        candidate = (chunk.get("candidates") or [{}])[0]
        piece = "".join(p.get("text", "") for p in (candidate.get("content") or {}).get("parts", [])
                        if not p.get("thought"))
        parts.append(piece)
        finish = candidate.get("finishReason") or finish
        usage = chunk.get("usageMetadata") or usage
        tail = (tail + piece)[-LOOP_WINDOW:]
        if looping(tail):
            raise _Unavailable(f"Gemini（{model}）輸出陷入重複迴圈，已中斷")
        if time.monotonic() > deadline:
            raise _Unavailable(f"Gemini（{model}）等待逾時，{budget:g} 秒內未完成")
    return "".join(parts), finish, usage


def looping(text: str) -> bool:
    m = LOOP_RE.search(text)
    return bool(m) and len(set(m.group(1).strip())) >= 4
