"""學生問題收件匣：AI 依審查結果擬回覆，監控者核准、修改或否決。（A 組，Agentic）

流程：學生來信 → AI 判斷問題類型、以唯讀工具查審查結果 → 程式檢查回覆有沒有依據 → 擬稿交監控者。
符合 docs/TEAM_PLAN.md〈Agentic 功能〉：
- 工具只能是唯讀白名單；以 llm.generate_json 回傳「呼叫工具／回答」的 JSON 由程式執行，最多 MAX_ROUNDS 輪。
- **只能查寄件學生本人**：學號由信件帶入，工具沒有學號參數，模型無法查別人。
- 回覆中的數字、課號、要求編號、承辦單位都必須出現在本次工具結果中（grounding），並禁止斷言用語；
  沒通過就把原因回饋模型反思 1 次，仍沒通過則標示「無法確認」並轉人工。
- AI 不計算學分、不改判定、不送出任何東西：回覆與覆核建議都是草稿，監控者核准後才生效；
  「核准送出」只把回覆標為已回覆、可複製，不串接寄信。覆核建議經監控者核准後，走既有人工覆核（add_review）。
- 資料更正、規章解讀、例外申請等類型一律轉人工；AI 只負責分類，回覆用固定文字。

依據檢查的限度：數字只檢查是否出現在工具結果中，不檢查是否用在正確的地方，所以擬稿仍須監控者核准。

送給模型的只有：問題原文（學號與長串數字先遮罩）與工具結果（不含學號、不含分數，只有是否及格）。
執行軌跡只記輪數、工具名稱、檢查結果與秒數，不記問題、回覆或課程內容。
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import dashboard, dashboard_batch, llm, replay
from .rule_engine import CATEGORY, DEPT_ELECTIVE, EXCLUDE, OUTSIDE

ROOT = Path(__file__).parent.parent
EVAL_LOG = ROOT / "data" / "eval" / "inbox_runs.jsonl"
DEMO_INBOX = ROOT / "data" / "samples" / "inbox_demo.json"
PROMPT_VERSION = "inbox-v1"
MAX_ROUNDS = 5
MAX_BODY = 1000
WORKERS = 2

TYPES = {"progress": "進度查詢", "course": "課程採計", "misjudge": "疑似誤判", "procedure": "辦理流程",
         "correction": "資料更正", "other": "其他（轉人工）"}
DRAFT_TYPES = ("progress", "course", "misjudge", "procedure")  # AI 可擬稿
TEMPLATES = {  # 轉人工的類型用固定文字，AI 只負責分類
    "correction": "已收到你對修課紀錄的更正需求。請提供相關證明文件（例如成績單或證書影本），承辦人核對後會再回覆你；"
                  "核對完成前，系統的審查結果暫不變動。",
    "other": "已收到你的問題。這類問題需要由承辦人依規章判斷，將由承辦人回覆你。",
}
FOOTER = "（以上依系辦目前資料的審查結果說明，實際以正式審查為準。）"
BANNED = ("保證", "一定可以畢業", "一定能畢業", "確定可以畢業", "確定能畢業", "絕對", "已核准", "已幫你", "已為你",
          "已完成辦理", "已經辦好")
OFFICE_ALLOWED = ("系辦", "系辦公室", "承辦人")  # 收件單位本身，不需工具依據
REVIEW_ACTIONS = (DEPT_ELECTIVE, OUTSIDE, EXCLUDE, CATEGORY)
REJECT_REASONS = {"wrong_fact": "引用錯誤", "tone": "語氣不當", "out_of_scope": "超出範圍", "other": "其他"}

NEW, PROCESSING, DRAFTED, MANUAL, FAILED, REPLIED, REJECTED = (
    "new", "processing", "drafted", "manual", "failed", "replied", "rejected")
STATES = {NEW: "新進", PROCESSING: "AI 處理中", DRAFTED: "待核准", MANUAL: "轉人工", FAILED: "AI 失敗",
          REPLIED: "已回覆", REJECTED: "已否決"}

STRING = {"type": "STRING"}
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING", "enum": ["call_tool", "answer"]},
        "tool": STRING,
        "args": {"type": "OBJECT", "properties": {"code": STRING, "id": STRING}},
        "type": {"type": "STRING", "enum": list(TYPES)},
        "reply": STRING,
        "citations": {"type": "ARRAY", "items": STRING},
        "proposals": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "code": STRING, "term": STRING, "action": STRING, "category": STRING, "reason": STRING},
            "required": ["code", "term", "action"]}},
    },
    "required": ["action"],
}

PROMPT = """你是大學系辦的回信助理，替承辦人草擬給學生的回覆。你不能計算學分、不能改變審查結果，只能引用工具查到的審查結果。
每一輪你只能做一件事，回傳 JSON：
- 呼叫工具：{"action": "call_tool", "tool": 工具名稱, "args": {...}}
- 回答：{"action": "answer", "type": 問題類型, "reply": 回覆內容, "citations": [引用的要求編號或課號], "proposals": [...]}

可用工具（只能查這位寄件學生，不需要也不能指定學號）：
- get_progress：審查判定、總學分、各類學分、尚未符合或待確認的畢業要求（含已有／需要、缺的課）
- get_flags：疑似誤判提示與需辦理事項（含承辦單位）
- find_record：某門課的修課紀錄（學期、學分、是否及格、類別）與能否人工覆核；args 填 {"code": 課號}
- get_requirement：某條畢業要求的內容與規章出處；args 填 {"id": 要求編號，例如 R6}

問題類型：
- progress 進度查詢（還差什麼、能不能畢業）
- course 課程採計（某門課怎麼算）
- misjudge 疑似誤判（校務系統結果和預期不同、學分超修）
- procedure 辦理流程（要找哪個單位辦）
- correction 資料更正（成績、紀錄有誤，或宣稱有未登錄的證明）
- other 其他（規章解讀、例外或專案申請、抱怨、與畢業審查無關）

規則：
1. 先用工具查，再回答。回覆裡出現的每個數字、課號、要求編號、承辦單位，都必須出現在工具結果中；工具沒有的不要寫，也不要推算。
2. 不要用條列編號，不要寫學號。不要說「保證」「一定可以畢業」「已核准」「已幫你辦好」這類斷言；判定以「目前審查結果」描述。
3. 承辦單位只能照 get_flags 的需辦理事項寫；工具沒有寫明單位時，說明「承辦單位待系辦確認」，不要自己猜。
4. correction 與 other 類型只要分類，reply 可留空，會改由承辦人處理。
5. course 類型中，若 find_record 顯示該紀錄「可人工覆核」，可在 proposals 提出覆核建議（code、term、action、category、reason），
   action 只能是 review_options 列出的方式；不確定就不要提。建議只是草稿，由承辦人決定。
6. 回覆用繁體中文、口吻親切簡潔，200 字以內。

學生來信（學號已遮罩）：
{question}

目前為止的工具呼叫與結果（JSON）：
{history}
{feedback}
請回傳下一步。"""

FEEDBACK = """
上一次的回答沒有通過程式檢查，原因如下；請依工具結果修正後重新回答（可再呼叫工具）：
{problems}"""


# ---------- 唯讀工具（綁定寄件學生） ----------
class Tools:
    """每個工具都由 student() 交接格式或規則庫組出，不新增判定；學號固定為寄件學生。"""

    def __init__(self, service, sid: str):
        self.service, self.sid = service, sid

    def _student(self) -> dict:
        try:
            return self.service.student(self.sid)
        except KeyError:
            raise ToolError("這位學生目前沒有審查結果（可能尚未確認修課紀錄）") from None

    def get_progress(self, args: dict) -> dict:
        s = self._student()
        return {
            "status": s["status"], "total_credits": s["total_credits"], "credit_breakdown": s["credit_breakdown"],
            "requirements_satisfied": sum(r["satisfied"] for r in s["requirements"]),
            "requirements_total": len(s["requirements"]),
            "unmet_or_pending": [
                {"id": r["id"], "title": r["title"], "have": r["have"], "need": r["need"], "pending": r["pending"],
                 "shortfall": r["shortfall"], "missing": [{"code": c["code"], "name": c["name"]} for c in r["missing"]],
                 "detail": r["detail"]}
                for r in s["requirements"] if not r["satisfied"] or r["pending"]],
            "records_needing_review": len(s["issues"]["review"]) + len(s["issues"]["dept_unverified"]),
        }

    def get_flags(self, args: dict) -> dict:
        s = self._student()
        return {"flags": [{"type": f["type"], "message": f["message"], "courses": f["courses"]} for f in s["flags"]],
                "actions": [{"title": a["title"], "office": a["office"], "courses": a["courses"]} for a in s["actions"]]}

    def find_record(self, args: dict) -> dict:
        code = str(args.get("code") or "").strip().upper()
        if not code:
            raise ToolError("請在 args 提供 code（課號）")
        s = self._student()
        kb = self.service.ruleset(self.sid).kb
        reviewable = {(r["code"], r["term"]) for r in s["issues"]["review"] + s["issues"]["dept_unverified"]}
        reviewed = {(r["code"], r["term"]): r["label"] for r in s["reviews"]}
        records = [{"term": r["term"], "name": r["name"], "credits": r["credits"],
                    "passed": r["grade"] >= kb.passing_grade, "category": r["category"],
                    "reviewable": (r["code"], r["term"]) in reviewable,
                    "reviewed_as": reviewed.get((r["code"], r["term"]))}
                   for r in s["records"] if r["code"].upper() == code]
        out = {"code": code, "records": records,
               "missing_for": [r["id"] for r in s["requirements"] if code in {c["code"] for c in r["missing"]}]}
        if any(r["reviewable"] for r in records):
            out["review_options"] = [DEPT_ELECTIVE, OUTSIDE, EXCLUDE] + [f"{CATEGORY}:{c}" for c in kb.categories]
        return out

    def get_requirement(self, args: dict) -> dict:
        rid = str(args.get("id") or "").strip().upper()
        for r in self._student()["requirements"]:
            if r["id"].upper() == rid:
                return {k: r[k] for k in ("id", "title", "satisfied", "pending", "have", "need", "detail", "citation")}
        raise ToolError(f"沒有畢業要求 {rid}")


class ToolError(Exception):
    pass


TOOLS = ("get_progress", "get_flags", "find_record", "get_requirement")


# ---------- 檢查（程式執行，不經 AI） ----------
def mask(text: str, sid: str) -> str:
    """遮罩學號與 7 位以上的長串數字（可能是學號或證號）。"""
    if sid:
        text = re.sub(re.escape(sid), "（學號）", text, flags=re.IGNORECASE)
    return re.sub(r"\d{7,}", "（號碼）", text)


NUMBER = re.compile(r"\d+(?:\.\d+)?")
CODE = re.compile(r"\b[A-Z]{2,}-?\d+[A-Z]?\b")
REQ = re.compile(r"\bR\d+\b")
OFFICE = re.compile(r"[一-鿿]{0,8}(?:組|處|中心|辦公室|系辦)")


def _numbers(text: str) -> set[float]:
    return {float(n) for n in NUMBER.findall(text)}


def check_reply(reply: str, evidence: str) -> list[str]:
    """回覆是否都有依據；回傳問題清單（空清單表示通過）。evidence 是本次全部工具結果的 JSON 文字。"""
    problems = []
    known = _numbers(evidence)
    unknown = sorted({n for n in NUMBER.findall(reply) if float(n) not in known}, key=float)
    if unknown:
        problems.append(f"回覆中的數字 {'、'.join(unknown)} 在工具結果中找不到")
    for pattern, label in ((CODE, "課號"), (REQ, "要求編號")):
        missing = sorted({c for c in pattern.findall(reply) if c not in evidence})
        if missing:
            problems.append(f"回覆中的{label} {'、'.join(missing)} 在工具結果中找不到")
    for m in OFFICE.finditer(reply):
        word = m.group(0)
        tails = [word[i:] for i in range(len(word) - 1)]
        if not any(t in evidence or t in OFFICE_ALLOWED for t in tails):
            problems.append(f"回覆中的單位「{word}」在需辦理事項中找不到；工具沒有寫明時請說承辦單位待系辦確認")
    used = [b for b in BANNED if b in reply]
    if used:
        problems.append(f"回覆不能使用斷言用語：{'、'.join(used)}")
    return problems


def check_proposals(proposals: list, history: list[dict]) -> tuple[list[dict], int]:
    """只保留 find_record 回報可人工覆核的紀錄，且採計方式在 review_options 內；回傳（保留, 丟棄數）。"""
    allowed: dict[tuple[str, str], set[str]] = {}
    for h in history:
        if h.get("tool") != "find_record" or "result" not in h:
            continue
        options = set(h["result"].get("review_options", []))
        for r in h["result"]["records"]:
            if r["reviewable"]:
                allowed[(h["result"]["code"], r["term"])] = options
    kept = []
    for p in proposals or []:
        if not isinstance(p, dict):
            continue
        code, term = str(p.get("code", "")).strip().upper(), str(p.get("term", "")).strip()
        action, category = str(p.get("action", "")).strip(), str(p.get("category", "")).strip()
        choice = f"{CATEGORY}:{category}" if action == CATEGORY else action
        if (code, term) in allowed and choice in allowed[(code, term)] and action in REVIEW_ACTIONS:
            kept.append({"code": code, "term": term, "action": action, "category": category if action == CATEGORY else "",
                         "reason": str(p.get("reason", ""))[:200], "applied": None})
    return kept, len(proposals or []) - len(kept)


# ---------- Agent 迴圈 ----------
def draft_reply(service, message: dict, generate=None) -> tuple[dict, dict]:
    """擬一封回覆；回傳（擬稿欄位, 執行軌跡）。generate 預設為 replay.generate_json（可錄製、重播）。"""
    generate = generate or (lambda prompt: replay.generate_json(prompt, SCHEMA))
    sid = message["student_id"]
    tools = Tools(service, sid)
    question = mask(f"主旨：{message['subject']}\n{message['body']}", sid)
    began = time.monotonic()
    trace = {"type": "inbox_trace", "at": datetime.now().isoformat(timespec="seconds"),
             "prompt_version": PROMPT_VERSION, "model": None, "rounds": 0, "tools": [], "invalid_tools": 0,
             "reflected": False, "grounding": "skipped", "to_manual": True, "question_type": None,
             "proposals_kept": 0, "proposals_dropped": 0, "replay": None}
    history: list[dict] = []
    feedback = ""
    answer = None
    problems: list[str] = []
    while trace["rounds"] < MAX_ROUNDS:
        service._require_ai()  # 「停止全部」後不再呼叫模型
        prompt = (PROMPT.replace("{question}", question)  # PROMPT 內有 JSON 範例的大括號，不用 str.format
                  .replace("{history}", json.dumps(history, ensure_ascii=False)).replace("{feedback}", feedback))
        result = generate(prompt)
        trace["rounds"] += 1
        trace["model"] = result.model
        trace["replay"] = getattr(result, "replay", None)
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("action") == "call_tool":
            name, args = str(data.get("tool") or ""), data.get("args") or {}
            if name not in TOOLS:
                trace["invalid_tools"] += 1
                history.append({"tool": name, "error": "工具不在白名單，請改用列出的工具"})
                continue
            trace["tools"].append(name)
            try:
                history.append({"tool": name, "args": args, "result": getattr(tools, name)(args)})
            except ToolError as e:
                history.append({"tool": name, "args": args, "error": str(e)})
            continue
        if data.get("action") != "answer" or data.get("type") not in TYPES:
            feedback = FEEDBACK.format(problems="- 請回傳 action 為 call_tool 或 answer，answer 須有列出的問題類型")
            continue
        answer = data
        qtype = data["type"]
        if qtype not in DRAFT_TYPES:
            break
        evidence = json.dumps([h.get("result") for h in history if "result" in h], ensure_ascii=False)
        reply = str(data.get("reply") or "").strip()
        problems = (["回覆是空的"] if not reply else []) + (
            ["沒有查詢任何審查結果就回答"] if not any("result" in h for h in history) else []) + check_reply(reply, evidence)
        if not problems:
            trace["grounding"] = "reflected_passed" if trace["reflected"] else "passed"
            break
        if trace["reflected"]:
            trace["grounding"] = "failed"
            break
        trace["reflected"] = True
        feedback = FEEDBACK.format(problems="\n".join(f"- {p}" for p in problems))
    trace["wait_seconds"] = round(time.monotonic() - began, 2)

    if answer is None:  # 輪數用完仍未回答
        trace["status"] = MANUAL
        return {"state": MANUAL, "type": None, "reply": "", "grounded": False, "proposals": [],
                "notes": [f"AI 在 {MAX_ROUNDS} 輪內沒有完成回答，請自行回覆"], "citations": []}, trace
    qtype = answer["type"]
    trace["question_type"] = qtype
    if qtype not in DRAFT_TYPES:
        trace["status"] = MANUAL
        return {"state": MANUAL, "type": qtype, "reply": TEMPLATES[qtype], "grounded": True, "proposals": [],
                "notes": [f"{TYPES[qtype]}一律由承辦人處理；回覆為固定文字"], "citations": []}, trace
    kept, dropped = check_proposals(answer.get("proposals"), history) if qtype == "course" else ([], 0)
    trace["proposals_kept"], trace["proposals_dropped"] = len(kept), dropped
    grounded = trace["grounding"] in ("passed", "reflected_passed")
    trace["to_manual"] = not grounded
    trace["status"] = DRAFTED if grounded else MANUAL
    reply = str(answer.get("reply") or "").strip()
    return {"state": DRAFTED if grounded else MANUAL, "type": qtype,
            "reply": f"{reply}\n{FOOTER}" if reply else "", "grounded": grounded, "proposals": kept,
            "citations": [str(c) for c in answer.get("citations") or []][:10],
            "notes": ([] if grounded else ["無法確認：回覆內容有部分在審查結果中找不到依據，請自行核對或改寫"] + problems)
            + ([f"已丟棄 {dropped} 項不合法的覆核建議"] if dropped else [])}, trace


# ---------- 收件匣（系辦端服務） ----------
class InboxMixin:
    """需搭配 OfficeMixin（self._require_ai、self._save_state、self.batch_uid、self.add_review）。"""

    def _inbox(self) -> dict:
        return self.__dict__.setdefault("_inbox_data", {"messages": {}, "order": []})

    def _inbox_pool(self) -> ThreadPoolExecutor:
        with _LOCK:
            if "_inbox_executor" not in self.__dict__:
                self._inbox_executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="inbox")
            return self._inbox_executor

    def _inbox_state(self) -> dict:
        with _LOCK:
            return {"inbox": json.loads(json.dumps(self._inbox(), ensure_ascii=False))}

    def _inbox_restore(self, data: dict | None) -> None:
        data = data or {"messages": {}, "order": []}
        for m in data["messages"].values():
            if m["state"] == PROCESSING:  # 重開時還在擬稿的改回新進，可再處理
                m["state"] = NEW
        self.__dict__["_inbox_data"] = data

    def inbox_reset(self) -> None:
        with _LOCK:
            self.__dict__["_inbox_data"] = {"messages": {}, "order": []}

    # ---------- 查詢 ----------
    def inbox_list(self) -> dict:
        with _LOCK:
            box = self._inbox()
            items = [_public(box["messages"][mid]) for mid in box["order"]]
        counts = {s: sum(m["state"] == s for m in items) for s in STATES}
        return {"messages": items, "counts": counts, "states": STATES, "types": TYPES,
                "reject_reasons": REJECT_REASONS}

    def inbox_get(self, mid: str) -> dict:
        with _LOCK:
            m = self._inbox()["messages"].get(mid)
            if not m:
                raise KeyError(f"找不到信件 {mid}")
            return json.loads(json.dumps(m, ensure_ascii=False))

    # ---------- 收信 ----------
    def inbox_add(self, student_id: str, subject: str, body: str, source: str = "import") -> dict:
        sid = (student_id or "").strip()
        if not sid or any(c in sid for c in ",\n\r\""):
            raise ValueError("請填寫學號（去識別化代號）")
        subject, body = str(subject or "").strip()[:100], str(body or "").strip()
        if not body:
            raise ValueError("信件內容是空的")
        if len(body) > MAX_BODY:
            raise ValueError(f"信件內容超過 {MAX_BODY} 字")
        mid = uuid.uuid4().hex[:10]
        with _LOCK:
            box = self._inbox()
            box["messages"][mid] = {"id": mid, "student_id": sid, "subject": subject or "（無主旨）", "body": body,
                                    "source": source, "received_at": datetime.now().isoformat(timespec="seconds"),
                                    "state": NEW, "type": None, "reply": "", "grounded": None, "proposals": [],
                                    "citations": [], "notes": [], "trace": None, "history": [], "stale": False,
                                    "error": ""}
            box["order"].append(mid)
        self._save_state()
        return self.inbox_get(mid)

    def inbox_import(self, messages: list[dict], source: str = "import") -> dict:
        """一次匯入多封（例如虛構示範信件）；先全部檢查，有錯就整批不收。"""
        if not isinstance(messages, list) or not messages:
            raise ValueError("請提供信件陣列")
        errors = []
        for i, m in enumerate(messages, 1):
            if not isinstance(m, dict) or not str(m.get("student_id") or "").strip() or not str(m.get("body") or "").strip():
                errors.append(f"第 {i} 封缺少 student_id 或 body")
            elif len(str(m["body"])) > MAX_BODY:
                errors.append(f"第 {i} 封內容超過 {MAX_BODY} 字")
        if errors:
            raise ValueError("；".join(errors))
        for m in messages:
            self.inbox_add(m["student_id"], m.get("subject", ""), m["body"], source)
        return self.inbox_list()

    def inbox_load_demo(self) -> dict:
        """清空收件匣，匯入虛構示範信件（寄件人為 DEMO-D03–D08）。"""
        demo = json.loads(DEMO_INBOX.read_text(encoding="utf-8"))
        self.inbox_reset()
        return self.inbox_import(demo["messages"], "demo")

    # ---------- AI 擬稿（背景執行） ----------
    def inbox_process(self, mid: str | None = None) -> dict:
        """mid 為 None 時處理全部新進與失敗的信；重新擬稿也走這裡。AI 停用時不送出。"""
        self._require_ai()
        with _LOCK:
            box = self._inbox()
            if mid is not None:
                m = box["messages"].get(mid)
                if not m:
                    raise KeyError(f"找不到信件 {mid}")
                if m["state"] in (REPLIED, REJECTED, PROCESSING):
                    raise ValueError(f"這封信目前是「{STATES[m['state']]}」，不能重新擬稿")
                targets = [m]
            else:
                targets = [box["messages"][i] for i in box["order"] if box["messages"][i]["state"] in (NEW, FAILED)]
            for m in targets:
                m["state"], m["error"] = PROCESSING, ""
            ids = [m["id"] for m in targets]
        for i in ids:
            self._inbox_pool().submit(self._inbox_run, i)
        self._save_state()
        return self.inbox_list()

    def _inbox_run(self, mid: str) -> None:
        with _LOCK:
            message = dict(self._inbox()["messages"][mid])
        try:
            draft, trace = draft_reply(self, message)
        except dashboard_batch.AIPaused:
            self._inbox_update(mid, state=NEW)
            return
        except llm.LLMError as e:
            self._inbox_update(mid, state=FAILED, error=f"AI 擬稿失敗：{str(e)[:200]}")
            dashboard.log_run({"type": "inbox_trace", "at": datetime.now().isoformat(timespec="seconds"),
                               "batch_id": self.batch_uid(), "prompt_version": PROMPT_VERSION, "status": FAILED,
                               "to_manual": True, "error": "llm_unavailable"}, EVAL_LOG)
            return
        except Exception as e:  # 單封失敗不影響其他信
            self._inbox_update(mid, state=FAILED, error=f"擬稿時發生錯誤：{type(e).__name__}")
            return
        trace["batch_id"] = self.batch_uid()
        dashboard.log_run(trace, EVAL_LOG)
        self._inbox_update(mid, **draft, trace=_trace_view(trace), stale=False, error="")

    def _inbox_update(self, mid: str, **fields) -> None:
        with _LOCK:
            m = self._inbox()["messages"].get(mid)
            if m:
                m.update(fields)
        self._save_state()

    def inbox_wait(self) -> None:
        """等背景擬稿結束（測試與命令列用）。"""
        pool = self.__dict__.get("_inbox_executor")
        if pool:
            pool.shutdown(wait=True)
            del self.__dict__["_inbox_executor"]

    # ---------- 監控者決定 ----------
    def inbox_decide(self, mid: str, action: str, reviewer: str, reply: str | None = None,
                     reason: str = "") -> dict:
        """approve：核准送出（可修改回覆）；reject：否決（須選原因）；self：轉給自己處理。"""
        reviewer = (reviewer or "").strip()
        if not reviewer:
            raise ValueError("請填寫監控者姓名")
        with _LOCK:
            m = self._inbox()["messages"].get(mid)
            if not m:
                raise KeyError(f"找不到信件 {mid}")
            if m["state"] not in (DRAFTED, MANUAL):
                raise ValueError(f"這封信目前是「{STATES[m['state']]}」，不能{'核准' if action == 'approve' else '處理'}")
            now = datetime.now().isoformat(timespec="seconds")
            entry = {"action": action, "by": reviewer, "at": now}
            log = {"type": "inbox_decision", "at": now, "batch_id": self.batch_uid(), "action": action,
                   "question_type": m["type"], "grounded": m["grounded"]}
            if action == "approve":
                final = (reply if reply is not None else m["reply"]).strip()
                if not final:
                    raise ValueError("回覆是空的，請填寫後再送出")
                if any(b in final for b in BANNED):
                    raise ValueError("回覆含有斷言用語（例如「保證」「已核准」），請改寫")
                edited = _changed_chars(m["reply"], final)
                m.update(state=REPLIED, reply=final)
                entry["edited_chars"] = log["edited_chars"] = edited
            elif action == "reject":
                if reason not in REJECT_REASONS:
                    raise ValueError("請選擇否決原因")
                m["state"] = REJECTED
                entry["reason"] = log["reason"] = reason
            elif action == "self":
                m.update(state=MANUAL, assignee=reviewer)
            else:
                raise ValueError("action 須為 approve、reject 或 self")
            m["history"].append(entry)
        dashboard.log_run(log, EVAL_LOG)
        self._save_state()
        return self.inbox_get(mid)

    def inbox_approve_many(self, ids: list[str], reviewer: str) -> dict:
        """批次核准只限：AI 可擬稿的類型、依據檢查通過、沒有覆核建議、待核准中的信。"""
        if not (reviewer or "").strip():
            raise ValueError("請填寫監控者姓名")
        with _LOCK:
            box = self._inbox()["messages"]
            blocked = [i for i in ids if i not in box or not _batch_ok(box[i])]
        if blocked:
            raise ValueError(f"{len(blocked)} 封不能批次核准（含覆核建議、依據未通過或不在待核准），請逐封處理")
        for i in ids:
            self.inbox_decide(i, "approve", reviewer)
        return self.inbox_list()

    def inbox_apply_proposal(self, mid: str, index: int, reviewer: str, accept: bool = True) -> dict:
        """監控者核准或拒絕 AI 的覆核建議；核准時走既有人工覆核，引擎立即重審，擬稿標為需重新擬稿。"""
        reviewer = (reviewer or "").strip()
        if not reviewer:
            raise ValueError("請填寫監控者姓名")
        m = self.inbox_get(mid)
        if not 0 <= index < len(m["proposals"]):
            raise KeyError("找不到這項覆核建議")
        p = m["proposals"][index]
        if p["applied"] is not None:
            raise ValueError("這項建議已處理過")
        if accept:
            self.add_review(m["student_id"], p["code"], p["term"], p["action"], reviewer, p["category"],
                            f"收件匣信件 {mid} 的 AI 覆核建議：{p['reason']}"[:300])
        now = datetime.now().isoformat(timespec="seconds")
        with _LOCK:
            live = self._inbox()["messages"][mid]
            live["proposals"][index]["applied"] = {"accepted": accept, "by": reviewer, "at": now}
            if accept:
                live["stale"] = True  # 審查結果已改變，原擬稿可能過時
            live["history"].append({"action": "apply_proposal" if accept else "decline_proposal", "by": reviewer,
                                    "at": now})
        dashboard.log_run({"type": "inbox_decision", "at": now, "batch_id": self.batch_uid(),
                           "action": "apply_proposal" if accept else "decline_proposal",
                           "question_type": m["type"], "grounded": m["grounded"]}, EVAL_LOG)
        self._save_state()
        return self.inbox_get(mid)


_LOCK = threading.RLock()


def _batch_ok(m: dict) -> bool:
    return m["state"] == DRAFTED and m["type"] in DRAFT_TYPES and m["grounded"] and not m["proposals"] \
        and not m.get("stale")


def _public(m: dict) -> dict:
    return {k: m[k] for k in ("id", "student_id", "subject", "received_at", "state", "type", "grounded", "stale",
                              "error")} | {"proposals": len(m["proposals"]), "batch_ok": _batch_ok(m)}


def _trace_view(trace: dict) -> dict:
    return {k: trace.get(k) for k in ("model", "rounds", "tools", "invalid_tools", "reflected", "grounding",
                                      "wait_seconds", "replay", "proposals_kept", "proposals_dropped")}


def _changed_chars(before: str, after: str) -> int:
    """監控者改了多少字（只記數量，不記內容）。"""
    sm = difflib.SequenceMatcher(None, before, after)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")


# ---------- 量測（見 docs/TEAM_PLAN.md〈實證從第一階段開始〉） ----------
EVAL_SET = ROOT / "data" / "samples" / "inbox_eval.json"


def eval_service():
    """評測用服務：虛構示範批次的標準答案當修課紀錄（不經 AI 解析），結果可重現。"""
    from .api import AcademicTwinService
    lines: list[str] = []
    for f in sorted((ROOT / "data" / "samples" / "demo_batch").glob("DEMO-D0[3-8]_answer.csv")):
        rows = f.read_text(encoding="utf-8-sig").splitlines()
        lines += rows if not lines else rows[1:]
    s = AcademicTwinService()
    roster = ROOT / "data" / "students" / "demo_batch_roster.csv"
    s.load_data(("inbox_eval.csv", "\n".join(lines) + "\n"), (roster.name, roster.read_text(encoding="utf-8-sig")))
    return s


def evaluate(service, cases: list[dict], generate=None) -> dict:
    """逐題擬稿並計分：類型正確、分流正確（該轉人工的轉人工、可擬稿的擬出有依據的稿）、必須提到的事實。
    只回傳彙總數字與每題的對錯，不含回覆內容。"""
    results = []
    for c in cases:
        message = {"id": c["id"], "student_id": c["student_id"], "subject": "", "body": c["body"]}
        try:
            draft, trace = draft_reply(service, message, generate)
        except llm.LLMError:
            results.append({"id": c["id"], "split": c["split"], "failed": True})
            continue
        manual_expected = c["expected_type"] not in DRAFT_TYPES
        results.append({
            "id": c["id"], "split": c["split"], "failed": False,
            "type_ok": draft["type"] == c["expected_type"],
            "routing_ok": (draft["state"] == MANUAL) if manual_expected else (draft["state"] == DRAFTED),
            "facts_ok": all(m in draft["reply"] for m in c.get("must_mention", [])),
            "grounding": trace["grounding"], "rounds": trace["rounds"], "seconds": trace["wait_seconds"]})

    def tally(rows):
        ok = [r for r in rows if not r["failed"]]
        return {"cases": len(rows), "model_failed": len(rows) - len(ok),
                **{k: {"k": sum(r[k] for r in ok), "n": len(ok)} for k in ("type_ok", "routing_ok", "facts_ok")},
                "grounding_failed": {"k": sum(r["grounding"] == "failed" for r in ok), "n": len(ok)},
                "reflected": {"k": sum(r["grounding"] == "reflected_passed" for r in ok), "n": len(ok)}}
    return {"overall": tally(results), "by_split": {s: tally([r for r in results if r["split"] == s])
                                                    for s in sorted({r["split"] for r in results})},
            "cases": results}


def main(argv: list[str]) -> None:
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="收件匣評測：以虛構問題量測分類、分流與依據檢查（需要 GEMINI_API_KEY）")
    parser.add_argument("--split", choices=["dev", "validation"], help="只跑某一組")
    args = parser.parse_args(argv)
    replay.disable()  # 量測一律即時呼叫，不用錄製結果
    data = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if not args.split or c["split"] == args.split]
    report = {"eval_version": data["version"], "prompt_version": PROMPT_VERSION, "model": llm.model_name(),
              "started_at": datetime.now().isoformat(timespec="seconds"), **evaluate(eval_service(), cases)}
    out = ROOT / "data" / "eval" / f"inbox_eval_{report['started_at'].replace(':', '').replace('-', '')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("overall", "by_split")}, ensure_ascii=False, indent=2))
    print(f"已寫入 {out}", file=sys.stderr)


if __name__ == "__main__":
    import sys as _sys
    main(_sys.argv[1:])
