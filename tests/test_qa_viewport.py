import json
import os
import subprocess
import time
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "qa"))


def test_explorer_routes_only_bounded_integer_viewports_without_a_live_browser():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True, **payload}

    result = bridge.act({"cmd": "viewport", "value": {"width": 390, "height": 844}})
    assert result["ok"] is True
    assert sent == [{"cmd": "viewport", "width": 390, "height": 844}]
    assert bridge.act({"cmd": "viewport", "value": {"width": 319, "height": 844}})["ok"] is False
    assert bridge.act({"cmd": "viewport", "value": {"width": "wide", "height": 844}})["ok"] is False
    assert len(sent) == 1


def test_duplicate_exact_labels_honor_only_an_equally_matching_observed_idx_tiebreak():
    import qa_explorer

    elements = [
        {"idx": 31, "tag": "input", "type": "text", "role": "textbox",
         "text": "Claim reference", "name": "evidenceClaimId", "id": "staff-evidence-claim"},
        {"idx": 40, "tag": "input", "type": "text", "role": "textbox",
         "text": "Claim reference", "name": "verifyClaimId", "id": "staff-verify-claim"},
        {"idx": 41, "tag": "button", "role": "button", "text": "Verify and make public"},
    ]

    assert qa_explorer._resolve_target(
        {"target_text": "Claim reference", "role": "textbox", "idx": 40}, elements)[0] == 40
    # A stale/unrelated idx cannot override the semantic label+role winner.
    assert qa_explorer._resolve_target(
        {"target_text": "Claim reference", "role": "textbox", "idx": 41}, elements)[0] == 31
    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    prepared, aim = explorer._prepare_action(
        {"cmd": "type", "target_text": "Claim reference", "role": "textbox",
         "idx": 40, "value": "claim_us010noevide"}, elements)
    assert prepared["idx"] == 40
    assert aim["resolved_idx"] == 40

    rendered = qa_explorer._fmt_elements(elements)
    assert 'LABEL="Claim reference" NAME="verifyClaimId" ID="staff-verify-claim"' in rendered


def test_missing_semantic_label_never_falls_back_to_an_adjacent_stale_index():
    import qa_explorer

    elements = [
        {"idx": 0, "tag": "button", "role": "button", "text": "Load US-010 claims"},
        {"idx": 1, "tag": "button", "role": "button", "text": "Load US-011 CEO risk"},
    ]
    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    prepared, aim = explorer._prepare_action(
        {"cmd": "click", "target_text": "Load US-011 claims", "role": "button", "idx": 0},
        elements,
    )

    assert "idx" not in prepared
    assert "selector" not in prepared
    assert aim["intended"] == "Load US-011 claims"
    assert aim["resolved_idx"] is None
    assert aim["score"] == 0

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {
        "ok": True, "clicked": False, "matched": None,
    }
    result = bridge.act({
        "cmd": "click", "target_text": "Load US-011 claims", "role": "button", "idx": 0,
    })
    assert result["clicked"] is False
    assert sent == [{"cmd": "clickByText", "text": "Load US-011 claims", "role": "button"}]


def test_explorer_routes_named_long_page_scrolls_to_exact_document_landmarks():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True, **payload}

    result = bridge.act({"cmd": "scroll", "target_text": "Risk signals"})

    assert result["ok"] is True
    assert sent == [{"cmd": "scrollToText", "text": "Risk signals"}]
    rendered = qa_explorer._fmt_state({
        "documentLandmarks": [{"label": "Risk signals", "role": "h2", "y": 1730}],
    })
    assert "DOCUMENT_LANDMARKS" in rendered
    assert '"label": "Risk signals"' in rendered
    source = (ROOT / "scripts" / "qa" / "qa_explorer.py").read_text()
    assert "Do not oscillate through repeated" in source


def test_scenario_matrix_executes_and_captures_each_nested_form_case():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    submitted = []
    sent = []

    def state(include_accessibility=False):
        return {
            "ok": True, "url": "http://app", "title": "App",
            "statusText": f"blocked case {len(submitted)}" if submitted else "ready",
            "viewportText": "Public body Run checks and publish",
            "console_errors": [], "recent_requests": [],
            "elements": [
                {"idx": 4, "tag": "textarea", "role": "textbox", "text": "Public body", "value": ""},
                {"idx": 5, "tag": "button", "role": "button", "text": "Run checks and publish"},
            ],
        }

    def send(payload):
        sent.append(payload)
        if payload["cmd"] == "clickByText":
            submitted.append(payload["text"])
            return {"ok": True, "clicked": True, "matched": payload["text"]}
        return {"ok": True, **payload}

    bridge.state = state
    bridge._send = send
    result = bridge.act({"cmd": "scenario_matrix", "cases": [
        {"name": "email", "actions": [
            {"cmd": "type", "target_text": "Public body", "role": "textbox",
             "value": "contact@example.test"},
            {"cmd": "click", "target_text": "Run checks and publish", "role": "button"},
        ]},
        {"name": "token", "actions": [
            {"cmd": "type", "target_text": "Public body", "role": "textbox",
             "value": "sk_live_qa_sentinel"},
            {"cmd": "click", "target_text": "Run checks and publish", "role": "button"},
        ]},
    ]})

    assert result["ok"] is True
    assert result["completed_cases"] == result["total_cases"] == 2
    assert result["action_count"] == 4
    assert [case["name"] for case in result["cases"]] == ["email", "token"]
    assert all(len(case["actions"]) == 2 for case in result["cases"])
    assert all(case["actions"][-1]["after"]["statusText"].startswith("blocked case")
               for case in result["cases"])
    assert [item["cmd"] for item in sent].count("fill") == 2
    assert [item["cmd"] for item in sent].count("clickByText") == 2
    assert qa_explorer._effect_registered({}, {}, result) is True


def test_paced_type_routes_to_trusted_human_typing_without_slowing_plain_fill():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True, **payload}

    bridge.act({"cmd": "type", "idx": 2, "value": "Ada", "pace_ms": 35})
    bridge.act({"cmd": "type", "idx": 3, "value": "ordinary setup"})

    assert sent == [
        {"cmd": "humanType", "idx": 2, "selector": None, "value": "Ada", "pace_ms": 35},
        {"cmd": "fill", "idx": 3, "selector": None, "value": "ordinary setup"},
    ]

    sent.clear()
    bridge.act({"cmd": "press", "idx": 4, "value": "ArrowDown"})
    assert sent == [{"cmd": "press", "idx": 4, "selector": None, "key": "ArrowDown"}]

    sent.clear()
    bridge.act({"cmd": "paste", "idx": 5, "value": "whole-value input"})
    assert sent == [{"cmd": "paste", "idx": 5, "selector": None,
                     "value": "whole-value input"}]


def test_story_authored_validation_boundaries_batch_without_model_planning():
    import qa_explorer

    aspects = [
        "Test an invalid or past start date; reject it and persist no side effect.",
        "Test a dog age below 0; reject it and persist no side effect.",
        "Test a dog age above 30; reject it and persist no side effect.",
        "Test near-limit overlong text; reject it and persist no side effect.",
        "Test mismatched email and phone preferred-contact data; reject it and persist no side effect.",
    ]
    coverage = [{"aspect": aspect, "covered": False, "atomic_kind": "validation_case"}
                for aspect in aspects]
    state = {"elements": [
        {"idx": 1, "tag": "input", "type": "text", "associatedLabel": "Name",
         "maxLength": 80, "formIndex": 0},
        {"idx": 2, "tag": "input", "type": "email", "associatedLabel": "Email",
         "maxLength": 120, "formIndex": 0},
        {"idx": 3, "tag": "input", "type": "tel", "associatedLabel": "Phone",
         "maxLength": 32, "formIndex": 0},
        {"idx": 4, "tag": "select", "associatedLabel": "Preferred contact",
         "options": "Either | Email | Phone", "formIndex": 0},
        {"idx": 5, "tag": "input", "type": "text", "associatedLabel": "Postcode",
         "maxLength": 16, "formIndex": 0},
        {"idx": 6, "tag": "input", "type": "date", "associatedLabel": "Start date",
         "formIndex": 0},
        {"idx": 7, "tag": "input", "type": "text", "associatedLabel": "Dog name",
         "maxLength": 80, "formIndex": 0},
        {"idx": 8, "tag": "input", "type": "number", "associatedLabel": "Dog age",
         "min": "0", "max": "30", "formIndex": 0},
        {"idx": 9, "tag": "textarea", "associatedLabel": "Anything staff should consider?",
         "maxLength": 600, "formIndex": 0},
        {"idx": 10, "tag": "button", "type": "submit", "text": "Send enquiry",
         "disabled": "true", "formIndex": 0},
    ]}

    decision = qa_explorer._pending_validation_matrix_decision(
        {"id": "US-004", "steps": aspects}, state, coverage)

    assert decision["covers"] == aspects
    action = decision["next_action"]
    assert action["cmd"] == "scenario_matrix"
    assert [case["name"] for case in action["cases"]] == [
        "past start date", "dog age below lower bound", "dog age above upper bound",
        "overlong text at every declared maximum (part 1)",
        "overlong text at every declared maximum (part 2)",
        "preferred-contact mismatch"]
    assert sum(len(case["actions"]) for case in action["cases"]) <= 48
    assert all(case["actions"][-2]["cmd"] == "press"
               and case["actions"][-2]["value"] == "Enter"
               and case["actions"][-1]["cmd"] == "click"
               for case in action["cases"])
    assert all(case["actions"][0]["cmd"] == "paste" for case in action["cases"])
    overlong = action["cases"][3]["actions"][:-3] + action["cases"][4]["actions"][:-3]
    assert [item["target_text"] for item in overlong] == [
        "Name", "Email", "Phone", "Postcode", "Dog name", "Anything staff should consider?"]

    targeting = {
        "driver_ok": True,
        "scenario_matrix_submit_disabled": True,
        "empty_required_fields_before": ["Name"],
    }
    assert qa_explorer._mechanical_incomplete_submit_verdict(
        {"id": "US-004", "steps": aspects}, action, targeting) is None
    assert qa_explorer._mechanical_pending_transition_verdict(
        action, {"driver_ok": True, "effect_registered": True},
        {"statusText": "Publishing and claim workflow ready."}) is None


def test_explicit_multi_field_story_clause_batches_browser_receipts_once():
    import qa_explorer

    story = {
        "id": "US-001",
        "steps": [
            "Open the app.",
            "Type a realistic name, email, postcode, weekly walking frequency, and dog details "
            "at human per-keystroke pace.",
            "Check consent and submit.",
        ],
        "expected_outcome": "The completed enquiry retains all realistic values.",
    }
    aspect = story["steps"][1]
    state = {"elements": [
        {"idx": 1, "tag": "input", "type": "text", "associatedLabel": "Name",
         "value": "", "formIndex": 0},
        {"idx": 2, "tag": "input", "type": "email", "associatedLabel": "Email",
         "value": "", "formIndex": 0},
        {"idx": 3, "tag": "input", "type": "text", "associatedLabel": "Postcode",
         "value": "", "formIndex": 0},
        {"idx": 4, "tag": "select", "associatedLabel": "Walking frequency",
         "options": "Choose | Weekly | One-off", "value": "", "formIndex": 0},
        {"idx": 5, "tag": "input", "type": "text", "associatedLabel": "Dog name",
         "value": "", "formIndex": 0},
        {"idx": 6, "tag": "input", "type": "number", "associatedLabel": "Dog age",
         "value": "", "formIndex": 0},
        {"idx": 7, "tag": "input", "type": "checkbox", "associatedLabel": "Retention consent",
         "checked": "false", "formIndex": 0},
        {"idx": 8, "tag": "button", "type": "submit", "text": "Send enquiry",
         "formIndex": 0},
    ]}

    decision = qa_explorer._pending_explicit_form_sequence_decision(
        story, state, [{"aspect": aspect, "covered": False}])

    assert decision["mechanical_setup"] is False
    assert decision["covers"] == [aspect]
    action = decision["next_action"]
    assert action["cmd"] == "scenario_matrix"
    nested = action["cases"][0]["actions"]
    assert [item["target_text"] for item in nested] == [
        "Name", "Email", "Postcode", "Walking frequency", "Dog name", "Dog age"]
    assert [item["value"] for item in nested] == [
        "Ada Walker", "qa.person@example.invalid", "SW1A 1AA", "Weekly", "Buddy", "4"]
    assert all(item.get("pace_ms") == 35 for item in nested if item["role"] == "textbox")
    assert "Retention consent" not in [item["target_text"] for item in nested]

    invalid_story = {**story, "steps": ["Type invalid input and verify form validation messages."]}
    assert qa_explorer._pending_explicit_form_sequence_decision(
        invalid_story, state,
        [{"aspect": invalid_story["steps"][0], "covered": False}]) is None


def test_combobox_targets_its_associated_label_or_an_exact_visible_option():
    import qa_explorer

    elements = [
        {"idx": 2, "tag": "label", "text": "Agent"},
        {"idx": 3, "tag": "select", "text": "Success Timeout Partial failure",
         "associatedLabel": "Agent", "options": "Success | Timeout | Partial failure"},
    ]

    assert qa_explorer._resolve_target(
        {"target_text": "Agent", "role": "combobox"}, elements) == (3, "Agent", 3)
    assert qa_explorer._resolve_target(
        {"target_text": "Timeout", "role": "combobox"}, elements) == (3, "Timeout", 3)
    rendered = qa_explorer._fmt_elements(elements)
    assert 'select LABEL="Agent" OPTIONS="Success | Timeout | Partial failure"' in rendered


def test_scenario_matrix_fails_closed_on_missing_nested_control_or_oversized_batch():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    bridge.state = lambda include_accessibility=False: {
        "ok": True, "url": "http://app", "elements": [], "console_errors": []}
    bridge._send = lambda payload: {"ok": True, **payload}

    missing = bridge.act({"cmd": "scenario_matrix", "cases": [{"name": "missing", "actions": [
        {"cmd": "type", "target_text": "Absent field", "value": "value"}]}]})
    assert missing["ok"] is False
    assert missing["completed_cases"] == 0
    assert "control not found" in missing["error"]
    oversized = bridge.act({"cmd": "scenario_matrix", "cases": [
        {"name": str(index), "actions": [{"cmd": "wait", "value": "0"}]}
        for index in range(13)]})
    assert oversized["ok"] is False


def test_focused_transient_submit_uses_one_browser_owned_timed_transition():
    import qa_explorer

    story = {
        "id": "US-006", "category": "focused-regression",
        "focused_finding": {"transient_recheck": True},
        "goal": "Submit the fresh enquiry and verify its corrected pending state remains stable for at least 10 seconds.",
        "expected_outcome": "The pending state lasts 10 seconds and prevents duplicate submission.",
    }
    coverage = [
        {"aspect": "Story step 2.1: Recreate one fresh, distinct triggering action.", "covered": False},
        {"aspect": "Story step 3.1: Verify transient pending safeguards and dwell 10 seconds.", "covered": False},
        {"aspect": "Story step 4.1: Verify the corrected outcome and no duplicate side effect.", "covered": False},
    ]
    state = {"elements": [
        {"idx": 7, "tag": "button", "type": "submit", "text": "Send enquiry",
         "disabled": False, "formValid": "true"},
    ]}

    decision = qa_explorer._pending_timed_transition_decision(story, state, coverage)

    assert decision["next_action"] == {
        "cmd": "timed_transition", "target_text": "Send enquiry", "role": "button",
        "idx": 7, "duration_s": 10.0, "completion_grace_ms": 5000,
        "require_full_duration": True,
    }
    assert decision["wait_for"] is None
    assert "duplicate pointer and Enter attempts" in decision["expected"]
    assert qa_explorer._pending_timed_transition_decision(
        {**story, "focused_finding": {}}, state, coverage) is None
    assert qa_explorer._pending_timed_transition_decision(
        story, {"elements": [{**state["elements"][0], "disabled": True}]}, coverage) is None

    restored = {"elements": [
        {"idx": 3, "tag": "select", "text": "Success Timeout Partial failure", "value": "success"},
        {"idx": 29, "tag": "button", "type": "submit", "text": "Send enquiry",
         "disabled": True, "formValid": "false"},
        {"idx": 55, "tag": "button", "type": "submit", "text": "Save draft",
         "formValid": "true"},
    ]}
    ordered = [
        {"aspect": "Story step 1.1: Inspect restored finding-time state.", "covered": False},
        *coverage,
    ]
    assert qa_explorer._pending_timed_transition_decision(story, restored, ordered) is None
    restored["elements"][0]["value"] = "timeout"
    assert qa_explorer._pending_timed_transition_decision(
        story, restored, [{**ordered[0], "covered": True}, *coverage]) is None, (
        "an unrelated valid staff form must never become the focused public transient trigger")


def test_release_story_timed_pending_contract_uses_atomic_transition_without_focused_marker():
    import qa_explorer

    story = {
        "id": "US-006", "category": "latency",
        "steps": [
            "Set the Agent scenario selector to Timeout.",
            "Fill the public enquiry form and submit.",
            "Dwell on the pending state for at least 10 seconds before completion.",
            "Switch to Partial Failure and repeat the same timed submission safeguards.",
        ],
        "expected_outcome": ("The Send enquiry button is disabled while pending, duplicate submission is "
                             "prevented, and confirmation appears after the timeout."),
    }
    coverage = [
        {"aspect": "Set the Agent scenario selector to Timeout.", "covered": True},
        {"aspect": "Submit once and verify the pending safeguards.", "covered": False},
        {"aspect": "Dwell on the pending state for at least 10 seconds.", "covered": False},
        {"aspect": "Verify confirmation and no duplicate side effect.", "covered": False},
        {"aspect": "Switch to Partial Failure and repeat the submission.", "covered": False},
    ]
    state = {"elements": [
        {"idx": 3, "tag": "select", "text": "Success Timeout", "value": "timeout"},
        {"idx": 29, "tag": "button", "type": "submit", "text": "Send enquiry",
         "disabled": False, "formValid": "true"},
    ]}

    decision = qa_explorer._pending_timed_transition_decision(story, state, coverage)

    assert decision["next_action"]["cmd"] == "timed_transition"
    assert decision["next_action"]["idx"] == 29
    assert decision["next_action"]["duration_s"] == 10.0
    assert decision["next_action"]["require_full_duration"] is True
    assert decision["mechanical_timed_transition"] is True

    state["elements"][0].update({"associatedLabel": "Agent", "text": "Agent",
                                  "options": "Success | Timeout | Partial failure",
                                  "value": "success"})
    state["elements"][1].update({"disabled": True, "formValid": "false"})
    setup = qa_explorer._pending_timed_transition_decision(story, state, coverage)
    assert setup["next_action"] == {"cmd": "type", "idx": 3, "value": "Timeout"}
    assert setup["mechanical_setup"] is True

    for item in coverage[1:4]:
        item["covered"] = True
    state["elements"][0]["value"] = "timeout"
    repeat = qa_explorer._pending_timed_transition_decision(story, state, coverage)
    assert repeat["next_action"] == {"cmd": "type", "idx": 3, "value": "Partial failure"}

    state["elements"][0]["value"] = "partial_failure"
    state["elements"][1].update({"disabled": False, "formValid": "true"})
    repeat_submit = qa_explorer._pending_timed_transition_decision(story, state, coverage)
    assert repeat_submit["next_action"] == {
        "cmd": "timed_transition", "target_text": "Send enquiry", "role": "button",
        "idx": 29, "duration_s": 0.25, "completion_grace_ms": 1000,
        "require_full_duration": False,
    }
    assert "degraded/recovery outcome" in repeat_submit["expected"]


def test_timed_story_fills_only_required_controls_from_its_named_submit_form_mechanically():
    import qa_explorer

    story = {
        "id": "US-006", "category": "latency",
        "steps": ["Submit the enquiry and dwell on pending for at least 10 seconds."],
    }
    coverage = [{"aspect": "Dwell on pending for at least 10 seconds.", "covered": False}]
    state = {"elements": [
        {"idx": 2, "tag": "input", "type": "text", "associatedLabel": "Staff reference",
         "required": "true", "value": "", "formIndex": 0},
        {"idx": 6, "tag": "input", "type": "text", "associatedLabel": "Name",
         "name": "name", "required": "true", "value": "", "formIndex": 1},
        {"idx": 7, "tag": "input", "type": "checkbox", "associatedLabel": "Retention consent",
         "name": "consent", "required": "true", "checked": "false", "formIndex": 1},
        {"idx": 8, "tag": "button", "type": "submit", "text": "Send enquiry",
         "disabled": "true", "formValid": "false", "formIndex": 1},
    ]}

    first = qa_explorer._pending_timed_transition_decision(story, state, coverage)
    assert first["next_action"] == {"cmd": "type", "idx": 6, "value": "Name QA test"}
    assert first["mechanical_setup"] is True
    state["elements"][1]["value"] = "Name QA test"
    second = qa_explorer._pending_timed_transition_decision(story, state, coverage)
    assert second["next_action"] == {"cmd": "click", "idx": 7}


def test_mechanical_prerequisite_clicks_skip_semantic_judge_but_only_credit_receipt_proof():
    import qa_explorer

    decision = {"mechanical_setup": True, "covers": ["Runtime diagnostics show degraded state."]}
    checkbox = qa_explorer._mechanical_routine_verdict(
        {"cmd": "click", "idx": 7},
        {"driver_ok": True, "label_matched": True, "effect_registered": True,
         "before_control_checked": False, "after_control_checked": True},
        {}, [], decision=decision)
    assert checkbox["verdict"] == "pass"
    assert checkbox["demonstrated"] == []
    assert qa_explorer._routine_action_needs_semantic_judge(
        {"cmd": "click"}, decision) is False

    not_checked = qa_explorer._mechanical_routine_verdict(
        {"cmd": "click", "idx": 7},
        {"driver_ok": True, "label_matched": True, "effect_registered": True,
         "before_control_checked": False, "after_control_checked": False},
        {}, [], decision=decision)
    assert not_checked is None

    proven = ["Runtime diagnostics show degraded state."]
    drain = qa_explorer._mechanical_routine_verdict(
        {"cmd": "click", "target_text": "Drain queue"},
        {"driver_ok": True, "label_matched": True, "effect_registered": True},
        {}, proven, decision=decision)
    assert drain["verdict"] == "pass"
    assert drain["demonstrated"] == proven


def test_repeat_timed_transition_can_complete_early_but_cannot_credit_timeout_dwell():
    import qa_explorer

    targeting = {
        "action_kind": "timed_transition", "driver_ok": True,
        "empty_required_fields_before": [],
        "timed_transition": {
            "required_duration_ms": 250, "full_duration_required": False,
            "pending_seen": True, "completion_observed": True,
            "transition_duration_ms": 18, "completed_before_required_duration": True,
            "stable_through_required_boundary": False,
            "duplicate_attempts": ["trusted-pointer", "trusted-keyboard-enter"],
            "submit_event_count": 1,
        },
    }
    repeat = "Switch to Partial Failure and repeat submission with the same pending safeguards."
    dwell = "Dwell on the pending state for at least 10 seconds without a persistent focus trap."
    after = {"url": "http://app", "bodyText": "Enquiry received. Send another enquiry. Queue degraded."}
    assert qa_explorer._grounded_demonstrated(
        [repeat, dwell], targeting, {"url": "http://app"}, after,
        require_mechanical=True) == [repeat]


def test_idempotent_viewport_command_credits_only_the_setup_boundary():
    import qa_explorer

    setup = ("Story step 3.1: Reapply the reported finding viewport dimensions once so the current "
             "revision reflows the restored state at the exact tested width and height; do not activate "
             "any sibling control.")
    semantic = ("Story step 4.1: Verify accessible names, visible focus, reading order, and no horizontal "
                "scrolling at the mobile viewport.")
    targeting = {
        "action_kind": "viewport", "action_value": {"width": 375, "height": 844},
        "driver_ok": True, "control_action": False, "effect_registered": False,
    }
    before = {"viewport": {"width": 375, "height": 844}}
    after = {"viewport": {"width": 375, "height": 844}}

    assert qa_explorer._grounded_demonstrated(
        [setup, semantic], targeting, before, after, require_mechanical=True) == [setup]
    assert qa_explorer._grounded_demonstrated(
        [setup], targeting, before, {"viewport": {"width": 390, "height": 844}},
        require_mechanical=True) == []


def test_restored_focused_inspection_mechanically_credits_both_read_only_setup_steps():
    import qa_explorer

    inspect = ("Story step 1.1: Inspect the restored finding-time state; do not load or seed an "
               "unrelated story fixture.")
    confirm = ("Story step 2.1: Confirm the restored state contains the finding's exact triggering "
               "input and prerequisite state; do not create or submit a duplicate record.")
    semantic = "Story step 4.1: Verify accessible names, focus order, and mobile reflow."
    ledger = [{"aspect": aspect, "covered": False, "explicit": True}
              for aspect in (inspect, confirm, semantic)]
    targeting = {
        "action_kind": "inspect_surfaces", "driver_ok": True,
        "landmark_dwell_summary": {
            "targets": ["Public enquiry", "Staff operating console"],
            "all_targets_matched": True, "all_stable": True,
        },
    }
    after = {"url": "http://app", "elements": [{"idx": 1, "label": "Agent"}]}

    proven = qa_explorer._mechanically_proven_unresolved(ledger, targeting, after, after)

    assert proven == [inspect, confirm]
    verdict = qa_explorer._mechanical_routine_verdict(
        {"cmd": "inspect_surfaces",
         "targets": ["Public enquiry", "Staff operating console"]},
        targeting, after, proven)
    assert verdict["demonstrated"] == [inspect, confirm]
    assert verdict["_raw"]["engine"] == "mechanical-browser-proof"

    unmatched = {**targeting, "landmark_dwell_summary": {
        **targeting["landmark_dwell_summary"], "all_targets_matched": False}}
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, unmatched, after, after) == []


def test_landmark_dwell_judge_compacts_repeated_page_state_without_losing_surface_facts():
    import qa_explorer

    controls = [{"id": f"field-{index}", "value": "x" * 160} for index in range(120)]
    observation = {
        "target": "Unresolved blockers", "requested_ms": 250, "elapsed_ms": 271,
        "stable": True,
        "scroll": {"scrolled": True, "matched": "Unresolved blockers", "y": 3400},
        "before": {"url": "http://app/", "active": {"tag": "button", "label": "Acknowledge"},
                   "horizontalOverflow": False, "controls": controls,
                   "viewportText": "before " * 3000},
        "after": {"url": "http://app/", "active": {"tag": "button", "label": "Acknowledge"},
                  "horizontalOverflow": False, "controls": controls,
                  "statusText": "One blocker remains open", "viewportText": "after " * 3000,
                  "scopeSummary": {"target": "Unresolved blockers", "text_chars": 2400,
                                   "headings": ["Unresolved blockers"],
                                   "definition_pairs": [{"label": "Status", "value": "open"}],
                                   "control_count": 2, "json_block_count": 0,
                                   "email_address_count": 0, "phone_number_count": 0,
                                   "raw_metadata_labels": [], "text_prefix": "Open blocker",
                                   "text_suffix": "Acknowledge"}},
    }
    raw = {"targets": ["Unresolved blockers"], "duration_ms_each": 250,
           "elapsed_ms": 271, "observations": [observation]}

    compact = qa_explorer._compact_landmark_dwell_receipt(raw)

    assert compact["observations"][0]["stable"] is True
    assert compact["observations"][0]["scope"]["email_address_count"] == 0
    assert compact["observations"][0]["scope"]["definition_pairs"] == [
        {"label": "Status", "value": "open"}]
    assert compact["observations"][0]["control_changes"] == []
    assert len(json.dumps(compact)) < 5000


def test_traversal_cannot_invent_an_activation_key_absent_from_driver_receipt():
    import qa_explorer

    tab_only = {"traversal": {"driver_ok": True, "key": "Tab", "direction": "forward"}}

    assert qa_explorer._unreceipted_traversal_key_false_positive(
        tab_only,
        "The Agent selector did not change from Success to Timeout when ArrowDown was pressed.",
    ) is True
    assert qa_explorer._unreceipted_traversal_key_false_positive(
        tab_only,
        "The focused acknowledgement button did not activate when Enter was pressed.",
    ) is True
    assert qa_explorer._unreceipted_traversal_key_false_positive(
        tab_only,
        "The Start date control lost its visible focus indicator while Tab traversed it.",
    ) is False

    enter_receipted = {"traversal": {
        "driver_ok": True,
        "key": "Tab",
        "keyboard_receipts": [{"type": "keydown", "key": "Enter"}],
    }}
    assert qa_explorer._unreceipted_traversal_key_false_positive(
        enter_receipted,
        "The focused acknowledgement button did not activate when Enter was pressed.",
    ) is False


def test_queued_timeout_requires_drain_before_failure_judgment_and_followup_diagnostic_inspection():
    import qa_explorer

    aspect = ("Observe storage success confirmation and Send another enquiry while staff/CEO diagnostics "
              "expose the failed, retry, or degraded job state despite async queue failure.")
    story = {"id": "US-006", "steps": ["Set the Agent scenario selector to Timeout."],
             "expected": aspect}
    state = {
        "bodyText": "Enquiry received. Send another enquiry. job_1 queued 0 attempts",
        "elements": [{"idx": 4, "tag": "button", "text": "Drain queue"}],
    }
    decision = qa_explorer._pending_queue_drain_decision(
        story, state, [{"aspect": aspect, "covered": False}])
    assert decision["next_action"] == {
        "cmd": "click", "target_text": "Drain queue", "role": "button", "idx": 4}
    assert decision["covers"] == [aspect]
    assert qa_explorer._live_queue_has_queued({
        "bodyText": "agentJobsQueued 1 Governance audit metadata status queued",
    })
    settled_with_history = {
        "bodyText": ("agentJobsQueued 0 Governance audit history "
                     "agent.enqueued metadata status queued job_old 0 attempts"),
        "elements": state["elements"],
    }
    assert not qa_explorer._live_queue_has_queued(settled_with_history)
    assert qa_explorer._pending_queue_drain_decision(
        story, settled_with_history, [{"aspect": aspect, "covered": False}]) is None

    passive_privacy_story = {
        "id": "US-011",
        "steps": ["Seed queued, running, retrying, failed, and dead-lettered jobs.",
                  "Inspect the CEO diagnostics."],
        "expected_outcome": "Metrics are accurate without exposing raw metadata.",
    }
    live_seeded_dashboard = {
        "bodyText": "agentJobsQueued 1 agentJobsFailed 2 raw metadata remains private",
        "elements": state["elements"],
    }
    assert qa_explorer._pending_queue_drain_decision(
        passive_privacy_story, live_seeded_dashboard,
        [{"aspect": "Inspect failed/retry/degraded diagnostics without raw metadata.",
          "covered": False}]) is None

    focused_safety_story = {
        "id": "US-011", "category": "focused-regression",
        "goal": "Exercise the approval-required scenario and the reported Drain queue action.",
    }
    focused_ledger = [{
        "aspect": ("Exercise the corrected user control; do not repeat a historical observation, scroll, "
                   "or diagnostic action."),
        "covered": False,
    }]
    assert qa_explorer._pending_queue_drain_decision(
        focused_safety_story, live_seeded_dashboard, focused_ledger) is None

    focused_runtime_ledger = [{
        "aspect": "Verify the queued runtime job exposes failed/retry diagnostics after the exact action.",
        "covered": False,
    }]
    scenario_state = {
        "bodyText": "agentJobsQueued 1",
        "elements": [
            {"idx": 3, "tag": "select", "label": "Agent scenario", "value": "success",
             "options": ["Success", "Approval required"]},
            {"idx": 4, "tag": "button", "text": "Drain queue"},
        ],
    }
    select_first = qa_explorer._pending_queue_drain_decision(
        focused_safety_story, scenario_state, focused_runtime_ledger)
    assert select_first["next_action"] == {"cmd": "type", "idx": 3,
                                            "value": "Approval required"}
    submit_targeting = {"action_kind": "timed_transition", "driver_ok": True}
    assert qa_explorer._queued_failure_not_triggered_false_positive(
        story, submit_targeting,
        "No honest queue warning or failed/retry/degraded diagnostics appeared.", state)

    drain_targeting = {"action_kind": "click", "driver_ok": True,
                       "intended": "Drain queue", "targeted_label": "Drain queue"}
    after = {"bodyText": ("Enquiry received. Send another enquiry. "
                          "lead_reviewer retrying runtime_timeout 1 of 3 attempts")}
    # The drain is the causal transition, but this compound contract also names both staff and CEO
    # diagnostics. One post-click page snapshot cannot certify both surfaces; the next sealed multi-surface
    # inspection owns that evidence boundary.
    assert qa_explorer._grounded_demonstrated(
        [aspect], drain_targeting, state, after, require_mechanical=True) == []
    partial_after = {"bodyText": ("Enquiry received. Send another enquiry. "
                                  "lead_reviewer succeeded; result status partial; diagnostic finding")}
    assert qa_explorer._grounded_demonstrated(
        [aspect], drain_targeting, state, partial_after, require_mechanical=True) == []

    optional_timing = {
        "driver_ok": True,
        "timed_transition": {"full_duration_required": False, "pending_seen": True,
                             "completion_observed": True,
                             "completed_before_required_duration": True},
    }
    assert qa_explorer._optional_timed_duration_false_positive(
        optional_timing, "The transition completed early before the required 250 ms boundary.")
    optional_timing["timed_transition"]["full_duration_required"] = True
    assert not qa_explorer._optional_timed_duration_false_positive(
        optional_timing, "The transition completed early before the required 10 second boundary.")


def test_timed_transition_routes_duration_and_is_a_batch_judge_receipt():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {
        "ok": True, "timedTransition": True,
        "transition": {"required_duration_ms": payload["duration_ms"],
                       "pending_seen": True, "completion_observed": True,
                       "transition_duration_ms": 10005,
                       "completed_before_required_duration": False,
                       "stable_through_required_boundary": True},
    }

    result = bridge.act({"cmd": "timed_transition", "idx": 4, "duration_s": 10})

    assert sent == [{"cmd": "timedTransition", "idx": 4, "selector": None,
                     "duration_ms": 10000, "completion_grace_ms": 5000,
                     "pending_text": ""}]
    assert qa_explorer._effect_registered({}, {}, result) is True
    targeting = {
        "action_kind": "timed_transition", "driver_ok": True,
        "empty_required_fields_before": [],
        "timed_transition": {
            "required_duration_ms": 10000, "pending_seen": True,
            "completion_observed": True, "transition_duration_ms": 10005,
            "completed_before_required_duration": False,
            "stable_through_required_boundary": True,
            "duplicate_attempts": ["trusted-pointer", "trusted-keyboard-enter"],
            "submit_event_count": 1,
        },
    }
    aspects = [
        "Submit once and verify pending duplicate-submission safeguards.",
        "Dwell on the busy state for at least 10 seconds without a persistent focus trap or unexpected navigation.",
        "Verify storage success produces confirmation and Send another enquiry.",
        "Verify an honest warning and failed, retry, or degraded diagnostics.",
    ]
    grounded = qa_explorer._grounded_demonstrated(
        aspects, targeting, {}, {"bodyText": "Enquiry received. Send another enquiry."},
        require_mechanical=True)
    assert grounded == aspects[:3]
    prompt = qa_explorer._batch_evaluate_prompt(
        "vision", {"steps": ["dwell pending 10 seconds"]}, "pending remains stable",
        {"driver_ok": True, "effect_registered": True,
         "timed_transition": result["transition"]}, {}, {},
        untested=["verify transient pending safeguards"])
    assert "timed transient transition" in prompt
    assert '"transition_duration_ms": 10005' in prompt
    assert "A dwell started after completion is not equivalent evidence" in prompt


def test_prior_journey_evidence_keeps_a_complete_distinct_validation_matrix():
    import qa_explorer

    records = []
    for cycle in range(2):
        for name in ("email", "phone", "token", "password", "secret"):
            records.extend([
                {"step": len(records) + 1,
                 "action": {"cmd": "type", "target_text": "Public body", "value": name},
                 "expected": f"body contains {name}", "targeting": {"driver_ok": True},
                 "verdict": {"verdict": "pass"}, "actual": {}},
                {"step": len(records) + 2,
                 "action": {"cmd": "click", "target_text": "Publish"},
                 "expected": f"{name} is blocked", "targeting": {"driver_ok": True},
                 "verdict": {"verdict": "pass"}, "actual": {}},
            ])

    rendered = json.loads(qa_explorer._fmt_prior_journey_evidence(records))

    assert len(rendered) == 10
    assert {item["action"].get("value") for item in rendered if item["action"]["cmd"] == "type"} == {
        "email", "phone", "token", "password", "secret"}
    assert min(item["step"] for item in rendered) > 10, "latest duplicate receipts should win"

    portable = json.loads(qa_explorer._fmt_prior_journey_evidence([{
        "step": 1, "action": "click idx=50", "expected": "token is blocked",
        "actual": "status=content_publish_blocked owner=security", "verdict": "match",
    }]))
    assert portable[0]["portable_actual"] == "status=content_publish_blocked owner=security"


def test_prior_journey_evidence_keeps_inconclusive_effectful_batch_for_cumulative_closure():
    import qa_explorer

    submit = {
        "step": 1,
        "action": {"cmd": "scenario_matrix", "cases": [{"name": "valid submission"}]},
        "expected": "submit then verify all populated panels",
        "targeting": {"driver_ok": True, "effect_registered": True,
                      "scenario_matrix_summary": {"completed_cases": 1, "total_cases": 1}},
        "verdict": {"verdict": "inconclusive", "matches_expected": False},
        "actual": {"statusText": "Enquiry received", "bodyText": "Enquiries total 1"},
    }
    repeated_inspections = [{
        "step": index,
        "action": {"cmd": "inspect_surfaces", "targets": ["Staff", "CEO"]},
        "expected": "all populated panels",
        "targeting": {"driver_ok": True, "effect_registered": True},
        "verdict": {"verdict": "inconclusive", "matches_expected": False},
        "actual": {"statusText": "Enquiry received", "bodyText": "Enquiries total 1"},
    } for index in range(2, 8)]

    rendered = json.loads(qa_explorer._fmt_prior_journey_evidence(
        [submit, *repeated_inspections], limit=4, max_chars=8000))

    assert any(item["action"]["cmd"] == "scenario_matrix" for item in rendered)
    assert any(item["targeting"].get("scenario_matrix_summary") for item in rendered)

    migrated = json.loads(qa_explorer._fmt_prior_journey_evidence([
        {"step": 1, "action": submit["action"], "expected": submit["expected"],
         "actual": "driver_ok=True; effect_registered=True; status='Enquiry received'",
         "verdict": "retry"},
        *repeated_inspections,
    ], limit=4, max_chars=8000))
    assert any(item["action"]["cmd"] == "scenario_matrix" for item in migrated)
    assert any("Enquiry received" in (item.get("portable_actual") or "") for item in migrated)


def test_portable_checkpoint_preserves_compact_batch_targeting(monkeypatch):
    import qa_explorer

    monkeypatch.setattr(qa_explorer.campaign_checkpoint, "compact_evidence_records", lambda rows: rows)
    portable = qa_explorer._portable_checkpoint_evidence([{
        "action": {"cmd": "scenario_matrix"},
        "actual": {"url": "http://app", "statusText": "Enquiry received"},
        "targeting": {"action_kind": "scenario_matrix", "driver_ok": True,
                      "effect_registered": True,
                      "scenario_matrix_summary": {"completed_cases": 1, "total_cases": 1}},
        "verdict": {"verdict": "inconclusive"},
    }])

    assert portable[0]["targeting"] == {
        "action_kind": "scenario_matrix", "effect_registered": True, "driver_ok": True,
        "scenario_matrix_summary": {"completed_cases": 1, "total_cases": 1},
    }


def test_batch_judge_receives_top_level_driver_success_for_scenario_matrix():
    import qa_explorer

    prompt = qa_explorer._batch_evaluate_prompt(
        "vision", {"steps": ["test cases"], "expected_outcome": "all blocked"}, "all blocked",
        {"driver_ok": True, "effect_registered": True,
         "scenario_matrix": {"completed_cases": 2, "total_cases": 2, "cases": []}},
        {}, {}, untested=["exercise cases"])

    assert '"driver_ok": true' in prompt
    assert '"effect_registered": true' in prompt


def test_batch_judge_compacts_every_scenario_case_instead_of_truncating_later_outcomes():
    import qa_explorer

    cases = []
    for index in range(8):
        value = f"case-{index}-sentinel"
        cases.append({"name": f"case-{index}", "actions": [
            {"action": {"cmd": "type", "target_text": "Public body", "value": value},
             "resolved_label": "Public body", "ok": True,
             "before": {"bodyText": "x" * 5000},
             "after": {"activeElement": {"associatedLabel": "Public body", "value": value},
                       "bodyText": "y" * 5000}},
            {"action": {"cmd": "click", "target_text": "Run checks and publish"},
             "resolved_label": "Run checks and publish", "ok": True,
             "before": {"statusText": "ready " + "b" * 4000},
             "after": {"url": "http://app", "statusText": f"blocked {value} " + "s" * 4000,
                       "viewportText": "No governed updates published " + "v" * 4000,
                       "console_errors": []}},
        ]})

    prompt = qa_explorer._batch_evaluate_prompt(
        "vision", {"steps": ["exercise every case"], "expected_outcome": "all blocked"},
        "all blocked", {"driver_ok": True, "effect_registered": True,
                         "scenario_matrix": {"ok": True, "completed_cases": 8,
                                             "total_cases": 8, "action_count": 16,
                                             "cases": cases}},
        {}, {}, untested=["exercise every case"])

    for index in range(8):
        assert f'"name": "case-{index}"' in prompt
        assert f"case-{index}-sentinel" in prompt
    assert '"completed_cases": 8' in prompt and '"total_cases": 8' in prompt
    assert "x" * 1000 not in prompt and "y" * 1000 not in prompt
    assert len(prompt) < 30000


def test_progress_signature_distinguishes_keyboard_focus_without_chasing_artifact_paths():
    import qa_explorer

    base = {
        "url": "http://app/", "viewport": {"width": 375, "height": 800},
        "scrollPosition": {"x": 0, "y": 0}, "viewportText": "Public enquiry",
        "statusText": "Ready", "screenshot": "/tmp/state-1.png",
        "activeElement": {"idx": 1, "tag": "input", "text": "Name"},
    }
    same_evidence_new_artifact = {**base, "screenshot": "/tmp/state-2.png"}
    same_evidence_new_clock = {**base, "statusText": "Ready Updated 2026-08-24T10:20:30.123Z"}
    prior_clock = {**base, "statusText": "Ready Updated 2026-08-24T10:19:29.321Z"}
    next_focus = {**base, "activeElement": {
        "idx": 2, "tag": "input", "text": "Email"}}
    next_status = {**base, "statusText": "Enquiry received"}

    assert qa_explorer._progress_view_signature(base) == \
           qa_explorer._progress_view_signature(same_evidence_new_artifact)
    assert qa_explorer._progress_view_signature(prior_clock) == \
           qa_explorer._progress_view_signature(same_evidence_new_clock)
    assert qa_explorer._progress_view_signature(base) != \
           qa_explorer._progress_view_signature(next_focus)
    assert qa_explorer._progress_view_signature(base) != \
           qa_explorer._progress_view_signature(next_status)


def test_batched_evidence_actions_do_not_mint_repeat_keys_from_their_final_focus_or_scroll():
    import qa_explorer

    forward_a = qa_explorer._repeat_action_policy(
        {"cmd": "traverse", "value": "forward"}, "focus-at-name")
    forward_b = qa_explorer._repeat_action_policy(
        {"cmd": "traverse", "value": "forward"}, "focus-at-postcode")
    backward = qa_explorer._repeat_action_policy(
        {"cmd": "traverse", "value": "backward"}, "focus-at-name")
    dwell_a = qa_explorer._repeat_action_policy(
        {"cmd": "dwell_surfaces", "targets": ["Public form", "CEO view"]}, "scroll-a")
    dwell_b = qa_explorer._repeat_action_policy(
        {"cmd": "dwell_surfaces", "targets": ["Public form", "CEO view"]}, "scroll-b")
    matrix_a = qa_explorer._repeat_action_policy(
        {"cmd": "scenario_matrix", "cases": [{"name": "email", "actions": [
            {"cmd": "type", "target_text": "Public body", "value": "a@example.test"},
            {"cmd": "click", "target_text": "Publish"},
        ]}]}, "focus-a")
    matrix_a_new_view = qa_explorer._repeat_action_policy(
        {"cmd": "scenario_matrix", "cases": [{"name": "email", "actions": [
            {"cmd": "type", "target_text": "Public body", "value": "a@example.test"},
            {"cmd": "click", "target_text": "Publish"},
        ]}]}, "focus-b")
    matrix_b = qa_explorer._repeat_action_policy(
        {"cmd": "scenario_matrix", "cases": [{"name": "token", "actions": [
            {"cmd": "type", "target_text": "Public body", "value": "sk_live_sentinel"},
            {"cmd": "click", "target_text": "Publish"},
        ]}]}, "focus-a")
    ordinary_a = qa_explorer._repeat_action_policy(
        {"cmd": "press", "value": "Tab"}, "focus-at-name")
    ordinary_b = qa_explorer._repeat_action_policy(
        {"cmd": "press", "value": "Tab"}, "focus-at-postcode")
    named_scroll_a = qa_explorer._repeat_action_policy(
        {"cmd": "scroll", "target_text": "CEO command view"}, "scroll-a")
    named_scroll_b = qa_explorer._repeat_action_policy(
        {"cmd": "scroll", "target_text": "CEO command view"}, "scroll-b")
    named_scroll_case_alias = qa_explorer._repeat_action_policy(
        {"cmd": "scroll", "target_text": "  CEO Command View  ", "value": "CEO Command View"},
        "scroll-c")
    named_scroll_after_viewport = qa_explorer._repeat_action_policy(
        {"cmd": "scroll", "target_text": "CEO command view"}, "scroll-b", batch_context=1)
    forward_after_reload = qa_explorer._repeat_action_policy(
        {"cmd": "traverse", "value": "forward"}, "focus-at-name", batch_context=1)
    incremental_scroll_a = qa_explorer._repeat_action_policy(
        {"cmd": "scroll", "value": 600}, "scroll-a")
    incremental_scroll_b = qa_explorer._repeat_action_policy(
        {"cmd": "scroll", "value": 600}, "scroll-b")
    publish_a = qa_explorer._repeat_action_policy(
        {"cmd": "click", "target_text": "Run checks and publish", "role": "button"},
        "one blocker")
    publish_b = qa_explorer._repeat_action_policy(
        {"cmd": "click", "target_text": "Run checks and publish", "role": "button"},
        "two duplicate blockers and a fresh timestamp")

    assert forward_a[0] == forward_b[0] and forward_a[1:] == (1, True)
    assert forward_a[0] != backward[0]
    assert dwell_a[0] == dwell_b[0] and dwell_a[1:] == (1, True)
    assert matrix_a[0] == matrix_a_new_view[0] and matrix_a[1:] == (1, True)
    assert matrix_a[0] != matrix_b[0]
    assert named_scroll_a[0] == named_scroll_b[0] and named_scroll_a[1:] == (1, True)
    assert named_scroll_a[0] == named_scroll_case_alias[0]
    assert named_scroll_a[0] != named_scroll_after_viewport[0]
    assert forward_a[0] != forward_after_reload[0]
    assert incremental_scroll_a[0] != incremental_scroll_b[0]
    assert ordinary_a[0] != ordinary_b[0] and ordinary_a[2] is False
    assert publish_a[0] == publish_b[0] and publish_a[2] is False


def test_resumed_seeded_story_recreates_fixture_baseline_once_before_replay():
    import qa_explorer

    story = {"steps": [
        "Seed enquiries across new, reviewing, and blocked statuses.",
        "Seed queued, succeeded, and dead-lettered jobs.",
        "Open the CEO command view.",
    ]}
    proposal = {"next_action": {"cmd": "scroll", "target_text": "Unresolved blockers"}}

    fenced = qa_explorer._fence_seeded_resume_decision(
        proposal, story, [], "/tmp/resume-state.json", "http://app")
    already_reset = qa_explorer._fence_seeded_resume_decision(
        proposal, story, [{"action": {"cmd": "reset_storage"}, "act_result": {"ok": True}}],
        "/tmp/resume-state.json", "http://app")
    fresh_worker = qa_explorer._fence_seeded_resume_decision(
        proposal, story, [], None, "http://app")
    proven_continuation = qa_explorer._fence_seeded_resume_decision(
        proposal, story, [], "/tmp/resume-state.json", "http://app",
        resume_has_proven_coverage=True)

    assert fenced["next_action"] == {"cmd": "reset_storage", "value": "http://app"}
    assert fenced["covers"] == [] and fenced["done"] is False
    assert already_reset == proposal
    assert fresh_worker == proposal
    assert proven_continuation == proposal


def test_reset_storage_semantic_value_reopens_pinned_target_instead_of_inventing_route():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://127.0.0.1:8816/", autostart=False)
    bridge._send = lambda payload: payload

    assert bridge.act({"cmd": "reset_storage", "value": "empty"}) == {
        "cmd": "resetStorage", "url": "http://127.0.0.1:8816/"}
    assert bridge.act({"cmd": "reset_storage", "value": "fresh"}) == {
        "cmd": "resetStorage", "url": "http://127.0.0.1:8816/"}
    assert bridge.act({"cmd": "reset_storage", "value": "https://invented.test/deep"}) == {
        "cmd": "resetStorage", "url": "http://127.0.0.1:8816/deep"}
    assert bridge.act({"cmd": "reset_storage", "url": "/explicit-route"}) == {
        "cmd": "resetStorage", "url": "http://127.0.0.1:8816/explicit-route"}


def test_resumed_seeded_story_resets_without_paid_navigation_decision():
    import qa_explorer

    class Bridge:
        def state(self):
            return {"url": "http://app", "title": "app", "bodyText": "stale acknowledged fixture",
                    "elements": [], "console_errors": [], "recent_requests": []}

        def act(self, action):
            assert action["cmd"] == "reset_storage"
            return {"ok": True, "reset": True, "effect": True}

    explorer = qa_explorer.Explorer(
        "http://app", "CEO dashboard", autostart=False, resume_state_path="checkpoint.json")
    explorer.bridge = Bridge()
    explorer._ai_coverage_plan = lambda *_args, **_kwargs: [
        {"aspect": "seed and inspect the CEO dashboard", "covered": False}]
    explorer._ai_decide = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("the deterministic seeded-resume fence must run before a paid decision"))
    explorer._checkpoint = lambda *_args, **_kwargs: None

    records = explorer.explore(
        {"title": "CEO dashboard", "steps": ["Seed enquiries", "Open the CEO command view"]},
        max_steps=1)

    assert [record["action"]["cmd"] for record in records] == ["reset_storage"]
    assert explorer.stop_reason == "safety-cap-incomplete"


def test_fresh_story_checkpoint_is_not_mistaken_for_a_resumed_worker():
    import qa_explorer

    story = {"steps": ["Load the exact fixture.", "Exercise the finding."]}
    proposal = {"next_action": {"cmd": "type", "target_text": "Public body"}}
    fresh = qa_explorer.Explorer("http://app", "vision", autostart=False)
    # A normal current-run checkpoint updates this handoff path after the first action.
    fresh.resume_state_path = "/tmp/current-run-checkpoint.json"
    assert fresh._resume_origin_state_path is None
    assert qa_explorer._fence_seeded_resume_decision(
        proposal, story, [], fresh._resume_origin_state_path, "http://app") == proposal

    resumed = qa_explorer.Explorer(
        "http://app", "vision", autostart=False, resume_state_path="/tmp/prior-worker.json")
    resumed.resume_state_path = "/tmp/current-run-checkpoint.json"
    assert resumed._resume_origin_state_path == "/tmp/prior-worker.json"
    fenced = qa_explorer._fence_seeded_resume_decision(
        proposal, story, [], resumed._resume_origin_state_path, "http://app")
    assert fenced["next_action"]["cmd"] == "reset_storage"


def test_browser_bridge_exposes_and_dispatches_viewport_ground_truth():
    source = (ROOT / "scripts" / "qa" / "browser_bridge.js").read_text()
    assert "async viewport(msg)" in source
    assert "this.page.setViewportSize({ width, height })" in source
    assert "case 'viewport':" in source
    assert "viewport = this.page.viewportSize()" in source
    assert "ACCESSIBILITY_REGIONS" in source
    assert "ariaSnapshot" in source
    assert "accessibilityRegions" in source and "accessibilityTree" in source
    assert "accessibilityEvents" in source and "focusVisible" in source and "focusStyle" in source
    assert "async history(direction)" in source
    assert "case 'back':" in source and "case 'forward':" in source
    assert "async reload()" in source and "case 'reload':" in source
    assert "async clickBurst(msg)" in source and "case 'clickBurst':" in source
    assert "intermediateSamples" in source
    assert "Accessibility.getFullAXTree" in source
    assert "Accessibility.nodesUpdated" in source
    assert "accessibilityPlatformEvents" in source


def test_evaluator_dossier_exposes_real_at_events_separately_from_chromium_ax():
    import qa_explorer

    rendered = qa_explorer._fmt_state({
        "actualAssistiveTechnologyAvailable": True,
        "actualAssistiveTechnologyEvents": [
            {"utterance": "Current count 1", "source": "orca-at-spi"}],
        "accessibilityPlatformEvents": [
            {"role": "status", "descendantText": "1", "source": "Accessibility.nodesUpdated"}],
    })
    assert "ACTUAL_ASSISTIVE_TECHNOLOGY_AVAILABLE: True" in rendered
    assert "Current count 1" in rendered and "orca-at-spi" in rendered
    assert "ACCESSIBILITY_PLATFORM_EVENTS" in rendered and "Accessibility.nodesUpdated" in rendered


def test_decision_contract_requires_observed_viewport_changes():
    import qa_explorer

    source = (ROOT / "scripts" / "qa" / "qa_explorer.py").read_text()
    assert 'cmd: "viewport"' in source
    assert "click|touch|pen|burst|timed_transition|type|press|hold|traverse|inspect_surfaces|dwell_surfaces|scenario_matrix|goto|reload|back|forward|reset_storage|viewport|scroll|wait" in source
    assert "Never infer mobile behavior from desktop pixels" in source
    assert 'VIEWPORT: {"width": 390, "height": 844}' in qa_explorer._fmt_state(
        {"viewport": {"width": 390, "height": 844}})


def test_explorer_routes_browser_history_without_rewriting_it():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True, **payload}
    assert bridge.act({"cmd": "back"})["ok"] is True
    assert bridge.act({"cmd": "forward"})["ok"] is True
    assert sent == [{"cmd": "back"}, {"cmd": "forward"}]


def test_explorer_routes_touch_pen_and_held_key_as_native_evidence_commands():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True}

    bridge.act({"cmd": "touch", "idx": 2})
    bridge.act({"cmd": "pen", "selector": "#go"})
    bridge.act({"cmd": "hold", "idx": 2, "value": "Space", "duration_ms": 450})

    assert sent == [
        {"cmd": "pointer", "idx": 2, "selector": None, "pointer_type": "touch"},
        {"cmd": "pointer", "idx": None, "selector": "#go", "pointer_type": "pen"},
        {"cmd": "hold", "idx": 2, "selector": None, "key": "Space", "duration_ms": 450},
    ]


def test_touch_pen_and_hold_coverage_require_matching_trusted_driver_receipts():
    import qa_explorer

    before = {"statusText": "0", "url": "http://app"}
    after = {"statusText": "1", "url": "http://app"}
    touch = "Required evidence: touch pointer increment"
    pen = "Required evidence: pen pointer increment"
    held = "Story step 4.1: Hold Space once and capture the repeat event"

    touch_facts = {"action_kind": "touch", "driver_ok": True,
                   "pointer_evidence": [{"type": "pointerdown", "pointerType": "touch",
                                          "isTrusted": True}]}
    pen_facts = {"action_kind": "pen", "driver_ok": True,
                 "pointer_evidence": [{"type": "pointerdown", "pointerType": "pen",
                                        "isTrusted": True}]}
    hold_facts = {"action_kind": "hold", "action_key": "Space", "driver_ok": True,
                  "keyboard_evidence": [
                      {"type": "keydown", "key": " ", "repeat": True, "isTrusted": True},
                      {"type": "click", "detail": 0, "isTrusted": True}],
                  }

    assert qa_explorer._grounded_demonstrated([touch], touch_facts, before, after,
                                               require_mechanical=True) == [touch]
    assert qa_explorer._grounded_demonstrated([touch], pen_facts, before, after,
                                               require_mechanical=True) == []
    assert qa_explorer._grounded_demonstrated([pen], pen_facts, before, after,
                                               require_mechanical=True) == [pen]
    pen_story = "Story step 6.1: Activate Increment once through trusted pen input"
    assert qa_explorer._grounded_demonstrated(
        [pen_story], touch_facts, before, after, require_mechanical=True) == []
    assert qa_explorer._grounded_demonstrated(
        [pen_story], pen_facts, before, after, require_mechanical=True) == [pen_story]
    assert qa_explorer._grounded_demonstrated(
        [pen], pen_facts, before, {"statusText": "3", "url": "http://app"},
        require_mechanical=True) == [], "an exact pen increment cannot be credited for a two-count delta"
    assert qa_explorer._grounded_demonstrated([held], hold_facts, before, after,
                                               require_mechanical=True) == [held]


def test_held_delta_and_post_hold_discrete_space_require_distinct_trusted_receipts():
    import qa_explorer

    held = "Required evidence: held-Space trusted repeat evidence and observed delta"
    discrete = "Required evidence: post-hold discrete Space increment"
    hold_facts = {
        "action_kind": "hold", "action_key": "Space", "driver_ok": True,
        "keyboard_evidence": [
            {"type": "keydown", "key": " ", "repeat": False, "isTrusted": True},
            {"type": "keydown", "key": " ", "repeat": True, "isTrusted": True},
            {"type": "keyup", "key": " ", "repeat": False, "isTrusted": True},
            {"type": "click", "detail": 0, "isTrusted": True},
        ],
    }
    assert qa_explorer._grounded_demonstrated(
        [held, discrete], hold_facts, {"statusText": "2"}, {"statusText": "3"},
        require_mechanical=True,
    ) == [held]

    press_facts = {
        "action_kind": "press", "action_key": "Space", "driver_ok": True,
        "keyboard_evidence": [
            {"type": "keydown", "key": " ", "repeat": False, "isTrusted": True},
            {"type": "keyup", "key": " ", "repeat": False, "isTrusted": True},
            {"type": "click", "detail": 0, "isTrusted": True},
        ],
    }
    assert qa_explorer._grounded_demonstrated(
        [held, discrete], press_facts, {"statusText": "3"}, {"statusText": "4"},
        require_mechanical=True,
    ) == [discrete]
    assert qa_explorer._grounded_demonstrated(
        [discrete], {**press_facts, "keyboard_evidence": press_facts["keyboard_evidence"][:-1]},
        {"statusText": "3"}, {"statusText": "4"}, require_mechanical=True,
    ) == []


def test_evaluator_prompt_exposes_action_scoped_pointer_and_keyboard_receipts():
    import qa_explorer

    rendered = qa_explorer._fmt_targeting({
        "action_kind": "hold", "driver_ok": True,
        "pointer_evidence": [{"type": "click", "pointerType": "mouse", "isTrusted": True}],
        "keyboard_evidence": [
            {"type": "keydown", "key": " ", "repeat": True, "isTrusted": True},
            {"type": "click", "detail": 0, "isTrusted": True},
        ],
    })
    assert "trusted pointer receipts for THIS action" in rendered
    assert "trusted keyboard/activation receipts for THIS action" in rendered
    assert '"repeat": true' in rendered and '"type": "click"' in rendered


def test_submit_prerequisite_receipt_is_exposed_to_judge_and_next_decision():
    import qa_explorer

    missing = [{"label": "Publication title", "name": "title", "tag": "input", "type": "text"}]
    rendered = qa_explorer._fmt_targeting({
        "action_kind": "click", "driver_ok": True,
        "empty_required_fields_before": missing,
    })
    assert "required form fields empty immediately BEFORE this action" in rendered
    assert "Publication title" in rendered
    history = qa_explorer._fmt_history([{
        "step": 2, "action": {"cmd": "click", "target_text": "Publish"},
        "expected": "publication succeeds", "matched": False,
        "targeting": {"empty_required_fields_before": missing},
    }])
    assert "required setup still empty before action=['Publication title']" in history


def test_browser_proven_submit_prerequisites_skip_model_decisions_then_retry_exact_submit():
    import qa_explorer

    story = {"title": "Publish verified content", "steps": ["Publish an eligible claim"]}
    submit = {"cmd": "click", "target_text": "Run checks and publish", "role": "button", "idx": 9}
    fields = [
        {"label": "Public title", "name": "title", "tag": "input", "type": "text"},
        {"label": "Public body", "name": "body", "tag": "textarea", "type": ""},
    ]
    base = {
        "action": submit, "reasoning": "test publish", "expected": "eligible content publishes",
        "targeting": {"empty_required_fields_before": fields},
        "verdict": {"verdict": "retry"}, "covers": ["Publish an eligible claim"],
    }
    state = {"elements": [
        {"idx": 4, "tag": "input", "type": "text", "text": "Public title",
         "name": "title", "value": ""},
        {"idx": 5, "tag": "textarea", "type": "", "text": "Public body",
         "name": "body", "value": ""},
        {"idx": 9, "tag": "button", "type": "submit", "text": "Run checks and publish"},
    ]}
    first = qa_explorer._pending_required_setup_decision(story, state, [base])
    assert first["mechanical_setup"] is True
    assert first["next_action"] == {"cmd": "type", "idx": 4,
                                     "value": "Public title QA test"}

    state["elements"][0]["value"] = "Public title QA test"
    second = qa_explorer._pending_required_setup_decision(story, state, [base])
    assert second["next_action"] == {"cmd": "type", "idx": 5,
                                      "value": "QA prerequisite completed for this test."}

    state["elements"][1]["value"] = "QA prerequisite completed for this test."
    retry = qa_explorer._pending_required_setup_decision(story, state, [base])
    assert retry["next_action"] == submit
    assert retry["mechanical_setup"] is False
    consumed = qa_explorer._pending_required_setup_decision(
        story, state, [base, {"action": submit, "verdict": {"verdict": "pass"}}])
    assert consumed is None

    validation_story = {"title": "Required field validation", "steps": ["Submit an empty form"]}
    assert qa_explorer._pending_required_setup_decision(validation_story, state, [base]) is None


def test_valid_enquiry_prerequisites_are_completed_before_model_can_submit_partial_form():
    import qa_explorer

    story = {"title": "Retry dead-lettered agent work", "steps": [
        "Set the Agent scenario to Timeout.",
        "Submit one valid enquiry and drain until dead letter.",
    ]}
    coverage = [
        {"aspect": "Set the Agent scenario to Timeout.", "covered": True},
        {"aspect": "Submit one valid enquiry and drain until dead letter.", "covered": False},
    ]
    state = {"elements": [
        {"idx": 1, "formIndex": 0, "tag": "input", "type": "text", "name": "name",
         "text": "Name", "required": True, "value": ""},
        {"idx": 2, "formIndex": 0, "tag": "input", "type": "email", "name": "email",
         "text": "Email", "value": ""},
        {"idx": 3, "formIndex": 0, "tag": "input", "type": "text", "name": "postcode",
         "text": "Postcode", "required": True, "value": "SW1A 1AA"},
        {"idx": 4, "formIndex": 0, "tag": "input", "type": "checkbox", "name": "consent",
         "text": "Retention consent", "required": True, "checked": True},
        {"idx": 9, "formIndex": 0, "formValid": False, "tag": "button", "type": "submit",
         "text": "Send enquiry", "disabled": True},
    ]}

    first = qa_explorer._pending_valid_form_setup_decision(story, state, coverage)
    assert first["mechanical_setup"] is False
    assert first["covers"] == [coverage[1]["aspect"]]
    assert first["next_action"]["_qa_valid_form_submission"] is True
    assert first["next_action"]["cases"][0]["actions"] == [
        {"cmd": "type", "target_text": "Name", "role": "textbox",
         "value": "Name QA test"},
        {"cmd": "type", "target_text": "Email", "role": "textbox",
         "value": "qa.person@example.invalid"},
        {"cmd": "click", "target_text": "Send enquiry", "role": "button"},
    ]

    # A semantic judge that returns inconclusive must not cause a second automatic business submission.
    consumed = qa_explorer._pending_valid_form_setup_decision(
        story, state, coverage, [{"action": first["next_action"]}])
    assert consumed is None

    coverage[0]["covered"] = False
    assert qa_explorer._pending_valid_form_setup_decision(story, state, coverage) is None
    validation = {"title": "Block invalid enquiry input", "steps": ["Submit an empty form"]}
    assert qa_explorer._pending_valid_form_setup_decision(validation, state, coverage) is None


def test_valid_enquiry_batch_ignores_unrelated_submit_form_even_when_story_mentions_attempts():
    import qa_explorer

    story = {"title": "Retry dead-lettered agent work", "steps": [
        "Submit one valid enquiry and drain until the job fails.",
        "Retry and verify attempts update without duplication.",
    ]}
    aspect = "Submit one valid enquiry, drain the queue, and verify the persisted job fails."
    state = {"elements": [
        {"idx": 1, "formIndex": 0, "tag": "input", "type": "text",
         "text": "Staff actor", "required": True, "value": ""},
        {"idx": 2, "formIndex": 0, "tag": "textarea", "type": "text",
         "text": "Decision reason", "required": True, "value": ""},
        {"idx": 3, "formIndex": 0, "tag": "button", "type": "submit",
         "text": "Attempt to send follow-up before approval", "disabled": False},
        {"idx": 10, "formIndex": 1, "tag": "input", "type": "text", "name": "name",
         "text": "Name", "required": True, "value": ""},
        {"idx": 11, "formIndex": 1, "tag": "input", "type": "email", "name": "email",
         "text": "Email", "value": ""},
        {"idx": 12, "formIndex": 1, "tag": "input", "type": "text", "name": "postcode",
         "text": "Postcode", "required": True, "value": "SW1A 1AA"},
        {"idx": 13, "formIndex": 1, "tag": "input", "type": "checkbox", "name": "consent",
         "text": "Retention consent", "required": True, "checked": True},
        {"idx": 14, "formIndex": 1, "tag": "button", "type": "submit",
         "text": "Send enquiry", "disabled": False},
    ]}

    decision = qa_explorer._pending_valid_form_setup_decision(
        story, state, [{"aspect": aspect, "covered": False}])
    actions = decision["next_action"]["cases"][0]["actions"]

    assert [action["target_text"] for action in actions] == ["Name", "Email", "Send enquiry"]
    assert all("follow-up" not in action["target_text"] for action in actions)


def test_focused_tab_replay_binds_reported_source_instead_of_incidental_page_focus():
    import qa_explorer

    coverage = [
        {"aspect": "Story step 1.1: inspect restored state", "covered": True},
        {"aspect": "Story step 2.1: confirm prerequisite", "covered": True},
        {"aspect": "Story step 3.1: exercise corrected control", "covered": False},
        {"aspect": "Story step 4.1: verify corrected behavior", "covered": False},
    ]
    elements = [
        {"idx": 18, "tag": "input", "type": "date", "text": "Start date"},
        {"idx": 20, "tag": "input", "type": "text", "text": "Dog name"},
        {"idx": 26, "tag": "input", "type": "checkbox",
         "text": "You may retain these details to respond to this enquiry."},
        {"idx": 28, "tag": "input", "type": "checkbox",
         "text": "Send occasional service updates."},
        {"idx": 31, "tag": "input", "type": "text", "text": "Claim reference"},
    ]
    date_story = {
        "category": "focused-regression",
        "expected_outcome": "Focus advances visibly to Dog name.",
        "focused_finding": {
            "detail": "Pressing Tab from the Start date control did not advance focus to Dog name.",
            "expected": "Focus advances visibly to Dog name.",
            "action": {"cmd": "press", "target_text": "Dog name", "value": "Tab"},
            "browser_state_restored": True,
        },
    }
    decision = qa_explorer._focused_reported_keyboard_decision(
        date_story, {"elements": elements}, coverage)

    assert decision["next_action"] == {
        "cmd": "press", "idx": 18, "target_text": "Start date", "value": "Tab",
        "_qa_reported_focus_source": True, "_qa_restored_focused_state": True,
        "role": "textbox",
    }
    assert decision["expected_control"] == "Dog name"

    checkbox_story = {
        "category": "focused-regression",
        "expected_outcome": "Focus advances visibly to Claim reference without a scroll jump.",
        "focused_finding": {
            "detail": ("Pressing Tab from the second consent checkbox moved focus to the next operable "
                       "control, Claim reference, but caused a large backward scroll jump."),
            "expected": "Focus advances visibly to Claim reference without a scroll jump.",
            "action": {"cmd": "press", "role": "checkbox",
                       "target_text": "Send occasional service updates.", "value": "Tab"},
            "browser_state_restored": True,
        },
    }
    decision = qa_explorer._focused_reported_keyboard_decision(
        checkbox_story, {"elements": elements}, coverage)

    assert decision["next_action"]["idx"] == 28
    assert decision["next_action"]["target_text"] == "Send occasional service updates."
    assert decision["expected_control"] == "Claim reference"
    prepared, aim = qa_explorer.Explorer(
        "http://unused", "vision", autostart=False)._prepare_action(
            decision["next_action"], elements)
    assert prepared["idx"] == 28
    assert aim["control_action"] is True
    assert aim["restored_focused_state"] is True

    open_coverage = [{**item, "covered": False} for item in coverage]
    immediate = qa_explorer._focused_reported_keyboard_decision(
        date_story, {"elements": elements}, open_coverage,
        records=[{"action": {"_qa_reported_focus_source": True}}])
    assert immediate["next_action"]["idx"] == 18
    preproofs = qa_explorer._mechanically_proven_unresolved(
        open_coverage,
        {"restored_focused_state": True, "driver_ok": True, "label_matched": True,
         "action_key": "Tab", "expected_control": "Dog name", "expected_control_present": True,
         "focus_transition": {
             "source": {"scroll": {"x": 0, "y": 745},
                        "active": {"label": "Start date", "focusVisible": True}},
             "destination": {"scroll": {"x": 0, "y": 745}, "horizontalOverflow": False,
                             "active": {"label": "Dog name", "focusVisible": True}},
             "scroll_delta": {"x": 0, "y": 0},
         }},
        {"url": "http://app", "elements": elements},
        {"url": "http://app", "elements": elements},
    )
    assert preproofs == [item["aspect"] for item in open_coverage]
    verdict = qa_explorer._mechanical_focused_keyboard_verdict(
        immediate["next_action"], open_coverage, preproofs)
    assert verdict["verdict"] == "pass"
    assert verdict["demonstrated"] == preproofs

    bridge = qa_explorer.BrowserBridge.__new__(qa_explorer.BrowserBridge)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True}
    bridge.act(immediate["next_action"])
    assert sent == [{"cmd": "press", "idx": 18, "selector": None, "key": "Tab",
                     "_qa_reported_focus_source": True}]


def test_business_reference_prerequisite_is_never_filled_with_fake_neutral_data():
    import qa_explorer

    story = {"title": "Verify a claim", "steps": ["Verify a valid claim with evidence"]}
    submit = {"cmd": "click", "target_text": "Verify and make public", "role": "button", "idx": 41}
    missing = [{"label": "Claim reference", "name": "verifyClaimId",
                "id": "staff-verify-claim", "tag": "input", "type": "text"}]
    record = {
        "action": submit, "expected": "the selected valid claim becomes public",
        "targeting": {"empty_required_fields_before": missing},
        "verdict": {"verdict": "retry"},
    }
    state = {"elements": [
        {"idx": 40, "tag": "input", "type": "text", "text": "Claim reference",
         "name": "verifyClaimId", "id": "staff-verify-claim", "value": ""},
        {"idx": 41, "tag": "button", "type": "submit", "text": "Verify and make public"},
    ]}

    assert qa_explorer._required_setup_value(missing[0], state["elements"][0]) is None
    assert qa_explorer._pending_required_setup_decision(story, state, [record]) is None


def test_sealed_traversal_regression_closes_from_complete_trusted_receipt():
    import qa_explorer

    coverage = [
        {"aspect": "Story step 1.1: inspect restored state", "covered": True},
        {"aspect": "Story step 2.1: confirm prerequisite state", "covered": True},
        {"aspect": "Story step 3.1: exercise corrected traversal", "covered": False},
        {"aspect": "Story step 4.1: verify the finding-named focus behavior", "covered": False},
    ]
    story = {
        "category": "focused-regression",
        "focused_finding": {"action": {"cmd": "traverse", "value": "forward"}},
    }
    sequence = [
        {"tag": "input", "focusVisible": True, "horizontalOverflow": False},
        {"tag": "button", "focusVisible": True, "horizontalOverflow": False},
        {"tag": "body", "focusVisible": False, "horizontalOverflow": False},
    ]
    targeting = {
        "driver_ok": True,
        "trusted_keyboard": True,
        "keyboard_evidence": [{"isTrusted": True, "key": "Tab", "type": "keydown"}],
        "traversal": {
            "direction": "forward", "count": 4, "derived_focusable_count": 2,
            "unique_controls": 2, "all_focus_visible": True,
            "horizontal_overflow_seen": False, "sequence": sequence,
        },
    }

    verdict = qa_explorer._mechanical_focused_traversal_verdict(
        story, {"cmd": "traverse", "value": "forward"}, targeting, coverage)

    assert verdict["verdict"] == "pass"
    assert verdict["demonstrated"] == [item["aspect"] for item in coverage if not item["covered"]]
    assert verdict["_raw"]["engine"] == "mechanical-focused-traversal-proof"
    # The real focused contract contains the phrase "do not repeat a historical observation".  The generic
    # prose oracle sees ``repeat`` as a held-key requirement and would discard step 3, then chronological
    # ordering would discard step 4.  A sealed mechanical traversal proof must survive that downstream stage.
    coverage[2]["aspect"] = (
        "Story step 3.1: Exercise the corrected user control required by the expected behavior; do not "
        "repeat a historical observation, scroll, or diagnostic action."
    )
    verdict["demonstrated"] = [item["aspect"] for item in coverage if not item["covered"]]
    grounded = qa_explorer._ground_verdict_demonstrated(verdict, targeting, {}, {})
    assert qa_explorer._ordered_grounded_aspects(coverage, grounded) == grounded
    assert qa_explorer._mechanical_focused_traversal_verdict(
        story, {"cmd": "traverse", "value": "forward"},
        {**targeting, "traversal": {**targeting["traversal"], "all_focus_visible": False}},
        coverage) is None
    assert qa_explorer._mechanical_focused_traversal_verdict(
        {"category": "happy", "focused_finding": story["focused_finding"]},
        {"cmd": "traverse", "value": "forward"}, targeting, coverage) is None


def test_initial_state_fence_resets_resume_once_then_allows_responsive_inspection():
    import qa_explorer

    aspect = "Verify initial empty-storage state at 375px mobile and desktop widths"
    viewport = {
        "reasoning": "inspect mobile", "next_action": {"cmd": "viewport", "value": {"width": 375, "height": 844}},
        "expected": "mobile is usable", "covers": [aspect], "done": False,
    }
    first = qa_explorer._fence_initial_state_decision(
        viewport, aspect, [], "/tmp/resumed-storage.json", "http://app")
    assert first["next_action"] == {"cmd": "reset_storage", "value": "http://app"}
    assert first["covers"] == []
    assert "cleared" in first["expected"]

    reset_record = {
        "action": {"cmd": "reset_storage"}, "act_result": {"ok": True},
    }
    second = qa_explorer._fence_initial_state_decision(
        viewport, aspect, [reset_record], "/tmp/resumed-storage.json", "http://app")
    assert second == viewport
    assert qa_explorer._fence_initial_state_decision(
        {**viewport, "next_action": {"cmd": "scroll", "value": "600"}},
        aspect, [reset_record], "/tmp/resumed-storage.json", "http://app")["next_action"]["cmd"] == "scroll"

    mutating = qa_explorer._fence_initial_state_decision(
        {**viewport, "next_action": {"cmd": "type", "target_text": "Name", "value": "Ada"}},
        aspect, [reset_record], "/tmp/resumed-storage.json", "http://app")
    assert mutating["next_action"] == {"cmd": "wait", "value": "0"}


def test_explorer_routes_true_reload_and_bounded_rapid_click_burst():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True, **payload}
    assert bridge.act({"cmd": "reload"})["ok"] is True
    assert bridge.act({"cmd": "burst", "idx": 4, "count": 7, "interval_ms": 20})["ok"] is True
    assert sent == [
        {"cmd": "reload"},
        {"cmd": "clickBurst", "idx": 4, "selector": None, "count": 7, "interval_ms": 20},
    ]


def test_coverage_contract_is_atomic_and_only_mechanical_evidence_can_credit_it():
    import qa_explorer

    assert qa_explorer._atomic_coverage_aspects([
        "Tab and Shift+Tab traversal", "Enter and Space activation", "Pointer click",
    ]) == [
        "Tab traversal", "Shift+Tab traversal", "Enter activation", "Space activation", "Pointer click",
    ]
    assert qa_explorer._atomic_coverage_aspects([
        "Acknowledge another blocker using keyboard Enter or Space",
    ]) == ["Acknowledge another blocker using keyboard Enter or Space"]

    requested = [
        "Shift+Tab traversal", "rapid click burst timing", "true reload behavior",
        "screen reader announcement", "Chromium accessibility platform live-region exposure", "Pointer click",
    ]
    before = {"accessibilityPlatformEvents": []}
    after_dom_only = {"accessibilityEvents": [{"role": "status", "text": "1"}],
                      "accessibilityPlatformEvents": []}
    assert qa_explorer._grounded_demonstrated(
        requested,
        {"action_kind": "goto", "action_key": "Tab", "burst": {}},
        before, after_dom_only,
    ) == []

    after_platform = {"accessibilityPlatformEvents": [
        {"source": "Accessibility.getFullAXTree:changed", "role": "status", "name": "5"},
    ]}
    assert qa_explorer._grounded_demonstrated(
        ["Shift+Tab traversal"],
        {"action_kind": "press", "action_key": "Shift+Tab"}, before, after_platform,
    ) == ["Shift+Tab traversal"]
    assert qa_explorer._grounded_demonstrated(
        ["rapid click burst timing", "Pointer click"],
        {"action_kind": "burst", "driver_ok": True,
         "burst": {"burst": True, "count": 5, "elapsed_ms": 120}}, before, after_platform,
    ) == ["rapid click burst timing", "Pointer click"]
    assert qa_explorer._grounded_demonstrated(
        ["true reload behavior"],
        {"action_kind": "reload", "reloaded": True}, before, after_platform,
    ) == ["true reload behavior"]
    assert qa_explorer._grounded_demonstrated(
        ["screen reader announcement", "Chromium accessibility platform live-region exposure"],
        {"action_kind": "click", "driver_ok": True}, before, after_platform,
    ) == ["Chromium accessibility platform live-region exposure"]

    # Intended coverage may repair an evaluator that omitted its optional demonstrated list, but only for
    # driver-provable aspect families. Arbitrary semantic prose remains uncredited.
    after_focus = {"url": "http://app", "perception": {"firstAt": 1},
                   "activeElement": {"tag": "button", "text": "Increment", "focusVisible": True}}
    before_focus = {"url": "http://app", "perception": {"firstAt": 1}}
    assert qa_explorer._grounded_demonstrated(
        ["Enter activation", "visible focus indicator", "no navigation", "looks delightful"],
        {"action_kind": "press", "action_key": "Enter"}, before_focus, after_focus,
        require_mechanical=True,
    ) == ["Enter activation", "visible focus indicator", "no navigation"]

    before_at = {"actualAssistiveTechnologyEvents": [
        {"utterance": "Current count 1", "source": "orca-at-spi"}]}
    after_at = {"actualAssistiveTechnologyEvents": [
        {"utterance": "Current count 1", "source": "orca-at-spi"},
        {"utterance": "Current count 2", "source": "orca-at-spi"}]}
    assert qa_explorer._grounded_demonstrated(
        ["Story step 2.1: Activate Increment again",
         "Story step 2.2: capture Orca announcing Current count 2"],
        {"action_kind": "click", "driver_ok": True}, before_at, after_at,
        require_mechanical=True,
    ) == ["Story step 2.1: Activate Increment again",
          "Story step 2.2: capture Orca announcing Current count 2"]
    assert qa_explorer._grounded_demonstrated(
        ["Story step 2.2: capture Orca announcing Current count 2"],
        {"action_kind": "click", "driver_ok": True}, {}, before_at,
        require_mechanical=True,
    ) == [], "a count-1 utterance cannot certify the contractual count-2 result"
    assert qa_explorer._requires_actual_at("post-reload AT event sequence") is True
    assert qa_explorer._requires_actual_at("Chromium AX tree exposure") is False


def test_story_step_and_open_words_do_not_invent_pointer_requirements():
    """Regression from a paid keyboard canary: ``step`` contains tap and ``open`` contains pen."""
    import qa_explorer

    coverage = [
        {"aspect": "Story step 1.1: Open the counter", "covered": False},
        {"aspect": "Story step 1.2: use reload for a true refresh", "covered": False},
        {"aspect": "Story step 1.3: perform no pointer actions", "covered": False},
        {"aspect": "Story step 2.1: Press Tab until Increment receives focus", "covered": False},
    ]
    state = {
        "url": "http://app/index.html", "title": "Accessible Counter",
        "bodyText": "Accessible Counter Current count 0 Increment",
        "statusText": "0", "activeElement": None,
    }
    targeting = {
        "action_kind": "reload", "driver_ok": True, "reloaded": True,
        "session_target_url": "http://app/index.html", "pointer_evidence": [],
    }

    assert qa_explorer._mechanically_proven_unresolved(
        coverage, targeting, state, state,
    ) == [item["aspect"] for item in coverage[:3]]


def test_keep_pointer_idle_is_a_negative_keyboard_constraint():
    """Regression from the paid audit-expansion keyboard story."""
    import qa_explorer

    aspect = "Story step 1.1: Begin with document focus before Increment and keep the pointer idle"
    before = {"url": "http://app", "activeElement": None}
    after = {
        "url": "http://app",
        "activeElement": {"tag": "button", "text": "Increment", "focusVisible": True},
    }
    keyboard = {
        "action_kind": "press", "action_key": "Tab", "driver_ok": True,
        "pointer_evidence": [],
    }
    click = {
        "action_kind": "click", "driver_ok": True,
        "pointer_evidence": [{"type": "pointerdown", "pointerType": "mouse", "isTrusted": True}],
    }

    assert qa_explorer._grounded_demonstrated(
        [aspect], keyboard, before, after, require_mechanical=True,
    ) == [aspect]
    assert qa_explorer._grounded_demonstrated(
        [aspect], click, before, after, require_mechanical=True,
    ) == []


def test_reload_proves_its_completed_load_record_and_initial_count_together():
    """Regression from the paid fresh-load keyboard audit story."""
    import qa_explorer

    coverage = [{"aspect": aspect, "covered": False} for aspect in (
        "Story step 1.1: Use browser reload for a true refresh",
        "Story step 1.2: capture the completed load/navigation record",
        "Story step 1.3: record the initial count as N",
    )]
    before = {"url": "http://app/index.html", "statusText": "0"}
    after = {"url": "http://app/index.html", "title": "Counter", "statusText": "0"}
    targeting = {
        "action_kind": "reload", "driver_ok": True, "effect_registered": True,
        "reloaded": True, "session_target_url": "http://app/index.html",
    }

    assert qa_explorer._mechanically_proven_unresolved(
        coverage, targeting, before, after,
    ) == [item["aspect"] for item in coverage]


def test_tab_return_to_increment_is_focus_not_page_navigation():
    """Regression from the paid reverse-traversal keyboard story."""
    import qa_explorer

    aspect = "Story step 3.2: then press Tab to return to Increment"
    before = {"url": "http://app", "activeElement": {"tag": "body"}}
    after = {
        "url": "http://app",
        "activeElement": {"tag": "button", "text": "Increment", "focusVisible": True},
    }
    targeting = {"action_kind": "press", "action_key": "Tab", "driver_ok": True}

    assert qa_explorer._grounded_demonstrated(
        [aspect], targeting, before, after, require_mechanical=True,
    ) == [aspect]


def test_agent_authored_use_back_is_compiled_as_a_separate_action():
    import qa_explorer

    aspects = qa_explorer._story_contract_aspects({"steps": [
        "Navigate to a valid same-origin page and use browser Back to re-enter. Record the observed "
        "count as M without asserting reset or persistence, acquire Increment with Tab, press Enter, "
        "and verify M+1."
    ]})
    assert aspects[:2] == [
        "Story step 1.1: Navigate to a valid same-origin page",
        "Story step 1.2: use browser Back to re-enter. Record the observed count as M without asserting "
        "reset or persistence",
    ]
    assert aspects[2:] == [
        "Story step 1.3: acquire Increment with Tab",
        "Story step 1.4: press Enter",
        "Story step 1.5: verify M+1",
    ]


def test_story_steps_remain_ordered_contract_when_coverage_is_lossy():
    import qa_explorer

    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    ledger = explorer._ai_coverage_plan({
        "steps": [
            "Activate Increment and capture Orca announcing Current count 1.",
            "Reload the page, activate Increment, and capture Orca announcing Current count 1 again.",
        ],
        "expected": "The real screen reader and visible count agree after reload.",
        "coverage": ["post-reload AT event sequence"],
    }, {})
    aspects = [item["aspect"] for item in ledger]
    reload_index = next(i for i, value in enumerate(aspects) if "Reload the page" in value)
    post_reload_click = next(i for i, value in enumerate(aspects)
                             if i > reload_index and "activate Increment" in value)
    post_reload_orca = next(i for i, value in enumerate(aspects)
                            if i > post_reload_click and "Current count 1 again" in value)
    assert reload_index < post_reload_click < post_reload_orca
    assert all(item["explicit"] and item["covered"] is False for item in ledger)

    grounded = [
        aspects[0],
        next(value for value in aspects if "Current count 1" in value),
        aspects[post_reload_orca],
        "Required evidence: post-reload AT event sequence",
    ]
    ordered = qa_explorer._ordered_grounded_aspects(ledger, grounded)
    assert aspects[0] in ordered
    assert aspects[post_reload_orca] not in ordered
    assert "Required evidence: post-reload AT event sequence" not in ordered
    ledger[reload_index]["covered"] = True
    assert "Required evidence: post-reload AT event sequence" in qa_explorer._ordered_grounded_aspects(
        ledger, grounded)


def test_complete_structured_coverage_plan_replaces_duplicate_clause_fanout(monkeypatch):
    import qa_explorer

    story = {
        "steps": [
            "Seed draft, private, and evidenced trust claims.",
            "Attempt publication for every claim type.",
            "Attempt publication containing an email, phone, token, password, or secret.",
            "Verify a valid claim and re-render the public app.",
        ],
        "expected_outcome": "Only eligible non-sensitive content renders publicly.",
    }
    monkeypatch.setattr(qa_explorer, "_call_agent", lambda *_args, **_kwargs: {
        "rc": 0, "out_full": json.dumps({"aspects": [
            {"aspect": "Seed and inspect every trust-claim eligibility state", "story_steps": [1]},
            {"aspect": "Exercise every claim-reference publication decision", "story_steps": [2]},
            {"aspect": "Exercise each sensitive-content denial", "story_steps": [3]},
            {"aspect": "Verify the eligible claim and confirm the governed public render", "story_steps": [4]},
        ]})})

    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    ledger = explorer._ai_coverage_plan(story, {})

    assert len(ledger) == 4
    assert [item["contract_steps"] for item in ledger] == [[1], [2], [3], [4]]
    assert all(item["explicit"] is False for item in ledger)
    assert not any(item["aspect"].startswith("Story step ") for item in ledger)


def test_structured_coverage_plan_missing_a_story_step_fails_closed_to_exact_contract(monkeypatch):
    import qa_explorer

    story = {"steps": ["Open the app.", "Submit the form.", "Reload and inspect it."],
             "expected_outcome": "The submission persists."}
    monkeypatch.setattr(qa_explorer, "_call_agent", lambda *_args, **_kwargs: {
        "rc": 0, "out_full": json.dumps({"aspects": [
            {"aspect": "Open and inspect the app", "story_steps": [1]},
            {"aspect": "Submit the form", "story_steps": [2]},
        ]})})

    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    ledger = explorer._ai_coverage_plan(story, {})

    assert [item["aspect"] for item in ledger] == qa_explorer._story_contract_aspects(story)
    assert all(item["explicit"] is True for item in ledger)


def test_uncovered_single_item_legacy_ledger_is_granularly_replanned_without_dropping_it(monkeypatch):
    import qa_explorer

    story = {
        "steps": ["Seed enquiries.", "Open the CEO view.", "Acknowledge a blocker."],
        "expected": "Metrics are accurate and the acknowledgement persists.",
    }
    legacy = [{"aspect": "Metrics are accurate; acknowledgement persists", "covered": False}]
    assert qa_explorer._legacy_single_coverage_needs_replan(legacy, story) is True
    assert qa_explorer._legacy_single_coverage_needs_replan(
        [{**legacy[0], "covered": True}], story) is False

    monkeypatch.setattr(qa_explorer, "_call_agent", lambda *_args, **_kwargs: {
        "rc": 0, "out_full": '{"aspects":["Metrics are accurate and acknowledgement persists"]}'})
    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    ledger = explorer._ai_coverage_plan(story, {})
    aspects = [item["aspect"] for item in ledger]

    assert aspects[:3] == [
        "Story step 1.1: Seed enquiries", "Story step 2.1: Open the CEO view",
        "Story step 3.1: Acknowledge a blocker"]
    assert "Metrics are accurate and acknowledgement persists" in aspects


def test_completed_post_reload_click_is_credited_without_model_repeating_ledger_label():
    import qa_explorer

    ledger = [
        {"aspect": "Story step 1.1: Perform a true refresh using browser reload",
         "covered": True, "explicit": True},
        {"aspect": "Story step 2.1: Activate Increment once with the pointer",
         "covered": False, "explicit": True},
        {"aspect": "Required evidence: post-reload pointer activation",
         "covered": False, "explicit": True},
        {"aspect": "Required evidence: product remains delightful",
         "covered": False, "explicit": True},
    ]
    proven = qa_explorer._mechanically_proven_unresolved(
        ledger,
        {"action_kind": "click", "driver_ok": True},
        {"url": "http://app", "bodyText": "Current count 0"},
        {"url": "http://app", "bodyText": "Current count 1"},
    )
    assert proven == [
        "Story step 2.1: Activate Increment once with the pointer",
        "Required evidence: post-reload pointer activation",
    ]


def test_available_capability_precondition_does_not_create_an_impossible_negative_branch():
    import qa_explorer

    story = {
        "steps": [
            "Confirm an actual assistive-technology driver is available; otherwise stop before browser "
            "action and route the capability gap to management",
            "Press Space once",
        ],
        "coverage": ["actual assistive-technology announcement after Space"],
    }
    ledger = [{"aspect": aspect, "covered": False, "explicit": True}
              for aspect in qa_explorer._story_contract_aspects(story)]
    assert any("Confirm an actual assistive-technology driver is available" in item["aspect"]
               for item in ledger)
    assert all("otherwise stop" not in item["aspect"] for item in ledger)

    proven = qa_explorer._mechanically_proven_unresolved(
        ledger,
        {"action_kind": "wait", "driver_ok": True},
        {"actualAssistiveTechnologyAvailable": True,
         "actualAssistiveTechnologyEvents": [{"utterance": "Screen reader on."}]},
        {"actualAssistiveTechnologyAvailable": True,
         "actualAssistiveTechnologyEvents": [{"utterance": "Screen reader on."}]},
    )
    assert proven == [ledger[0]["aspect"]]

    paraphrased = qa_explorer._story_contract_aspects({
        "steps": [
            "Before any browser action, require capability admission for an actual assistive-technology "
            "driver such as Orca; if unavailable, route the requirement to management",
            "Press Enter",
        ],
        "coverage": ["Enter activation"],
    })
    assert all("if unavailable" not in item.casefold() for item in paraphrased)
    admission = next(item for item in paraphrased if "capability admission" in item)
    assert qa_explorer._grounded_demonstrated(
        [admission],
        {"action_kind": "wait", "driver_ok": True},
        {}, {"actualAssistiveTechnologyAvailable": True},
        require_mechanical=True,
    ) == [admission]


def test_repeated_contract_requires_distinct_actions_and_exact_count_transition():
    import qa_explorer

    story = {
        "steps": [
            "Activate Increment twice and capture the driver output after each activation",
        ],
        "coverage": ["repeated activation changes 1 to 2"],
    }
    aspects = qa_explorer._story_contract_aspects(story)
    occurrences = [item for item in aspects if "[occurrence" in item]
    assert len(occurrences) == 2
    assert "occurrence 1 of 2" in occurrences[0]
    assert "occurrence 2 of 2" in occurrences[1]

    ledger = [{"aspect": aspect, "covered": False, "explicit": True} for aspect in aspects]
    click = {"action_kind": "click", "driver_ok": True}
    first = qa_explorer._mechanically_proven_unresolved(
        ledger, click, {"statusText": "0"}, {"statusText": "1"})
    assert occurrences[0] in first and occurrences[1] not in first
    assert "Required evidence: repeated activation changes 1 to 2" not in first
    for item in ledger:
        if item["aspect"] in first:
            item["covered"] = True
    second = qa_explorer._mechanically_proven_unresolved(
        ledger, click, {"statusText": "1"}, {"statusText": "2"})
    assert occurrences[1] in second
    assert "Required evidence: repeated activation changes 1 to 2" in second


def test_initial_real_at_output_is_static_evidence_but_action_announcement_requires_change():
    import qa_explorer

    state = {
        "statusText": "0",
        "actualAssistiveTechnologyAvailable": True,
        "actualAssistiveTechnologyEvents": [
            {"utterance": "Screen reader on.", "source": "orca-at-spi"},
            {"utterance": "Current count 0", "source": "orca-at-spi"},
        ],
    }
    targeting = {"action_kind": "wait", "driver_ok": True}
    assert qa_explorer._grounded_demonstrated(
        ["capture actual assistive-technology output for the initial value"],
        targeting, state, state, require_mechanical=True,
    ) == ["capture actual assistive-technology output for the initial value"]
    assert qa_explorer._grounded_demonstrated(
        ["actual assistive-technology driver announcement after activation"],
        targeting, state, state, require_mechanical=True,
    ) == []
    assert qa_explorer._actual_at_availability_requirement(
        "Confirm an actual assistive-technology driver is available") is True
    assert qa_explorer._actual_at_availability_requirement(
        "Open a fresh counter session showing 0 with an actual assistive-technology driver active") is False
    assert qa_explorer._actual_at_availability_requirement(
        "confirm an actual assistive-technology driver with Orca/AT-SPI output is available") is True


def test_static_count_observation_and_reporting_contract_are_not_browser_blockers():
    import qa_explorer

    aspects = qa_explorer._story_contract_aspects({
        "steps": [
            "Record the currently visible count as N",
            "Press Enter once",
            "Link every screenshot, keyboard trace, and AT-driver record to the exact step it proves",
        ],
        "coverage": ["Enter activation"],
    })
    assert all("Link every" not in item for item in aspects)
    count_aspect = next(item for item in aspects if "visible count as N" in item)
    assert qa_explorer._grounded_demonstrated(
        [count_aspect], {"action_kind": "wait", "driver_ok": True},
        {"statusText": "0"}, {"statusText": "0"}, require_mechanical=True,
    ) == [count_aspect]


def test_direct_navigation_away_and_reentry_are_grounded_against_session_target():
    import qa_explorer

    target = "http://app.test/counter"
    common = {"driver_ok": True, "session_target_url": target}
    direct = qa_explorer._grounded_demonstrated(
        ["Start a new browser context and explicitly navigate to the counter page"],
        {**common, "action_kind": "goto", "action_value": target},
        {"url": target}, {"url": target}, require_mechanical=True,
    )
    assert direct
    away = qa_explorer._grounded_demonstrated(
        ["Navigate away"], {**common, "action_kind": "goto", "action_value": "http://app.test/away"},
        {"url": target}, {"url": "http://app.test/away"}, require_mechanical=True,
    )
    assert away
    reentry = qa_explorer._grounded_demonstrated(
        ["explicitly re-enter the counter page"],
        {**common, "action_kind": "back"},
        {"url": "http://app.test/away"}, {"url": target}, require_mechanical=True,
    )
    assert reentry
    assert qa_explorer._grounded_demonstrated(
        ["Navigate away"], {**common, "action_kind": "goto", "action_value": target},
        {"url": target}, {"url": target}, require_mechanical=True,
    ) == []

    clauses = qa_explorer._story_contract_aspects({
        "steps": [
            "Before any activation, capture a screenshot showing count 0",
            "Without pointer input or refocusing, press Space",
        ],
        "coverage": ["Space activation"],
    })
    assert all(not item.endswith(": Before any activation")
               and not item.endswith(": Without pointer input or refocusing") for item in clauses)


def test_qualified_space_press_is_credited_without_pointer_or_refocus_churn():
    import qa_explorer

    aspect = "Story step 6.1: Without pointer input or refocusing press Space once"
    ledger = [{"aspect": aspect, "covered": False, "explicit": True}]
    active = {"tag": "button", "role": "button", "text": "Increment", "focusVisible": True}
    before = {"url": "http://app", "statusText": "1", "activeElement": dict(active)}
    after = {"url": "http://app", "statusText": "2", "activeElement": dict(active)}

    assert qa_explorer._mechanically_proven_unresolved(
        ledger, {"action_kind": "press", "action_key": "Space", "driver_ok": True},
        before, after,
    ) == [aspect]
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, {"action_kind": "click", "action_key": "", "driver_ok": True},
        before, after,
    ) == [], "a pointer click cannot prove the explicit no-pointer Space step"
    moved_focus = {**after, "activeElement": {**active, "text": "Other"}}
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, {"action_kind": "press", "action_key": "Space", "driver_ok": True},
        before, moved_focus,
    ) == [], "the no-refocus qualifier requires the same control to remain focused"


def test_initial_session_open_unblocks_ordered_click_and_observation_without_redundant_goto():
    import qa_explorer

    target = "http://app.test/counter"
    aspects = [
        "Story step 1.1: Open the counter",
        "Story step 2.1: Activate Increment",
        "Story step 3.1: Observe the new count",
    ]
    ledger = [{"aspect": aspect, "covered": False, "explicit": True} for aspect in aspects]
    targeting = {
        "action_kind": "click", "driver_ok": True, "session_target_url": target,
    }
    before = {"url": target, "title": "Counter", "bodyText": "Current count 0",
              "statusText": "0"}
    after = {"url": target, "title": "Counter", "bodyText": "Current count 1",
             "statusText": "1"}

    assert qa_explorer._mechanically_proven_unresolved(
        ledger, targeting, before, after) == aspects

    wrong_before = {**before, "url": "http://app.test/elsewhere"}
    wrong_after = {**after, "url": "http://app.test/elsewhere"}
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, targeting, wrong_before, wrong_after) == [], (
            "a rendered page at a different URL cannot satisfy the session-open prerequisite")

    explicit = [{"aspect": "Story step 1.1: Explicitly navigate to the counter page",
                 "covered": False, "explicit": True}]
    assert qa_explorer._mechanically_proven_unresolved(
        explicit, targeting, before, after) == [], (
            "explicit navigation still requires a real goto/back/forward command")


def test_valid_distinct_history_page_and_scoped_console_capture_are_mechanical_evidence():
    import qa_explorer

    target = "http://app.test/counter"
    away = target + "?history-away=1"
    clauses = qa_explorer._story_contract_aspects({
        "steps": [
            "Clear the console, start a timestamped console capture, and navigate to a verified valid "
            "non-error page",
            "Inspect the time-scoped console capture and fail the test for any console error, including a 404",
        ],
        "coverage": ["valid navigation-away destination", "clean time-scoped console record"],
    })
    assert not any(item.endswith(": including a 404") for item in clauses)
    ledger = [{"aspect": item, "covered": False, "explicit": True} for item in clauses]
    before = {"url": target, "title": "Counter", "bodyText": "Counter", "console_errors": [],
              "recent_requests": [{"method": "GET", "url": target, "status": 200}]}
    after = {"url": away, "title": "Counter", "bodyText": "Counter", "console_errors": [],
             "recent_requests": [
                 {"method": "GET", "url": target, "status": 200},
                 {"method": "GET", "url": away, "status": 200},
             ]}
    targeting = {"action_kind": "goto", "action_value": away, "driver_ok": True,
                 "session_target_url": target}
    proven = qa_explorer._mechanically_proven_unresolved(ledger, targeting, before, after)
    assert any("Clear the console" in item for item in proven)
    assert any("timestamped console capture" in item for item in proven)
    assert any("verified valid non-error page" in item for item in proven)
    assert "Required evidence: valid navigation-away destination" in proven
    assert "Required evidence: clean time-scoped console record" in proven

    failed = {**after, "recent_requests": after["recent_requests"] + [
        {"method": "GET", "url": away + "&missing=1", "status": 404}]}
    failed_proven = qa_explorer._mechanically_proven_unresolved(
        ledger, targeting, before, failed)
    assert not any("console" in item.casefold() for item in failed_proven)
    assert "Required evidence: clean time-scoped console record" not in failed_proven


def test_recorder_clear_is_an_explicit_receipt_not_an_empty_array_inference(tmp_path):
    import qa_explorer

    aspect = "explicitly clear the console and start capture"
    state = {"url": "http://app", "screenshot": "shot.png", "console_errors": [],
             "recent_requests": []}
    assert qa_explorer._recorder_start_provable(
        aspect, state, artifact_dir=tmp_path, bridge_active=True) is False
    receipt = {"cleared_at": 123.5, "console_before": 2, "requests_before": 7}
    assert qa_explorer._recorder_start_provable(
        aspect, state, artifact_dir=tmp_path, bridge_active=True, clear_receipt=receipt) is True

    class Bridge:
        def _send(self, payload):
            assert payload == {"cmd": "clearEvidence"}
            return {"ok": True, **receipt}

    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False, artifact_dir=tmp_path)
    explorer.artifact_dir = tmp_path
    explorer.bridge = Bridge()
    explorer.coverage = [{"aspect": aspect, "covered": False, "explicit": True}]
    assert explorer._begin_recorder_scope() is True
    assert explorer._capture_started_at == 123.5
    explorer._credit_recorder_start(state)
    assert explorer.artifact_evidence[0]["clear_receipt"] == receipt

    initial = "Story step 2.2: record its URL and count"
    assert qa_explorer._recorder_requirement_stage(initial) == "start"
    assert qa_explorer._recorder_start_provable(
        initial, {**state, "statusText": "0"}, artifact_dir=tmp_path,
        bridge_active=True, clear_receipt=receipt) is True
    assert qa_explorer._recorder_start_provable(
        initial, state, artifact_dir=tmp_path,
        bridge_active=True, clear_receipt=receipt) is False


def test_history_rebase_sanitizes_the_stored_expectation_before_evaluation():
    import qa_explorer

    story = {
        "steps": ["Use Back, record the returned count as C, click Increment and verify C+1"],
        "expected": "The observed returned count becomes C and the next click produces C+1",
    }
    proposed = "Back must preserve the earlier count of 1 and C must equal 1"
    grounded, corrected = qa_explorer._contract_grounded_history_expected(
        story, proposed, {"action_kind": "back", "history_direction": "back"})
    assert corrected is True
    assert "fresh baseline" in grounded and "no pre-navigation count value is imposed" in grounded
    assert "equal 1" not in grounded


def test_invalid_route_requires_the_goto_and_matching_error_response_not_later_back():
    import qa_explorer

    navigate = "Story step 1.3: navigate to a guaranteed nonexistent same-origin route"
    inspect = "Story step 2.1: Record the invalid route's actual network status and rendered error state"
    target, missing = "http://app/index.html", "http://app/__missing"
    ledger = [
        {"aspect": "Story step 1.1: Open the counter", "covered": True, "explicit": True},
        {"aspect": "Story step 1.2: record its URL and observed count as N", "covered": True,
         "explicit": True},
        {"aspect": navigate, "covered": False, "explicit": True},
        {"aspect": inspect, "covered": False, "explicit": True},
    ]
    error_state = {"url": missing, "title": "Error", "bodyText": "404 Not Found",
                   "recent_requests": [{"method": "GET", "url": missing, "status": 404}]}
    goto = {"action_kind": "goto", "driver_ok": True, "session_target_url": target}
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, goto, {"url": target}, error_state) == [navigate, inspect]

    back = {"action_kind": "back", "history_direction": "back", "driver_ok": True,
            "session_target_url": target}
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, back, error_state,
        {"url": target, "title": "Counter", "bodyText": "0", "recent_requests": [
            {"method": "GET", "url": missing, "status": 404},
            {"method": "GET", "url": target, "status": 200}]}) == []


def test_post_history_return_activation_is_current_click_fenced_by_prior_back():
    import qa_explorer

    back = "Story step 3.1: Use browser Back to return to the counter"
    activation = "Required evidence: post-history-return pointer activation"
    ledger = [
        {"aspect": back, "covered": False, "explicit": True},
        {"aspect": activation, "covered": False, "explicit": True},
    ]
    targeting = {"action_kind": "click", "driver_ok": True}
    before = {"url": "http://app/counter", "statusText": "0"}
    after = {"url": "http://app/counter", "statusText": "1"}

    assert qa_explorer._grounded_demonstrated(
        [activation], targeting, before, after, require_mechanical=True) == [activation]
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, targeting, before, after) == [], (
            "a click cannot be called post-return until a Back journey step is already proven")
    ledger[0]["covered"] = True
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, targeting, before, after) == [activation]

    post_back = "Required evidence: post-Back pointer activation"
    assert qa_explorer._post_history_return_requirement(post_back)
    assert qa_explorer._mechanically_proven_unresolved(
        [{"aspect": back, "covered": True, "explicit": True},
         {"aspect": post_back, "covered": False, "explicit": True}],
        targeting, before, after) == [post_back]


def test_tab_focus_grounding_uses_actual_element_and_one_ordered_keypress():
    import qa_explorer

    focus = "Story step 4.1: Use Tab to visibly focus Increment"
    again = "Story step 5.1: Use Tab again"
    past = "Story step 5.2: verify focus moves past Increment"
    ledger = [{"aspect": item, "covered": False, "explicit": True}
              for item in (focus, again, past)]
    targeting = {"action_kind": "press", "action_key": "Tab", "driver_ok": True}
    button = {"tag": "button", "text": "Increment", "focusVisible": True}
    body = {"tag": "body", "text": "", "focusVisible": False}

    grounded = qa_explorer._grounded_demonstrated(
        [focus, again, past], targeting,
        {"activeElement": body}, {"activeElement": button})
    assert qa_explorer._ordered_grounded_aspects(ledger, grounded) == [focus]

    ledger[0]["covered"] = True
    grounded = qa_explorer._grounded_demonstrated(
        [focus, again, past], targeting,
        {"activeElement": button}, {"activeElement": body})
    assert qa_explorer._ordered_grounded_aspects(ledger, grounded) == [again, past]


def test_recorder_owned_requirements_are_credited_at_real_start_and_only_finalize_last(tmp_path):
    import qa_explorer

    start = "Story step 1.1: Start a continuous session video or equivalent uninterrupted browser trace"
    clear = "Required evidence: explicit console clear"
    journey = "Story step 2.1: Open the counter"
    end = "Required evidence: final complete-record inspection artifact"
    clean = "Story step 7.3: fail for any console error or HTTP 404"
    ex = qa_explorer.Explorer("http://app", "vision", autostart=False)
    ex.bridge = types.SimpleNamespace()
    ex.artifact_dir = tmp_path
    ex.coverage = [
        {"aspect": start, "covered": False, "explicit": True},
        {"aspect": clear, "covered": False, "explicit": True},
        {"aspect": journey, "covered": False, "explicit": True},
        {"aspect": end, "covered": False, "explicit": True},
        {"aspect": clean, "covered": False, "explicit": True},
    ]
    state = {"url": "http://app", "screenshot": str(tmp_path / "start.png"),
             "console_errors": [], "recent_requests": []}
    ex._recorder_start_receipt = {"cleared_at": 1.0, "console_before": 0, "requests_before": 1}
    assert ex._credit_recorder_start(state) == [start, clear]
    record = {"actual": {**state, "recent_requests": [
        {"method": "GET", "url": "http://app", "status": 200}]}}
    assert ex._credit_recorder_end([record]) == [], (
        "end/inspection evidence cannot bypass an unfinished user journey")
    ex.coverage[2]["covered"] = True
    assert ex._credit_recorder_end([record]) == [end, clean]
    assert [item["stage"] for item in ex.artifact_evidence] == ["start", "start", "end", "end"]

    broken = qa_explorer.Explorer("http://app", "vision", autostart=False)
    broken.bridge = types.SimpleNamespace()
    broken.artifact_dir = tmp_path
    broken.coverage = [{"aspect": end, "covered": False, "explicit": True}]
    failed_record = {"actual": {**state, "console_errors": ["boom"],
                                "recent_requests": []}}
    assert broken._credit_recorder_end([failed_record]) == []


def test_history_return_cannot_invent_persistence_when_story_rebases_observed_count():
    import qa_explorer

    story = {
        "steps": [
            "Use browser Forward to revisit the distinct page, then browser Back",
            "Record the returned count as C, activate Increment, and verify C+1",
        ],
        "expected": "Back and Forward traverse successful pages; each post-return activation increments once.",
    }
    targeting = {"action_kind": "back", "history_direction": "back", "driver_ok": True}
    actual = {"console_errors": [], "recent_requests": [
        {"method": "GET", "url": "http://app/counter", "status": 200}]}
    expected = "The counter returns and preserves the previous count C=1."
    bug = "The counter reset instead of preserving the pre-navigation count."
    assert qa_explorer._history_rebased_state_false_positive(
        story, expected, targeting, bug, actual)

    explicit = {**story, "expected": "The count persists unchanged across browser history."}
    assert not qa_explorer._history_rebased_state_false_positive(
        explicit, expected, targeting, bug, actual)


def test_direct_same_url_reentry_and_later_operability_are_distinct_ordered_proofs():
    import qa_explorer

    target = "http://app.test/counter"
    reentry = "Story step 6.1: Directly re-enter the counter URL"
    operability = "Required evidence: post-direct-re-entry operability"
    ledger = [
        {"aspect": reentry, "covered": False, "explicit": True},
        {"aspect": operability, "covered": False, "explicit": True},
    ]
    state = {"url": target, "title": "Counter", "bodyText": "Counter", "statusText": "0"}
    goto = {"action_kind": "goto", "action_value": target, "driver_ok": True,
            "session_target_url": target}
    assert qa_explorer._mechanically_proven_unresolved(ledger, goto, state, state) == [reentry]

    click = {"action_kind": "click", "driver_ok": True, "effect_registered": True,
             "session_target_url": target}
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, click, state, {**state, "statusText": "1"}) == [], (
            "post-re-entry operation cannot be credited before re-entry itself")
    ledger[0]["covered"] = True
    assert qa_explorer._mechanically_proven_unresolved(
        ledger, click, state, {**state, "statusText": "1"}) == [operability]


def test_unavailable_real_at_capability_stops_before_any_browser_action(monkeypatch):
    import qa_explorer

    monkeypatch.delenv("AOS_QA_AT_DRIVER", raising=False)
    monkeypatch.setattr(qa_explorer, "_actual_at_driver_facts",
                        lambda: {"available": False, "driver": "orca", "missing": ["orca"]})
    assert qa_explorer._missing_required_capabilities([
        {"aspect": "initial count actual assistive-technology driver announcement"},
        {"aspect": "changed Chromium AX event"},
    ], {}) == [{
        "capability": "actual-assistive-technology",
        "aspects": ["initial count actual assistive-technology driver announcement"],
        "reason": ("The story requires observable output from a real assistive-technology driver, "
                   "but this worker has only DOM and Chromium Accessibility-domain instrumentation."),
    }]
    assert qa_explorer._missing_required_capabilities(
        [{"aspect": "DOM live-region mutation and Chromium AX exposure"}], {}) == []

    class Bridge:
        def state(self):
            return {"url": "http://app", "elements": [],
                    "actualAssistiveTechnologyAvailable": False}

    explorer = qa_explorer.Explorer("http://app", "accessible counter", autostart=False)
    explorer.bridge = Bridge()
    explorer._ai_coverage_plan = lambda *_: [
        {"aspect": "updated count screen-reader announcement", "covered": False, "explicit": True}]
    explorer._ai_decide = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("an unavailable capability must be admitted before a paid decision/action"))
    records = explorer.explore({"title": "AT output", "expected": "spoken update"})
    assert records == []
    assert explorer.stop_reason == "capability-unavailable"
    assert explorer.missing_capabilities[0]["capability"] == "actual-assistive-technology"


def test_orca_speech_parser_accepts_only_real_debug_utterances():
    import at_driver

    line = ("11:39:53.584710 - SPEECH OUTPUT: 'Current count 1' "
            "{'established': False, 'family': {'name': None}}")
    assert at_driver.parse_speech_line(line) == {
        "ts": "11:39:53.584710",
        "utterance": "Current count 1",
        "source": "orca-at-spi",
    }
    assert at_driver.parse_speech_line("SPEECH: Speak 'invented text'") is None
    assert at_driver.parse_speech_line("11:39:53.000000 - SPEECH OUTPUT: not-a-python-string") is None
    assert at_driver.node_reply_timeout_s({
        "cmd": "dwellLandmarks", "targets": ["Public", "Staff", "CEO", "Diagnostics"],
        "duration_s": 10,
    }) == 48.0
    assert at_driver.node_reply_timeout_s({
        "cmd": "tabTraverse", "pace_ms": 300,
    }) == 65.0
    assert at_driver.node_reply_timeout_s({"cmd": "click"}) == 30.0


def test_locally_available_actual_at_is_activated_before_paid_action(monkeypatch):
    import qa_explorer

    class Bridge:
        actual_at = False
        available = False

        def state(self):
            return {"url": "http://app", "elements": [],
                    "actualAssistiveTechnologyAvailable": self.available}

    bridge = Bridge()
    explorer = qa_explorer.Explorer("http://app", "accessible counter", autostart=False)
    explorer.bridge = bridge
    explorer._ai_coverage_plan = lambda *_: [
        {"aspect": "updated count screen-reader announcement", "covered": False, "explicit": True}]
    monkeypatch.setattr(qa_explorer, "_actual_at_driver_facts",
                        lambda: {"available": True, "driver": "orca", "missing": []})
    activations = []

    def activate():
        activations.append("orca")
        bridge.available = True
        bridge.actual_at = True

    explorer._activate_actual_at = activate
    explorer._ai_decide = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        qa_explorer.ModelDecisionUnavailable("paid decision deliberately not invoked in unit test"))

    assert explorer.explore({"title": "AT output", "expected": "spoken update"}) == []
    assert activations == ["orca"]
    assert explorer.missing_capabilities == []
    assert explorer.stop_reason == "model-infrastructure-incomplete"


def test_fully_covered_resume_closes_without_paid_decision_or_browser_action():
    """A provider failure after the final proof must not make the gap-fill repeat completed work."""
    import qa_explorer

    class Bridge:
        def state(self):
            return {"url": "http://app", "title": "app", "bodyText": "approved",
                    "elements": [{"idx": 0, "label": "Approved", "role": "status"}]}

    explorer = qa_explorer.Explorer("http://app", "approval workflow", autostart=False)
    explorer.bridge = Bridge()
    explorer._ai_decide = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a complete durable ledger must not make another paid decision"))
    explorer._begin_recorder_scope = lambda: (_ for _ in ()).throw(
        AssertionError("a complete durable ledger must not start another browser recording"))
    checkpoints = []
    explorer._checkpoint = lambda _story, records: checkpoints.append(list(records))

    records = explorer.explore(
        {"id": "US-009", "title": "approve follow-up"},
        resume_coverage=[{"aspect": "approval ticket persisted", "covered": True,
                          "explicit": True}],
        resume_steps_detail=[{"action": "approve", "verdict": "match",
                              "covers": ["approval ticket persisted"]}],
    )

    assert records == []
    assert explorer.stop_reason == "coverage-complete"
    assert checkpoints == [[]]


def test_inconclusive_batch_preserves_exact_grounded_partial_coverage():
    """A broad batch expectation must not erase the exact early clause its receipt proved."""
    import qa_explorer

    aspect = "Story step 1.1: Reload the restored page once"

    class Bridge:
        def state(self, **_kwargs):
            return {"url": "http://app", "title": "app", "bodyText": "restored state",
                    "viewportText": "restored state", "statusText": "ready", "elements": [],
                    "console_errors": [], "recent_requests": []}

        def act(self, action):
            assert action == {"cmd": "reload"}
            return {"ok": True, "effect": True, "reloaded": True}

    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    explorer.bridge = Bridge()
    explorer._ai_decide = lambda *_args, **_kwargs: {
        "next_action": {"cmd": "reload"}, "expected": "all restored checks pass",
        "covers": [], "expected_control": "", "wait_for": None, "done": False,
    }
    explorer._ai_evaluate = lambda *_args, **_kwargs: {
        "matches_expected": False, "verdict": "inconclusive", "target_confirmed": True,
        "bug": None, "severity": "none", "blocking": False, "demonstrated": [aspect],
        "model_failed": False, "infrastructure_error": None, "_raw": None,
    }
    explorer._checkpoint = lambda *_args, **_kwargs: None

    explorer.explore(
        {"id": "US-010", "title": "restored diagnostic"}, max_steps=1,
        resume_coverage=[{"aspect": aspect, "covered": False, "explicit": True}],
    )

    assert explorer.coverage[0]["aspect"] == aspect
    assert explorer.coverage[0]["covered"] is True
    assert explorer.coverage[0]["explicit"] is True
    assert explorer.coverage[0]["proof"]["action_kind"] == "reload"


def test_novel_dom_views_do_not_hide_acceptance_coverage_stagnation(monkeypatch):
    """A changing validation view is browser liveness, not proof that the story is converging."""
    import qa_explorer

    aspect = "Every invalid-input case is blocked and leaves zero persisted records"

    class Bridge:
        count = 0

        def state(self, **_kwargs):
            return {"url": "http://app", "title": "app", "bodyText": f"error {self.count}",
                    "viewportText": f"error {self.count}", "statusText": f"case {self.count}",
                    "elements": [], "console_errors": [], "recent_requests": []}

        def act(self, action):
            assert action == {"cmd": "reload"}
            self.count += 1
            return {"ok": True, "effect": True, "reloaded": True}

    explorer = qa_explorer.Explorer("http://app", "vision", autostart=False)
    explorer.bridge = Bridge()
    explorer._ai_decide = lambda *_args, **_kwargs: {
        "next_action": {"cmd": "reload"}, "expected": "acceptance closes",
        "covers": [], "expected_control": "", "wait_for": None, "done": False,
    }
    explorer._ai_evaluate = lambda *_args, **_kwargs: {
        "matches_expected": True, "verdict": "pass", "target_confirmed": True,
        "bug": None, "severity": "none", "blocking": False, "demonstrated": [],
        "model_failed": False, "infrastructure_error": None, "_raw": None,
    }
    explorer._ai_incomplete_diagnosis = lambda *_args, **_kwargs: {
        "disposition": "app_defect", "bug": "responsive views never proved acceptance",
        "severity": "high", "blocking": True, "demonstrated": [],
        "reason": "two distinct views produced no grounded coverage",
    }
    explorer._checkpoint = lambda *_args, **_kwargs: None
    monkeypatch.setattr(qa_explorer, "_STALL_LIMIT", 2)
    monkeypatch.setattr(qa_explorer, "_DEAD_LIMIT", 99)

    records = explorer.explore(
        {"id": "US-004", "title": "invalid matrix"},
        resume_coverage=[{"aspect": aspect, "covered": False, "explicit": True}],
    )

    assert explorer.bridge.count == 2
    assert explorer.stop_reason == "diagnosed-actionable-finding"
    assert explorer.coverage[0]["covered"] is False
    assert records[-1]["action"]["cmd"] == "diagnose_incomplete"


def test_story_contract_cannot_lose_real_at_requirement_in_vague_coverage(monkeypatch):
    import qa_explorer

    story = {
        "title": "Reload and re-enter the counter",
        "steps": ["Reload, then click Increment while the real Orca assistive-technology driver is active"],
        "expected": "Capture the actual screen-reader announcement after re-entry",
    }
    missing = qa_explorer._missing_required_capabilities(
        [{"aspect": "post-reload AT event sequence"}],
        {"actualAssistiveTechnologyAvailable": False}, story=story)
    assert missing and missing[0]["capability"] == "actual-assistive-technology"
    assert any("Orca" in aspect for aspect in missing[0]["aspects"])


def test_orca_wait_targets_are_derived_but_never_promoted_to_evidence():
    import at_driver

    response = {"accessibilityRegions": [
        {"tag": "output", "role": "status", "labelText": "Current count", "text": "2"},
        {"tag": "div", "text": "not a live region"},
    ]}
    assert at_driver.live_region_phrases(response) == ["Current count 2"]
    assert at_driver._utterance_matches("Current count 2", "Current count 2") is True
    assert at_driver._utterance_matches("Unrelated value 2", "2") is False
    assert "actualAssistiveTechnologyEvents" not in response


def test_orca_debug_stream_is_visible_before_process_exit(tmp_path):
    target = tmp_path / "orca-debug.log"
    env = dict(os.environ)
    env["AOS_ORCA_DEBUG_PATH"] = str(target)
    env["PYTHONPATH"] = str(ROOT / "scripts" / "qa" / "orca_runtime")
    proc = subprocess.Popen([
        sys.executable, "-c",
        "import os,time; f=open(os.environ['AOS_ORCA_DEBUG_PATH'],'w'); "
        "f.write('SPEECH OUTPUT: real\\n'); time.sleep(3)",
    ], env=env)
    try:
        deadline = time.monotonic() + 1.5
        observed = ""
        while time.monotonic() < deadline:
            try:
                observed = target.read_text()
            except OSError:
                observed = ""
            if "SPEECH OUTPUT: real" in observed:
                break
            time.sleep(0.05)
        assert "SPEECH OUTPUT: real" in observed
        assert proc.poll() is None, "proof must be visible while Orca is still running"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_responsive_decision_prompt_renders_without_interpolating_example_names():
    import qa_explorer

    prompt = qa_explorer._decide_prompt(
        "vision", {"title": "Responsive view", "steps": ["Open mobile"], "expected": "readable"},
        {"url": "http://app", "viewport": {"width": 1280, "height": 800}, "elements": []}, [],
        [{"aspect": "Open mobile", "covered": False}],
    )

    assert '{"width": 390, "height": 844}' in prompt
    assert "{width,height} for viewport" in prompt


def test_tab_with_expected_target_stays_page_level_focus_navigation():
    import qa_explorer

    explorer = qa_explorer.Explorer("http://app", vision="accessible counter", autostart=False)
    elements = [{"idx": 0, "tag": "button", "role": "button", "text": "Increment"}]
    action, aim = explorer._prepare_action(
        {"cmd": "press", "target_text": "Increment", "role": "button", "value": "Tab"}, elements)
    assert action["value"] == "Tab" and "idx" not in action and "selector" not in action
    assert aim["intended"] == "Increment" and aim["resolved_label"] == "Increment"
    assert aim["control_action"] is False

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {"ok": True}
    bridge.act(action)
    assert sent == [{"cmd": "press", "idx": None, "selector": None, "key": "Tab"}]

    reverse, reverse_aim = explorer._prepare_action(
        {"cmd": "press", "target_text": "Increment", "value": "Shift+Tab"}, elements)
    assert reverse["value"] == "Shift+Tab" and "idx" not in reverse
    assert reverse_aim["control_action"] is False


def test_batched_tab_traversal_routes_once_and_exposes_the_receipt_to_the_judge():
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("http://app", autostart=False)
    sent = []
    bridge._send = lambda payload: sent.append(payload) or {
        "ok": True, "traversal": True, "count": 3, "unique_controls": 3,
        "all_focus_visible": True, "horizontal_overflow_seen": False,
        "sequence": [{"order": 1, "label": "Name", "focusVisible": True}],
    }
    result = bridge.act({"cmd": "traverse", "value": "backward", "count": 3})

    assert result["traversal"] is True
    assert sent == [{"cmd": "tabTraverse", "direction": "backward", "count": 3}]
    explorer = qa_explorer.Explorer("http://app", vision="accessible app", autostart=False)
    _action, aim = explorer._prepare_action(
        {"cmd": "traverse", "value": "forward"}, [])
    targeting = explorer._targeting_facts(
        aim, result, {"elements": []}, {"elements": [], "settled": True}, settled=True)
    rendered = qa_explorer._fmt_targeting(targeting)
    assert targeting["control_action"] is False
    assert targeting["traversal_summary"]["unique_controls"] == 3
    assert "mechanical full keyboard-traversal receipt" in rendered
    assert '"label": "Name"' in rendered


def test_browser_bridge_closes_its_owned_browser_when_parent_stdin_disappears():
    script = r"""
const { runBridge } = require('./scripts/qa/browser_bridge.js');
runBridge({
  start: async () => {},
  close: async () => { process.stderr.write('BRIDGE_CLOSED_ON_EOF\n'); },
  handle: async () => ({})
});
"""

    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, input="", text=True,
        capture_output=True, timeout=5, check=False)

    assert completed.returncode == 0, completed.stderr
    assert "BRIDGE_CLOSED_ON_EOF" in completed.stderr
