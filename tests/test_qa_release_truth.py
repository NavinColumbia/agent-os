import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "qa"))

import qa_agentic


def test_open_findings_can_never_be_described_as_all_clear():
    text = qa_agentic._release_cleanliness_verdict(
        "AGENTIC QA — COVERAGE COMPLETE: 12 stories",
        [
            {"severity": "critical", "blocking": True},
            {"severity": "high", "blocking": False},
        ],
        clean=False,
    )

    assert "RELEASE NOT CLEAN" in text
    assert "ALL CLEAR" not in text
    assert "2 unresolved" in text
    assert "1 critical" in text and "1 high" in text
    assert "Shipping and delivery remain held" in text


def test_clean_coverage_gets_the_all_clear_headline():
    text = qa_agentic._release_cleanliness_verdict(
        "AGENTIC QA — COVERAGE COMPLETE: 12 stories", [], clean=True)

    assert text == "AGENTIC QA — ALL CLEAR: 12 stories"


def _actor(actor_id, *, focused=False):
    return {
        "actor_id": actor_id,
        "role": "qa-explorer",
        "memory": {"context": {"tool_args": ({"_qa_review_id": "review-1"} if focused else {})}},
    }


def _finding(event_id=10, actor_id=1):
    return {
        "id": event_id,
        "kind": "finding",
        "frm": actor_id,
        "payload": {
            "finding_id": "finding-1",
            "kind": "bug",
            "story": "US-1",
            "title": "old observation",
            "severity": "medium",
            "blocking": False,
        },
    }


def _clean_done(event_id=20, actor_id=2):
    return {
        "id": event_id,
        "kind": "done",
        "frm": actor_id,
        "payload": {
            "story": "US-1",
            "findings_count": 0,
            "result": {
                "story": "US-1",
                "stop_reason": "coverage-complete",
                "bugs": 0,
                "coverage": [{"aspect": "complete journey", "covered": True}],
                "steps_detail": [{"action": "submit", "verdict": "match",
                                  "covers": ["complete journey"]}],
            },
        },
    }


def test_later_full_story_proof_supersedes_nonblocking_history_without_deleting_it():
    findings, open_findings = qa_agentic._reconcile_historical_findings(
        [_finding(), _clean_done()], [_actor(1), _actor(2)], [], {"US-1": "clean"},
        revision_matches=True)

    assert open_findings == []
    assert len(findings) == 1
    assert findings[0]["fixed"] is True
    assert findings[0]["resolution_proof_event_id"] == 20
    assert "final product revision" in findings[0]["resolution"]


def test_story_proof_does_not_supersede_without_every_fence():
    scenarios = [
        ([_clean_done(event_id=5), _finding(event_id=10)], [_actor(1), _actor(2)], True),
        ([_finding(), _clean_done()], [_actor(1), _actor(2)], False),
        ([_finding(), _clean_done()], [_actor(1), _actor(2, focused=True)], True),
        ([_finding(actor_id=2), _clean_done(actor_id=2)], [_actor(2)], True),
    ]

    for events, actors, revision_matches in scenarios:
        findings, open_findings = qa_agentic._reconcile_historical_findings(
            events, actors, [], {"US-1": "clean"}, revision_matches=revision_matches)
        assert len(findings) == 1
        assert len(open_findings) == 1
        assert not findings[0].get("fixed")


def test_current_revision_disposition_is_a_terminal_exact_resolution():
    findings, open_findings = qa_agentic._reconcile_historical_findings(
        [_finding()], [_actor(1)], [{
            "finding_id": "finding-1",
            "story": "US-1",
            "title": "old observation",
            "disposition": "superseded_by_current_revision",
        }], {"US-1": "clean"}, revision_matches=False)

    assert open_findings == []
    assert findings[0]["resolved"] is True


def test_exact_false_positive_resolution_compensates_immutable_explorer_bug_count():
    result = dict(_clean_done(actor_id=7)["payload"]["result"], bugs=1)
    event = _finding(actor_id=7)
    resolution = {
        "finding_id": "finding-1",
        "story": "US-1",
        "disposition": "verified_false_positive",
    }

    compensated = qa_agentic._result_with_resolved_findings(
        result, 7, [event], [resolution])

    assert compensated["bugs"] == 0
    assert compensated["resolved_false_positive_findings"] == ["finding-1"]
    assert compensated["finding_compensation"] == \
        "exact-durable-false-positive-adjudication"


def test_result_compensation_fails_closed_for_nonexact_or_supersession_dispositions():
    result = dict(_clean_done(actor_id=7)["payload"]["result"], bugs=1)
    event = _finding(actor_id=7)

    for resolution in (
            {"finding_id": "other", "story": "US-1",
             "disposition": "verified_false_positive"},
            {"finding_id": "finding-1", "story": "US-1",
             "disposition": "superseded_by_current_revision"},
            {"finding_id": "finding-1", "story": "US-2",
             "disposition": "verified_false_positive"}):
        unchanged = qa_agentic._result_with_resolved_findings(
            result, 7, [event], [resolution])
        assert unchanged["bugs"] == 1
        assert "finding_compensation" not in unchanged


def test_compensated_complete_story_proof_supersedes_older_history():
    old = _finding(event_id=10, actor_id=1)
    current_finding = {
        "id": 19,
        "kind": "finding",
        "frm": 2,
        "payload": {
            "finding_id": "finding-current",
            "story": "US-1",
            "title": "current observation later adjudicated false",
            "blocking": True,
        },
    }
    current_done = _clean_done(event_id=20, actor_id=2)
    current_done["payload"]["findings_count"] = 1
    current_done["payload"]["result"]["bugs"] = 1
    resolutions = [{
        "finding_id": "finding-current",
        "story": "US-1",
        "disposition": "verified_false_positive",
    }]

    findings, open_findings = qa_agentic._reconcile_historical_findings(
        [old, current_finding, current_done], [_actor(1), _actor(2)], resolutions,
        {"US-1": "clean"}, revision_matches=True)

    assert open_findings == []
    assert findings[0]["fixed"] is True
    assert findings[0]["resolution_proof_event_id"] == 20
    assert findings[1]["resolved"] is True
