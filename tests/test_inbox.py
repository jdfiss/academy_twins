"""學生問題收件匣：唯讀工具、只能查寄件學生、依據檢查與反思、轉人工、監控者決定、存檔。替身模型，不連網。"""
import glob
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import dashboard, inbox, llm
from academic_twin.api import AcademicTwinService

ROOT = Path(__file__).parent.parent
ROSTER = ROOT / "data" / "students" / "demo_batch_roster.csv"


def demo_service():
    """以虛構示範批次的標準答案當修課紀錄（不經 AI 解析），供收件匣查詢。"""
    s = AcademicTwinService()
    lines = []
    for f in sorted(glob.glob(str(ROOT / "data" / "samples" / "demo_batch" / "DEMO-D0[3-8]_answer.csv"))):
        rows = Path(f).read_text(encoding="utf-8-sig").splitlines()
        lines += rows if not lines else rows[1:]
    s.load_data(("demo.csv", "\n".join(lines) + "\n"), ("roster.csv", ROSTER.read_text(encoding="utf-8-sig")))
    return s


def result(data):
    return llm.LLMResult(data, "fake-model", "2026-11-07T00:00:00")


def call(tool, **args):
    return result({"action": "call_tool", "tool": tool, "args": args})


def answer(qtype, reply="", proposals=None, citations=None):
    return result({"action": "answer", "type": qtype, "reply": reply, "proposals": proposals or [],
                   "citations": citations or []})


class Script:
    """依序回傳預先排好的模型回應，並記下每次收到的提示。"""

    def __init__(self, *steps):
        self.steps, self.prompts = list(steps), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.steps.pop(0)


def message(sid="DEMO-D03", body="我還差哪些條件才能畢業？", subject="畢業進度"):
    return {"id": "m1", "student_id": sid, "subject": subject, "body": body}


class AgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = demo_service()

    def run_agent(self, *steps, msg=None):
        script = Script(*steps)
        draft, trace = inbox.draft_reply(self.service, msg or message(), script)
        return draft, trace, script

    def test_tools_only_see_sender_and_no_grades(self):
        tools = inbox.Tools(self.service, "DEMO-D03")
        progress = tools.get_progress({})
        self.assertEqual(progress["status"], "MANUAL_REVIEW")
        self.assertIn("R1", {r["id"] for r in progress["unmet_or_pending"]})
        record = tools.find_record({"code": "IM1001"})
        self.assertNotIn("grade", record["records"][0])
        self.assertTrue(record["records"][0]["passed"])
        dumped = json.dumps([progress, tools.get_flags({}), record], ensure_ascii=False)
        self.assertNotIn("DEMO-D03", dumped)
        self.assertNotIn('"grade"', dumped)
        with self.assertRaises(inbox.ToolError):
            tools.get_requirement({"id": "R99"})

    def test_grounded_reply_is_drafted_with_footer(self):
        draft, trace, _ = self.run_agent(
            call("get_progress"),
            answer("progress", "依目前審查結果，R10 最低畢業學分目前 97 學分，需要 128 學分；R1 系訂必修還缺 IM4048。"))
        self.assertEqual((draft["state"], draft["grounded"], trace["grounding"]), ("drafted", True, "passed"))
        self.assertTrue(draft["reply"].endswith(inbox.FOOTER))
        self.assertEqual(trace["tools"], ["get_progress"])
        self.assertFalse(trace["to_manual"])

    def test_tool_outside_whitelist_is_refused(self):
        draft, trace, script = self.run_agent(
            call("get_student", id="DEMO-D04"), call("get_progress"),
            answer("progress", "依目前審查結果，R10 最低畢業學分還需要補足。"))
        self.assertEqual(trace["invalid_tools"], 1)
        self.assertIn("不在白名單", script.prompts[1])
        self.assertEqual(draft["state"], "drafted")

    def test_unsupported_number_triggers_one_reflection(self):
        draft, trace, script = self.run_agent(
            call("get_progress"),
            answer("progress", "你還差 999 學分就能畢業。"),
            answer("progress", "依目前審查結果，總學分 97 學分，最低畢業學分為 128 學分。"))
        self.assertEqual(trace["grounding"], "reflected_passed")
        self.assertTrue(trace["reflected"])
        self.assertIn("999", script.prompts[2])  # 檢查原因回饋給模型
        self.assertEqual(draft["state"], "drafted")

    def test_still_ungrounded_after_reflection_goes_to_manual(self):
        draft, trace, _ = self.run_agent(
            call("get_progress"), answer("progress", "你還差 999 學分。"), answer("progress", "你還差 998 學分。"))
        self.assertEqual((draft["state"], draft["grounded"], trace["grounding"]), ("manual", False, "failed"))
        self.assertIn("無法確認", draft["notes"][0])
        self.assertTrue(trace["to_manual"])

    def test_check_reply_rules(self):
        evidence = json.dumps({"actions": [{"title": "確認 X 抵修", "office": "教務處註冊組"}], "need": 128,
                               "id": "R10", "code": "IM4048"}, ensure_ascii=False)
        self.assertEqual(inbox.check_reply("請洽教務處註冊組，R10 需要 128 學分，缺 IM4048。", evidence), [])
        self.assertEqual(inbox.check_reply("承辦單位待系辦確認。", evidence), [])
        problems = inbox.check_reply("請到學務處課外活動組辦理，保證可以畢業，R12 缺 IM9999，共 5 學分。", evidence)
        text = "\n".join(problems)
        for word in ("課外活動組", "保證", "R12", "IM9999", "5"):
            self.assertIn(word, text)

    def test_answer_without_tools_is_not_trusted(self):
        draft, trace, _ = self.run_agent(answer("progress", "依目前審查結果還要努力。"),
                                         answer("progress", "依目前審查結果還要努力。"))
        self.assertEqual(draft["state"], "manual")
        self.assertIn("沒有查詢任何審查結果就回答", draft["notes"])

    def test_round_limit(self):
        draft, trace, _ = self.run_agent(*[call("get_progress") for _ in range(inbox.MAX_ROUNDS)])
        self.assertEqual(trace["rounds"], inbox.MAX_ROUNDS)
        self.assertEqual(draft["state"], "manual")
        self.assertIsNone(draft["type"])

    def test_correction_and_other_use_fixed_text(self):
        for qtype in ("correction", "other"):
            with self.subTest(qtype=qtype):
                draft, trace, _ = self.run_agent(answer(qtype, "我幫你把英檢改成通過了"))
                self.assertEqual((draft["state"], draft["reply"]), ("manual", inbox.TEMPLATES[qtype]))
                self.assertEqual(trace["grounding"], "skipped")

    def test_only_valid_review_proposals_are_kept(self):
        draft, trace, _ = self.run_agent(
            call("find_record", code="XFER-01"), call("find_record", code="IM1001"),
            answer("course", "XFER-01 目前列為需人工覆核，承辦人會確認採計方式。", proposals=[
                {"code": "XFER-01", "term": "114-1", "action": "dept_elective", "reason": "他校程式課"},
                {"code": "IM1001", "term": "114-1", "action": "exclude", "reason": "不可覆核"},
                {"code": "XFER-01", "term": "114-1", "action": "delete_record", "reason": "不合法方式"}]),
            msg=message(body="XFER-01 為什麼沒算？"))
        self.assertEqual(draft["state"], "drafted")
        self.assertEqual([(p["code"], p["action"]) for p in draft["proposals"]], [("XFER-01", "dept_elective")])
        self.assertEqual((trace["proposals_kept"], trace["proposals_dropped"]), (1, 2))

    def test_prompt_masks_student_id_and_long_numbers(self):
        _, _, script = self.run_agent(
            call("get_progress"), answer("other"),
            msg=message(body="我是 DEMO-D03，學號 109403012，我的證照號碼是 12345678"))
        for prompt in script.prompts:
            self.assertNotIn("DEMO-D03", prompt)
            self.assertNotIn("109403012", prompt)
            self.assertNotIn("12345678", prompt)

    def test_sender_without_audit_result(self):
        draft, _, script = self.run_agent(call("get_progress"), answer("progress", "目前查不到資料。"),
                                          answer("progress", "目前查不到資料。"), msg=message(sid="NOBODY"))
        self.assertIn("沒有審查結果", script.prompts[1])
        self.assertEqual(draft["state"], "manual")


def responder(prompt, schema=None):
    """背景擬稿用的替身：先查進度；問 XFER-01 的再查紀錄並提出覆核建議；問英檢更正的歸資料更正。"""
    history = prompt.split("目前為止的工具呼叫與結果（JSON）：")[1]
    if "多益" in prompt:
        return answer("correction")
    if "get_progress" not in history:
        return call("get_progress")
    if "XFER-01" in prompt.split("目前為止")[0]:
        if "find_record" not in history:
            return call("find_record", code="XFER-01")
        return answer("course", "XFER-01 目前列為需人工覆核。", proposals=[
            {"code": "XFER-01", "term": "114-1", "action": "dept_elective", "reason": "他校程式課"}])
    return answer("progress", "依目前審查結果，請參考各項畢業要求的說明。")


class EvaluateTest(unittest.TestCase):
    def test_evaluate_scores_type_routing_and_facts(self):
        cases = json.loads(inbox.EVAL_SET.read_text(encoding="utf-8"))["cases"]
        self.assertEqual(len(cases), 30)
        self.assertEqual({c["split"] for c in cases}, {"dev", "validation"})
        pick = [c for c in cases if c["id"] in ("q01", "q08")]
        report = inbox.evaluate(inbox.eval_service(), pick, generate=responder)
        rows = {r["id"]: r for r in report["cases"]}
        self.assertTrue(rows["q08"]["type_ok"] and rows["q08"]["routing_ok"])  # 替身把多益歸為資料更正
        self.assertFalse(rows["q01"]["facts_ok"])  # 替身的回覆沒有提到 IM4048
        self.assertEqual(report["overall"]["type_ok"], {"k": 2, "n": 2})
        self.assertNotIn("reply", json.dumps(report))  # 不含回覆內容


class InboxServiceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.log = self.tmp / "inbox.jsonl"
        for target, path in ((inbox, self.log), (dashboard, self.tmp / "runs.jsonl")):
            p = mock.patch.object(target, "EVAL_LOG", path)
            p.start()
            self.addCleanup(p.stop)
        self.service = demo_service()

    def process(self, mid=None):
        with mock.patch.object(inbox.replay, "generate_json", side_effect=responder) as gen:
            self.service.inbox_process(mid)
            self.service.inbox_wait()
        return gen

    def by_subject(self):
        return {m["subject"]: m for m in self.service.inbox_list()["messages"]}

    def test_demo_inbox_flow(self):
        box = self.service.inbox_load_demo()
        self.assertEqual(box["counts"]["new"], 10)
        self.process()
        msgs = self.by_subject()
        self.assertEqual(msgs["想確認畢業進度"]["state"], "drafted")
        self.assertEqual(msgs["英檢其實有過"]["state"], "manual")
        self.assertEqual(msgs["轉學抵免的課"]["proposals"], 1)
        self.assertFalse(msgs["轉學抵免的課"]["batch_ok"])
        self.assertTrue(msgs["想確認畢業進度"]["batch_ok"])
        traces = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(traces), 10)
        dumped = json.dumps(traces, ensure_ascii=False)
        for secret in ("DEMO-D0", "多益", "XFER-01", "畢業進度"):
            self.assertNotIn(secret, dumped)  # 軌跡不記學號、信件與課程內容

    def test_decisions(self):
        self.service.inbox_load_demo()
        self.process()
        msgs = self.by_subject()
        mid = msgs["想確認畢業進度"]["id"]
        with self.assertRaises(ValueError):
            self.service.inbox_decide(mid, "approve", "")
        with self.assertRaisesRegex(ValueError, "斷言"):
            self.service.inbox_decide(mid, "approve", "監控者", reply="保證可以畢業")
        done = self.service.inbox_decide(mid, "approve", "監控者", reply="依目前審查結果，請再補修課程。")
        self.assertEqual(done["state"], "replied")
        self.assertGreater(done["history"][-1]["edited_chars"], 0)
        with self.assertRaises(ValueError):
            self.service.inbox_decide(mid, "approve", "監控者")  # 已回覆的不能再送
        other = msgs["我可以畢業了嗎"]["id"]
        with self.assertRaisesRegex(ValueError, "否決原因"):
            self.service.inbox_decide(other, "reject", "監控者")
        self.assertEqual(self.service.inbox_decide(other, "reject", "監控者", reason="tone")["state"], "rejected")
        decisions = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()
                     if '"inbox_decision"' in x]
        self.assertEqual([d["action"] for d in decisions], ["approve", "reject"])

    def test_batch_approve_only_safe_drafts(self):
        self.service.inbox_load_demo()
        self.process()
        msgs = self.by_subject()
        with self.assertRaisesRegex(ValueError, "不能批次核准"):
            self.service.inbox_approve_many([msgs["轉學抵免的課"]["id"], msgs["想確認畢業進度"]["id"]], "監控者")
        safe = [m["id"] for m in msgs.values() if m["batch_ok"]]
        box = self.service.inbox_approve_many(safe, "監控者")
        self.assertEqual(box["counts"]["replied"], len(safe))

    def test_apply_proposal_goes_through_review_and_marks_stale(self):
        self.service.inbox_load_demo()
        self.process()
        mid = self.by_subject()["轉學抵免的課"]["id"]
        self.assertEqual(self.service.student("DEMO-D03")["status"], "MANUAL_REVIEW")
        m = self.service.inbox_apply_proposal(mid, 0, "監控者")
        self.assertTrue(m["stale"])
        self.assertTrue(m["proposals"][0]["applied"]["accepted"])
        s = self.service.student("DEMO-D03")
        self.assertEqual(s["reviews"][0]["code"], "XFER-01")
        self.assertIn("收件匣", s["reviews"][0]["note"])
        with self.assertRaisesRegex(ValueError, "已處理過"):
            self.service.inbox_apply_proposal(mid, 0, "監控者")
        self.process(mid)  # 重新擬稿
        self.assertFalse(self.service.inbox_get(mid)["stale"])

    def test_ai_stopped_sends_nothing(self):
        self.service.inbox_load_demo()
        self.service.dashboard_batch_stop()
        with mock.patch.object(inbox.replay, "generate_json") as gen, self.assertRaises(llm.LLMError):
            self.service.inbox_process()
        gen.assert_not_called()
        self.assertEqual(self.service.inbox_list()["counts"]["new"], 10)

    def test_model_failure_marks_failed_and_can_retry(self):
        self.service.inbox_add("DEMO-D04", "問題", "我可以畢業嗎？")
        with mock.patch.object(inbox.replay, "generate_json", side_effect=llm.LLMError("429")):
            self.service.inbox_process()
            self.service.inbox_wait()
        [m] = self.service.inbox_list()["messages"]
        self.assertEqual(m["state"], "failed")
        self.process()
        self.assertEqual(self.service.inbox_list()["messages"][0]["state"], "drafted")

    def test_import_validates_everything_first(self):
        with self.assertRaises(ValueError):
            self.service.inbox_import([{"student_id": "DEMO-D03", "body": "ok"}, {"student_id": "", "body": "x"}])
        self.assertEqual(self.service.inbox_list()["messages"], [])
        with self.assertRaises(ValueError):
            self.service.inbox_add("DEMO-D03", "長", "字" * (inbox.MAX_BODY + 1))

    def test_inbox_survives_restart(self):
        state = self.tmp / "state"
        self.service.restore_office_state(state)
        self.service.inbox_load_demo()
        self.process()
        again = AcademicTwinService()
        again.restore_office_state(state)
        self.assertEqual(again.inbox_list()["counts"], self.service.inbox_list()["counts"])

    def test_demo_batch_clears_inbox(self):
        self.service.inbox_load_demo()
        self.service.load_demo_batch()
        self.assertEqual(self.service.inbox_list()["messages"], [])


if __name__ == "__main__":
    unittest.main()
