import json
import os
import sys
import time
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "qa"))

import factory  # noqa: E402
import dev_loop  # noqa: E402
import qa_explorer  # noqa: E402


def test_codex_qa_tiers_are_explicit_and_cost_ordered():
    assert factory.CODEX_LIGHT_MODEL == "gpt-5.6-luna"
    assert factory.CODEX_MODEL == "gpt-5.6-sol"
    assert factory.CODEX_LIGHT_REASONING_EFFORT == "low"
    assert factory._codex_cost(1_000_000, 0, factory.CODEX_LIGHT_MODEL) < \
           factory._codex_cost(1_000_000, 0, factory.CODEX_MODEL)


def test_adversarial_page_text_cannot_make_qa_decision_prompt_unbounded():
    state = {
        "url": "http://127.0.0.1:8816/", "title": "fixture",
        "viewport": {"width": 390, "height": 844},
        "statusText": "status" * 10000, "bodyText": "body" * 10000,
        "console_errors": ["error" * 1000] * 10,
        "recent_requests": [
            {"method": "GET", "url": "https://example.invalid/" + "x" * 5000, "status": 200}
            for _ in range(20)
        ],
        "elements": [
            {"idx": i, "tag": "button", "text": f"control-{i}-" + "x" * 1000,
             "href": "https://example.invalid/" + "y" * 1000}
            for i in range(80)
        ],
    }
    history = [
        {"step": i, "action": {"cmd": "click", "value": "x" * 3000},
         "expected": "expected" * 1000, "bug": "bug" * 1000, "matched": False}
        for i in range(12)
    ]
    checklist = [{"aspect": "aspect" * 500, "covered": False} for _ in range(6)]
    prompt = qa_explorer._decide_prompt(
        "vision", {"title": "story", "steps": ["inspect"], "expected": "safe"},
        state, history, checklist)

    assert len(prompt) < 50000
    assert "additional controls omitted from this bounded prompt" in prompt
    assert prompt.count("STILL UNTESTED:") == 1


def test_routine_story_omits_irrelevant_accessibility_payload_and_declares_mechanical_wait():
    state = {
        "url": "http://app/", "title": "app", "viewport": {"width": 1280, "height": 800},
        "bodyText": "Enquiry form", "statusText": "Ready", "elements": [],
        "accessibilityTree": "AX" * 20_000,
        "accessibilityEvents": [{"text": "event" * 1000}] * 20,
    }
    story = {"title": "Submit an enquiry", "steps": ["Enter a name", "Send"],
             "expected": "The enquiry is accepted."}
    prompt = qa_explorer._decide_prompt(
        "vision", story, state, [], [{"aspect": "submit the enquiry", "covered": False}])

    assert "ACCESSIBILITY_TREE" not in prompt
    assert "ASYNC/EXTERNAL WAITING" in prompt
    assert '"wait_for"' in prompt
    assert "do not open a paid vision pass for navigation planning" in prompt
    assert len(prompt) < 22_000


def test_accessibility_story_keeps_accessibility_evidence():
    state = {"url": "http://app/", "title": "app", "bodyText": "Status", "statusText": "Ready",
             "elements": [], "accessibilityTree": "- status: Ready"}
    story = {"title": "Screen reader announcement", "steps": ["Use a screen reader"],
             "expected": "The ARIA live region is announced."}
    prompt = qa_explorer._decide_prompt(
        "vision", story, state, [], [{"aspect": "hear the announcement", "covered": False}])
    assert "ACCESSIBILITY_TREE" in prompt


def test_qa_call_forwards_no_retry_operation_bound(monkeypatch):
    received = {}

    def fake_agent(role, repo, task, **kwargs):
        received.update(kwargs)
        return {"rc": 0, "out": "{}"}

    monkeypatch.setattr(factory, "agent", fake_agent)
    qa_explorer._call_agent("qa-security", ".", "task", light=True, timeout=37, retries=0)
    assert received == {"light": True, "timeout": 37, "retries": 0, "compact": True}


def test_qa_structured_judge_can_use_frontier_model_with_bounded_reasoning(monkeypatch):
    received = {}

    def fake_agent(role, repo, task, **kwargs):
        received.update(kwargs)
        return {"rc": 0, "out": "{}"}

    monkeypatch.setattr(factory, "agent", fake_agent)
    qa_explorer._call_agent(
        "qa-security", ".", "task", timeout=90, retries=0, reasoning_effort="high")
    assert received == {
        "compact": True, "timeout": 90, "retries": 0, "reasoning_effort": "high"}
    assert qa_explorer._EVALUATE_REASONING_EFFORT in (
        "minimal", "low", "medium", "high", "xhigh", "max")


def test_accessibility_evaluation_sends_one_full_tree_plus_bounded_delta():
    story = {"title": "Keyboard and screen reader", "steps": ["Press Tab"],
             "expected": "Focus and the announcement are accessible."}
    common = {
        "url": "http://app/", "title": "app", "bodyText": "Page", "statusText": "Ready",
        "elements": [], "accessibilityRegions": [{"role": "status", "text": "x" * 500}] * 20,
        "accessibilityTree": "AX-NODE " * 2000,
        "accessibilityEvents": [{"text": "old"}],
        "actualAssistiveTechnologyEvents": [{"text": "old utterance"}],
        "accessibilityPlatformEvents": [{"type": "old"}],
    }
    before = {**common, "activeElement": {"role": "button", "name": "Before"}}
    after = {**common, "activeElement": {"role": "button", "name": "After"},
             "accessibilityEvents": common["accessibilityEvents"] + [{"text": "new status"}],
             "actualAssistiveTechnologyEvents": common["actualAssistiveTechnologyEvents"]
                                                   + [{"text": "new utterance"}],
             "accessibilityPlatformEvents": common["accessibilityPlatformEvents"] + [{"type": "focus"}]}
    prompt = qa_explorer._evaluate_prompt("vision", story, "focus advances", {}, before, after, ["focus"])
    assert prompt.count("ACCESSIBILITY_TREE") == 1
    assert "NEW ACTUAL AT EVENTS" in prompt and "new utterance" in prompt
    assert len(prompt) < 30_000


def test_incomplete_diagnosis_is_compact_and_uses_bounded_full_model_reasoning(monkeypatch):
    received = {}

    def fake_call(role, repo, task, **kwargs):
        received.update({"role": role, "repo": repo, "task": task, "kwargs": kwargs})
        return {"model": "gpt-5.6-sol", "out": json.dumps({
            "disposition": "continue_possible", "bug": None, "severity": "none",
            "blocking": False, "reason": "use the named landmark", "demonstrated": [],
        })}

    monkeypatch.setattr(qa_explorer, "_call_agent", fake_call)
    explorer = qa_explorer.Explorer("http://app/", "vision", autostart=False)
    story = {"title": "Accessible navigation", "steps": ["Use a screen reader"],
             "expected": "Every control is announced."}
    state = {
        "url": "http://app/", "title": "app", "bodyText": "Page " * 5000,
        "statusText": "Ready " * 1000, "accessibilityTree": "AX " * 5000,
        "accessibilityEvents": [{"text": "event " * 200}] * 30,
        "actualAssistiveTechnologyEvents": [{"text": "utterance " * 100}] * 30,
        "accessibilityPlatformEvents": [{"type": "focus", "name": "x" * 500}] * 30,
        "elements": [{"idx": i, "tag": "button", "text": "control-" + str(i)}
                     for i in range(80)],
    }
    history = [{"action": {"cmd": "press"}, "state": "x" * 1000} for _ in range(20)]
    diagnosis = explorer._ai_incomplete_diagnosis(
        story, state, ["reach every control"], history, records=history)

    assert diagnosis["disposition"] == "continue_possible"
    assert received["kwargs"]["reasoning_effort"] == qa_explorer._DIAGNOSE_REASONING_EFFORT
    assert received["kwargs"]["retries"] == 0
    assert len(received["task"]) < 22_000


def test_nonvisual_governance_judge_retains_but_does_not_open_screenshot():
    story = {"title": "Gate irreversible sending", "steps": ["Attempt send before approval"],
             "expected": "The send is denied until approval and no external side effect occurs."}
    state = {"url": "http://app/", "title": "app", "bodyText": "Approval required",
             "statusText": "Denied", "elements": [], "screenshot": "/tmp/large-proof.png"}
    prompt = qa_explorer._evaluate_prompt(
        "vision", story, "The queue records an approval-required denial.",
        {"action_kind": "click"}, state, state, ["deny send before approval"])

    assert "/tmp/large-proof.png" not in prompt
    assert "do not open a paid vision pass" in prompt


def test_visual_story_keeps_screenshot_available_to_judge():
    story = {"title": "Responsive mobile layout", "steps": ["Switch to a 390px viewport"],
             "expected": "No controls overlap or clip."}
    state = {"url": "http://app/", "title": "app", "bodyText": "Page", "statusText": "Ready",
             "elements": [], "screenshot": "/tmp/mobile-proof.png"}
    prompt = qa_explorer._evaluate_prompt(
        "vision", story, "The mobile layout remains aligned.",
        {"action_kind": "viewport"}, state, state, ["inspect mobile layout"])

    assert "/tmp/mobile-proof.png" in prompt
    assert "pixels are material" in prompt


def test_composite_judge_receives_bounded_prior_grounded_journey_without_images():
    story = {"title": "Gate sending", "steps": ["Deny, approve, then send"],
             "expected": "No send before approval; audit actor and reason; approved send succeeds."}
    state = {"url": "http://app/", "title": "app", "bodyText": "Sent after approval",
             "statusText": "Sent", "elements": [], "screenshot": "/tmp/final.png"}
    prior = [{
        "step": index, "action": {"cmd": "click", "target_text": "Drain queue"},
        "expected": "Denied before approval", "targeting": {"driver_ok": True},
        "verdict": {"verdict": "pass", "matches_expected": True},
        "actual": {"url": "http://app/", "statusText": "Approval required",
                   "bodyText": "send denied " + "x" * 5000, "screenshot": f"/tmp/prior-{index}.png"},
    } for index in range(20)]
    prompt = qa_explorer._evaluate_prompt(
        "vision", story, "The approved follow-up is sent once.", {"action_kind": "click"},
        state, state, ["deny, approve with audit, and send"], prior_records=prior)

    assert "PRIOR GROUNDED JOURNEY EVIDENCE" in prompt
    assert "prior-19.png" not in prompt and "/tmp/final.png" not in prompt
    assert "Denied before approval" in prompt
    assert len(prompt) < 32_000


def test_focused_regression_judge_keeps_only_useful_bounded_journey_receipts():
    story = {
        "id": "US-008", "category": "focused-regression",
        "goal": "Retry exactly one dead-lettered job",
        "steps": ["Reach dead letter", "Retry"],
        "expected_outcome": "The retry succeeds.",
    }
    state = {"url": "http://app/", "title": "Queue", "bodyText": "Retry available " * 1000,
             "statusText": "dead_letter", "elements": [{"idx": 1, "tag": "button", "text": "Retry"}]}
    prior = [{
        "step": index, "action": {"cmd": "click", "target_text": f"irrelevant-old-{index}"},
        "expected": f"setup-{index}", "targeting": {"driver_ok": True},
        "verdict": {"verdict": "pass", "matches_expected": True},
        "demonstrated": (["Reached dead letter"] if index == 4 else []),
        "actual": {"url": "http://app/", "statusText": f"state-{index}",
                   "bodyText": "x" * 3000},
    } for index in range(12)]

    prompt = qa_explorer._focused_evaluate_prompt(
        "vision", story, "Retry the dead-lettered job", {"driver_ok": True},
        state, state, ["Retry succeeds"], prior_records=prior)

    assert "SEALED FOCUSED REGRESSION" in prompt
    assert "irrelevant-old-0" not in prompt
    assert "irrelevant-old-4" in prompt
    assert "irrelevant-old-11" in prompt
    assert len(prompt) < 26_000


def test_focused_regression_next_action_prompt_is_bounded_and_keeps_driver_contract():
    story = {
        "id": "US-008", "category": "focused-regression",
        "goal": "Retry the exact dead-lettered job",
        "steps": ["Inspect restored state", "Click Retry", "Drain to success"],
        "expected_outcome": "The governed retry succeeds once.",
    }
    state = {
        "url": "http://app/", "title": "Queue", "bodyText": "document " * 5000,
        "viewportText": "dead_letter Retry", "statusText": "dead_letter " * 1000,
        "documentLandmarks": [{"label": f"surface-{index}", "y": index * 100}
                              for index in range(100)],
        "elements": [{"idx": index, "tag": "button", "text": f"control-{index}"}
                     for index in range(100)],
    }
    checklist = [{"aspect": f"Story step {index}.1", "covered": index == 1}
                 for index in range(1, 5)]

    prompt = qa_explorer._focused_decide_prompt(
        "vision", story, state, [{"step": 0, "action": {"cmd": "scroll"}, "matched": True}],
        checklist)

    assert "SEALED FOCUSED REGRESSION" in prompt
    assert "EARLIEST UNTESTED work owns the next action" in prompt
    assert "exact observed LABEL" in prompt
    assert "wait_for" in prompt and "scenario_matrix" in prompt
    assert "Story step 2.1" in prompt
    assert "cannot be waived by done=true" in prompt
    assert len(prompt) < 18_000


def test_one_remaining_resumed_story_clause_uses_compact_gapfill_prompts(monkeypatch):
    story = {
        "id": "US-011", "category": "happy", "title": "Operate CEO risk dashboard",
        "steps": ["Seed dashboard state", "Acknowledge by mouse and keyboard, then reload"],
        "expected_outcome": "Acknowledgements persist, focus remains visible, and private data stays hidden.",
    }
    ledger = [
        {"aspect": "Seed and inspect the dashboard", "covered": True},
        {"aspect": "Acknowledge by mouse and keyboard, reload, inspect focus and privacy", "covered": False},
    ]
    state = {
        "url": "http://app/", "title": "CEO", "bodyText": "Unresolved blockers",
        "viewportText": "Acknowledge", "statusText": "Ready",
        "elements": [{"idx": 1, "tag": "button", "text": "Acknowledge"}],
    }
    prompts = []

    def fake_call(_role, _repo, prompt, **_kwargs):
        prompts.append(prompt)
        if "Choose ONE next browser action" in prompt:
            body = {"reasoning": "exercise final clause", "next_action": {"cmd": "click",
                    "target_text": "Acknowledge", "role": "button"},
                    "expected": "The blocker is acknowledged", "wait_for": None,
                    "covers": [ledger[1]["aspect"]], "done": False}
        else:
            body = {"target_confirmed": True, "matches_expected": True, "verdict": "pass",
                    "bug": None, "severity": "none", "blocking": False, "demonstrated": []}
        return {"rc": 0, "out": json.dumps(body)}

    monkeypatch.setattr(qa_explorer, "_call_agent", fake_call)
    explorer = qa_explorer.Explorer("http://app/", "vision", autostart=False)
    explorer._ai_decide(story, state, [], checklist=ledger)
    explorer._ai_evaluate(
        story, "The blocker is acknowledged", {"driver_ok": True}, state, state,
        untested=[ledger[1]["aspect"]], prior_records=[{"verdict": {"verdict": "pass"}}],
        compact_gapfill=qa_explorer._compact_gapfill_prompt_mode(story, ledger))

    assert qa_explorer._compact_gapfill_prompt_mode(story, ledger) is True
    assert all("SEALED FOCUSED REGRESSION" in prompt for prompt in prompts)
    assert max(map(len, prompts)) < 26_000


def test_successful_but_truncated_decision_gets_one_bounded_structured_retry(monkeypatch):
    story = {
        "id": "US-009", "title": "Gate follow-up sending",
        "steps": ["Edit the draft body", "Submit the draft for approval"],
        "expected_outcome": "Sending remains gated until approval.",
    }
    ledger = [{"aspect": "Story step 1.1: Edit the draft body", "covered": False,
               "explicit": True}]
    state = {
        "url": "http://app/", "title": "Draft", "bodyText": "Subject Body",
        "viewportText": "Body", "statusText": "Draft ready",
        "elements": [{"idx": 30, "tag": "textarea", "text": "Body", "role": "textbox"}],
    }
    calls = []

    def fake_call(_role, _repo, prompt, **_kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return {"rc": 0, "out_full": '{"reasoning":"edit body","next_action":{"cmd":"type",'
                    '"target_text":"Body","value":"Thanks for'}
        return {"rc": 0, "out_full": json.dumps({
            "reasoning": "edit body", "intent": "Body",
            "next_action": {"cmd": "type", "target_text": "Body", "role": "textbox",
                            "value": "Thanks for getting in touch."},
            "expected": "The Body field contains the concise draft text.",
            "wait_for": None, "covers": [ledger[0]["aspect"]], "done": False,
        })}

    monkeypatch.setattr(qa_explorer, "_call_agent", fake_call)
    explorer = qa_explorer.Explorer("http://app/", "vision", autostart=False)

    decision = explorer._ai_decide(story, state, [], checklist=ledger)

    assert len(calls) == 2
    assert "STRUCTURED-OUTPUT RECOVERY" in calls[1]
    assert decision["next_action"]["cmd"] == "type"
    assert decision["next_action"]["value"] == "Thanks for getting in touch."


def test_judge_never_requires_uncreated_independent_entity_to_be_nonzero():
    prompt = qa_explorer._evaluate_prompt(
        "vision",
        {"id": "US-003", "steps": ["Submit an enquiry", "Drain its review queue"],
         "expected_outcome": "All panels transition from empty to populated."},
        "Every panel reflects the enquiry.", {"intended": None, "driver_ok": True},
        {"bodyText": "Approval Total 0 Pending 0"},
        {"bodyText": "Enquiry received Approval Total 0 Pending 0", "settled": True},
        untested=["Inspect all panels"], prior_records=[])

    normalized = " ".join(prompt.split())
    assert "CAUSAL-POPULATION GUARD" in normalized
    assert "when no story step created or submitted that entity" in normalized
    assert "QUANTIFIER-SCOPE GUARD" in normalized
    assert "does not silently require exactly one audit event" in normalized
    assert "never paraphrase that ticket constraint" in normalized.lower()
    assert "PROJECTION-SCOPE GUARD" in normalized
    assert "broad request to inspect both panels is not a parity requirement" in normalized


def test_seeded_story_uses_matching_fixture_before_paid_or_keyboard_decision():
    story = {
        "id": "US-011", "steps": ["Seed enquiries across every required status.", "Inspect the CEO view."],
        "expected_outcome": "The seeded dashboard is accurate.",
    }
    state = {"elements": [
        {"idx": 1, "tag": "button", "role": "button", "text": "Load US-010 claims"},
        {"idx": 2, "tag": "button", "role": "button", "text": "Load US-011 CEO risk"},
    ]}
    coverage = [{"aspect": "Seed all statuses", "covered": False}]

    decision = qa_explorer._pending_story_seed_decision(story, state, coverage, [])

    assert decision["next_action"] == {
        "cmd": "click", "target_text": "Load US-011 CEO risk", "role": "button"}
    assert decision["covers"] == []


def test_incomplete_diagnosis_cannot_reuse_traversal_from_before_fixture_load(monkeypatch):
    def fake_call(*_args, **_kwargs):
        return {"out_full": json.dumps({
            "disposition": "app_defect",
            "bug": "No reachable acknowledgement control exists.",
            "severity": "high", "blocking": True,
            "reason": "The exhaustive traversal found no acknowledgement control.",
            "demonstrated": [],
        })}

    monkeypatch.setattr(qa_explorer, "_call_agent", fake_call)
    explorer = qa_explorer.Explorer("http://app/", "vision", autostart=False)
    records = [
        {"action": {"cmd": "traverse", "value": "forward"},
         "targeting": {"driver_ok": True}},
        {"action": {"cmd": "click", "target_text": "Load US-011 CEO risk"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Load US-011 CEO risk"}},
    ]

    result = explorer._ai_incomplete_diagnosis(
        {"id": "US-011", "steps": ["Seed risks", "Acknowledge one"]},
        {"url": "http://app/", "bodyText": "5 risks loaded", "elements": []},
        ["Acknowledge one risk"], [], records=records)

    assert result["disposition"] == "continue_possible"
    assert result["bug"] is None
    assert "predates a confirmed seed" in result["reason"]


def test_resumed_checkpoint_actions_remain_visible_to_the_next_action_planner():
    rows = [{
        "action": "traverse  ='forward'",
        "expected": "Every focusable control is reached in visual order.",
        "actual": "driver_ok=True; effect_registered=True; target=''; settled=True",
        "verdict": "match",
    }, {
        "action": "press idx=3 ='ArrowDown'",
        "expected": "The Agent selector advances from Success to Timeout.",
        "actual": "driver_ok=True; effect_registered=False; target='Agent'; settled=True",
        "verdict": "mismatch",
    }, {
        "action": "viewport  ={'width': 375, 'height': 844}",
        "expected": "The mobile layout remains usable.",
        "actual": "driver_ok=True; effect_registered=True; target=''; settled=True",
        "verdict": "match",
    }]

    history = qa_explorer._planner_history_from_resume(rows)
    rendered = qa_explorer._fmt_history(history, limit=5)

    assert [item["action"]["cmd"] for item in history] == ["traverse", "press", "viewport"]
    assert history[0]["action"]["value"] == "forward"
    assert history[1]["action"]["value"] == "ArrowDown"
    assert history[2]["action"]["value"] == {"width": 375, "height": 844}
    assert history[1]["action"]["target_text"] == "Agent"
    assert history[1]["targeting"]["effect_registered"] is False
    assert "ArrowDown" in rendered and "375" in rendered and "traverse" in rendered


def test_ceo_acknowledgement_surface_fence_avoids_read_only_staff_blocker_list():
    story = {"id": "US-011", "persona": "CEO", "steps": [
        "Acknowledge one open blocker with the mouse and another with Enter."]}
    state = {"documentLandmarks": [
        {"label": "Unresolved blockers"}, {"label": "CEO Command View"},
        {"label": "Risk Signals"},
    ]}

    inspect = qa_explorer._contract_fenced_owned_surface_action(
        {"cmd": "inspect_surfaces", "targets": ["Unresolved blockers"]},
        story, "Inspect acknowledgement controls for open blockers.", state)
    scroll = qa_explorer._contract_fenced_owned_surface_action(
        {"cmd": "scroll", "target_text": "Unresolved blockers"},
        story, "Reach the CEO acknowledgement controls.", state)

    assert inspect["targets"] == ["Risk Signals"]
    assert scroll["target_text"] == "Risk Signals"
    assert inspect["_qa_surface_owner_fenced"] is True


def test_keyboard_acknowledgement_privacy_clause_is_not_misread_as_form_entry():
    aspect = ("Acknowledge one blocker with the mouse and another with Enter or Space; verify private audit "
              "events expose no contact email, phone, dog notes, or raw metadata.")
    story = {"id": "US-011", "steps": [aspect]}
    state = {"elements": [
        {"idx": 1, "tag": "input", "type": "email", "associatedLabel": "Email", "formIndex": 0},
        {"idx": 2, "tag": "input", "type": "tel", "associatedLabel": "Phone", "formIndex": 0},
        {"idx": 3, "tag": "input", "type": "text", "associatedLabel": "Dog name", "formIndex": 0},
        {"idx": 4, "tag": "input", "type": "number", "associatedLabel": "Dog age", "formIndex": 0},
        {"idx": 5, "tag": "button", "type": "submit", "text": "Send enquiry", "formIndex": 0},
    ]}

    assert qa_explorer._pending_explicit_form_sequence_decision(
        story, state, [{"aspect": aspect, "covered": False}]) is None


def test_prior_journey_accepts_portable_string_actions_without_crashing():
    rows = [{
        "action": "scenario_matrix  ={'cases': ['submit and drain']}",
        "expected": "Create the enquiry and complete its queued review.",
        "actual": "driver_ok=True; effect_registered=True; target=''; settled=True",
        "targeting": "legacy portable targeting receipt",
        "verdict": "mismatch",
    }]

    rendered = qa_explorer._fmt_prior_journey_evidence(rows)

    assert "scenario_matrix" in rendered
    assert "driver_ok=True" in rendered


def test_checkpoint_recovery_retries_exact_failed_keyboard_boundary_once():
    keyboard = ("Using only Tab, Shift+Tab, Arrow keys, Space, and Enter, reach and operate every control; "
                "verify visible focus.")
    story = {"id": "US-012", "steps": [keyboard], "expected_outcome": "All controls are operable."}
    coverage = [{"aspect": keyboard, "covered": False}]
    state = {"elements": [{"idx": 3, "tag": "select", "role": "combobox",
                            "associatedLabel": "Agent role", "value": "Success"}]}
    rows = [{
        "action": "press idx=3 ='ArrowDown'",
        "expected": "The Agent role selector advances from Success to Timeout.",
        "actual": "driver_ok=True; effect_registered=False; target='Agent role'; settled=True",
        "verdict": "mismatch",
    }]

    decision = qa_explorer._checkpoint_failed_keyboard_decision(
        story, state, coverage, resume_rows=rows, records=[])

    assert decision["next_action"] == {
        "cmd": "press", "target_text": "Agent role", "value": "ArrowDown",
        "_qa_checkpoint_keyboard_retry": True, "role": "combobox",
    }
    assert decision["covers"] == [keyboard]
    assert qa_explorer._checkpoint_failed_keyboard_decision(
        story, state, coverage, resume_rows=rows,
        records=[{"action": decision["next_action"]}]) is None

    dwell_first = [
        {"aspect": "Dwell idle for 10 seconds and verify no focus loss.", "covered": False},
        {"aspect": keyboard, "covered": False},
    ]
    assert qa_explorer._checkpoint_failed_keyboard_decision(
        story, state, dwell_first, resume_rows=rows, records=[]) is None


def test_explicit_dwell_and_refresh_clauses_compile_from_story_and_live_state():
    dwell = ("Dwell idle for 10 seconds on the public form, confirmation screen, staff console, CEO view, "
             "diagnostics, and any error screen; verify no focus or scroll loss.")
    refresh = "After interactions, refresh and repeat one keyboard path; all controls remain accessible."
    coverage = [{"aspect": dwell, "covered": False}, {"aspect": refresh, "covered": False}]
    state = {"documentLandmarks": [
        {"label": "Dog Walking Trust-First", "role": "h1", "y": 0},
        {"label": "Public enquiry", "role": "region", "y": 200},
        {"label": "Staff operating console", "role": "region", "y": 900},
        {"label": "CEO Command View", "role": "region", "y": 1800},
        {"label": "Operational Diagnostics", "role": "h2", "y": 2600},
    ]}

    decision = qa_explorer._pending_explicit_evidence_decision({}, state, coverage, records=[])

    assert decision["next_action"]["cmd"] == "dwell_surfaces"
    assert decision["next_action"]["duration_s"] == 10
    assert set(decision["next_action"]["targets"]) == {
        "Public enquiry", "Staff operating console", "CEO Command View", "Operational Diagnostics",
    }
    coverage[0]["covered"] = True
    reload = qa_explorer._pending_explicit_evidence_decision({}, state, coverage, records=[])
    assert reload["next_action"]["cmd"] == "reload"
    repeated = qa_explorer._pending_explicit_evidence_decision(
        {}, state, coverage, records=[{"action": reload["next_action"]}])
    assert repeated["next_action"]["cmd"] == "traverse"
    assert repeated["next_action"]["_qa_post_refresh_keyboard"] is True
    assert repeated["next_action"]["_qa_inventory_derived"] is True
    assert repeated["next_action"]["pace_ms"] == 300

    # An unavailable conditional dwell state must not starve the later read-only post-refresh proof. A generic
    # model-authored traversal is not the sealed paced/AT boundary, so the compiler may safely supersede it.
    unavailable_confirmation = {
        "aspect": "Dwell idle for 10 seconds on confirmation screen; verify no focus loss.",
        "covered": False, "atomic_kind": "dwell_surface"}
    post_refresh = {"aspect": refresh, "covered": False,
                    "atomic_kind": "post_reload_keyboard"}
    superseding = qa_explorer._pending_explicit_evidence_decision(
        {}, state, [unavailable_confirmation, post_refresh], records=[
            {"action": {"cmd": "reload"}}, {"action": {"cmd": "traverse", "value": "forward"}}])
    assert superseding["next_action"]["_qa_post_refresh_keyboard"] is True
    assert superseding["covers"] == [refresh]


def test_compound_timed_sequence_does_not_skip_its_ordered_actions():
    compound = ("Check data-retention consent, leave marketing consent unchecked, dwell idle for 10 "
                "seconds, verify input and focus remain unchanged, then press Enter to submit.")
    state = {"documentLandmarks": [
        {"label": "Public enquiry", "role": "region", "y": 200},
        {"label": "Staff operating console", "role": "region", "y": 900},
        {"label": "CEO Command View", "role": "region", "y": 1800},
    ]}

    decision = qa_explorer._pending_explicit_evidence_decision(
        {}, state, [{"aspect": compound, "covered": False}], records=[])

    # The ordinary planner must perform consent -> timed observation -> Enter in story order.  Treating the
    # compound row as a pure dwell would spend 30 seconds here while making the submission impossible.
    assert decision is None


def test_compound_consent_dwell_enter_migrates_and_requires_effectful_form_enter():
    source = ("Check data-retention consent, leave marketing consent unchecked, dwell idle for 10 seconds, "
              "verify input and focus remain unchanged, then press Enter to submit.")
    migrated = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": source, "covered": False, "explicit": False}])

    assert [item["atomic_kind"] for item in migrated] == ["semantic", "timed_wait", "form_enter"]
    migrated[0]["covered"] = True
    wait = qa_explorer._pending_explicit_evidence_decision({}, {}, migrated, records=[])
    assert wait["next_action"] == {"cmd": "wait", "value": "10s"}
    migrated[1]["covered"] = True
    state = {"elements": [
        {"idx": 6, "tag": "input", "type": "text", "associatedLabel": "Name", "formIndex": 0},
        {"idx": 26, "tag": "input", "type": "checkbox", "associatedLabel": "Consent", "formIndex": 0},
    ]}
    enter = qa_explorer._pending_explicit_evidence_decision({}, state, migrated, records=[])
    assert enter["next_action"] == {
        "cmd": "press", "idx": 6, "target_text": "Name", "role": "textbox", "value": "Enter"}

    aspect = migrated[2]["aspect"]
    targeting = {"action_kind": "press", "action_key": "Enter", "effect_registered": False}
    assert qa_explorer._grounded_demonstrated(
        [aspect], targeting, {}, {}, require_mechanical=True) == []
    targeting["effect_registered"] = True
    assert qa_explorer._grounded_demonstrated(
        [aspect], targeting, {}, {}, require_mechanical=True) == [aspect]

    formerly_broad = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": source, "covered": True,
         "proof": {"engine": "mechanical-browser-proof", "action_kind": "press",
                   "recorded_at": 10.0}}])
    assert all(item["covered"] is False for item in formerly_broad)
    assert all("proof" not in item for item in formerly_broad)
    assert all(item["coverage_repaired"] == "compound-boundary-required-fresh-proof"
               for item in formerly_broad)


def test_passive_browser_commands_cannot_prove_imperative_business_mutations():
    submit = "Story step 3.1: Submit the first valid public enquiry and verify it is accepted."
    drain = "Required evidence: Drain the queued enquiry exactly once."
    evaluator_claims = [submit, drain]
    settled = {"url": "http://app", "bodyText": "Enquiries queued"}

    for command in ("reload", "wait", "inspect_surfaces", "reset_storage", "viewport"):
        assert qa_explorer._grounded_demonstrated(
            evaluator_claims,
            {"action_kind": command, "driver_ok": True, "reloaded": command == "reload"},
            settled, settled,
        ) == []

    # The fence is causal, not lexical: an observation whose subject is a submission may still be proven by
    # the appropriate non-passive receipt elsewhere; only the leading imperative is rejected here.
    observation = "Expected outcome: Submission confirmation remains visible after the transition."
    assert qa_explorer._grounded_demonstrated(
        [observation], {"action_kind": "reload", "driver_ok": True, "reloaded": True},
        settled, settled,
    ) == [observation]


def test_click_cannot_prove_ten_second_dwell_and_invalid_checkpoint_atom_reopens():
    aspect = ("Dwell idle for 10 seconds on diagnostics; verify no input loss, focus loss, scroll jump, "
              "navigation, layout break, or unannounced status change")
    state = {
        "url": "http://app", "bodyText": "Agent jobs diagnostics: retrying",
        "scrollPosition": {"x": 0, "y": 200},
        "activeElement": {"tag": "button", "id": "drain"},
        "elements": [{"id": "drain", "tag": "button", "disabled": False}],
        "horizontalOverflow": False,
    }
    click = {"action_kind": "click", "driver_ok": True, "effect_registered": True,
             "label_matched": True, "intended": "Drain queue", "targeted_label": "Drain queue"}
    assert qa_explorer._grounded_demonstrated([aspect], click, state, state) == []

    wait = {"action_kind": "wait", "driver_ok": True,
            "wait_summary": {"waited": True, "requested_ms": 10000, "elapsed_ms": 10005}}
    assert qa_explorer._grounded_demonstrated([aspect], wait, state, state) == [aspect]

    repaired = qa_explorer._migrate_compound_coverage_ledger([{
        "aspect": aspect, "covered": True, "atomic_parent": "dwell-parent",
        "atomic_kind": "dwell_surface", "proof": {
            "engine": "mechanical-browser-proof", "action_kind": "click", "recorded_at": 10.0},
    }])
    assert repaired[0]["covered"] is False
    assert repaired[0]["coverage_repaired"] == "temporal-boundary-required-fresh-proof"
    assert "proof" not in repaired[0]

    post_reload = ("After refresh/reload, repeat one story-required keyboard path and verify focus, "
                   "announcements, and control operability.")
    assert qa_explorer._grounded_demonstrated([post_reload], click, state, state) == []
    repaired_keyboard = qa_explorer._migrate_compound_coverage_ledger([{
        "aspect": post_reload, "covered": True, "atomic_parent": "reload-parent",
        "atomic_kind": "post_reload_keyboard", "proof": {
            "engine": "mechanical-browser-proof", "action_kind": "click", "recorded_at": 11.0},
    }])
    assert repaired_keyboard[0]["covered"] is False
    assert repaired_keyboard[0]["coverage_repaired"] == "post-reload-keyboard-required-fresh-proof"


def test_empty_reload_cannot_prove_created_state_persistence_and_stale_checkpoint_reopens():
    create = ("Submit one valid enquiry, drain the agent queue, and verify lead review completes with a "
              "follow-up draft and completed agent job.")
    persist = ("Refresh the page and verify the created enquiry, completed job, follow-up draft, diagnostics, "
               "and audit trail persist without stale or corrupt UI.")
    ledger = [
        {"aspect": "Verify the initial app is empty.", "covered": True},
        {"aspect": create, "covered": False},
        {"aspect": persist, "covered": False},
    ]

    assert qa_explorer._ordered_grounded_aspects(ledger, [persist]) == []
    ledger[1]["covered"] = True
    assert qa_explorer._ordered_grounded_aspects(ledger, [persist]) == [persist]

    stale = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": "Verify the initial app is empty.", "covered": True},
        {"aspect": create, "covered": False},
        {"aspect": persist, "covered": True,
         "proof": {"engine": "codex", "action_kind": "reload"}},
    ])
    assert stale[2]["covered"] is False
    assert "proof" not in stale[2]
    assert stale[2]["coverage_repaired"] == "persisted-state-required-prior-creation-proof"

    legitimate = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": create, "covered": True,
         "proof": {"engine": "codex", "action_kind": "scenario_matrix", "recorded_at": 10}},
        {"aspect": persist, "covered": True,
         "proof": {"engine": "codex", "action_kind": "reload", "recorded_at": 20}},
    ])
    assert legitimate[1]["covered"] is True

    reversed_evidence = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": create, "covered": True,
         "proof": {"engine": "cumulative", "action_kind": "diagnose", "recorded_at": 30}},
        {"aspect": persist, "covered": True,
         "proof": {"engine": "codex", "action_kind": "reload", "recorded_at": 20}},
    ])
    assert reversed_evidence[1]["covered"] is False
    assert reversed_evidence[1]["coverage_repaired"] == \
        "persisted-state-required-prior-creation-proof"


def test_ordered_durable_records_restore_only_post_creation_persistence():
    create = ("Submit one valid enquiry, drain the agent queue, and verify lead review completes with a "
              "follow-up draft and completed agent job.")
    reload_aspect = ("Refresh the page and verify the created enquiry, completed job, follow-up draft, "
                     "diagnostics, and audit trail persist without stale or corrupt UI.")
    reopen_aspect = ("Close and reopen the app in a browser context retaining local storage, then verify "
                     "public confirmation/new-enquiry state, inbound leads, follow-up drafts, agent jobs, "
                     "CEO readiness, diagnostics, and audit events remain consistent.")
    ledger = [
        {"aspect": create, "covered": True},
        {"aspect": reload_aspect, "covered": False,
         "coverage_repaired": "persisted-state-required-prior-creation-proof"},
        {"aspect": reopen_aspect, "covered": False,
         "coverage_repaired": "persisted-state-required-prior-creation-proof"},
    ]
    records = [
        # The original reload was before creation and must not be allowed to certify persistence.
        {"action": "reload", "verdict": "match", "coverage_grounded": True,
         "covers": [reload_aspect]},
        {"action": "diagnose_cumulative_progress", "verdict": "match",
         "coverage_grounded": True, "covers": [create]},
        {"action": "goto  ='http://app/?reopen=1'", "verdict": "match",
         "coverage_grounded": True, "covers": [reopen_aspect]},
    ]

    restored = qa_explorer._restore_chronological_persistence_coverage(ledger, records)

    assert restored[0]["proof"]["recorded_at"] == 2.0
    assert restored[1]["covered"] is False
    assert restored[2]["covered"] is True
    assert restored[2]["proof"]["recorded_at"] == 3.0

    records.append({"action": "reload", "verdict": "match", "coverage_grounded": True,
                    "covers": [reload_aspect]})
    restored = qa_explorer._restore_chronological_persistence_coverage(ledger, records)
    assert restored[1]["covered"] is True
    assert restored[1]["proof"]["recorded_at"] == 4.0


def test_diagnostics_dwell_never_injects_unrelated_queue_drain_prerequisite():
    aspect = ("Dwell idle for 10 seconds on diagnostics; verify no input loss, focus loss, scroll jump, "
              "navigation, layout break, or unannounced status change")
    story = {"steps": ["Inspect queue diagnostics, then drain the queue in a separate later scenario."]}
    state = {
        "bodyText": "App jobs queued: 1. Pipeline jobs queued: 0. Queue diagnostics ready.",
        "elements": [{"idx": 4, "tag": "button", "text": "Drain queue", "disabled": False}],
    }

    assert qa_explorer._pending_queue_drain_decision(
        story, state, [{"aspect": aspect, "covered": False}]) is None


def test_queue_failure_setup_chooses_a_live_story_authorized_scenario():
    aspect = ("Submit one enquiry and drain the queue repeatedly until the job is failed or dead-lettered.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work", "steps": [
        "Set the Agent scenario to Timeout or Malformed Response.",
        aspect,
    ]}
    base = {
        "bodyText": "Agent jobs queued 1. Queue diagnostics ready.",
        "elements": [
            {"idx": 3, "tag": "select", "text": "Agent", "name": "agent-scenario",
             "value": "malformed", "options": "Success | Timeout | Approval required | Partial failure"},
            {"idx": 4, "tag": "button", "text": "Drain queue"},
        ],
    }
    coverage = [{"aspect": aspect, "covered": False}]

    setup = qa_explorer._pending_queue_drain_decision(story, base, coverage)
    assert setup["next_action"]["cmd"] == "type"
    assert setup["next_action"]["value"] == "Timeout"

    ready = {**base, "elements": [dict(base["elements"][0], value="timeout"), base["elements"][1]]}
    drain = qa_explorer._pending_queue_drain_decision(story, ready, coverage)
    assert drain["next_action"]["target_text"] == "Drain queue"


def test_live_agent_scenario_selection_is_mechanical_coverage():
    aspect = ("Set the Agent scenario to Timeout or Malformed Response and verify the selected failure mode "
              "is active before submission.")
    facts = {"action_kind": "type", "action_value": "Timeout", "driver_ok": True,
             "driver_control_value": "timeout"}
    assert qa_explorer._grounded_demonstrated(
        [aspect], facts, {}, {}, require_mechanical=True) == [aspect]
    assert qa_explorer._grounded_demonstrated(
        [aspect], {**facts, "driver_control_value": "success"}, {}, {},
        require_mechanical=True) == []


def test_empty_first_run_transition_keeps_approval_diagnostics_honestly_empty():
    final = ("Drain the queue and verify all public, staff, CEO, queue, approval, and audit panels "
             "transition live from empty to populated without a reload or blank screen.")
    story = {"id": "US-003", "title": "Recover from empty first-run data", "steps": [
        "Submit the first valid public enquiry from this empty state.",
        "Drain the queue and observe all panels again.",
    ]}
    targets = ["Public enquiry", "Staff operating console", "CEO command view",
               "Queue and Dead Letters", "Approval Summary", "Audit Summary"]
    action = {"cmd": "inspect_surfaces", "targets": targets}
    targeting = {"driver_ok": True, "landmark_dwell_summary": {
        "targets": targets, "all_targets_matched": True, "all_stable": True}}
    after = {"url": "http://app", "bodyText": (
        "Queue processing complete. 1 queued item processed. Approval Summary Total 0 "
        "No pending approval decisions."), "console_errors": []}
    records = [
        {"action": {"cmd": "scenario_matrix", "_qa_valid_form_submission": True},
         "targeting": {"driver_ok": True, "effect_registered": True}},
        {"action": {"cmd": "click", "target_text": "Drain queue"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Drain queue", "targeted_label": "Drain queue"}},
    ]
    coverage = [{"aspect": final, "covered": False}]

    assert qa_explorer._successful_empty_first_run_transition_coverage(
        story, action, {"verdict": "pass", "matches_expected": True, "bug": None},
        coverage, targeting, after, records) == [final]
    corrected, changed = qa_explorer._contract_grounded_empty_first_run_expected(
        story, "All six surfaces show consistent populated state including approval diagnostics.", targeting)
    assert changed is True and "may honestly remain empty" in corrected


def test_without_reload_is_not_compiled_as_a_reload_action():
    aspect = ("Submit the first valid public enquiry and verify all panels transition from empty to populated "
              "without reload or a blank screen.")
    coverage = [{"aspect": aspect, "covered": False}]

    assert qa_explorer._positive_refresh_requirement(aspect) is False
    assert qa_explorer._positive_refresh_requirement(
        "Refresh/reload after the story interactions and verify the refreshed state.") is True
    decision = qa_explorer._pending_explicit_evidence_decision(
        {"id": "US-003", "steps": [aspect]}, {"elements": [], "bodyText": "Ready"}, coverage, [])
    assert decision is None or decision["next_action"].get("cmd") != "reload"


def test_empty_first_run_continuation_resets_duplicates_then_finishes_from_one_submit_and_drain():
    submit = ("Submit the first valid public enquiry and verify all panels transition from empty to populated "
              "without reload or a blank screen.")
    drain = ("Drain the queue and verify the populated state remains coherent across the public, staff, CEO, "
             "queue, approval, and audit panels.")
    story = {"id": "US-003", "title": "Recover from empty first-run data", "steps": [
        "Reset local storage to an empty valid app state.", "Submit the first valid public enquiry.",
        "Drain the queue."]}
    coverage = [
        {"aspect": "Reset local storage to an empty valid app state.", "covered": True, "proof": {}},
        {"aspect": "Verify empty public trust claims and diagnostics.", "covered": True, "proof": {}},
        {"aspect": submit, "covered": False}, {"aspect": drain, "covered": False},
    ]
    state = {"bodyText": "Staff console 2 total/open enquiries", "elements": [
        {"idx": 4, "tag": "button", "text": "Drain queue"}]}
    base = [
        {"action": {"cmd": "reset_storage"},
         "targeting": {"driver_ok": True, "effect_registered": True}},
        {"action": {"cmd": "scenario_matrix", "_qa_valid_form_submission": True},
         "targeting": {"driver_ok": True, "effect_registered": True}},
    ]
    duplicate = base + [{"action": {"cmd": "scenario_matrix", "_qa_valid_form_submission": True},
                         "targeting": {"driver_ok": True, "effect_registered": True}}]
    reset = qa_explorer._pending_empty_first_run_transition_decision(
        story, state, coverage, duplicate)
    assert reset["next_action"]["cmd"] == "reset_storage"
    assert coverage[0]["covered"] is False and coverage[1]["covered"] is False

    clean_coverage = [dict(item, covered=(index < 2)) for index, item in enumerate(coverage)]
    drained = base + [{"action": {"cmd": "click", "target_text": "Drain queue"},
                       "targeting": {"driver_ok": True, "effect_registered": True,
                                     "intended": "Drain queue"}}]
    inspect = qa_explorer._pending_empty_first_run_transition_decision(
        story, state, clean_coverage, drained)
    assert inspect["next_action"]["cmd"] == "inspect_surfaces"
    assert "Approval Summary" in inspect["next_action"]["targets"]

    assert qa_explorer._empty_first_run_contaminated_resume_false_positive(
        story, {"action_kind": "scenario_matrix"},
        "The scenario did not begin in the required empty state; before submission 2 enquiries existed.",
        state) is True


def test_one_queue_click_cannot_prove_multi_surface_or_ordered_recovery_rows():
    surfaces = ("Verify the failed job appears across the staff console, CEO command view, queue diagnostics, "
                "dead-letter diagnostics, notifications, and blockers.")
    recovery = ("Switch the Agent scenario to Success, retry the failed job, and drain the queue; verify "
                "attempts, runAfter, status, and audit preserve failure and recovery.")
    state = {"url": "http://app", "bodyText": "queued failed retrying dead-letter diagnostics"}
    drain = {"action_kind": "click", "driver_ok": True, "effect_registered": True,
             "label_matched": True, "intended": "Drain queue", "targeted_label": "Drain queue"}

    assert qa_explorer._grounded_demonstrated(
        [surfaces, recovery], drain, state, state, require_mechanical=True) == []
    repaired = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": surfaces, "covered": True,
         "proof": {"engine": "mechanical-browser-proof", "action_kind": "click"}},
        {"aspect": recovery, "covered": True,
         "proof": {"engine": "mechanical-browser-proof", "action_kind": "click"}},
    ])
    assert [item["covered"] for item in repaired] == [False, False]
    assert repaired[0]["coverage_repaired"] == "multi-surface-inspection-required-fresh-proof"
    assert repaired[1]["coverage_repaired"] == "ordered-queue-recovery-required-fresh-proof"
    keyboard = ("Tab traversal: reach the agent selector, queue button, public, staff, CEO, and diagnostic "
                "controls; verify visual-order focus and visible focus.")
    assert qa_explorer._requires_multi_surface_inspection(keyboard) is False


def test_ordered_queue_recovery_closes_only_after_success_retry_drain_and_inspection():
    recovery = ("Switch the Agent scenario to Success, retry the failed or dead-lettered job, and drain the "
                "queue; verify attempts, runAfter, and status update without duplicating the enquiry, the "
                "operational failure signal clears, and the audit trail preserves failure and recovery.")
    coverage = [{"aspect": recovery, "covered": False}]
    records = [
        {"action": {"cmd": "click", "target_text": "Drain queue"},
         "targeting": {"driver_ok": True, "effect_registered": True},
         "actual": "Agent job failed and dead-lettered."},
        {"action": {"cmd": "type", "target_text": "Agent scenario", "value": "Success"},
         "targeting": {"driver_ok": True, "effect_registered": False, "intended": "Agent scenario"}},
        {"action": {"cmd": "click", "target_text": "Retry"},
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Retry"}},
        {"action": {"cmd": "click", "target_text": "Drain queue"},
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Drain queue"}},
    ]
    action = {"cmd": "inspect_surfaces", "targets": ["Staff", "CEO", "Queue", "Audit"]}
    targeting = {"driver_ok": True, "landmark_dwell_summary": {
        "targets": action["targets"], "all_targets_matched": True, "all_stable": True}}
    after = {"url": "http://app", "bodyText": (
        "Queue processing complete. Job succeeded. Attempts 4. runAfter cleared. Audit failure recovery."),
        "console_errors": []}
    verdict = {"verdict": "pass", "matches_expected": True, "bug": None}

    assert qa_explorer._successful_queue_recovery_coverage(
        {"id": "US-008"}, action, verdict, coverage, targeting, after, records) == [recovery]
    assert qa_explorer._successful_queue_recovery_coverage(
        {"id": "US-008"}, action, verdict, coverage, targeting, after, records[:-1]) == []

    wrong_scenario = records[:-1] + [
        {"action": {"cmd": "type", "target_text": "Agent scenario", "value": "Timeout"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Agent scenario"}},
        records[-1],
    ]
    assert qa_explorer._successful_queue_recovery_coverage(
        {"id": "US-008"}, action, verdict, coverage, targeting, after, wrong_scenario) == []


def test_retry_recovery_reestablishes_success_after_wrong_resumed_scenario():
    failure = "Verify failed work across staff, CEO, queue, notifications, and blockers."
    recovery = ("Switch the Agent scenario to Success, retry the failed job, and drain the queue; verify "
                "attempts, runAfter, status, and audit preserve failure and recovery.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work",
             "steps": [failure, recovery]}
    coverage = [{"aspect": failure, "covered": True}, {"aspect": recovery, "covered": False}]
    records = [
        {"action": {"cmd": "click", "target_text": "Drain queue"},
         "targeting": {"driver_ok": True, "effect_registered": True},
         "actual": "driver_ok=True; effect_registered=True; job status failed"},
        {"action": {"cmd": "type", "target_text": "Agent scenario", "value": "Success"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Agent scenario"}},
        {"action": {"cmd": "click", "target_text": "Retry failed job"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Retry failed job"}},
        {"action": {"cmd": "type", "target_text": "Agent scenario", "value": "Timeout"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Agent scenario"}},
        {"action": {"cmd": "click", "target_text": "Drain queue"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Drain queue"}},
    ]
    state = {"bodyText": "Agent jobs queued: 1. Audit: prior job failed.", "elements": [
        {"idx": 3, "tag": "select", "text": "Agent", "name": "agent-scenario",
         "value": "success"},
        {"idx": 4, "tag": "button", "text": "Drain queue"},
    ]}
    decision = qa_explorer._pending_retry_story_transition_decision(
        story, state, coverage, records)
    assert decision["next_action"] == {"cmd": "type", "idx": 3, "value": "Success"}
    assert qa_explorer._queue_recovery_wrong_scenario_false_positive(
        story,
        "After retrying, the job did not succeed and the failure signal remains open.",
        records,
    ) is True
    compact_records = [
        {"action": "type idx=3 ='Success'",
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Agent"}},
        {"action": "click idx=52",
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Retry dead-letter agent job job_123"}},
        {"action": "type idx=3 ='Timeout'",
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Agent"}},
        {"action": "click idx=4",
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Drain queue"}},
    ]
    assert qa_explorer._queue_recovery_wrong_scenario_false_positive(
        story, "The retried job remains retrying with Succeeded 0.", compact_records) is True


def test_retry_uses_passed_failure_receipt_and_rejects_retry_preceded_by_timeout():
    failure = "Verify failed work across staff, CEO, queue, notifications, and blockers."
    recovery = ("Switch the Agent scenario to Success, retry the failed job, and drain the queue; verify "
                "attempts, runAfter, status, and audit preserve failure and recovery.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work",
             "steps": [failure, recovery]}
    coverage = [{"aspect": failure, "covered": True}, {"aspect": recovery, "covered": False}]
    records = [
        {"action": {"cmd": "inspect_surfaces"}, "covers": [failure],
         "targeting": {"driver_ok": True, "effect_registered": True}},
        {"action": {"cmd": "type", "target_text": "Agent", "value": "Success"},
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Agent"}},
        {"action": {"cmd": "click", "target_text": "Retry dead-letter job"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Retry dead-letter job"},
         "actual": "The dead-letter job entered retrying state."},
        {"action": {"cmd": "type", "target_text": "Agent", "value": "Timeout"},
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Agent"}},
        {"action": {"cmd": "click", "target_text": "Drain queue"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Drain queue"}},
        {"action": {"cmd": "click", "target_text": "Retry dead-letter job"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Retry dead-letter job"}},
    ]
    state = {"bodyText": "Agent jobs queued: 1. Historical job failed.", "elements": [
        {"idx": 3, "tag": "select", "text": "Agent", "name": "agent-scenario",
         "value": "success"},
        {"idx": 4, "tag": "button", "text": "Drain queue"},
    ]}
    decision = qa_explorer._pending_retry_story_transition_decision(story, state, coverage, records)
    assert decision["next_action"] == {"cmd": "type", "idx": 3, "value": "Success"}


def test_generic_queue_fallback_uses_success_when_only_recovery_remains():
    recovery = ("Switch the Agent scenario to Success, retry the failed job, and drain the queue; verify "
                "attempts, runAfter, status, and audit preserve failure and recovery.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work",
             "steps": [recovery]}
    coverage = [{"aspect": recovery, "covered": False}]
    state = {"bodyText": "Agent jobs queued: 1", "elements": [
        {"idx": 3, "tag": "select", "text": "Agent scenario", "name": "agent-scenario",
         "value": "timeout", "options": "Success | Timeout"},
        {"idx": 4, "tag": "button", "text": "Drain queue"},
    ]}
    decision = qa_explorer._pending_queue_drain_decision(story, state, coverage)
    assert decision["next_action"]["value"] == "Success"


def test_retry_story_duplicate_state_resets_and_terminal_failure_is_inspected():
    submit = ("Submit one enquiry, confirm it persists as a single enquiry with agent work created, then "
              "drain the queue repeatedly until the job is failed or dead-lettered.")
    failure = ("Verify the failed or dead-lettered job appears as engineering-owned risk across the staff "
               "console, CEO command view, queue diagnostics, dead-letter diagnostics, notifications, and blockers.")
    recovery = ("Switch the Agent scenario to Success, retry the failed job, and drain the queue; verify "
                "attempts, runAfter, status, and audit preserve failure and recovery.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work", "steps": [submit, failure, recovery]}
    coverage = [
        {"aspect": "Set the Agent scenario to Timeout and verify it is active.", "covered": True, "proof": {}},
        {"aspect": submit, "covered": False}, {"aspect": failure, "covered": False},
        {"aspect": recovery, "covered": False},
    ]
    state = {"bodyText": "Total enquiries: 2. One job failed and dead-lettered.", "elements": [
        {"idx": 3, "tag": "select", "text": "Agent", "name": "agent-scenario", "value": "timeout"}]}
    duplicate = [
        {"action": {"cmd": "scenario_matrix", "_qa_valid_form_submission": True},
         "targeting": {"driver_ok": True, "effect_registered": True}},
        {"action": {"cmd": "scenario_matrix", "_qa_valid_form_submission": True},
         "targeting": {"driver_ok": True, "effect_registered": True}},
    ]
    reset = qa_explorer._pending_retry_story_transition_decision(story, state, coverage, duplicate)
    assert reset["next_action"]["cmd"] == "reset_storage"
    assert all(item["covered"] is False for item in coverage)

    one = [{"action": {"cmd": "scenario_matrix", "_qa_valid_form_submission": True},
            "targeting": {"driver_ok": True, "effect_registered": True}},
           {"action": {"cmd": "click", "target_text": "Drain queue"},
            "targeting": {"driver_ok": True, "effect_registered": True,
                          "intended": "Drain queue"},
            "actual": "driver_ok=True; effect_registered=True; Agent jobs failed: 1"}]
    one_audit = ("Total enquiries: 1. Agent jobs failed: 1. One job dead-lettered. "
                 "enquiry.created: enquiry_abc123")
    single_state = {**state, "bodyText": one_audit, "viewportText": one_audit,
                    "elements": state["elements"] + [
                        {"idx": 9, "tag": "button", "text": "Retry failed job"}]}
    inspect = qa_explorer._pending_retry_story_transition_decision(story, single_state, coverage, one)
    assert inspect["next_action"]["cmd"] == "inspect_surfaces"
    assert inspect["next_action"]["targets"] == [
        "Staff operating console", "Agent jobs", "CEO command view",
        "Operational diagnostics", "Queue and Dead Letters",
        "Governance audit history", "Enquiry review details"]
    assert qa_explorer._live_terminal_queue_failure({
        "bodyText": "Failed 0. Dead-lettered 0. Failure diagnostics. Retry policy available.",
        "elements": []}) is False


def test_retry_story_uses_first_failure_anchor_when_audit_retains_failure_text():
    failure = ("Verify failed work across staff, CEO, queue, dead-letter diagnostics, notifications, and blockers.")
    recovery = ("Switch the Agent scenario to Success, retry the failed job, and drain the queue; verify "
                "attempts, runAfter, status, and audit preserve failure and recovery.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work", "steps": [failure, recovery]}
    coverage = [{"aspect": failure, "covered": True}, {"aspect": recovery, "covered": False}]
    records = [
        {"action": {"cmd": "click", "target_text": "Drain queue"},
         "targeting": {"driver_ok": True, "effect_registered": True},
         "actual": "driver_ok=True; effect_registered=True; job status failed"},
        {"action": {"cmd": "type", "value": "Success"},
         "targeting": {"driver_ok": True, "effect_registered": False}},
        {"action": {"cmd": "click", "target_text": "Retry failed job"},
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Retry failed job"},
         "actual": "driver_ok=True; effect_registered=True; audit history says prior job failed"},
    ]
    state = {"bodyText": "Agent jobs queued: 1. Audit: prior job failed.", "elements": [
        {"idx": 3, "tag": "select", "text": "Agent", "name": "agent-scenario", "value": "success"},
        {"idx": 4, "tag": "button", "text": "Drain queue"},
    ]}
    decision = qa_explorer._pending_retry_story_transition_decision(story, state, coverage, records)
    assert decision["next_action"]["target_text"] == "Drain queue"


def test_queue_failure_journey_requires_one_submission_timeout_and_terminal_drain():
    aspect = ("Submit one enquiry, confirm it persists as a single enquiry with agent work created, then drain "
              "the queue repeatedly until the job is failed or dead-lettered.")
    coverage = [{"aspect": aspect, "covered": False}]
    prior = [
        {"action": {"cmd": "type", "target_text": "Agent scenario", "value": "Timeout"},
         "targeting": {"driver_ok": True, "effect_registered": True}},
        {"action": {"cmd": "scenario_matrix", "_qa_valid_form_submission": True},
         "targeting": {"driver_ok": True, "effect_registered": True}},
    ]
    action = {"cmd": "click", "target_text": "Drain queue"}
    targeting = {"driver_ok": True, "effect_registered": True, "intended": "Drain queue"}
    verdict = {"verdict": "pass", "matches_expected": True, "bug": None}
    after = {"url": "http://app", "bodyText": "Agent jobs failed: 1. Agent job dead-lettered.",
             "elements": [{"idx": 9, "tag": "button", "text": "Retry failed job"}],
             "console_errors": []}
    assert qa_explorer._successful_queue_failure_coverage(
        {"id": "US-008"}, action, verdict, coverage, targeting, after, prior) == [aspect]
    assert qa_explorer._successful_queue_failure_coverage(
        {"id": "US-008"}, action, verdict, coverage, targeting, after, prior + [prior[-1]]) == []

    failure = ("Verify failed work across staff, CEO, queue, dead-letter diagnostics, notifications, and blockers.")
    inspect_action = {"cmd": "inspect_surfaces"}
    inspect_targeting = {"driver_ok": True, "landmark_dwell_summary": {
        "targets": ["Staff", "CEO", "Queue", "Notifications", "Blockers"],
        "all_targets_matched": True, "all_stable": True}}
    assert qa_explorer._successful_queue_failure_inspection_coverage(
        {"id": "US-008"}, inspect_action, verdict, [{"aspect": failure, "covered": False}],
        inspect_targeting, after) == [failure]


def test_retry_projection_fence_expands_partial_probe_to_every_authored_surface():
    failure = ("Drain the queue until failed, then verify staff console, CEO command view, queue diagnostics, "
               "dead-letter diagnostics, notifications, and blockers consistently show the failure.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work", "steps": [failure]}
    action = {"cmd": "inspect_surfaces", "targets": ["Agent jobs", "CEO command view"]}
    state = {"bodyText": "Agent jobs failed 1. Dead letters 1.", "elements": []}

    fenced = qa_explorer._contract_fenced_queue_projection_action(
        action, story, [{"aspect": failure, "covered": False}], state)

    assert fenced["targets"] == qa_explorer._QUEUE_RETRY_PROJECTION_TARGETS
    assert fenced["_qa_queue_projection_fenced"] is True


def test_queue_failure_projection_is_mechanical_only_with_complete_cross_surface_truth():
    failure = ("Drain the queue repeatedly until the agent job is failed or dead-lettered, then verify staff "
               "console, CEO command view, queue diagnostics, dead-letter diagnostics, notifications, and "
               "blockers consistently surface it as engineering-owned risk; confirm retry controls are "
               "available only for retryable states.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work", "steps": [failure]}
    action = {"cmd": "inspect_surfaces", "targets": qa_explorer._QUEUE_RETRY_PROJECTION_TARGETS}
    targeting = {"driver_ok": True, "action_kind": "inspect_surfaces",
                 "landmark_dwell_summary": {
                     "targets": qa_explorer._QUEUE_RETRY_PROJECTION_TARGETS,
                     "all_targets_matched": True, "all_stable": True}}
    body = """enquiriesTotal 1
Staff operating console
Agent jobs
job_1: lead_reviewer - dead_letter - 3 of 3 attemptsRetry
Unresolved blockers
engineering: Agent job is failed or dead-lettered. (persisted, open)
Governance audit history
agent.failed: agent_job job_1
blocker.created: agent_job job_1 - actor engineering
Enquiry review details
1 agent job
Related notifications
Governance blocker escalated
Agent job is failed or dead-lettered.
CEO Command View
Agent Activity
Failed 1
Risk Signals
Open (1)
Agent job is failed or dead-lettered. engineering
Dead letters 1
Unresolved blockers 1
Operational Diagnostics
App jobs Total 1 Dead letter 1
Pipeline jobs Total 1 Dead letter 1
Dead letters Total 1
"""
    after = {"url": "http://app", "bodyText": body, "console_errors": []}

    verdict = qa_explorer._mechanical_queue_projection_verdict(
        story, action, {"expected": failure}, targeting, after,
        [{"aspect": failure, "covered": False}], [])

    assert verdict["verdict"] == "pass"
    assert verdict["_raw"]["engine"] == "mechanical-queue-failure-projection"
    missing_notification = {**after, "bodyText": body.replace(
        "Governance blocker escalated\nAgent job is failed or dead-lettered.", "No related notifications")}
    assert qa_explorer._mechanical_queue_projection_verdict(
        story, action, {"expected": failure}, targeting, missing_notification,
        [{"aspect": failure, "covered": False}], []) is None


def test_queue_recovery_projection_requires_runafter_cleared_risk_and_ordered_receipts():
    failure = "Verify failed work across staff, CEO, queue, notifications, and blockers."
    recovery = ("Retry the failed or dead-lettered job, drain the queue again, and verify attempts, runAfter, "
                "and status update; the original enquiry is not duplicated, the operational failure signal "
                "is cleared or superseded, and the audit trail preserves both failure and recovery.")
    story = {"id": "US-008", "title": "Retry dead-lettered agent work",
             "steps": [failure, recovery]}
    coverage = [{"aspect": failure, "covered": True}, {"aspect": recovery, "covered": False}]
    action = {"cmd": "inspect_surfaces", "targets": qa_explorer._QUEUE_RETRY_PROJECTION_TARGETS}
    targeting = {"driver_ok": True, "action_kind": "inspect_surfaces",
                 "landmark_dwell_summary": {
                     "targets": qa_explorer._QUEUE_RETRY_PROJECTION_TARGETS,
                     "all_targets_matched": True, "all_stable": True}}
    body = """Total enquiries 1
Staff operating console
Agent jobs
job_1: lead_reviewer - succeeded - 1 of 3 attempts - run after 2026-08-14T16:00:03.182Z
Unresolved blockers
No records
Governance audit history
agent.failed: agent_job job_1
agent.completed: agent_job job_1
blocker.resolved: agent_job job_1
Enquiry review details
1 agent job
Related notifications
Agent job recovered
CEO Command View
Agent Activity
Succeeded 1
Failed 0
Risk Signals
Open (0)
Dead letters 0
Unresolved blockers 0
Operational Diagnostics
App jobs Total 1 Succeeded 1 Dead letter 0
Pipeline jobs Total 1 Succeeded 1 Dead letter 0
Dead letters Total 0
"""
    after = {"url": "http://app", "bodyText": body, "console_errors": []}
    records = [
        {"action": {"cmd": "inspect_surfaces"}, "covers": [failure],
         "targeting": {"driver_ok": True, "effect_registered": True}},
        {"action": {"cmd": "type", "target_text": "Agent", "value": "Success"},
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Agent"}},
        {"action": {"cmd": "click", "target_text": "Retry dead-letter job"},
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "intended": "Retry dead-letter job"}},
        {"action": {"cmd": "click", "target_text": "Drain queue"},
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Drain queue"}},
    ]

    verdict = qa_explorer._mechanical_queue_projection_verdict(
        story, action, {"expected": recovery}, targeting, after, coverage, records)

    assert verdict["verdict"] == "pass"
    assert verdict["_raw"]["engine"] == "mechanical-queue-recovery-projection"
    assert qa_explorer._mechanical_queue_projection_verdict(
        story, action, {"expected": recovery}, targeting,
        {**after, "bodyText": body.replace(
            " - run after 2026-08-14T16:00:03.182Z", "")}, coverage, records) is None
    wrong_scenario = records[:-1] + [
        {"action": {"cmd": "type", "target_text": "Agent", "value": "Timeout"},
         "targeting": {"driver_ok": True, "effect_registered": True, "intended": "Agent"}},
        records[-1],
    ]
    assert qa_explorer._mechanical_queue_projection_verdict(
        story, action, {"expected": recovery}, targeting, after, coverage, wrong_scenario) is None


def test_queue_projection_expected_allows_aggregate_cards_without_duplicating_job_identity():
    story = {"id": "US-008", "title": "Retry dead-lettered agent work", "steps": [
        "Confirm staff, CEO, queue diagnostics, notifications, and blockers show the failure."]}
    targeting = {"action_kind": "inspect_surfaces", "landmark_dwell_summary": {
        "targets": qa_explorer._QUEUE_RETRY_PROJECTION_TARGETS}}

    expected, changed = qa_explorer._contract_grounded_queue_projection_expected(
        story, "Every targeted card repeats job_1 and its full failure details.", targeting)

    assert changed is True
    assert "need not repeat the job identifier" in expected


def test_effectful_named_business_control_is_causal_mechanical_proof():
    approve = "Story step 5.2: then approve or reject through the available approval control/runtime method"
    facts = {
        "action_kind": "click", "driver_ok": True, "effect_registered": True,
        "label_matched": True, "intended": "Approve send", "targeted_label": "Approve send",
    }
    assert qa_explorer._grounded_demonstrated(
        [approve], facts, {}, {}, require_mechanical=True) == [approve]
    assert qa_explorer._grounded_demonstrated(
        [approve], facts | {"effect_registered": False}, {}, {}, require_mechanical=True) == []
    assert qa_explorer._grounded_demonstrated(
        [approve], facts | {"intended": "Drain queue", "targeted_label": "Drain queue"},
        {}, {}, require_mechanical=True) == []


def test_tenant_scoped_nongit_writer_requires_exact_durable_pre_mutation_snapshot(
        monkeypatch, tmp_path):
    import dev_loop
    import types

    products = tmp_path / "products"
    repo = products / "product-a"
    repo.mkdir(parents=True)
    calls = []
    fake_versions = types.SimpleNamespace(
        factory=types.SimpleNamespace(PRODUCTS=products),
        snapshot=lambda tenant, product, label: calls.append((tenant, product, label)) or {
            "version": 4, "snapshot_path": str(tmp_path / "product-a.v4.tar.gz")})
    monkeypatch.setitem(__import__("sys").modules, "versions", fake_versions)

    receipt = dev_loop._snapshot_before_nongit_mutation(
        repo, "tenant-a", {"finding_id": "qaf-1"}, 2)
    assert receipt["status"] == "created" and receipt["version"] == 4
    assert calls == [("tenant-a", "product-a", "pre-dev-fix qaf-1 attempt 2")]

    wrong = dev_loop._snapshot_before_nongit_mutation(
        tmp_path / "outside" / "product-a", "tenant-a", {"finding_id": "qaf-1"}, 3)
    assert wrong["status"] == "failed"


def test_compound_invalid_form_matrix_migrates_to_independent_validation_cases():
    source = (
        "Enter invalid or past start dates, dog ages below 0 or above 30, near-limit overlong text, "
        "and mismatched email/phone contact data; blur and paste each value, press Enter, and click Send "
        "enquiry, verifying paste works, focus remains visible and recoverable, invalid values are rejected "
        "with clear user-facing messages, and no invalid submission occurs."
    )

    migrated = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": source, "covered": True,
         "proof": {"engine": "codex", "action_kind": "scenario_matrix", "recorded_at": 10.0}}])

    assert [item["atomic_kind"] for item in migrated] == ["validation_case"] * 5
    labels = "\n".join(item["aspect"] for item in migrated)
    assert "invalid or past start date" in labels
    assert "dog age below 0" in labels
    assert "dog age above 30" in labels
    assert "near-limit overlong text" in labels
    assert "mismatched email and phone" in labels
    assert all("no invalid submission occurs" in item["aspect"] for item in migrated)
    assert all(item["covered"] is False and "proof" not in item for item in migrated)


def test_multi_surface_dwell_migrates_and_only_batches_currently_live_atoms():
    source = ("Dwell idle for 10 seconds on the public form, confirmation screen, staff console, CEO view, "
              "diagnostics, and any error screen; verify no input loss, focus loss, scroll jump, navigation, "
              "overlap, or unexpected horizontal scrolling.")
    migrated = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": source, "covered": False, "explicit": True}])

    assert len(migrated) == 6
    assert {item["atomic_kind"] for item in migrated} == {"dwell_surface"}
    assert all(item["source_aspect"] == source for item in migrated)
    state = {"documentLandmarks": [
        {"label": "Public enquiry", "role": "region", "y": 200},
        {"label": "Staff operating console", "role": "region", "y": 900},
        {"label": "CEO Command View", "role": "region", "y": 1800},
    ]}
    decision = qa_explorer._pending_explicit_evidence_decision({}, state, migrated, records=[])

    action = decision["next_action"]
    assert action["cmd"] == "dwell_surfaces"
    assert action["_qa_atomic_dwell"] is True
    assert set(action["targets"]) == {
        "Public enquiry", "Staff operating console", "CEO Command View"}
    assert len(decision["covers"]) == 3
    assert not any("confirmation" in item.casefold() or "error screen" in item.casefold()
                   for item in decision["covers"])

    confirmation_state = {"documentLandmarks": [
        {"label": "Thanks. We have your enquiry and will review service fit next.",
         "role": "h2", "y": 200}]}
    for item in migrated:
        item["covered"] = "confirmation screen" not in item["aspect"].casefold()
    confirmation = qa_explorer._pending_explicit_evidence_decision(
        {}, confirmation_state, migrated, records=[])
    assert confirmation["next_action"]["targets"] == [
        "Thanks. We have your enquiry and will review service fit next."]
    assert len(confirmation["covers"]) == 1
    assert "confirmation screen" in confirmation["covers"][0].casefold()


def test_responsive_pair_migrates_to_two_real_viewports_without_wait_loop():
    source = ("Verify fresh initial load at 375px mobile and desktop widths, including visible focus, "
              "no overlap or horizontal scrolling, and all controls present.")
    migrated = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": source, "covered": False, "explicit": False}])

    assert [item["atomic_kind"] for item in migrated] == [
        "viewport_mobile", "viewport_desktop"]
    mobile = qa_explorer._pending_explicit_evidence_decision({}, {}, migrated, records=[])
    assert mobile["next_action"] == {"cmd": "viewport", "value": {"width": 375, "height": 844}}
    assert mobile["covers"] == [migrated[0]["aspect"]]
    migrated[0]["covered"] = True
    desktop = qa_explorer._pending_explicit_evidence_decision({}, {}, migrated, records=[])
    assert desktop["next_action"] == {"cmd": "viewport", "value": {"width": 1280, "height": 800}}
    assert desktop["covers"] == [migrated[1]["aspect"]]


def test_legacy_dwell_checkpoint_repairs_fake_refresh_landmarks_without_losing_proof():
    source = ("Dwell idle for 10 seconds on the public form, confirmation screen, staff console, CEO view, "
              "diagnostics, and any error screen, then refresh after interactions and repeat one keyboard "
              "path; verify no input loss, focus loss, scroll jump, navigation, or accessibility regression.")
    parent = "legacy-parent"
    proof = {"engine": "mechanical-atomic-dwell-proof", "action_kind": "dwell_surfaces",
             "recorded_at": 123.0}
    legacy = [
        {"aspect": ("Dwell idle for 10 seconds on the public form; verify no input loss, focus loss, "
                    "scroll jump, navigation, or accessibility regression"),
         "covered": True, "proof": proof, "source_aspect": source, "atomic_parent": parent,
         "atomic_kind": "dwell_surface", "atomic_index": 1, "atomic_total": 8},
        {"aspect": ("Dwell idle for 10 seconds on then refresh after interactions; verify no input loss, "
                    "focus loss, scroll jump, navigation, or accessibility regression"),
         "covered": False, "source_aspect": source, "atomic_parent": parent,
         "atomic_kind": "dwell_surface", "atomic_index": 7, "atomic_total": 8},
        {"aspect": ("Dwell idle for 10 seconds on repeat one keyboard path; verify no input loss, focus "
                    "loss, scroll jump, navigation, or accessibility regression"),
         "covered": False, "source_aspect": source, "atomic_parent": parent,
         "atomic_kind": "dwell_surface", "atomic_index": 8, "atomic_total": 8},
    ]

    repaired = qa_explorer._migrate_compound_coverage_ledger(legacy)

    assert len(repaired) == 8
    assert [item["atomic_kind"] for item in repaired[-2:]] == ["reload", "post_reload_keyboard"]
    assert not any("on then refresh" in item["aspect"].casefold()
                   or "on repeat one keyboard" in item["aspect"].casefold() for item in repaired)
    public = next(item for item in repaired if "on the public form" in item["aspect"].casefold())
    assert public["covered"] is True
    assert public["proof"] == proof


def test_atomic_dwell_receipt_closes_exact_bound_surfaces_without_model():
    public = "Dwell idle for 10 seconds on the public form; verify no input or focus loss."
    staff = "Dwell idle for 10 seconds on staff console; verify no input or focus loss."
    bindings = [
        {"aspect": public, "target": "Public enquiry"},
        {"aspect": staff, "target": "Staff operating console"},
    ]
    action = {"cmd": "dwell_surfaces", "duration_s": 10, "_qa_atomic_dwell": True,
              "_qa_dwell_bindings": bindings}
    stable = {"url": "http://app", "scroll": {"x": 0, "y": 100},
              "active": {"tag": "input", "label": "Name"},
              "controls": [{"id": "name", "value": "Ada"}], "horizontalOverflow": False}
    observations = [{
        "target": binding["target"], "scroll": {"scrolled": True, "matched": binding["target"]},
        "requested_ms": 10000, "elapsed_ms": 10003, "stable": True,
        "before": stable, "after": dict(stable),
    } for binding in bindings]
    targeting = {
        "driver_ok": True,
        "landmark_dwell_summary": {"duration_ms_each": 10000,
                                   "all_targets_matched": True, "all_stable": True},
        "landmark_dwell": {"duration_ms_each": 10000, "observations": observations},
    }
    verdict = qa_explorer._mechanical_atomic_dwell_verdict(
        action, {"covers": [public, staff]}, targeting)

    assert verdict["verdict"] == "pass"
    assert verdict["demonstrated"] == [public, staff]
    assert verdict["_raw"]["engine"] == "mechanical-atomic-dwell-proof"
    assert qa_explorer._ground_verdict_demonstrated(
        verdict, targeting, {}, {}) == [public, staff]
    targeting["landmark_dwell"]["observations"][0]["after"] = {
        **stable, "horizontalOverflow": True}
    assert qa_explorer._mechanical_atomic_dwell_verdict(
        action, {"covers": [public, staff]}, targeting) is None


def test_missing_confirmation_compiles_one_valid_form_batch_then_requires_real_dwell():
    confirmation = ("Dwell idle for 10 seconds on confirmation screen; verify no input loss, focus loss, "
                    "scroll jump, navigation, overlap, or unexpected horizontal scrolling")
    story = {"title": "Submit an enquiry and inspect its confirmation",
             "steps": ["Complete and send the public enquiry form."]}
    state = {
        "documentLandmarks": [{"label": "Public enquiry", "role": "region", "y": 100}],
        "elements": [
            {"idx": 1, "tag": "input", "type": "text", "associatedLabel": "Name",
             "name": "name", "value": "", "required": "true", "formIndex": 0},
            {"idx": 2, "tag": "input", "type": "email", "associatedLabel": "Email",
             "name": "email", "value": "", "formIndex": 0},
            {"idx": 3, "tag": "input", "type": "text", "associatedLabel": "Postcode",
             "name": "postcode", "value": "", "required": "true", "formIndex": 0},
            {"idx": 4, "tag": "input", "type": "checkbox",
             "associatedLabel": "You may retain these details.", "name": "consent",
             "checked": "false", "required": "true", "formIndex": 0},
            {"idx": 5, "tag": "button", "type": "submit", "text": "Send enquiry",
             "disabled": "true", "formValid": "false", "formIndex": 0},
        ],
    }
    decision = qa_explorer._pending_conditional_surface_setup_decision(
        story, state, [{"aspect": confirmation, "covered": False,
                        "atomic_kind": "dwell_surface"}])

    action = decision["next_action"]
    assert action["cmd"] == "scenario_matrix"
    nested = action["cases"][0]["actions"]
    assert [item["target_text"] for item in nested] == [
        "Name", "Postcode", "You may retain these details.", "Email", "Send enquiry"]
    assert decision["covers"] == []
    assert action["_qa_expected_surface"] == confirmation

    targeting = {"driver_ok": True,
                 "scenario_matrix_summary": {"completed_cases": 1, "total_cases": 1}}
    revealed = {"documentLandmarks": [{
        "label": "Thanks. We have your enquiry and will review service fit next.",
        "role": "h2", "y": 100}]}
    verdict = qa_explorer._mechanical_conditional_surface_setup_verdict(
        action, targeting, revealed)
    assert verdict["verdict"] == "pass"
    assert verdict["demonstrated"] == []
    dwell = qa_explorer._pending_explicit_evidence_decision(
        story, revealed, [{"aspect": confirmation, "covered": False,
                           "atomic_kind": "dwell_surface"}], records=[])
    assert dwell["next_action"]["cmd"] == "dwell_surfaces"
    assert dwell["covers"] == [confirmation]


def test_plain_timed_wait_can_ground_revealed_confirmation_dwell_only():
    confirmation = ("Dwell idle for 10 seconds on confirmation screen; verify no input loss, focus loss, "
                    "scroll jump, navigation, overlap, or unexpected horizontal scrolling")
    diagnostics = "Dwell idle for 10 seconds on diagnostics; verify no navigation."
    control = {"id": "send-another", "tag": "button", "value": "", "disabled": None}
    before = {"url": "http://app", "scrollPosition": {"x": 0, "y": 120},
              "activeElement": {"tag": "button", "label": "Send another enquiry"},
              "elements": [control], "horizontalOverflow": False,
              "bodyText": "Enquiry received. Send another enquiry."}
    after = {**before, "elements": [dict(control)]}
    targeting = {"action_kind": "wait", "driver_ok": True,
                 "wait_summary": {"waited": True, "requested_ms": 10000,
                                  "elapsed_ms": 10004}}

    assert qa_explorer._grounded_demonstrated(
        [confirmation], targeting, before, after, require_mechanical=True) == [confirmation]
    assert qa_explorer._grounded_demonstrated(
        [diagnostics], targeting, before, after, require_mechanical=True) == []
    targeting["wait_summary"]["elapsed_ms"] = 9999
    assert qa_explorer._grounded_demonstrated(
        [confirmation], targeting, before, after, require_mechanical=True) == []


def test_post_refresh_traversal_requires_app_control_orca_announcement():
    aspect = ("After refresh/reload, repeat one story-required keyboard path and verify focus, "
              "announcements, and control operability.")
    sequence = [
        {"tag": "input", "label": "Walking frequency", "focusVisible": True,
         "horizontalOverflow": False},
        {"tag": "button", "label": "Drain queue", "focusVisible": True,
         "horizontalOverflow": False},
    ]
    targeting = {
        "action_kind": "traverse", "driver_ok": True, "effect_registered": True,
        "history_direction": "forward", "trusted_keyboard": True,
        "keyboard_evidence": [
            {"type": "keydown", "key": "Tab", "code": "Tab", "isTrusted": True}],
        "traversal": {"direction": "forward", "derived_focusable_count": 2,
                      "unique_controls": 2, "count": 4, "all_focus_visible": True,
                      "horizontal_overflow_seen": False, "sequence": sequence},
    }
    before = {"url": "http://app", "actualAssistiveTechnologyEvents": []}
    chrome_only = {
        "url": "http://app", "actualAssistiveTechnologyAvailable": True,
        "actualAssistiveTechnologyEvents": [
            {"utterance": "Tab search push button.", "source": "orca-at-spi"}],
        "activeElement": {"tag": "button", "label": "Drain queue", "focusVisible": True},
    }
    announced = {**chrome_only, "actualAssistiveTechnologyEvents": [
        {"utterance": "Walking frequency combo box.", "source": "orca-at-spi"}]}

    assert qa_explorer._grounded_demonstrated(
        [aspect], targeting, before, chrome_only, require_mechanical=True) == []
    assert qa_explorer._grounded_demonstrated(
        [aspect], targeting, before, announced, require_mechanical=True) == [aspect]
    wrapped_to_document = {**announced, "activeElement": {"tag": "body", "label": ""}}
    assert qa_explorer._grounded_demonstrated(
        [aspect], targeting, before, wrapped_to_document, require_mechanical=True) == [aspect]
    coverage = [
        {"aspect": "Refresh/reload after the story interactions and verify the refreshed accessible state.",
         "covered": True, "atomic_kind": "reload"},
        {"aspect": aspect, "covered": False, "atomic_kind": "post_reload_keyboard"},
    ]
    assert qa_explorer._mechanically_proven_unresolved(
        coverage, targeting, before, wrapped_to_document) == [aspect]


def test_dev_self_qa_threads_campaign_identity_and_live_deadline_into_explorer(monkeypatch):
    created = []
    explored = []
    live = {"deadline": time.time() + 600}

    class Explorer:
        def __init__(self, *_args, **kwargs):
            created.append(kwargs)
            self.coverage = [{"aspect": "retry", "covered": True}]
            self.stop_reason = "coverage-complete"
            self.infrastructure_error = None
            self.missing_capabilities = []

        def explore(self, _story, **kwargs):
            explored.append(kwargs)
            return [{"verdict": {"matches_expected": True}, "demonstrated": ["retry"]}]

        def close(self):
            pass

    monkeypatch.setattr(qa_explorer, "Explorer", Explorer)
    monkeypatch.setattr(dev_loop, "_audit", lambda *_a, **_k: None)
    deadline = lambda: live["deadline"]

    report = dev_loop.dev_self_qa(
        "http://app", "vision", [{"id": "US-008"}], return_report=True,
        deadline=deadline, scope_run_id=2416, scope_tenant="tenant")

    assert report["complete"] is True
    assert created == [{"token": None, "org": "0", "scope_run_id": 2416,
                        "scope_tenant": "tenant"}]
    assert explored[0]["deadline"] is deadline


def test_evaluation_baseline_omits_duplicate_control_catalog_but_keeps_target_value_delta():
    elements = [{"idx": i, "tag": "button", "text": f"control-{i}"} for i in range(60)]
    state = {"url": "http://app/", "title": "app", "bodyText": "Page", "statusText": "Ready",
             "elements": elements, "screenshot": "/tmp/proof.png"}
    targeting = {"action_kind": "type", "intended": "Public body", "label_matched": True,
                 "driver_ok": True, "before_control_value": "old", "after_control_value": "new"}

    prompt = qa_explorer._evaluate_prompt(
        "vision", {"steps": ["Type the public body"], "expected": "The value changes."},
        "The intended field contains the new value.", targeting, state, state, ["type value"])

    before = prompt.split("=== STATE BEFORE THE ACTION ===", 1)[1].split(
        "=== STATE AFTER THE ACTION ===", 1)[0]
    assert "control-59" not in before
    assert "intended control value BEFORE: 'old'" in prompt
    assert "intended control value AFTER: 'new'" in prompt


def test_action_judge_omits_planner_only_landmark_index_and_stays_compact():
    story = {"title": "Inspect queue", "steps": ["Open queue health"],
             "expected": "Queue health is accurate."}
    state = {
        "url": "http://app/", "title": "app", "bodyText": "queue " * 5000,
        "viewportText": "Queue health ready " * 500,
        "statusText": "Ready " * 1000,
        "documentLandmarks": [
            {"label": f"section-{index}-" + "x" * 200, "role": "h2", "y": index * 500}
            for index in range(100)
        ],
        "elements": [{"idx": index, "tag": "button", "text": f"control-{index}"}
                     for index in range(80)],
    }
    prompt = qa_explorer._evaluate_prompt(
        "vision", story, "Queue health is shown.", {"action_kind": "scroll"},
        state, state, ["Open queue health"])

    assert "section-99" not in prompt
    assert "DOCUMENT_LANDMARKS (exact long-page scroll targets): (omitted from action judge)" in prompt
    assert len(prompt) < 25_000


def test_batched_traversal_uses_story_specific_compact_judge(monkeypatch):
    captured = {}

    def fake_call(_role, _repo, task, **_kwargs):
        captured["task"] = task
        return {"rc": 0, "out_full": json.dumps({
            "target_confirmed": True, "matches_expected": False, "verdict": "bug",
            "bug": "The Date control loses its visible focus indicator.",
            "severity": "medium", "blocking": False, "demonstrated": [],
        })}

    monkeypatch.setattr(qa_explorer, "_call_agent", fake_call)
    sequence = [{
        "order": index + 1, "idx": str(index), "tag": "input", "label": f"Control {index}",
        "focusVisible": index != 17, "scrollX": 0, "scrollY": index * 40,
        "horizontalOverflow": False,
    } for index in range(28)]
    targeting = {
        "action_kind": "traverse", "control_action": False, "driver_ok": True,
        "traversal": {"direction": "forward", "key": "Tab", "count": 36,
                      "derived_focusable_count": 28, "unique_controls": 28,
                      "all_focus_visible": False, "horizontal_overflow_seen": False,
                      "sequence": sequence},
    }
    state = {
        "url": "http://app/", "title": "app", "viewport": {"width": 390, "height": 844},
        "bodyText": "body " * 10_000, "statusText": "status " * 2_000,
        "elements": [{"idx": index, "tag": "input", "label": f"Control {index}"}
                     for index in range(100)],
    }
    explorer = qa_explorer.Explorer("http://app/", "accessible product " * 1_000, autostart=False)
    verdict = explorer._ai_evaluate(
        {"steps": ["Use Tab to reach every control"],
         "expected": "Every focus indicator remains visible."},
        "Every focus stop is visible.", targeting, state, state,
        ["Use Tab to reach every control and keep focus visible."], prior_records=[])

    assert verdict["verdict"] == "bug"
    assert "SEALED DRIVER RECEIPT" in captured["task"]
    assert "STATE BEFORE THE ACTION" not in captured["task"]
    assert '"label": "Control 17"' in captured["task"]
    assert len(captured["task"]) < 30_000


def test_seeded_resume_reset_is_a_zero_model_setup_verdict():
    verdict = qa_explorer._mechanical_routine_verdict(
        {"cmd": "reset_storage", "value": "http://app/"},
        {"driver_ok": True}, {"url": "http://app/", "console_errors": []}, [])

    assert verdict["verdict"] == "pass"
    assert verdict["demonstrated"] == []
    assert verdict["_raw"]["engine"] == "mechanical-browser-proof"


def test_self_contained_dev_judges_request_compact_strong_prompt(monkeypatch):
    received = {}

    def fake_agent(role, repo, task, **kwargs):
        received.update(kwargs)
        return {"rc": 0, "out": '{"verdict":"uncertain"}'}

    monkeypatch.setattr(factory, "agent", fake_agent)
    result = dev_loop._ai_json(
        "reviewer", ".", "self-contained evidence contract", compact=True,
        timeout=30, retries=0)
    assert result["verdict"] == "uncertain"
    assert received == {"timeout": 30, "retries": 0, "compact": True}

    source = Path(factory.__file__).read_text()
    assert "elif compact:" in source
    assert "Follow the task's evidence, permission, and output contract" in source


def test_recovery_fix_judge_receives_fenced_files_and_complete_browser_report(monkeypatch):
    captured = {}

    def judge(_role, _repo, prompt, **_kwargs):
        captured["prompt"] = prompt
        return {"fixed": True, "confidence": 0.99, "reason": "complete fresh proof"}

    monkeypatch.setattr(dev_loop, "_ai_json", judge)
    verdict = dev_loop._judge_fixed(
        {"detail": "a token was public"}, "secrets remain private",
        ["src/governance.js"], "", [], {"restarted": False}, repo=".",
        verification={"complete": True, "stories": [{"stop_reason": "coverage-complete",
                                                        "covered": 4, "coverage_total": 4}]},
        prior_mutation_receipt=True)

    assert verdict["fixed"] is True
    assert "exact fenced prior-worker mutation receipt" in captured["prompt"]
    assert "NOT RECONSTRUCTIBLE ACROSS THE PROCESS HANDOFF" in captured["prompt"]
    assert '"complete": true' in captured["prompt"] and '"coverage_total": 4' in captured["prompt"]
    assert "EMPTY — no verifiable change was made" not in captured["prompt"]


def test_fix_judge_capsule_keeps_late_transition_proof_without_full_browser_payload(monkeypatch):
    captured = {}
    aspect_empty = "Observe valid empty operational state"
    aspect_drain = "Drain queue and show the populated staff and CEO state without reload"
    records = [
        {"action": f"setup-{index}", "actual": "navigation-noise-" + ("x" * 5000),
         "verdict": "pass", "covers": []}
        for index in range(18)
    ]
    records += [
        {"action": "inspect_surfaces", "actual": "No records; queue total 0",
         "expected": aspect_empty, "verdict": "pass", "covers": [aspect_empty]},
        {"action": "click Drain queue",
         "actual": "Queue processing complete. 1 queued item processed; staff lead reviewing; CEO healthy",
         "expected": aspect_drain, "verdict": "pass", "covers": [aspect_drain]},
    ]

    def judge(_role, _repo, prompt, **_kwargs):
        captured["prompt"] = prompt
        return {"fixed": True, "confidence": 0.99, "reason": "late transition is proved"}

    monkeypatch.setattr(dev_loop, "_ai_json", judge)
    verdict = dev_loop._judge_fixed(
        {"story": "US-003", "detail": "queue never populated", "expected": aspect_drain},
        "empty state recovers", ["src/browser.js"], "@@ drain fix", [],
        {"restarted": False}, repo=".", verification={
            "complete": True,
            "coverage": [{"aspect": aspect_empty, "covered": True},
                         {"aspect": aspect_drain, "covered": True}],
            "steps_detail": records,
            "stories": [{"story": "US-003", "complete": True,
                         "stop_reason": "coverage-complete", "covered": 2,
                         "coverage_total": 2}],
        })

    assert verdict["fixed"] is True
    assert "Queue processing complete. 1 queued item processed" in captured["prompt"]
    assert "navigation-noise" not in captured["prompt"]
    assert len(captured["prompt"]) < 30_000


def test_repair_planner_uses_balanced_stage_tier_without_weakening_final_judge(monkeypatch):
    received = {}

    def fake_json(*_args, **kwargs):
        received.update(kwargs)
        return {"agents": [{"role": "security", "files": ["src/policy.js"], "task": "fix"}],
                "rationale": "one bounded policy change"}

    monkeypatch.setattr(dev_loop, "_ai_json", fake_json)
    plan = dev_loop._plan_fix({"detail": "bare token leaked"}, {}, "secure product",
                              repo=".")

    assert plan["agents"][0]["files"] == ["src/policy.js"]
    assert received["compact"] is True
    assert received["codex_model"] == "gpt-5.6-terra"
    assert received["reasoning_effort"] == "high"


def test_repair_specialty_is_mapped_to_a_manifest_authorized_writer(monkeypatch, tmp_path):
    assert dev_loop._authorized_fix_writer_role("security-governance") == "security-redteam"
    assert dev_loop._authorized_fix_writer_role("frontend") == "frontend-engineer"
    assert dev_loop._authorized_fix_writer_role("invented-specialist") == "staff-engineer"
    calls = []
    monkeypatch.setattr(factory, "agent", lambda role, repo, task, **kwargs: calls.append(
        {"role": role, "repo": repo, "task": task, "kwargs": kwargs}) or {"rc": 0, "out": "done"})

    result = dev_loop._spawn_fix_agents(
        {"agents": [{"role": "security-governance", "files": ["src/policy.js"], "task": "fix leak"}]},
        {"detail": "leak"}, "secure product", repo=tmp_path)

    assert result["ok"] is True
    assert calls[0]["role"] == "security-redteam"
    assert "SPECIALTY REQUESTED BY THE REPAIR PLAN: security-governance" in calls[0]["task"]
    assert "filesystem-only sandbox" in calls[0]["task"]
    assert "start with the named files" in calls[0]["task"]
    assert "Do not spend time retrying" in calls[0]["task"]
    assert "outer QA coordinator owns" in calls[0]["task"]
    assert calls[0]["kwargs"] == {
        "timeout": dev_loop.FIX_AGENT_STAGE_TIMEOUT,
        "retries": 0,
        "compact": True,
        "codex_model": "gpt-5.6-sol",
        "reasoning_effort": "high",
    }
    assert result["results"][0]["requested_role"] == "security-governance"


def test_fixer_reproduction_is_bound_to_exact_finding_not_full_sibling_story():
    source = {"id": "US-010", "title": "Publish only verified trust content",
              "steps": ["test every claim", "test every sensitive class",
                        "attempt to publish token content", "verify a valid claim"],
              "expected_outcome": "all trust workflows work"}
    bug = {"detail": "a bare sk_live_qa_sentinel token was published",
           "expected": "the token is denied with a security-owned blocker",
           "action": {"cmd": "click", "target_text": "Run checks and publish"},
           "_qa_adjudication": {"disposition": "confirmed_defect"}}

    focused = dev_loop._focused_repro_stories([source], bug)[0]

    assert focused["id"] == "US-010"
    assert focused["category"] == "focused-regression"
    assert focused["_qa_atomic_steps"] is True
    assert "sk_live_qa_sentinel" in focused["goal"]
    assert "purpose-built seed, load, demo, or fixture" in focused["goal"]
    assert "test every claim" in focused["goal"]
    assert "attempt to publish token content" in focused["goal"]
    assert focused["expected_outcome"] == bug["expected"]
    assert "test every sensitive class" not in focused["goal"]
    assert "Historical mismatch" in focused["goal"]
    assert "do not require it to reappear" in focused["goal"]
    assert bug["expected"] in focused["goal"]
    assert "staff/internal administration records are not public exposure" in focused["goal"]
    assert "only for finding-named controls" in focused["steps"][3]
    assert "persistence" not in focused["steps"][3]
    assert all(len(step) < 180 for step in focused["steps"])
    explicit = qa_explorer._story_contract_aspects(focused)
    assert len([item for item in explicit if item.startswith("Story step ")]) == 4
    explorer = qa_explorer.Explorer("http://unused", "vision", autostart=False)
    try:
        ledger = explorer._ai_coverage_plan(focused, {})
    finally:
        explorer.close()
    assert len(ledger) == 4
    assert all(item["explicit"] for item in ledger)


def test_fixer_reproduction_narrows_unambiguous_story_expected_clause():
    source = {"id": "US-010", "title": "Publish only verified trust content",
              "steps": ["seed claims", "attempt token publication", "publish a safe claim"]}
    sensitive_clause = (
        "Attempt public content containing a standalone token and confirm it is blocked with a persisted "
        "security-owned blocker and never renders publicly."
    )
    sibling_clause = (
        "Attach evidence to a claim, verify it, publish safe content, and record all allow decisions."
    )
    bug = {
        "title": "Standalone API token bypasses privacy validation",
        "detail": "An sk_live_ token renders publicly instead of creating a security blocker.",
        "expected": f"{sensitive_clause}; {sibling_clause}",
        "action": {"cmd": "diagnose_incomplete"},
    }

    focused = dev_loop._focused_repro_stories([source], bug)[0]

    assert focused["expected_outcome"] == sensitive_clause
    assert sensitive_clause in focused["goal"]
    assert sibling_clause not in focused["goal"]


def test_fixer_reproduction_prefers_exact_temporal_failure_over_timeout_setup_clause():
    source = {"id": "US-006", "title": "Handle slow and failed review queueing",
              "steps": ["Set Timeout", "Fill the form", "Dwell pending for 10 seconds"]}
    setup = (
        "Set the Agent scenario to Timeout, complete the public enquiry form at human pace, and confirm "
        "submission becomes enabled only when valid required details and consent are provided."
    )
    pending = (
        "Submit once and verify the button disables with a pending label, double-submit is prevented, and "
        "the pending state remains stable for at least 10 seconds."
    )
    bug = {
        "title": "Selecting Agent = Timeout does not create the required slow pending submission state",
        "detail": ("The enquiry completes in approximately 9 ms, immediately showing confirmation, so the "
                   "pending label, 10-second stability, and sustained double-submit prevention cannot be exercised."),
        "expected": f"{setup}; {pending}",
        "action": {"cmd": "diagnose_incomplete"},
    }

    focused = dev_loop._focused_repro_stories([source], bug)[0]

    assert focused["expected_outcome"] == pending
    assert pending in focused["goal"]
    assert setup not in focused["goal"]


def test_focused_reproduction_restores_sealed_finding_state_instead_of_wrong_fixture(tmp_path):
    run_dir = tmp_path / "run"
    screenshots = run_dir / "screenshots"
    screenshots.mkdir(parents=True)
    shot = screenshots / "state.png"
    state = run_dir / "storage-state.json"
    shot.write_bytes(b"png")
    state.write_text("{}")
    bug = {"story": "US-008", "detail": "dead-letter Retry was absent",
           "expected": "Retry is available", "screenshot": str(shot)}

    focused = dev_loop._focused_repro_stories([{
        "id": "US-008", "steps": ["Set Timeout", "Switch Success", "Retry"]}], bug)[0]

    assert dev_loop._finding_browser_state_path(bug) == str(state)
    assert focused["focused_finding"]["browser_state_restored"] is True
    assert "exact finding-time browser state is restored" in focused["goal"]
    assert "do not submit a duplicate record" in focused["goal"]
    assert "do not load or seed an unrelated story fixture" in focused["steps"][0]
    assert "do not create or submit a duplicate record" in focused["steps"][1]
    assert "corrected user control" in focused["steps"][2]


def test_finding_state_prefers_exact_immutable_snapshot_and_rejects_later_story_cursor(tmp_path):
    run_dir = tmp_path / "run"
    screenshots = run_dir / "screenshots"
    screenshots.mkdir(parents=True)
    shot = screenshots / "state.png"
    shot.write_bytes(b"png")
    cursor = run_dir / "storage-state.json"
    cursor.write_text("{}")
    os.utime(cursor, (shot.stat().st_mtime + 30, shot.stat().st_mtime + 30))
    bug = {"shot": str(shot)}

    assert dev_loop._finding_browser_state_path(bug) is None

    exact = run_dir / "finding-state-a1b2c3.json"
    exact.write_text("{}")
    bug["finding_state_path"] = str(exact)
    assert dev_loop._finding_browser_state_path(bug) == str(exact)


def test_explorer_seals_owner_only_browser_state_at_finding_boundary(tmp_path):
    class Bridge:
        def storage_state(self):
            return {"ok": True, "state": {"cookies": [{"name": "fixture", "value": "secret"}]}}

    explorer = qa_explorer.Explorer("http://app/", "vision", autostart=False)
    explorer.artifact_dir = tmp_path
    explorer.bridge = Bridge()
    bug = {"step": 3, "action": {"cmd": "click", "target_text": "Acknowledge"},
           "shot": str(tmp_path / "screenshots" / "state.png")}

    captured = explorer._capture_finding_state(bug)

    assert captured == bug["finding_state_path"]
    path = Path(captured)
    assert path.name.startswith("finding-state-")
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["cookies"][0]["value"] == "secret"


def test_focused_pending_click_recreates_transient_form_action_instead_of_requiring_dom_checkpoint(
        tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    bug = {
        "story": "US-008",
        "detail": ('After clicking Send enquiry, the settled UI remained stuck on "Sending..." '
                   "under the Timeout scenario."),
        "expected": "The enquiry is accepted with a confirmation under the Timeout scenario.",
        "action": {"cmd": "click", "target_text": "Send enquiry"},
    }
    source = {
        "id": "US-008",
        "title": "Retry dead-lettered agent work",
        "steps": [
            "Set the Agent scenario selector to Timeout.",
            "Fill every required public enquiry field with valid details and consent.",
            "Submit once and dwell on the pending state for at least 10 seconds.",
            "Verify the confirmation and queued job.",
        ],
    }

    focused = dev_loop._focused_repro_stories(
        [source], bug, browser_state_path=str(state))[0]

    assert focused["focused_finding"]["transient_recheck"] is True
    assert "Recreate one fresh, distinct triggering user action" in focused["steps"][1]
    assert "dwell for the finding's stated duration" in focused["steps"][2]
    assert "Fill every required public enquiry field" in focused["goal"]
    assert "already-created exact triggering input" not in focused["goal"]


def test_focused_diagnostic_finding_reloads_restored_state_instead_of_demanding_impossible_control(
        tmp_path):
    run_dir = tmp_path / "run"
    screenshots = run_dir / "screenshots"
    screenshots.mkdir(parents=True)
    shot = screenshots / "state.png"
    (run_dir / "storage-state.json").write_text("{}")
    shot.write_bytes(b"png")
    bug = {
        "story": "US-010",
        "detail": "standalone token was published",
        "expected": "the restored token is denied and never rendered publicly",
        "screenshot": str(shot),
        "action": {"cmd": "diagnose_incomplete"},
    }

    focused = dev_loop._focused_repro_stories([{
        "id": "US-010", "steps": ["Seed claim", "Attempt token publication"]}], bug)[0]

    assert "Reload the restored page once" in focused["steps"][2]
    assert "re-evaluates and re-renders" in focused["steps"][2]
    assert "do not submit a duplicate record" in focused["steps"][2]
    assert "corrected user control" not in focused["steps"][2]
    assert "do not require a blocker, hidden value, or duplicate record" in focused["steps"][1]
    assert "exact triggering input" not in focused["steps"][1]


def test_focused_diagnostic_uses_durable_transport_state_path_when_finding_does_not_embed_it(
        tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    bug = {
        "story": "US-010",
        "detail": "standalone token was published",
        "expected": "the restored token is denied and never rendered publicly",
        "action": {"cmd": "diagnose_incomplete"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-010", "steps": ["Seed claim", "Attempt token publication"]}],
        bug,
        browser_state_path=str(state),
    )[0]

    assert focused["focused_finding"]["browser_state_restored"] is True
    assert "do not require a blocker, hidden value, or duplicate record" in focused["steps"][1]
    assert "Reload the restored page once" in focused["steps"][2]


def test_focused_surface_observation_never_mutates_the_restored_business_state(tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    bug = {
        "story": "US-009",
        "detail": "pending approval audit omitted the requesting actor and reason",
        "expected": "one pending ticket remains visible with attributable audit evidence",
        "action": {"cmd": "inspect_surfaces", "targets": ["Governance audit history"]},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-009", "steps": ["Submit draft", "Inspect governance surfaces"]}],
        bug,
        browser_state_path=str(state),
    )[0]

    assert "Reload the restored page once" in focused["steps"][2]
    assert "corrected user control" not in focused["steps"][2]
    prompt = qa_explorer._focused_decide_prompt("vision", focused, {}, [], checklist=[])
    assert "Never approve, reject, send, publish, acknowledge, drain" in prompt


def test_focused_reload_finding_names_the_exact_navigation_instead_of_inventing_a_control(tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    bug = {
        "story": "US-011",
        "detail": "After refresh the status surface exposes raw private audit metadata.",
        "expected": "Acknowledged blockers persist and raw audit metadata remains absent after refresh.",
        "action": {"cmd": "reload"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-011", "steps": ["Seed CEO risk", "Acknowledge", "Refresh"]}],
        bug, browser_state_path=str(state))[0]

    assert "Reload the restored page once" in focused["steps"][2]
    assert "corrected user control" not in focused["steps"][2]
    assert focused["focused_finding"]["action"] == {"cmd": "reload"}


def test_focused_post_action_focus_finding_performs_one_fresh_equivalent_action(tmp_path):
    state = tmp_path / "finding-state-focus.json"
    state.write_text("{}")
    bug = {
        "story": "US-011",
        "detail": ("After the mouse acknowledgement, focus moved to the remaining Acknowledge button, "
                   "but no distinct visible focus indicator was rendered."),
        "expected": ("That blocker becomes acknowledged, another blocker remains separately open, "
                     "and focus remains visible without exposing private metadata."),
        "action": {"cmd": "click", "target_text": "Acknowledge risk"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-011", "steps": ["Load risk fixture", "Acknowledge one risk"]}],
        bug, browser_state_path=str(state))[0]

    assert focused["focused_finding"]["interaction_mechanics_recheck"] is True
    assert focused["focused_finding"]["settled_outcome_recheck"] is False
    assert "Identify one currently open finding-named action control" in focused["steps"][1]
    assert "Perform exactly one fresh finding-named action" in focused["steps"][2]
    assert "distinct visible focus indicator" in focused["steps"][2]
    assert not any("Reload the restored page" in step for step in focused["steps"])


def test_focused_blank_form_enter_finding_uses_validation_contract_not_list_action_template(tmp_path):
    state = tmp_path / "finding-state-blank-form.json"
    state.write_text("{}")
    bug = {
        "story": "US-004",
        "detail": ("Pressing Enter in the blank required Name field left submission blocked but produced "
                   "no field-specific required-field message."),
        "expected": ("Invalid values are rejected with clear field-specific or form-level messages, focus "
                     "remains visible, and no enquiry, job, audit event, or notification is persisted."),
        "action": {"cmd": "press", "value": "Enter"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-004", "steps": ["Open form", "Press Enter on blank fields"]}],
        bug, browser_state_path=str(state))[0]

    assert focused["focused_finding"]["form_enter_validation_recheck"] is True
    assert focused["focused_finding"]["interaction_mechanics_recheck"] is False
    assert "blank required Name field" in focused["steps"][1]
    assert "Press Enter exactly once" in focused["steps"][2]
    assert "field-specific Name error" in focused["steps"][3]
    assert "no enquiry, agent job, audit event, or notification persisted" in focused["steps"][3]
    assert "equivalent action" not in " ".join(focused["steps"])


def test_focused_valid_form_enter_finding_rebuilds_and_submits_instead_of_testing_blank_validation(tmp_path):
    state = tmp_path / "finding-state-after-broken-enter.json"
    state.write_text("{}")
    bug = {
        "story": "US-001",
        "title": ("Pressing Enter in the correctly targeted Name field with no required fields empty "
                  "triggered required-field validation errors"),
        "detail": ("Pressing Enter in the correctly targeted Name field with no required fields empty "
                   "triggered required-field validation errors instead of submitting the enquiry."),
        "expected": "press Enter to submit.",
        "action": {"cmd": "press", "value": "Enter", "target_text": "Name"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-001", "title": "Submit a minimal valid enquiry",
          "steps": ["Open enquiry form", "Fill every required field", "Press Enter to submit"]}],
        bug, browser_state_path=str(state))[0]

    assert focused["focused_finding"]["form_enter_submission_recheck"] is True
    assert focused["focused_finding"]["form_enter_validation_recheck"] is False
    assert "Populate every required enquiry field" in focused["steps"][1]
    assert "press Enter exactly once" in focused["steps"][2]
    assert "one enquiry is accepted" in focused["steps"][3]
    assert "blank required Name" not in " ".join(focused["steps"])


def test_version_seven_misclassified_valid_enter_contract_is_upgraded_to_submission_recheck():
    legacy = {
        "id": "US-001", "category": "focused-regression", "_qa_focused_contract_version": 7,
        "focused_finding": {
            "browser_state_restored": True,
            "detail": ("Pressing Enter from Name with no required fields empty showed validation instead "
                       "of submitting the enquiry."),
            "expected": "press Enter to submit.",
            "action": {"cmd": "press", "value": "Enter", "target_text": "Name"},
            "form_enter_validation_recheck": True,
        },
        "steps": dev_loop._form_enter_validation_steps(),
    }

    upgraded = dev_loop._normalize_focused_repro_contract(legacy)

    assert upgraded["_qa_focused_contract_version"] == 9
    assert upgraded["focused_finding"]["form_enter_submission_recheck"] is True
    assert upgraded["focused_finding"]["form_enter_validation_recheck"] is False
    assert "Populate every required enquiry field" in upgraded["steps"][1]


def test_version_four_focus_contract_is_upgraded_to_active_interaction_recheck():
    legacy = {
        "id": "US-011", "category": "focused-regression", "_qa_focused_contract_version": 4,
        "focused_finding": {
            "browser_state_restored": True,
            "detail": "After the mouse acknowledgement focus was not visible.",
            "expected": "Another blocker remains open and the focus indicator is visible.",
            "action": {"cmd": "click", "target_text": "Acknowledge risk"},
        },
        "steps": ["Inspect.", "Confirm.", "Reload the restored page once.", "Verify."],
    }

    upgraded = dev_loop._normalize_focused_repro_contract(legacy)

    assert upgraded["_qa_focused_contract_version"] == 9
    assert upgraded["focused_finding"]["interaction_mechanics_recheck"] is True
    assert "Perform exactly one fresh finding-named action" in upgraded["steps"][2]


def test_read_only_residual_cannot_authorize_mutation_for_an_unperformed_historical_action(
        monkeypatch, tmp_path):
    def forbidden_reviewer(*_args, **_kwargs):
        raise AssertionError("repository reviewers must not judge a missing browser action receipt")

    monkeypatch.setattr(factory, "agent", forbidden_reviewer)
    finding = {
        "bug": "The selected blocker was not acknowledged and focus did not move.",
        "expected": "The selected blocker is acknowledged and another remains open with visible focus.",
        "action": {"cmd": "inspect_surfaces", "targets": ["Unresolved blockers"]},
        "recovery_trigger": {"action": {"cmd": "click", "target_text": "Acknowledge risk"}},
    }

    triage = dev_loop._triage_finding(finding, {}, "vision", repo=tmp_path)

    assert triage["disposition"] == "revision_reverify_required"
    assert triage["may_mutate"] is False
    assert triage["evidence_contract_recheck"] is True
    assert "no prior effectful action receipt" in triage["reason"]


def test_read_only_focus_residual_replays_its_effectful_recovery_trigger(tmp_path):
    state = tmp_path / "finding-state-focus-residual.json"
    state.write_text("{}")
    residual = {
        "bug": "The selected blocker was not acknowledged and focus did not move.",
        "expected": "The selected blocker is acknowledged and another remains open with visible focus.",
        "action": {"cmd": "inspect_surfaces", "targets": ["Unresolved blockers"]},
        "recovery_trigger": {"action": {"cmd": "click", "target_text": "Acknowledge risk"}},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-011", "steps": ["Load fixture", "Acknowledge one risk"]}],
        residual, browser_state_path=str(state))[0]

    assert focused["focused_finding"]["action"]["cmd"] == "click"
    assert focused["focused_finding"]["interaction_mechanics_recheck"] is True
    assert "Perform exactly one fresh finding-named action" in focused["steps"][2]


def test_focused_post_action_settled_outcome_reloads_instead_of_repeating_publish(tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    bug = {
        "story": "US-010",
        "detail": ("Publication was correctly blocked, but the settled UI only showed a generic message "
                   "and did not expose a staff-owned blocker identifying claim_private as ineligible."),
        "expected": ("No content is published and a persisted staff blocker record identifies "
                     "claim_private as ineligible."),
        "action": {"cmd": "click", "target_text": "Run checks and publish"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-010", "steps": ["Load claims", "Fill publication form", "Publish"]}],
        bug, browser_state_path=str(state))[0]

    assert focused["focused_finding"]["settled_outcome_recheck"] is True
    assert "Reload the restored page once" in focused["steps"][2]
    assert "finding-named restored prerequisites that are observable" in focused["steps"][1]
    assert "Exercise the corrected user control" not in " ".join(focused["steps"])
    assert "do not submit a duplicate record" in focused["steps"][2]


def test_focused_settled_diagnostics_reloads_instead_of_repeating_queue_drain(tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    bug = {
        "story": "US-006",
        "detail": "Drain registered an effect but the settled diagnostics show no degraded state.",
        "expected": "Settled staff and CEO diagnostics expose the terminal or degraded state.",
        "action": {"cmd": "click", "target_text": "Drain queue"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-006", "steps": ["Submit", "Drain queue"]}],
        bug, browser_state_path=str(state))[0]

    assert focused["focused_finding"]["settled_outcome_recheck"] is True
    assert "Reload the restored page once" in focused["steps"][2]
    assert "corrected user control" not in focused["steps"][2]


def test_effectful_business_action_ledger_preserves_mismatched_mutations_beyond_recent_history():
    import qa_explorer

    history = [{"step": index, "action": {"cmd": "scroll", "value": "600"},
                "targeting": {"driver_ok": True, "effect_registered": True}}
               for index in range(8)]
    history.extend([
        {"step": 8, "action": {"cmd": "click", "target_text": "Acknowledge blocker one"},
         "matched": False, "verdict": "bug",
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "targeted_label": "Acknowledge blocker one"}},
        {"step": 9, "action": {"cmd": "press", "target_text": "Acknowledge blocker two",
                                "value": "Enter"},
         "matched": False, "verdict": "bug",
         "targeting": {"driver_ok": True, "effect_registered": True,
                       "targeted_label": "Acknowledge blocker two"}},
    ])

    rendered = qa_explorer._fmt_effectful_control_history(history)

    assert "Acknowledge blocker one" in rendered
    assert "Acknowledge blocker two" in rendered
    assert '"key": "Enter"' in rendered
    assert '"cmd": "scroll"' not in rendered


def test_focused_business_mutation_is_mechanically_one_attempt_even_after_composite_mismatch():
    import qa_explorer

    action = {"cmd": "press", "target_text": "Acknowledge risk: Failed agent job", "value": "Enter"}
    targeting = {
        "action_kind": "press", "driver_ok": True, "effect_registered": True,
        "label_matched": True, "intended": "Acknowledge risk: Failed agent job",
        "targeted_label": "Acknowledge risk: Failed agent job",
    }

    assert qa_explorer._registered_business_mutation(action, targeting) is True
    assert qa_explorer._registered_business_mutation(
        {"cmd": "click", "target_text": "Load US-011 CEO risk"}, targeting | {
            "intended": "Load US-011 CEO risk", "targeted_label": "Load US-011 CEO risk",
        }) is False
    assert qa_explorer._registered_business_mutation(action, targeting | {"effect_registered": False}) is False


def test_negative_repeat_instruction_is_not_misread_as_a_held_key_requirement():
    import qa_explorer

    aspect = ("Story step 3.1: Perform exactly one fresh finding-named action; do not repeat a historical "
              "observation, scroll, or diagnostic action.")
    targeting = {
        "action_kind": "click", "driver_ok": True, "effect_registered": True,
        "label_matched": True,
    }
    before = {"url": "http://app", "activeElement": {"tag": "button", "ariaLabel": "Acknowledge"}}
    after = {"url": "http://app", "activeElement": {
        "tag": "button", "ariaLabel": "Acknowledge next", "focusVisible": True,
    }}

    assert qa_explorer._grounded_demonstrated([aspect], targeting, before, after) == [aspect]


def test_exhaustive_traversal_uses_live_inventory_instead_of_model_supplied_cap():
    import qa_explorer

    action = {"cmd": "traverse", "direction": "forward", "count": 30}
    exhaustive = {
        "id": "US-012", "steps": ["Use Tab to reach and activate every interactive control."],
    }
    focused = {
        "category": "focused-regression",
        "focused_finding": {"action": {"cmd": "traverse", "count": 30}},
        "steps": ["Reproduce the named traversal."],
    }

    assert "count" not in qa_explorer._contract_fenced_traversal_action(action, exhaustive)
    assert "count" not in qa_explorer._contract_fenced_traversal_action(action, focused)
    assert qa_explorer._contract_fenced_traversal_action(
        action, {"steps": ["Inspect the next few focus stops."]}
    )["count"] == 30


def test_focused_viewport_recheck_drops_broad_sibling_control_clues(tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    bug = {
        "story": "US-012",
        "detail": "At 375px the Agent combobox and consent checkboxes have incorrect accessible names.",
        "expected": "The named controls retain programmatically determinable labels at mobile width.",
        "action": {"cmd": "viewport", "value": {"width": 375, "height": 844}},
    }
    focused = dev_loop._focused_repro_stories([{
        "id": "US-012",
        "steps": ["Open at mobile width", "Activate Drain queue and every unrelated staff control"],
    }], bug, browser_state_path=str(state))[0]

    assert "Reapply the reported finding viewport dimensions once" in focused["steps"][2]
    assert "exact tested width and height" in focused["steps"][2]
    assert "Drain queue" not in focused["goal"]
    assert "every unrelated staff control" not in focused["goal"]
    assert "never exercise a sibling control" in focused["goal"]
    assert "co-rendered siblings to be absent" in focused["steps"][3]
    assert "controls restricted to a different state are absent" not in focused["steps"][3]


def test_focused_latency_diagnostic_does_not_invent_an_associated_blocker(tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    bug = {
        "story": "US-006",
        "detail": "Timeout submission completed in 9 ms instead of remaining pending for 10 seconds.",
        "expected": "The pending state remains stable for at least 10 seconds.",
        "action": {"cmd": "diagnose_incomplete"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-006", "steps": ["Set Timeout", "Submit", "Dwell for 10 seconds"]}],
        bug,
        browser_state_path=str(state),
    )[0]

    assert focused["focused_finding"]["transient_recheck"] is True
    assert "fresh, distinct triggering user action" in focused["steps"][1]
    assert "transient busy/pending safeguards" in focused["steps"][2]
    assert "stated duration" in focused["steps"][2]
    assert "Reload the restored page" not in " ".join(focused["steps"])
    assert "associated blocker" not in " ".join(focused["steps"])
    assert "context, but the defect concerns a transient state" in focused["goal"]


def test_dev_self_qa_resumes_focused_browser_state_and_coverage_as_one_checkpoint(
        monkeypatch, tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    created, explored = [], []

    class Explorer:
        def __init__(self, *_args, **kwargs):
            created.append(kwargs)
            self.coverage = [{"aspect": "retry transition", "covered": True}]
            self.stop_reason = "coverage-complete"
            self.infrastructure_error = None
            self.missing_capabilities = []
            self.resume_state_path = str(state)

        def explore(self, _story, **kwargs):
            explored.append(kwargs)
            return [{"verdict": {"matches_expected": True},
                     "demonstrated": ["retry transition"]}]

        def close(self):
            pass

    monkeypatch.setattr(qa_explorer, "Explorer", Explorer)
    monkeypatch.setattr(dev_loop, "_audit", lambda *_a, **_k: None)
    ledger = [{"aspect": "retry transition", "covered": True}]
    prior_receipts = [{"verdict": "match", "covers": ["retry transition"]}]
    report = dev_loop.dev_self_qa(
        "http://app", "vision", [{"id": "US-008", "category": "focused-regression",
                                   "steps": ["retry transition"]}],
        return_report=True, browser_state_path=str(state),
        resume_covered=["retry transition"], resume_coverage=ledger,
        resume_steps_detail=prior_receipts)

    assert created[0]["resume_state_path"] == str(state)
    assert explored[0]["resume_covered"] == ["retry transition"]
    assert explored[0]["resume_coverage"] == ledger
    assert explored[0]["resume_steps_detail"] == prior_receipts
    assert report["resume_state_path"] == str(state)
    assert report["coverage"] == ledger
    assert report["steps_detail"] == [
        {"verdict": "match", "covers": ["retry transition"]},
        {"verdict": {"matches_expected": True}, "demonstrated": ["retry transition"]},
    ]
    assert report["complete"] is True


def test_focused_fresh_state_console_finding_replays_only_reset_storage():
    bug = {
        "story": "US-012",
        "detail": "Fresh-state reopen emitted a console 404 error.",
        "expected": "Browser storage clears and the target reopens without console or network errors.",
        "action": {"cmd": "reset_storage", "value": "http://app"},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-012", "steps": ["Navigate all controls"]}], bug)[0]

    assert focused["_qa_focused_contract_version"] == 9
    assert focused["focused_finding"]["reset_storage_recheck"] is True
    assert len(focused["steps"]) == 4
    assert all("fresh-state reset" in step for step in focused["steps"])
    assert "submit" not in focused["steps"][1].casefold()
    decision = qa_explorer._pending_focused_reset_storage_decision(
        focused, [], "http://app")
    assert decision["next_action"] == {"cmd": "reset_storage", "value": "http://app"}
    coverage = [{"aspect": f"Story step {index}.1: {step}", "covered": False, "explicit": True}
                for index, step in enumerate(focused["steps"], start=1)]
    proven = qa_explorer._mechanically_proven_unresolved(
        coverage, {"action_kind": "reset_storage", "driver_ok": True}, {},
        {"url": "http://app", "console_errors": [],
         "recent_requests": [{"url": "http://app", "status": 200}]})
    assert proven == [item["aspect"] for item in coverage]


def test_focused_contract_upgrade_rejects_stale_coverage_rows(monkeypatch, tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    explored = []

    class Explorer:
        def __init__(self, *_args, **_kwargs):
            self.coverage = []
            self.stop_reason = "cancelled-incomplete"
            self.infrastructure_error = None
            self.missing_capabilities = []
            self.resume_state_path = str(state)

        def explore(self, _story, **kwargs):
            explored.append(kwargs)
            return []

        def close(self):
            pass

    monkeypatch.setattr(qa_explorer, "Explorer", Explorer)
    dev_loop.dev_self_qa(
        "http://app", "vision", [{"id": "US-012", "category": "focused-regression",
                                   "steps": dev_loop._reset_storage_recheck_steps()}],
        browser_state_path=str(state), resume_covered=["old generic submit"],
        resume_coverage=[{"aspect": "old generic submit", "covered": True}],
        resume_steps_detail=[{"verdict": "match", "covers": ["old generic submit"]}])

    assert "resume_covered" not in explored[0]
    assert "resume_coverage" not in explored[0]
    assert "resume_steps_detail" not in explored[0]


def test_storage_resume_reopens_unsaved_form_and_dwell_claims_when_dom_is_empty():
    ledger = [
        {"aspect": "Verify initial empty-storage form and disabled submit.", "covered": True},
        {"aspect": "Enter realistic name, email, postcode, frequency, and dog details.", "covered": True},
        {"aspect": "Check data-retention consent and leave marketing consent unchecked.", "covered": True},
        {"aspect": "Dwell idle for 10 seconds and verify input and focus remain unchanged.", "covered": True},
        {"aspect": "Press Enter to submit.", "covered": False},
        {"aspect": "Verify Enquiry received confirmation and persisted staff/CEO updates.", "covered": False},
    ]
    state = {"elements": [
        {"idx": 1, "tag": "input", "type": "text", "name": "name", "required": "true",
         "value": "", "formIndex": 0},
        {"idx": 2, "tag": "input", "type": "checkbox", "name": "retention",
         "required": "true", "checked": "false", "formIndex": 0},
        {"idx": 3, "tag": "button", "type": "submit", "text": "Send enquiry",
         "formIndex": 0, "formValid": False},
    ]}

    repaired, reopened = qa_explorer._reopen_transient_dom_resume_claims(
        ledger, state, {"title": "Submit a minimal valid enquiry"})

    assert repaired[0]["covered"] is True
    assert [item["covered"] for item in repaired[1:4]] == [False, False, False]
    assert reopened == {item["aspect"] for item in ledger[1:4]}
    assert all(item.get("coverage_repaired") == "transient-dom-state-not-restored"
               for item in repaired[1:4])


def test_storage_resume_keeps_form_claims_when_restored_dom_is_still_valid():
    ledger = [
        {"aspect": "Enter realistic name and email fields.", "covered": True},
        {"aspect": "Press Enter to submit.", "covered": False},
    ]
    state = {"elements": [
        {"tag": "input", "type": "text", "required": "true", "value": "Asha", "formIndex": 0},
        {"tag": "button", "type": "submit", "text": "Send enquiry",
         "formIndex": 0, "formValid": True},
    ]}

    repaired, reopened = qa_explorer._reopen_transient_dom_resume_claims(
        ledger, state, {"title": "Submit a minimal valid enquiry"})

    assert repaired == ledger
    assert reopened == set()


def test_live_browser_checkpoint_atomically_pairs_coverage_with_resume_receipts(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()
    (repo / "app.js").write_text("export const currentRevision = true;\n")
    run = tmp_path / "evidence" / "run"
    run.mkdir(parents=True)
    aspect = "The exact denied publication remains absent after reload"

    class Bridge:
        @staticmethod
        def storage_state():
            return {"ok": True, "state": {"cookies": [], "origins": []}}

    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    explorer.bridge = Bridge()
    explorer.artifact_dir = run
    explorer._current_story_id = "US-010"
    explorer.coverage = [{"aspect": aspect, "covered": True, "explicit": True,
                          "proof": {"action_kind": "inspect_surfaces",
                                    "recorded_at": time.time(), "engine": "codex"}}]
    explorer._checkpoint({"title": "Governed publishing"}, [{
        "action": {"cmd": "inspect_surfaces", "targets": ["Public"]},
        "reasoning": "inspect the settled public surface",
        "expected": aspect,
        "actual": {"url": "http://app", "title": "app", "statusText": "blocked",
                   "bodyText": "full private page text must not be copied into the checkpoint",
                   "console_errors": [], "screenshot": "/tmp/proof.png"},
        "targeting": {"driver_ok": True, "effect_registered": True,
                      "targeted_label": "Public"},
        "verdict": {"matches_expected": True, "verdict": "pass", "bug": None},
        "demonstrated": [aspect],
    }])

    checkpoint = json.loads((run / "checkpoint.json").read_text())
    assert checkpoint["tested"] == [aspect]
    assert checkpoint["steps_detail"][0]["covers"] == [aspect]
    assert "full private page text" not in (run / "checkpoint.json").read_text()
    assert (run / "checkpoint.json").stat().st_mode & 0o777 == 0o600

    recovered = dev_loop._latest_resume_checkpoint(
        {"title": "Governed publishing"}, repo, tmp_path / "evidence")
    assert recovered["covered"] == [aspect]
    assert recovered["coverage"][0]["covered"] is True
    assert recovered["steps_detail"][0]["covers"] == [aspect]
    assert Path(recovered["resume_state_path"]).is_file()


def test_fixer_checkpoint_preserves_exact_finding_for_reviewer_resume(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    finding = {"finding_id": "fresh-finding", "story": "US-010",
               "bug": "current revision still misattributes the blocker"}

    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "checkpoint_required", "may_mutate": False,
        "reason": "insufficient safety runway for the independent reviewers",
        "reviews": [], "checkpoint_required": True,
    })
    result = dev_loop.fix_bug(
        finding, {"repo": str(repo)}, "vision", target_url="http://app",
        stories=[{"id": "US-010", "steps": ["Publish"]}], repo=str(repo))

    assert result["checkpoint_required"] is True
    assert result["resume_triage_finding"] == finding


def test_resumed_triage_finding_does_not_relaunch_focused_browser(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    original = {"finding_id": "historical", "story": "US-010", "bug": "old observation"}
    fresh = {"finding_id": "fresh", "story": "US-010", "bug": "current sealed observation"}
    reviewed = []

    def triage(item, *_args, **_kwargs):
        reviewed.append(item.get("finding_id"))
        return {"disposition": "confirmed_defect", "may_mutate": True,
                "reason": "current repository evidence confirms it", "reviews": []}

    monkeypatch.setattr(dev_loop, "_triage_finding", triage)
    monkeypatch.setattr(dev_loop, "_triage_citations_current", lambda *_a, **_k: True)
    monkeypatch.setattr(dev_loop, "_external_stage_has_runway", lambda *_a, **_k: False)
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("sealed triage resume must not repay for browser reproduction")))
    result = dev_loop.fix_bug(
        original, {"repo": str(repo)}, "vision", target_url="http://app",
        stories=[{"id": "US-010", "steps": ["Publish"]}], repo=str(repo),
        resume_triage_finding=fresh, resume_existing=True)

    assert reviewed == ["fresh"]
    assert result["checkpoint_required"] is True
    assert result["attempts"] == 0


def test_dev_self_qa_seals_direct_fixer_residual_before_return(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.js").write_text("export const ready = false;\n")
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    class Explorer:
        def __init__(self, *_args, **_kwargs):
            self.coverage = [{"aspect": "behavior works", "covered": True}]
            self.stop_reason = "blocking-wall"
            self.infrastructure_error = None
            self.missing_capabilities = []
            self.resume_state_path = None
            self.artifact_dir = evidence

        def explore(self, _story, **kwargs):
            kwargs["on_bug"]({
                "story": "US-1", "bug": "behavior is still broken", "blocking": True,
                "severity": "high", "url": "http://app", "expected": "behavior works",
                "action": {"cmd": "click", "target_text": "Run"},
            })
            return [{"step": 0}]

        def close(self):
            pass

    monkeypatch.setattr(qa_explorer, "Explorer", Explorer)
    monkeypatch.setattr(dev_loop, "_audit", lambda *_a, **_k: None)
    report = dev_loop.dev_self_qa(
        "http://app", "vision", [{"id": "US-1", "steps": ["Run behavior"]}],
        return_report=True, resume_repo=str(repo))

    assert report["bugs"][0]["finding_id"].startswith("qaf-")
    provenance = report["bugs"][0]["evidence_provenance"]
    assert Path(provenance["manifest_path"]).is_file()
    assert provenance["file_count"] == 1


def test_fix_reverification_prefers_sealed_finding_state_over_mutated_worker_checkpoint(
        monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    finding_run = tmp_path / "finding-run"
    (finding_run / "screenshots").mkdir(parents=True)
    finding_state = finding_run / "storage-state.json"
    finding_state.write_text("{}")
    shot = finding_run / "screenshots" / "finding.png"
    shot.write_bytes(b"png")
    worker_state = tmp_path / "later-mutated-worker-state.json"
    worker_state.write_text("{}")
    observed = {}

    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: {
        "disposition": "revision_reverify_required", "may_mutate": False,
        "reason": "revision changed", "reviews": [],
    })

    def fake_self_qa(*_args, **kwargs):
        observed.update(kwargs)
        return {"bugs": [], "complete": True, "coverage": [],
                "resume_state_path": kwargs.get("browser_state_path")}

    monkeypatch.setattr(dev_loop, "dev_self_qa", fake_self_qa)
    result = dev_loop.fix_bug(
        {"story": "US-009", "detail": "audit attribution missing",
         "expected": "one attributable audit event", "action": {"cmd": "inspect_surfaces"},
         "screenshot": str(shot)},
        {"repo": str(repo)}, "vision", target_url="http://app",
        stories=[{"id": "US-009", "steps": ["Inspect audit"]}], repo=str(repo),
        resume_state_path=str(worker_state), resume_covered=["wrong state proof"],
        resume_coverage=[{"aspect": "wrong state proof", "covered": True}],
        resume_existing=True,
    )

    assert result["fixed"] is True
    assert observed["browser_state_path"] == str(finding_state)
    assert observed["resume_covered"] is None
    assert observed["resume_coverage"] is None


def test_senior_adjudication_cannot_authorize_mutation_for_a_different_residual():
    adjudication = {
        "case_id": "case-1", "review_id": "review-1", "finding_id": "finding-original",
        "disposition": "confirmed_defect",
    }

    assert dev_loop._adjudication_applies(
        {"finding_id": "finding-original"}, adjudication) is True
    assert dev_loop._adjudication_applies(
        {"bug": "unrelated residual", "recovery_trigger": {"finding_id": "finding-original"}},
        adjudication) is False
    assert dev_loop._adjudication_applies(
        {"finding_id": "finding-other"}, adjudication) is False


def test_dev_self_qa_boots_from_an_explicit_finding_state_without_restoring_old_ledger(
        monkeypatch, tmp_path):
    state = tmp_path / "storage-state.json"
    state.write_text("{}")
    created = []

    class Explorer:
        def __init__(self, *_args, **kwargs):
            created.append(kwargs)
            self.coverage = [{"aspect": "inspect", "covered": True}]
            self.stop_reason = "coverage-complete"
            self.infrastructure_error = None
            self.missing_capabilities = []

        def explore(self, _story, **_kwargs):
            return [{"verdict": {"matches_expected": True}, "demonstrated": ["inspect"]}]

        def close(self):
            pass

    monkeypatch.setattr(qa_explorer, "Explorer", Explorer)
    monkeypatch.setattr(dev_loop, "_audit", lambda *_a, **_k: None)

    report = dev_loop.dev_self_qa(
        "http://app", "vision", [{"id": "US-008", "category": "focused-regression"}],
        browser_state_path=str(state), return_report=True)

    assert report["complete"] is True
    assert created == [{"token": None, "org": "0", "resume_state_path": str(state)}]


def test_dispute_verification_uses_the_same_focused_reproduction_contract():
    runtime_source = (SCRIPTS / "orchestra" / "runtime.py").read_text()

    assert "review_story = dev_loop._focused_repro_stories(" in runtime_source
    assert "state_path = dev_loop._finding_browser_state_path" in runtime_source
    assert 'review_resume["resume_state_path"] = state_path' in runtime_source
    assert '"story": review_story' in runtime_source
    assert "full story remains owned by the post-review continuation" in runtime_source


def test_legacy_focused_reproduction_contract_is_upgraded_on_resume():
    legacy = {
        "id": "US-012",
        "category": "focused-regression",
        "steps": [
            "Inspect state.",
            "Confirm input.",
            "Reapply viewport.",
            ("Verify the corrected expected behavior after the transition on only surfaces named by the "
             "source story or finding; confirm controls restricted to a different state are absent."),
        ],
    }

    upgraded = dev_loop._normalize_focused_repro_contract(legacy)

    assert upgraded is not legacy
    assert "co-rendered siblings to be absent" in upgraded["steps"][3]
    assert "controls restricted to a different state are absent" not in upgraded["steps"][3]
    assert upgraded["_qa_focused_contract_version"] == 9


def test_legacy_focused_reload_contract_is_upgraded_on_resume():
    legacy = {
        "id": "US-011", "category": "focused-regression",
        "focused_finding": {"action": {"cmd": "reload"}},
        "steps": [
            "Inspect state.", "Confirm input.",
            ("Exercise the corrected user control required by the expected behavior; do not repeat a "
             "historical observation, scroll, or diagnostic action."),
            "Verify corrected behavior.",
        ],
    }

    upgraded = dev_loop._normalize_focused_repro_contract(legacy)

    assert "Reload the restored page once" in upgraded["steps"][2]
    assert upgraded["_qa_focused_contract_version"] == 9


def test_focused_surface_inspection_remains_read_only_without_restored_state():
    finding = {
        "story": "US-011",
        "detail": "Inbound leads does not identify any lead older than three days.",
        "expected": "The named surfaces identify at least one lead older than three days.",
        "action": {"cmd": "inspect_surfaces", "targets": ["Inbound leads", "Enquiry review details"]},
    }

    focused = dev_loop._focused_repro_stories(
        [{"id": "US-011", "title": "Operate CEO risk dashboard"}], finding)[0]

    assert focused["focused_finding"]["observational_recheck"] is True
    assert "reported read-only observation" in focused["steps"][2]
    assert "do not click or submit" in focused["steps"][2]
    assert "Exercise the corrected user control" not in " ".join(focused["steps"])


def test_legacy_focused_surface_inspection_is_upgraded_on_resumed_actor():
    legacy = {
        "id": "US-011", "category": "focused-regression", "_qa_focused_contract_version": 8,
        "focused_finding": {
            "browser_state_restored": False,
            "action": {"cmd": "inspect_surfaces", "targets": ["Inbound leads"]},
        },
        "steps": ["Load fixture.", "Confirm.", "Exercise the corrected user control.", "Verify."],
    }

    upgraded = dev_loop._normalize_focused_repro_contract(legacy)

    assert upgraded["_qa_focused_contract_version"] == 9
    assert upgraded["focused_finding"]["observational_recheck"] is True
    assert "reported read-only observation" in upgraded["steps"][2]


def test_focused_fix_verification_never_restores_an_older_template_ledger(monkeypatch, tmp_path):
    created = []

    class Explorer:
        def __init__(self, *_args, **kwargs):
            created.append(kwargs)

        def explore(self, _story, **_kwargs):
            return []

        def close(self):
            pass

    monkeypatch.setattr(qa_explorer, "Explorer", Explorer)
    monkeypatch.setattr(dev_loop, "_latest_resume_checkpoint", lambda *_a, **_k: (
        (_ for _ in ()).throw(AssertionError("focused verification must not load an old checkpoint"))))
    monkeypatch.setattr(dev_loop, "_audit", lambda *_a, **_k: None)

    bugs = dev_loop.dev_self_qa(
        "http://app", "vision", [{"id": "US-010", "category": "focused-regression",
                                    "steps": ["attempt token-shaped input"],
                                    "expected_outcome": "the input is denied"}],
        resume_repo=str(tmp_path))

    assert bugs == []
    assert created == [{"token": None, "org": "0"}]


def test_dev_self_qa_report_distinguishes_clean_completion_from_empty_incomplete(monkeypatch):
    class Explorer:
        def __init__(self, *_args, **_kwargs):
            self.coverage = None
            self.stop_reason = None
            self.infrastructure_error = None
            self.missing_capabilities = []

        def explore(self, story, **_kwargs):
            complete = story["id"] == "complete"
            self.coverage = [{"aspect": "exact finding", "covered": complete}]
            self.stop_reason = "coverage-complete" if complete else "cancelled-incomplete"
            return [{"step": 0, "verdict": {"matches_expected": complete},
                     "demonstrated": ["exact finding"] if complete else []}]

        def close(self):
            pass

    monkeypatch.setattr(qa_explorer, "Explorer", Explorer)
    monkeypatch.setattr(dev_loop, "_audit", lambda *_a, **_k: None)

    clean = dev_loop.dev_self_qa(
        "http://app", "vision", [{"id": "complete"}], return_report=True)
    incomplete = dev_loop.dev_self_qa(
        "http://app", "vision", [{"id": "incomplete"}], return_report=True)

    assert clean["bugs"] == [] and clean["complete"] is True
    assert clean["stories"][0]["covered"] == clean["stories"][0]["coverage_total"] == 1
    assert incomplete["bugs"] == [] and incomplete["complete"] is False
    assert incomplete["stories"][0]["stop_reason"] == "cancelled-incomplete"


def test_structured_judge_can_be_enforced_into_an_empty_isolated_workspace(monkeypatch, tmp_path):
    received = {}

    def fake_agent(role, repo, task, **kwargs):
        received.update({"repo": repo, "task": task, "kwargs": kwargs})
        assert not list(Path(repo).iterdir())
        return {"rc": 0, "out": '{"verdict":"uncertain"}'}

    monkeypatch.setattr(factory, "agent", fake_agent)
    result = dev_loop._ai_json(
        "reviewer", tmp_path, "bounded dossier", compact=True,
        isolated_repo=True, timeout=30, retries=0)

    assert result["verdict"] == "uncertain"
    assert Path(received["repo"]) != tmp_path
    assert received["kwargs"] == {"timeout": 30, "retries": 0, "compact": True}


def test_triage_capsule_is_bounded_line_addressed_and_sealed(tmp_path):
    source = tmp_path / "src" / "approval.js"
    test = tmp_path / "tests" / "approval.test.js"
    source.parent.mkdir(); test.parent.mkdir()
    source.write_text("export function approveThenSend() { return sendMessage(); }\n")
    test.write_text("assert.equal(approveThenSend(), 'sent');\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"; evidence.mkdir()
    provenance_ref = dev_loop.capture_finding_provenance(tmp_path, evidence)
    _ref, provenance = dev_loop._load_finding_provenance(tmp_path, provenance_ref)

    capsule = dev_loop._triage_evidence_capsule(
        tmp_path, {"bug": "approval has no sendMessage control"},
        {"paths": ["src/approval.js"]}, "approve then send", provenance_ref, provenance)

    assert len(capsule) <= dev_loop.TRIAGE_CAPSULE_CHAR_LIMIT
    assert '"path":"src/approval.js"' in capsule
    assert '"start_line":1' in capsule and "sendMessage" in capsule
    assert provenance_ref["manifest_sha256"] in capsule


def test_triage_capsule_follows_relevant_local_helper_call_to_its_definition(tmp_path):
    source = tmp_path / "src" / "form.js"
    source.parent.mkdir()
    lines = [
        "export function buildConsent() {",
        '  return checkboxField("Retain details", "consent", false);',
        "}",
    ] + [f"// unrelated spacer {index}" for index in range(45)] + [
        "function checkboxField(label, name, checked) {",
        "  const id = `field-${name}`;",
        "  return element('label', { for: id }, [element('input', { id, checked }), label]);",
        "}",
    ]
    source.write_text("\n".join(lines) + "\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-helper-evidence"
    evidence.mkdir()
    provenance_ref = dev_loop.capture_finding_provenance(tmp_path, evidence)
    _ref, provenance = dev_loop._load_finding_provenance(tmp_path, provenance_ref)

    capsule = dev_loop._triage_evidence_capsule(
        tmp_path, {"bug": "Retain details consent has no accessible name"},
        {"paths": ["src/form.js"]}, "consent controls remain labelled", provenance_ref, provenance)

    assert "Retain details" in capsule and capsule.count("checkboxField") >= 2
    assert "function checkboxField(label, name, checked)" in capsule
    assert "for: id" in capsule


def test_triage_capsule_prioritizes_story_specific_evidence_over_repeated_generic_terms(tmp_path):
    relevant = tmp_path / "src" / "ceo_risk_dashboard.js"
    relevant.parent.mkdir()
    relevant.write_text(
        "export function acknowledgeBlockerWithKeyboard(event) {\n"
        "  if (event.key === 'Enter') return privacySafeAuditSummary('US-011');\n"
        "}\n"
    )
    distractors = tmp_path / "tests" / "generic"
    distractors.mkdir(parents=True)
    for index in range(18):
        (distractors / f"generic_{index}.test.js").write_text(
            "\n".join(["// private status audit browser metadata"] * 80) + "\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-ranked-evidence"
    evidence.mkdir()
    provenance_ref = dev_loop.capture_finding_provenance(tmp_path, evidence)
    _ref, provenance = dev_loop._load_finding_provenance(tmp_path, provenance_ref)

    capsule = dev_loop._triage_evidence_capsule(
        tmp_path,
        {"story": "US-011", "detail": "Keyboard Enter acknowledges a blocker without raw private audit metadata"},
        {}, "CEO risk dashboard remains privacy safe", provenance_ref, provenance)

    assert len(capsule) <= dev_loop.TRIAGE_CAPSULE_CHAR_LIMIT
    assert '"path":"src/ceo_risk_dashboard.js"' in capsule
    assert "acknowledgeBlockerWithKeyboard" in capsule


def test_recovery_reuses_exact_confirmed_triage_receipt_before_browser_checkpoint(tmp_path, monkeypatch):
    contract = tmp_path / "src" / "privacy_contract.js"
    repaired = tmp_path / "src" / "browser.js"
    contract.parent.mkdir()
    contract.write_text("export const auditSurface = 'aggregate-only';\n")
    repaired.write_text("export const auditView = 'raw';\n")
    evidence = tmp_path.parent / f"{tmp_path.name}-triage-receipt"
    evidence.mkdir()
    finding = {
        "finding_id": "qaf-exact", "story": "US-011", "title": "raw audit exposed",
        "detail": "raw audit metadata is visible", "expected": "aggregate-only status",
    }
    finding["evidence_provenance"] = dev_loop.capture_finding_provenance(tmp_path, evidence)
    citation = dev_loop._verified_repo_citations(tmp_path, [{
        "path": "src/privacy_contract.js", "start_line": 1, "end_line": 1,
        "quote": "export const auditSurface = 'aggregate-only';",
    }], provenance=finding["evidence_provenance"])[0]
    receipt = {
        "disposition": "confirmed_defect", "may_mutate": True,
        "finding_fingerprint": dev_loop._finding_fingerprint(finding),
        "evidence_provenance": finding["evidence_provenance"],
        "reviews": [
            {"verdict": "defect", "confidence": .99, "citations": [citation]},
            {"verdict": "defect", "confidence": .99, "citations": [citation]},
        ],
    }
    repaired.write_text("export const auditView = 'aggregate-only';\n")
    monkeypatch.setattr(dev_loop, "_triage_finding", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("the exact completed triage gate must not run again")))
    monkeypatch.setattr(dev_loop, "dev_self_qa", lambda *_a, **_k: {
        "bugs": [], "complete": True, "stop_reason": "coverage-complete",
        "coverage": [{"aspect": "privacy regression", "covered": True}],
    })
    monkeypatch.setattr(dev_loop, "_judge_fixed", lambda *_a, **_k: {
        "fixed": True, "confidence": .99, "reason": "fresh browser proof is clean",
    })

    result = dev_loop.fix_bug(
        finding, {}, "privacy-safe CEO diagnostics", target_url="http://app",
        stories=[{"id": "US-011"}], repo=str(tmp_path), resume_existing=True,
        resume_changed_files=["src/browser.js"], resume_triage_receipt=receipt)

    assert result["fixed"] is True and result["recovery_verified"] is True
    assert result["triage"]["reused_for_recovery_verification"] is True


def test_browser_wait_uses_separate_long_poll_budget_and_restores_protocol_timeout(monkeypatch):
    bridge = qa_explorer.BrowserBridge("http://app", autostart=False, timeout=2)
    sent = {}

    def fake_send(message):
        sent.update(message)
        assert bridge.timeout == qa_explorer._WAIT_MAX_S + 5
        return {"ok": True, "matched": True, "elapsed_ms": 25}

    monkeypatch.setattr(bridge, "_send", fake_send)
    result = bridge.wait_for({"kind": "status", "value": "Reply ready", "timeout_s": 9999})
    assert result["matched"] is True
    assert sent["cmd"] == "waitFor"
    assert sent["timeout_ms"] == qa_explorer._WAIT_MAX_S * 1000
    assert bridge.timeout == 2


def test_browser_dwell_is_real_bounded_command_not_noop(monkeypatch):
    bridge = qa_explorer.BrowserBridge("http://app", autostart=False, timeout=45)
    sent = {}

    def fake_send(message):
        sent.update(message)
        assert bridge.timeout == message["timeout_ms"] / 1000 + 5
        return {"ok": True, "waited": True, "elapsed_ms": message["timeout_ms"]}

    bridge.timeout = 2
    monkeypatch.setattr(bridge, "_send", fake_send)
    result = bridge.act({"cmd": "wait", "value": "10s"})
    assert result["waited"] is True
    assert sent == {"cmd": "wait", "timeout_ms": 10_000}
    assert bridge.timeout == 2

    bridge.act({"cmd": "wait", "duration_s": qa_explorer._WAIT_MAX_S + 100})
    assert sent["timeout_ms"] == qa_explorer._WAIT_MAX_S * 1000


def test_no_effect_semantic_click_retry_uses_reresolved_element_command(monkeypatch):
    bridge = qa_explorer.BrowserBridge("http://app", autostart=False, timeout=2)
    sent = {}
    monkeypatch.setattr(bridge, "_send", lambda message: sent.update(message) or {"ok": True})

    bridge.act({"cmd": "click", "idx": 41, "target_text": "Attempt to send follow-up before approval",
                "role": "button", "_qa_retry_after_no_effect": True})

    assert sent == {"cmd": "click", "idx": 41, "selector": None,
                    "_qa_retry_after_no_effect": True}


def test_multi_surface_inspection_reuses_grounded_landmark_receipt_without_long_dwell(monkeypatch):
    bridge = qa_explorer.BrowserBridge("http://app", autostart=False, timeout=2)
    sent = {}

    def fake_send(message):
        sent.update(message)
        return {"ok": True, "landmarkDwell": True, "observations": [{
            "target": "Agent jobs", "scroll": {"scrolled": True},
            "before": {"viewportText": "dead_letter"},
            "after": {"viewportText": "dead_letter"}, "stable": True,
        }]}

    monkeypatch.setattr(bridge, "_send", fake_send)
    result = bridge.act({"cmd": "inspect_surfaces", "targets": ["Agent jobs"]})

    assert result["landmarkDwell"] is True
    assert sent == {"cmd": "dwellLandmarks", "targets": ["Agent jobs"], "duration_s": 0.025}


def test_multi_surface_dwell_uses_one_real_bounded_browser_command(monkeypatch):
    bridge = qa_explorer.BrowserBridge("http://app", autostart=False, timeout=2)
    sent = {}

    def fake_send(message):
        sent.update(message)
        assert bridge.timeout >= 28
        return {"ok": True, "landmarkDwell": True, "observations": [
            {"target": target, "stable": True, "elapsed_ms": 10_000}
            for target in message["targets"]
        ]}

    monkeypatch.setattr(bridge, "_send", fake_send)
    result = bridge.act({
        "cmd": "dwell_surfaces", "targets": ["Public enquiry", "CEO command view"],
        "duration_s": 10,
    })

    assert result["landmarkDwell"] is True
    assert sent == {"cmd": "dwellLandmarks",
                    "targets": ["Public enquiry", "CEO command view"], "duration_s": 10.0}
    assert bridge.timeout == 2


def test_exact_typing_setup_skips_semantic_judge_without_claiming_story_coverage():
    verdict = qa_explorer._mechanical_routine_verdict(
        {"cmd": "type", "value": "Ada Walker"},
        {"driver_ok": True, "label_matched": True, "after_control_value": "Ada Walker"},
        {}, [])
    assert verdict["verdict"] == "pass"
    assert verdict["demonstrated"] == []
    assert verdict["_raw"]["engine"] == "mechanical-browser-proof"

    assert qa_explorer._mechanical_routine_verdict(
        {"cmd": "type", "value": "Ada Walker"},
        {"driver_ok": True, "label_matched": True, "after_control_value": "wrong"},
        {}, []) is None

    # The exact locator receipt wins when a later broad snapshot omitted or misidentified the control.
    direct = qa_explorer._mechanical_routine_verdict(
        {"cmd": "type", "value": "Ada Walker"},
        {"driver_ok": True, "label_matched": True, "after_control_value": "on",
         "driver_control_value_matches": True}, {}, [])
    assert direct["_raw"]["engine"] == "mechanical-browser-proof"
    assert qa_explorer._mechanical_routine_verdict(
        {"cmd": "type", "value": "Ada Walker"},
        {"driver_ok": True, "label_matched": True, "after_control_value": "Ada Walker",
         "driver_control_value_matches": False}, {}, []) is None


def test_exact_named_landmark_scroll_skips_semantic_judge_without_story_credit():
    verdict = qa_explorer._mechanical_routine_verdict(
        {"cmd": "scroll", "target_text": "Agent jobs"},
        {"driver_ok": True, "landmark_scroll": {
            "scrolled": True, "matched": "Agent jobs", "requested": "Agent jobs", "y": 1787}},
        {"console_errors": [], "recent_requests": [{"status": 200}]}, [])

    assert verdict["verdict"] == "pass"
    assert verdict["demonstrated"] == []
    assert verdict["_raw"]["engine"] == "mechanical-browser-proof"
    assert qa_explorer._mechanical_routine_verdict(
        {"cmd": "scroll", "target_text": "Agent jobs"},
        {"driver_ok": True, "landmark_scroll": {
            "scrolled": False, "matched": None, "requested": "Agent jobs"}},
        {"console_errors": [], "recent_requests": []}, []) is None


def test_landmark_scroll_uses_semantic_judge_when_it_claims_story_evidence():
    action = {"cmd": "scroll", "target_text": "Agent jobs"}

    assert qa_explorer._scroll_needs_semantic_judge(
        action, {"covers": ["Inspect the restored dead-letter state"]}) is True
    assert qa_explorer._scroll_needs_semantic_judge(
        action, {"covers": [], "expected_control": "Retry"}) is True
    assert qa_explorer._scroll_needs_semantic_judge(
        action, {"covers": [], "expected_control": ""}) is False


def test_story_claiming_type_action_does_not_take_zero_credit_mechanical_path():
    action = {"cmd": "type", "target_text": "Agent", "value": "Timeout"}

    assert qa_explorer._routine_action_needs_semantic_judge(
        action, {"covers": ["Set Timeout while the queue starts empty"]}) is True
    assert qa_explorer._routine_action_needs_semantic_judge(action, {"covers": []}) is False
    source = Path(qa_explorer.__file__).read_text()
    assert "manual processing-tick control" in source
    assert "`status` means an ARIA status/live region only" in source


def test_incomplete_submit_receipt_skips_semantic_judge_but_not_validation_story():
    targeting = {
        "driver_ok": True,
        "empty_required_fields_before": [{"label": "Public title"}],
    }
    verdict = qa_explorer._mechanical_incomplete_submit_verdict(
        {"title": "Publish eligible content"}, {"cmd": "click"}, targeting)
    assert verdict["verdict"] == "retry"
    assert verdict["bug"] is None
    assert verdict["_raw"]["engine"] == "mechanical-browser-submit-prerequisite"
    assert qa_explorer._mechanical_incomplete_submit_verdict(
        {"title": "Required field validation", "steps": ["Submit an empty form"]},
        {"cmd": "click"}, targeting) is None

    matrix_verdict = qa_explorer._mechanical_incomplete_submit_verdict(
        {"title": "Retry dead-lettered agent work"},
        {"cmd": "scenario_matrix"},
        {"driver_ok": True, "scenario_matrix_submit_disabled": True})
    assert matrix_verdict["verdict"] == "retry"
    assert matrix_verdict["bug"] is None


def test_visible_pending_submit_is_continued_instead_of_reported_as_immediate_bug():
    assert qa_explorer._visible_pending_transition(
        {"statusText": "Publishing and claim workflow ready."}) is False
    assert qa_explorer._visible_pending_transition({"statusText": "Sending..."}) is True
    pending = qa_explorer._mechanical_pending_transition_verdict(
        {"cmd": "click", "target_text": "Send enquiry"},
        {"driver_ok": True, "effect_registered": True, "targeted_label": "Send enquiry"},
        {"statusText": "", "viewportText": "Sending..."})

    assert pending["verdict"] == "inconclusive"
    assert pending["bug"] is None
    assert pending["_raw"]["engine"] == "mechanical-visible-pending-transition"

    record = {"reasoning": "submit valid enquiry", "expected": "Confirmation appears",
              "covers": ["Submit one valid enquiry"],
              "action": {"cmd": "click", "target_text": "Send enquiry"},
              "verdict": pending}
    wait = qa_explorer._pending_business_completion_wait_decision(
        {"title": "Timeout enquiry"}, {"viewportText": "Sending..."}, [record])
    assert wait["next_action"] == {"cmd": "wait", "value": "12s"}
    assert wait["expected"] == "Confirmation appears"
    assert wait["covers"] == ["Submit one valid enquiry"]
    assert qa_explorer._pending_business_completion_wait_decision(
        {"title": "Timeout enquiry"}, {"viewportText": "Sending..."},
        [record, {"action": {"cmd": "wait"}}]) is None


def test_ordinary_action_judge_bounds_unrelated_controls_but_accessibility_keeps_full_set():
    state = {
        "url": "http://app", "bodyText": "document " * 1000,
        "viewportText": "viewport " * 1000,
        "elements": [{"idx": index, "tag": "button", "text": f"control-{index}"}
                     for index in range(60)],
    }
    routine = qa_explorer._fmt_story_state(
        state, {"title": "Publish content"}, evaluation=True)
    assert "control-25" in routine and "control-26" not in routine
    assert "34 additional controls omitted" in routine
    accessibility = qa_explorer._fmt_story_state(
        state, {"title": "Screen reader controls"}, evaluation=True)
    assert "control-54" in accessibility


def test_codex_trace_records_real_elapsed_time(monkeypatch, tmp_path):
    traces = []

    def fake_run(*_args, **_kwargs):
        time.sleep(0.01)
        return 0, "ok", 0.01, 12, 3, "gpt-5.6-luna"

    monkeypatch.setattr(factory, "_run_once_codex", fake_run)
    monkeypatch.setattr(factory, "_add_spend", lambda *_: None)
    monkeypatch.setattr(factory.audit, "append", lambda **_: None)
    monkeypatch.setattr(factory, "_trace", lambda *args, **kwargs: traces.append((args, kwargs)))
    result = factory._agent_codex(
        "qa-security", str(tmp_path), "prompt", None, timeout=5, retries=0,
        model="gpt-5.6-luna", reasoning_effort="low")

    assert result["rc"] == 0 and result["elapsed_s"] >= 0.01
    assert traces and traces[-1][0][5] >= 0.01


def test_explorer_releases_without_synchronous_mp4_transcode(monkeypatch, tmp_path):
    import evidencepublisher

    raw = tmp_path / "videos" / "story.webm"
    raw.parent.mkdir()
    raw.write_bytes(b"raw-evidence")

    class Bridge:
        video_path = None

        def close(self):
            self.video_path = str(raw)

    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    explorer.artifact_dir = tmp_path
    explorer.bridge = Bridge()
    monkeypatch.delenv("AOS_QA_EAGER_TRANSCODE", raising=False)
    monkeypatch.setattr(evidencepublisher, "_roots", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(
        qa_explorer.artifacts, "webm_to_mp4",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must be deferred")))

    explorer.close()

    assert explorer.video_path == str(raw)
    assert explorer.video_mp4 is None
    receipt = json.loads((tmp_path / "encoding-status.json").read_text())
    assert receipt["status"] == "deferred"
    assert Path(receipt["spool"]).is_file()


def test_compound_keyboard_and_refresh_checkpoint_migrates_losslessly():
    keyboard = (
        "Using only Tab, Shift+Tab, Arrow keys, Space, and Enter, reach and operate every listed public, "
        "agent, queue, CEO, and staff control; verify visual-order focus, visible focus, and usable layout."
    )
    refresh = (
        "After interactions, refresh and repeat one keyboard path; verify refreshed state, focus behavior, "
        "announcements, and all controls remain accessible and operable."
    )
    migrated = qa_explorer._migrate_compound_coverage_ledger([
        {"aspect": "initial layout", "covered": True, "contract_steps": [1]},
        {"aspect": keyboard, "covered": False, "contract_steps": [2]},
        {"aspect": refresh, "covered": False, "contract_steps": [5]},
    ])

    assert migrated[0] == {"aspect": "initial layout", "covered": True, "contract_steps": [1]}
    atoms = migrated[1:6]
    assert [item["atomic_kind"] for item in atoms] == [
        "tab", "shift_tab", "arrow", "enter", "space"]
    assert all(item["source_aspect"] == keyboard and not item["covered"] for item in atoms)
    assert all(len(qa_explorer._atomic_coverage_aspects([item["aspect"]])) == 1 for item in atoms)
    assert [item["atomic_kind"] for item in migrated[6:]] == ["reload", "post_reload_keyboard"]
    # A rolling successor must not split an already-migrated ledger again.
    assert qa_explorer._migrate_compound_coverage_ledger(migrated) == migrated


def test_traversal_receipt_credits_tab_atom_without_action_key_field():
    aspect = "Tab traversal: reach every story-listed control with visible focus and complete focus order."
    targeting = {
        "action_kind": "traverse", "driver_ok": True, "history_direction": "forward",
        "trusted_keyboard": True,
        "keyboard_evidence": [
            {"type": "keydown", "key": "Tab", "code": "Tab", "isTrusted": True}],
        "traversal": {"direction": "forward", "derived_focusable_count": 28,
                      "unique_controls": 28, "count": 36,
                      "all_focus_visible": True, "horizontal_overflow_seen": False},
    }
    assert qa_explorer._grounded_demonstrated(
        [aspect], targeting, {}, {"activeElement": {"tag": "button", "focusVisible": True}},
        require_mechanical=True) == [aspect]
    compound = "Tab and Shift+Tab traversal of every control"
    assert qa_explorer._grounded_demonstrated(
        [compound], targeting, {}, {"activeElement": {"tag": "button", "focusVisible": True}},
        require_mechanical=True) == []


def test_keyboard_matrix_adapts_to_new_controls_and_proves_every_requested_key():
    class FakeBridge:
        _matrix_state_receipt = staticmethod(qa_explorer.BrowserBridge._matrix_state_receipt)

        def __init__(self):
            self.revealed = False
            self.pressed = []

        def state(self, include_accessibility=False):
            elements = [
                {"idx": 0, "tag": "select", "text": "Agent", "name": "agent"},
                {"idx": 1, "tag": "input", "type": "checkbox", "text": "Consent", "name": "consent"},
                {"idx": 2, "tag": "button", "type": "button", "text": "Load CEO risk"},
                {"idx": 3, "tag": "input", "type": "text", "text": "Name", "name": "name"},
            ]
            if self.revealed:
                elements.append({"idx": 4, "tag": "button", "type": "button",
                                 "text": "Acknowledge CEO risk"})
            return {"url": "http://app", "title": "app", "elements": elements,
                    "console_errors": [], "recent_requests": []}

        def act(self, action):
            if action["cmd"] == "traverse":
                backward = action.get("value") == "backward"
                key = "Shift+Tab" if backward else "Tab"
                return {"ok": True, "traversal": True,
                        "direction": action["value"], "key": key, "count": 6,
                        "derived_focusable_count": 4, "unique_controls": 4,
                        "all_focus_visible": True, "horizontal_overflow_seen": False,
                        "sequence": [{"tag": "button", "focusVisible": True}],
                        "keyboardEvidence": [{"type": "keydown", "key": "Tab", "code": "Tab",
                                              "isTrusted": True}]}
            key = action["value"]
            self.pressed.append((action["target_text"], key))
            if action["target_text"] == "Load CEO risk":
                self.revealed = True
            event_key = " " if key == "Space" else key
            return {"ok": True, "matched": action["target_text"],
                    "keyboardEvidence": [{"type": "keydown", "key": event_key,
                                          "code": "Space" if key == "Space" else key,
                                          "isTrusted": True}]}

    bridge = FakeBridge()
    result = qa_explorer.BrowserBridge._keyboard_matrix(
        bridge, {"value": ["Tab", "Shift+Tab", "ArrowDown", "Space", "Enter"]})

    assert result["ok"] is True and result["complete"] is True
    assert set(result["keys_proven"]) == {"Tab", "Shift+Tab", "ArrowDown", "Space", "Enter"}
    assert ("Acknowledge CEO risk", "Enter") in bridge.pressed
    assert result["all_applicable_controls_exercised"] is True


def test_atomic_keyboard_decision_uses_zero_model_batch_action():
    item = {"aspect": "Arrow-key operation: operate every applicable story-listed control.",
            "covered": False, "atomic_kind": "arrow"}
    decision = qa_explorer._pending_atomic_keyboard_decision({}, {}, [item])
    assert decision["next_action"] == {
        "cmd": "keyboard_matrix", "value": ["ArrowDown"], "_qa_inventory_derived": True}
    assert decision["covers"] == [item["aspect"]]


def test_atomic_keyboard_mechanical_proof_closes_without_semantic_judge():
    aspect = "Shift+Tab traversal: reach every story-listed control with visible focus."
    action = {"cmd": "traverse", "value": "backward", "_qa_inventory_derived": True}
    verdict = qa_explorer._mechanical_inventory_keyboard_verdict(
        action, {"covers": [aspect]}, [aspect])

    assert verdict["verdict"] == "pass"
    assert verdict["demonstrated"] == [aspect]
    assert verdict["_raw"]["engine"] == "mechanical-inventory-keyboard-proof"
    assert qa_explorer._mechanical_inventory_keyboard_verdict(
        {"cmd": "traverse", "value": "backward"}, {"covers": [aspect]}, [aspect]) is None
    assert qa_explorer._mechanical_inventory_keyboard_verdict(
        action, {"covers": [aspect]}, []) is None


def test_keyboard_inventory_proof_cannot_credit_dwell_and_repairs_old_checkpoint():
    space = "Space activation: operate every listed control with visible focus."
    dwell = "Dwell idle for 10 seconds and verify no focus loss or navigation."
    targeting = {
        "action_kind": "keyboard_matrix", "driver_ok": True,
        "keyboard_matrix": {
            "keys_proven": ["Space"], "all_requested_keys_proven": True,
            "all_applicable_controls_exercised": True, "complete": True,
        },
    }
    assert qa_explorer._grounded_demonstrated(
        [space, dwell], targeting, {}, {}, require_mechanical=True) == [space]

    ledger = [{"aspect": space, "covered": True}, {"aspect": dwell, "covered": True}]
    records = [{
        "action": {"cmd": "keyboard_matrix", "_qa_inventory_derived": True},
        "covers": [space], "demonstrated": [space, dwell],
        "mechanically_proven": [space, dwell],
    }]
    repaired, cleaned, reopened = qa_explorer._sanitize_inventory_checkpoint_claims(
        ledger, records)
    assert repaired == [
        {"aspect": space, "covered": True},
        {"aspect": dwell, "covered": False,
         "coverage_repaired": "inventory-cross-modality-proof"},
    ]
    assert cleaned[0]["demonstrated"] == [space]
    assert cleaned[0]["mechanically_proven"] == [space]
    assert reopened == {dwell}

    # Even after the bad inventory record ages out of the bounded resume dossier, an old ledger cannot retain
    # a duration claim with no explicit timed receipt.
    aged_out, _records, aged_reopened = qa_explorer._sanitize_inventory_checkpoint_claims(
        ledger, [])
    assert aged_out[1]["covered"] is False
    assert aged_out[1]["coverage_repaired"] == "missing-timed-dwell-receipt"
    assert aged_reopened == {dwell}


def test_foreign_story_fixture_is_detected_before_it_can_replace_recovery_state():
    elements = [
        {"idx": 1, "tag": "button", "text": "Load US-011 CEO risk"},
        {"idx": 2, "tag": "button", "text": "Load US-002 persistence"},
    ]
    assert qa_explorer._foreign_story_fixture_target(
        {"cmd": "click", "idx": 1}, {"id": "US-002"}, elements
    ) == "Load US-011 CEO risk"
    assert qa_explorer._foreign_story_fixture_target(
        {"cmd": "click", "idx": 2}, {"id": "US-002"}, elements
    ) is None


def test_focused_read_only_recheck_replays_exact_sealed_action_once():
    story = {"id": "US-002", "category": "focused-regression", "focused_finding": {
        "action": {"cmd": "inspect_surfaces", "targets": ["Agent jobs", "Audit diagnostics"]},
        "expected": "The persisted audit count remains visible.",
    }}
    coverage = [{"aspect": "Perform the reported read-only observation using its named targets.",
                 "covered": False}]
    decision = qa_explorer._focused_reported_observation_decision(story, coverage, [])
    assert decision["next_action"]["targets"] == ["Agent jobs", "Audit diagnostics"]
    assert decision["next_action"]["_qa_reported_observation_source"] is True
    assert decision["covers"] == [coverage[0]["aspect"]]
    assert qa_explorer._focused_reported_observation_decision(
        story, coverage, [{"action": decision["next_action"]}]) is None


def test_focused_read_only_recheck_does_not_repeat_after_surface_owner_repair():
    story = {"id": "US-011", "category": "focused-regression", "focused_finding": {
        "action": {"cmd": "inspect_surfaces", "targets": ["Unresolved blockers"]},
        "expected": "The CEO risk panel exposes acknowledgement controls.",
    }}
    coverage = [{"aspect": "Perform the reported read-only observation using its named targets.",
                 "covered": False}]
    repaired = {"cmd": "inspect_surfaces", "targets": ["Risk Signals"],
                "_qa_reported_observation_source": True, "_qa_surface_owner_fenced": True}

    assert qa_explorer._focused_reported_observation_decision(
        story, coverage, [{"action": repaired}]) is None


def test_one_settled_focused_observation_closes_its_three_audit_rows():
    aspects = [
        "Story step 1.1: Load only the fixture and prerequisite state needed for this exact finding.",
        ("Story step 2.1: Confirm only the finding-named prerequisites and surfaces that are observable; "
         "do not create or submit a duplicate record."),
        ("Story step 3.1: Perform the reported read-only observation exactly once using its named targets, "
         "viewport, or scroll boundary; inspect the surface directly and do not click or submit a control."),
        ("Story step 4.1: Verify the corrected expected behavior on only the finding-named surfaces. Do not "
         "require co-rendered siblings to be absent unless the finding itself names illegal exposure."),
    ]
    story = {"id": "US-011", "category": "focused-regression", "focused_finding": {
        "observational_recheck": True,
        "action": {"cmd": "inspect_surfaces", "targets": ["Unresolved blockers"]},
    }}
    action = {"cmd": "inspect_surfaces", "targets": ["Risk Signals"],
              "_qa_reported_observation_source": True, "_qa_surface_owner_fenced": True}
    verdict = {"verdict": "pass", "matches_expected": True, "bug": None,
               "model_failed": False, "infrastructure_error": None}
    targeting = {"driver_ok": True, "label_matched": True,
                 "landmark_dwell_summary": {"targets": ["Risk Signals"],
                                             "all_targets_matched": True, "all_stable": True}}
    after = {"url": "http://127.0.0.1:8816/", "bodyText": "Risk Signals Acknowledge risk",
             "console_errors": []}
    coverage = [{"aspect": aspect, "covered": index == 0}
                for index, aspect in enumerate(aspects)]

    assert qa_explorer._successful_focused_observation_coverage(
        story, action, verdict, coverage, targeting, after) == aspects[1:]
    assert qa_explorer._successful_focused_observation_coverage(
        story, action, {**verdict, "verdict": "inconclusive", "matches_expected": False},
        coverage, targeting, after) == []
    assert qa_explorer._successful_focused_observation_coverage(
        story, action, verdict, coverage,
        {**targeting, "landmark_dwell_summary": {
            "targets": ["Risk Signals"], "all_targets_matched": False, "all_stable": True}},
        after) == []


def test_us009_approval_diagnostics_does_not_invent_queue_and_blocker_parity():
    story = {"id": "US-009", "steps": [
        "Submit the draft for approval.",
        "Inspect approval diagnostics, staff console, CEO command view, blockers, and audit events.",
    ], "expected_outcome": (
        "Submitting creates exactly one approval ticket and blocker; staff and CEO views show pending "
        "approval load, and no external send side effect occurs.")}
    expected = ("Operational diagnostics show exactly one pending approval ticket and blocker, while both "
                "job summaries show one Approval required job.")
    targeting = {"landmark_dwell_summary": {
        "targets": ["Operational diagnostics"], "all_targets_matched": True, "all_stable": True}}

    repaired, changed = qa_explorer._contract_grounded_approval_projection_expected(
        story, expected, targeting)

    assert changed is True
    assert "pending approval ticket" in repaired
    assert "does not require an agent job" in repaired
    assert "separate staff, CEO, blocker, and audit projections" in repaired
    assert qa_explorer._contract_grounded_approval_projection_expected(
        story, expected, {"landmark_dwell_summary": {
            "targets": ["Operational diagnostics", "Unresolved blockers"]}}) == (expected, False)


def test_acknowledged_keyboard_control_may_move_focus_to_the_next_open_blocker():
    story = {"id": "US-011", "steps": [
        "Acknowledge an open blocker using mouse and then another using keyboard Enter or Space."
    ], "expected_outcome": (
        "Acknowledging blockers works by mouse and keyboard, persists after refresh, and keeps focus visible.")}
    expected = "The follow-up blocker becomes acknowledged and its control remains focused."

    repaired, changed = qa_explorer._contract_grounded_acknowledgement_focus_expected(
        story, expected, {"action_kind": "press", "action_key": "Enter"})

    assert changed is True
    assert "next available open-blocker acknowledgement control" in repaired
    assert "removed action control itself need not retain focus" in repaired
    assert qa_explorer._contract_grounded_acknowledgement_focus_expected(
        story, expected, {"action_kind": "click"}) == (expected, False)


def test_enter_or_space_acknowledgement_clause_requires_one_effectful_alternative():
    aspect = "Story step 5.2: another using keyboard Enter or Space"
    base = {"action_kind": "press", "driver_ok": True, "effect_registered": True,
            "label_matched": True, "intended": "Acknowledge risk",
            "targeted_label": "Acknowledge risk"}

    assert qa_explorer._grounded_demonstrated(
        [aspect], base | {"action_key": "Enter"}, {}, {}, require_mechanical=True) == [aspect]
    assert qa_explorer._grounded_demonstrated(
        [aspect], base | {"action_key": "Space"}, {}, {}, require_mechanical=True) == [aspect]
    assert qa_explorer._grounded_demonstrated(
        [aspect], base | {"action_key": "Enter", "effect_registered": False},
        {}, {}, require_mechanical=True) == []


def test_durable_enter_or_space_acknowledgement_receipt_repairs_old_open_row():
    mouse = "Story step 5.1: Acknowledge an open blocker using mouse and"
    keyboard = "Story step 5.2: another using keyboard Enter or Space"
    ledger = [{"aspect": mouse, "covered": True}, {"aspect": keyboard, "covered": False}]
    record = {
        "recorded_at": 42.0,
        "action": {"cmd": "press", "target_text": "Acknowledge risk: approval", "value": "Enter"},
        "targeting": {"driver_ok": True, "effect_registered": True, "label_matched": True,
                      "intended": "Acknowledge risk: approval"},
        "verdict": {"verdict": "pass", "matches_expected": True, "bug": None},
    }

    restored = qa_explorer._restore_alternative_keyboard_coverage(ledger, [record])

    assert restored[1]["covered"] is True
    assert restored[1]["proof"] == {
        "engine": "durable-alternative-keyboard-receipt",
        "action_kind": "press", "recorded_at": 42.0}
    assert qa_explorer._restore_alternative_keyboard_coverage(
        ledger, [{**record, "targeting": {**record["targeting"], "effect_registered": False}}]
    )[1]["covered"] is False


def test_focus_handoff_to_next_acknowledgement_is_not_a_product_bug():
    story = {"id": "US-011", "steps": [
        "Acknowledge an open blocker using mouse and then another using keyboard Enter or Space."
    ], "expected_outcome": "Acknowledgement keeps focus visible."}
    targeting = {"action_kind": "press", "action_key": "Enter", "driver_ok": True,
                 "effect_registered": True, "label_matched": True}
    after = {"activeElement": {"tag": "button", "text": "Acknowledge next risk",
                                "focusVisible": True}}
    bug = ("The blocker was acknowledged, but its button was removed and focus moved to the next "
           "acknowledgement button instead of remaining on the activated button.")

    assert qa_explorer._acknowledgement_focus_transfer_false_positive(
        story, targeting, bug, after) is True
    assert qa_explorer._acknowledgement_focus_transfer_false_positive(
        story, targeting, bug, {"activeElement": {"tag": "body"}}) is False


def test_pending_ticket_diagnostics_does_not_require_job_or_blocker_projection_parity():
    story = {"id": "US-009", "steps": [
        "Inspect approval diagnostics, staff console, CEO command view, blockers, and audit events."
    ]}
    targeting = {"action_kind": "inspect_surfaces", "driver_ok": True,
                 "landmark_dwell_summary": {"targets": ["Operational diagnostics"],
                                             "all_targets_matched": True, "all_stable": True}}
    bug = ("Operational diagnostics shows one pending approval ticket, but both job summaries report "
           "Approval required 0 and no separate blocker is shown.")

    assert qa_explorer._approval_diagnostics_projection_false_positive(
        story, targeting, bug, {"console_errors": []}) is True
    assert qa_explorer._approval_diagnostics_projection_false_positive(
        story, {**targeting, "landmark_dwell_summary": {
            "targets": ["Unresolved blockers"], "all_targets_matched": True, "all_stable": True}},
        bug, {"console_errors": []}) is False


def test_effectful_submit_receipt_repairs_inconclusive_semantic_row():
    submit = "Story step 3.1: Submit the draft for approval"
    ledger = [
        {"aspect": "Story step 1.1: Create an enquiry", "covered": True},
        {"aspect": "Story step 2.1: Edit the draft", "covered": True},
        {"aspect": submit, "covered": False},
    ]
    record = {
        "recorded_at": 11.0,
        "action": {"cmd": "click", "target_text": "Submit for approval"},
        "targeting": {"action_kind": "click", "driver_ok": True, "effect_registered": True,
                      "label_matched": True, "intended": "Submit for approval",
                      "targeted_label": "Submit for approval"},
        "verdict": {"verdict": "inconclusive", "matches_expected": False},
    }

    restored = qa_explorer._restore_effectful_business_mutation_coverage(ledger, [record])

    assert restored[2]["covered"] is True
    assert restored[2]["proof"]["engine"] == "durable-effectful-business-mutation"
    assert qa_explorer._restore_effectful_business_mutation_coverage(
        ledger, [{**record, "targeting": {**record["targeting"], "effect_registered": False}}]
    )[2]["covered"] is False


def test_exact_passed_approval_diagnostics_closes_only_that_surface_clause():
    story = {"id": "US-009", "steps": [
        "Inspect approval diagnostics, staff console, CEO command view, blockers, and audit events."
    ]}
    coverage = [
        {"aspect": "Story step 4.1: Inspect approval diagnostics", "covered": False},
        {"aspect": "Story step 4.2: staff console", "covered": False},
    ]
    action = {"cmd": "inspect_surfaces", "targets": ["Operational diagnostics"]}
    verdict = {"verdict": "pass", "matches_expected": True, "bug": None}
    targeting = {"driver_ok": True, "landmark_dwell_summary": {
        "targets": ["Operational diagnostics"], "all_targets_matched": True, "all_stable": True}}
    after = {"url": "http://app", "bodyText": "Approval state Total 1 Pending 1",
             "console_errors": []}

    assert qa_explorer._successful_approval_diagnostics_coverage(
        story, action, verdict, coverage, targeting, after) == [coverage[0]["aspect"]]
    assert qa_explorer._successful_approval_diagnostics_coverage(
        story, action, {**verdict, "matches_expected": False}, coverage, targeting, after) == []


def test_settled_audit_inspection_closes_prior_denied_send_and_human_decision():
    step5a = "Story step 5.1: Attempt to perform the send action without approval"
    step5b = "Story step 5.2: then approve or reject through the available approval control/runtime method"
    story = {"id": "US-009", "steps": [
        "Attempt to perform the send action without approval, then approve or reject through the available approval control/runtime method."
    ]}
    coverage = [{"aspect": step5a, "covered": False}, {"aspect": step5b, "covered": False}]
    action = {"cmd": "inspect_surfaces", "targets": [
        "Follow-up drafts", "Unresolved blockers", "Governance audit history"]}
    expected = ("The ticket shows approved with actor and reason, the blocker is cleared, and no external "
                "send event occurs.")
    verdict = {"verdict": "pass", "matches_expected": True, "bug": None}
    targeting = {"driver_ok": True, "landmark_dwell_summary": {
        "targets": action["targets"], "all_targets_matched": True, "all_stable": True}}
    after = {"url": "http://app", "bodyText": "Approved by qa-security. No send event.",
             "console_errors": []}
    records = [{
        "action": {"cmd": "click", "target_text": "Attempt to send follow-up before approval"},
        "targeting": {"driver_ok": True, "effect_registered": True, "label_matched": True,
                      "intended": "Attempt to send follow-up before approval"},
    }, {
        "action": {"cmd": "click", "target_text": "Approve send"},
        "targeting": {"driver_ok": True, "effect_registered": True, "label_matched": True,
                      "intended": "Approve send"},
    }]

    assert qa_explorer._successful_denied_send_decision_coverage(
        story, action, expected, verdict, coverage, targeting, after, records) == [step5a, step5b]
    assert qa_explorer._successful_denied_send_decision_coverage(
        story, action, expected, verdict, coverage, targeting, after, records[:1]) == []


def test_open_denied_send_never_targets_an_approved_send_control():
    denied = "Story step 5.1: Attempt to perform the send action without approval"
    story = {"id": "US-009", "steps": [
        "Create or seed an enquiry with a follow-up draft.",
        "Submit the draft for approval.",
        "Attempt to perform the send action without approval, then approve or reject."
    ]}
    coverage = [{"aspect": denied, "covered": False}]

    decision = qa_explorer._pending_denied_send_setup_decision(story, {"elements": [
        {"tag": "button", "text": "Send approved follow-up"},
        {"tag": "button", "ariaLabel": "Attempt to send follow-up before approval"},
    ]}, coverage)

    assert decision["next_action"]["target_text"] == "Attempt to send follow-up before approval"
    assert decision["next_action"]["_qa_denied_send_exact"] is True
    assert decision["next_action"]["_qa_denied_send_exact_v2"] is True
    assert decision["covers"] == [denied]


def test_open_denied_send_recreates_only_its_missing_causal_state():
    denied = "Story step 5.1: Attempt to perform the send action without approval"
    story = {"id": "US-009", "steps": [
        "Create or seed an enquiry with a follow-up draft.",
        "Submit the draft for approval.",
        "Attempt to perform the send action without approval, then approve or reject."
    ]}
    coverage = [{"aspect": denied, "covered": False}]
    submit = qa_explorer._pending_denied_send_setup_decision(story, {"elements": [
        {"tag": "button", "text": "Submit for approval"},
        {"tag": "button", "text": "Send approved follow-up"},
    ]}, coverage)

    assert submit["next_action"]["target_text"] == "Submit for approval"
    assert submit["covers"] == []
    assert submit["expected_control"] == "Attempt to send follow-up before approval"

    staged = qa_explorer._pending_denied_send_setup_decision(story, {"elements": [
        {"tag": "button", "text": "Send approved follow-up"},
    ]}, coverage, records=[{
        "action": {"cmd": "scenario_matrix", "_qa_denied_send_reseed": True,
                   "_qa_denied_send_reseed_nonce": 2},
        "targeting": {"driver_ok": True, "effect_registered": True},
    }])
    assert staged["next_action"]["target_text"] == "Submit for approval"


def test_denied_send_reseed_uses_a_unique_valid_identity():
    story = {"id": "US-009", "steps": [
        "Create or seed an enquiry with a follow-up draft.",
        "Submit the draft for approval.",
        "Attempt to perform the send action without approval, then approve or reject."
    ]}
    coverage = [{"aspect": "Attempt to perform the send action without approval", "covered": False}]
    fields = [
        {"tag": "input", "type": "text", "text": "Name", "required": "true", "formIndex": 1},
        {"tag": "input", "type": "text", "text": "Postcode", "required": "true", "formIndex": 1},
        {"tag": "input", "type": "text", "text": "Dog name", "required": "true", "formIndex": 1},
        {"tag": "input", "type": "number", "text": "Dog age", "required": "true", "formIndex": 1},
        {"tag": "input", "type": "checkbox",
         "text": "You may retain these details to respond to this enquiry.",
         "required": "true", "formIndex": 1},
        {"tag": "input", "type": "email", "text": "Email", "formIndex": 1},
        {"tag": "button", "type": "submit", "text": "Send enquiry", "formIndex": 1},
    ]

    decision = qa_explorer._pending_denied_send_setup_decision(
        story, {"elements": fields}, coverage, records=[{
            "action": {"_qa_denied_send_reseed": True},
        }])
    action = decision["next_action"]
    values = {nested.get("target_text"): nested.get("value")
              for case in action["cases"] for nested in case["actions"]}
    assert action["_qa_denied_send_reseed_nonce"] == 2
    assert values["Name"] == "Denied send QA 2"
    assert values["Email"] == "qa.denied.2@example.invalid"
