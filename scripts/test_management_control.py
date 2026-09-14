#!/usr/bin/env python3
"""Focused integration proof for the durable management control plane."""
import os
import sys
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import management as m


def run():
    suffix = uuid.uuid4().hex[:10]
    keys = [f"management-selftest:{suffix}:{i}" for i in range(6)]
    case_ids = []
    pulse_ids = []
    sent, humans, incidents = [], [], []
    ok = False
    try:
        # State-change wakeup + real progress accounting.
        c1 = m.signal(keys[0], "long test suite", "started", {"test": 1},
                      worker=f"qa-worker-{suffix}", progress=True)
        case_ids.append(c1["case_id"])
        with m._conn() as c, c.cursor() as cur:
            cur.execute("SELECT progress_seq FROM management_cases WHERE case_id=%s", (c1["case_id"],))
            progress_once = cur.fetchone()[0] == 1

        # Concurrent tick safety: one lease wins; a second manager cannot overlap.
        first = m._claim_one(c1["case_id"], f"owner-a-{suffix}")
        second = m._claim_one(c1["case_id"], f"owner-b-{suffix}")
        lease_exclusive = first is not None and second is None
        first["lease_owner"] = f"owner-a-{suffix}"
        continued = m._apply(first, {"action": "continue", "rationale": "healthy progress",
                                        "confidence": .96, "review_in_s": 60},
                             alert_fn=lambda *a: incidents.append(a))
        timer_not_stop = continued["status"] == "open" and continued["action"] == "continue"

        # A manager report request is async and durable. It releases the process and
        # wakes the case only when the addressed worker answers.
        # A timer alone must not manufacture another meeting. A real semantic
        # progress change wakes the next manager turn and advances its generation.
        m.signal(keys[0], "long test suite", "progress_update", {"test": 2},
                 worker=f"qa-worker-{suffix}", progress=True)
        ask_claim = m._claim_one(c1["case_id"], f"owner-c-{suffix}")
        assert ask_claim is not None
        ask_claim["lease_owner"] = f"owner-c-{suffix}"
        requested = m._apply(ask_claim, {"action": "request_status", "rationale": "need current evidence",
                                          "message": "What completed since the last check?",
                                          "confidence": .8, "review_in_s": 60},
                               send_fn=lambda *a: sent.append(a))
        with m._conn() as c, c.cursor() as cur:
            cur.execute("SELECT question_id,status FROM management_questions WHERE case_id=%s", (c1["case_id"],))
            qid, qstatus = cur.fetchone()
        wrong_rejected = False
        try:
            m.answer(qid, "fake", responder="someone-else")
        except PermissionError:
            wrong_rejected = True
        answered = m.answer(qid, "4 tests passed; test 5 is downloading a dependency",
                            responder=f"qa-worker-{suffix}")
        with m._conn() as c, c.cursor() as cur:
            cur.execute("SELECT status,next_review_at<=now() FROM management_cases WHERE case_id=%s", (c1["case_id"],))
            awake = cur.fetchone() == ("open", True)
            cur.execute("UPDATE management_cases SET next_review_at=now()+interval '1 hour' WHERE case_id=%s",
                        (c1["case_id"],))
        async_question = (requested["status"] == "waiting_internal" and qstatus == "open"
                          and len(sent) == 1 and wrong_rejected and answered and awake)

        # An AI cannot invent a human gate. A request without a named, genuine
        # authority boundary moves up the internal chain; a credential boundary may page.
        c2 = m.signal(keys[1], "uncertain implementation", "blocked", {"why": "unclear"},
                      tenant_id=f"tenant-{suffix}", worker=f"dev-{suffix}")
        case_ids.append(c2["case_id"])
        cl2 = m._claim_one(c2["case_id"], f"owner-d-{suffix}"); assert cl2 is not None
        cl2["lease_owner"] = f"owner-d-{suffix}"
        no_human = m._apply(cl2, {"action": "request_human", "human_boundary": "not_sure",
                                  "rationale": "I am uncertain", "confidence": .2, "review_in_s": 60},
                             human_fn=lambda *a, **kw: humans.append((a, kw)) or {"request_id": 1},
                             alert_fn=lambda *a: incidents.append(a))
        fabricated_gate_rejected = no_human["action"] == "escalate_manager" and not humans

        c3 = m.signal(keys[2], "production login", "blocked", {"need": "credential"},
                      tenant_id=f"tenant-{suffix}", worker=f"operator-{suffix}")
        case_ids.append(c3["case_id"])
        cl3 = m._claim_one(c3["case_id"], f"owner-e-{suffix}"); assert cl3 is not None
        cl3["lease_owner"] = f"owner-e-{suffix}"
        real_human = m._apply(cl3, {"action": "request_human", "human_boundary": "credential",
                                    "message": "Please provide the production login through the vault.",
                                    "rationale": "Only the account owner has it", "confidence": .99,
                                    "review_in_s": 300},
                               human_fn=lambda *a, **kw: humans.append((a, kw)) or {"request_id": 987654321},
                               alert_fn=lambda *a: incidents.append(a))
        genuine_gate_routed = real_human["status"] == "human_wait" and len(humans) == 1

        # Invalid/provider-failed model output must never be reinterpreted as a manager decision.
        # The scheduler/review wrapper owns retry timing and preserves the durable case.
        c4 = m.signal(keys[3], "novel failure", "error", {"error": "new"}, worker=f"worker-{suffix}")
        case_ids.append(c4["case_id"])
        unavailable_rejected = malformed_rejected = False
        try:
            m._parse_decision({"rc": 1, "failed": True,
                               "reason": "codex provider usage exhausted", "out": "nonsense"})
        except m.ManagementDecisionUnavailable:
            unavailable_rejected = True
        try:
            m._parse_decision({"rc": 0, "out": "nonsense"})
        except m.ManagementDecisionUnavailable:
            malformed_rejected = True
        failed_turn_rejected = False
        try:
            m.review_now(
                c4["case_id"],
                decide_fn=lambda *_a: {"rc": 1, "failed": True,
                                        "reason": "provider authentication unavailable"})
        except m.ManagementDecisionUnavailable:
            failed_turn_rejected = True
        with m._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT lease_owner,lease_until,next_review_at<=now()+interval '31 seconds'
                           FROM management_cases WHERE case_id=%s""", (c4["case_id"],))
            released = cur.fetchone() == (None, None, True)
            cur.execute("SELECT count(*) FROM management_decisions WHERE case_id=%s", (c4["case_id"],))
            no_invented_decision = cur.fetchone()[0] == 0
        fail_owned = (unavailable_rejected and malformed_rejected and failed_turn_rejected
                      and released and no_invented_decision)

        # A controller may request an immediate targeted decision. If the duty
        # sweep owns its lease, review_now returns immediately without a second
        # model call; after release exactly one targeted turn decides it.
        c5 = m.signal(keys[4], "controller generic failure", "state_change",
                      {"error": "unexpected"}, worker=f"worker2-{suffix}")
        case_ids.append(c5["case_id"])
        held = m._claim_one(c5["case_id"], f"duty-owner-{suffix}")
        calls = []
        busy = m.review_now(c5["case_id"], decide_fn=lambda *a: calls.append(a) or {"action": "continue"})
        with m._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE management_cases SET lease_owner=NULL,lease_until=NULL WHERE case_id=%s",
                        (c5["case_id"],))
        targeted = m.review_now(
            c5["case_id"],
            decide_fn=lambda *a: calls.append(a) or
                {"action": "retry", "rationale": "transient internal failure", "confidence": .9,
                 "review_in_s": 60, "message": "retry with fresh context"},
            send_fn=lambda *a: sent.append(a), alert_fn=lambda *a: incidents.append(a))
        targeted_no_overlap = (held is not None and busy["action"] == "busy" and len(calls) == 1
                               and targeted["action"] == "retry")

        # A terminal pulse cannot answer an old async status request. Reconcile
        # it exactly once instead of reopening a manager meeting every tick.
        terminal_work = f"management-terminal-pulse:{suffix}"
        pulse_ids.append(terminal_work)
        c6 = m.signal(keys[5], "retired QA worker", "heartbeat_silent",
                      {"beat_age_s": 999}, work_id=terminal_work,
                      worker=f"retired-worker-{suffix}")
        case_ids.append(c6["case_id"])
        terminal_qid = m.ask_internal(c6["case_id"], "team-lead",
                                      f"retired-worker-{suffix}", "Send status",
                                      reply_within_s=30, send_fn=lambda *a: sent.append(a))
        with m._conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO agent_pulse(work_id,kind,label,status,finished_at)
                           VALUES (%s,'qa-story','retired fixture','reaped',now())""",
                        (terminal_work,))
        # Other live terminal questions may legitimately reconcile in the same
        # sweep; assert our fixture's state below instead of assuming a global count.
        terminal_reconciled = m._reconcile_terminal_questions() >= 1
        with m._conn() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM management_questions WHERE question_id=%s",
                        (terminal_qid,))
            question_cancelled = cur.fetchone() == ("cancelled",)
            cur.execute("SELECT status,trigger FROM management_cases WHERE case_id=%s",
                        (c6["case_id"],))
            terminal_case_resolved = cur.fetchone() == ("resolved", "terminal_work_reconciled")
        terminal_question_one_shot = (terminal_reconciled and question_cancelled
                                      and terminal_case_resolved
                                      and m._reconcile_terminal_questions() == 0)

        ok = all((progress_once, lease_exclusive, timer_not_stop, async_question,
                  fabricated_gate_rejected, genuine_gate_routed, fail_owned, targeted_no_overlap,
                  terminal_question_one_shot))
        print(f"progress={progress_once} lease_exclusive={lease_exclusive} timer_not_stop={timer_not_stop} "
              f"async_question={async_question} fake_human_gate_rejected={fabricated_gate_rejected} "
              f"real_human_gate={genuine_gate_routed} invalid_decision_owned={fail_owned} "
              f"targeted_no_overlap={targeted_no_overlap} "
              f"terminal_question_one_shot={terminal_question_one_shot} "
              f"terminal_reconciled={terminal_reconciled} question_cancelled={question_cancelled} "
              f"terminal_case_resolved={terminal_case_resolved}")
        print("PASS: durable event+periodic agentic management, async escalation, no overlap" if ok else "FAIL")
    finally:
        if case_ids:
            with m._conn() as c, c.cursor() as cur:
                cur.execute("DELETE FROM management_questions WHERE case_id=ANY(%s)", (case_ids,))
                cur.execute("DELETE FROM management_decisions WHERE case_id=ANY(%s)", (case_ids,))
                cur.execute("DELETE FROM management_events WHERE case_id=ANY(%s)", (case_ids,))
                cur.execute("DELETE FROM management_cases WHERE case_id=ANY(%s)", (case_ids,))
                cur.execute("DELETE FROM authority_reviews WHERE tenant_id LIKE %s", (f"tenant-{suffix}%",))
                cur.execute("DELETE FROM authority_decisions WHERE tenant_id LIKE %s", (f"tenant-{suffix}%",))
                cur.execute("DELETE FROM authority_envelopes WHERE tenant_id LIKE %s", (f"tenant-{suffix}%",))
                if pulse_ids:
                    cur.execute("DELETE FROM agent_pulse WHERE work_id=ANY(%s)", (pulse_ids,))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
