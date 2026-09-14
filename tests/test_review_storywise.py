import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review


def test_multi_story_audit_uses_complete_focused_juries(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "run-input.json").write_text(json.dumps({"vision": "two complete journeys"}))
    (evidence / "coverage.json").write_text(json.dumps([
        {"story": "US-1", "stop_reason": "coverage-complete", "tested": ["alpha-only"],
         "yet_to_test": []},
        {"story": "US-2", "stop_reason": "coverage-complete", "tested": ["beta-only"],
         "yet_to_test": []},
    ]))
    (evidence / "run-final.json").write_text(json.dumps({"stories": [
        {"id": "US-1", "status": "passed", "steps": [{"reasoning": "alpha-only",
         "action": "alpha action", "actual": "alpha result", "covers": ["alpha-only"]}]},
        {"id": "US-2", "status": "passed", "steps": [{"reasoning": "beta-only",
         "action": "beta action", "actual": "beta result", "covers": ["beta-only"]}]},
    ]}))
    prompts = []

    def fake_review(_evidence_dir, focused, _rubric, lens):
        prompts.append((focused, lens))
        return {"passed_audit": True, "score": 9, "lens": lens, "skipped_flows": [],
                "unbacked_claims": [], "vague_reporting": [], "evidence_gaps": [],
                "summary": "accepted", "recommendation": "accept"}

    monkeypatch.setattr(review, "_one_review", fake_review)
    monkeypatch.setenv("AOS_AUDITOR_PARALLEL", "3")
    verdict = review.review(evidence, write=False, ensemble=3)

    assert verdict["passed_audit"] is True
    assert verdict["audit_strategy"] == "story-specific-unanimous-jury"
    assert verdict["jurors_per_story"] == 3
    assert len(prompts) == 6
    us1 = [prompt for prompt, _lens in prompts if "Complete step-by-step evidence for US-1" in prompt]
    us2 = [prompt for prompt, _lens in prompts if "Complete step-by-step evidence for US-2" in prompt]
    assert len(us1) == len(us2) == 3
    assert all("alpha action" in prompt and "beta action" not in prompt for prompt in us1)
    assert all("beta action" in prompt and "alpha action" not in prompt for prompt in us2)
    assert all("US-1: stop=" in prompt and "US-2: stop=" in prompt for prompt, _lens in prompts)


def test_any_focused_juror_dissent_blocks_the_whole_release(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "run-input.json").write_text(json.dumps({"vision": "one"}))
    (evidence / "coverage.json").write_text(json.dumps([
        {"story": "US-1", "stop_reason": "coverage-complete", "tested": ["flow"], "yet_to_test": []},
        {"story": "US-2", "stop_reason": "coverage-complete", "tested": ["flow"], "yet_to_test": []},
    ]))
    (evidence / "run-final.json").write_text(json.dumps({"stories": [
        {"id": "US-1", "status": "passed", "steps": []},
        {"id": "US-2", "status": "passed", "steps": []},
    ]}))

    def fake_review(_evidence_dir, focused, _rubric, lens):
        reject = "US-2" in focused and lens == "evidence"
        return {"passed_audit": not reject, "score": 4 if reject else 9, "lens": lens,
                "skipped_flows": ["missing recovery"] if reject else [], "unbacked_claims": [],
                "vague_reporting": [], "evidence_gaps": [], "summary": "reviewed",
                "recommendation": "reject" if reject else "accept"}

    monkeypatch.setattr(review, "_one_review", fake_review)
    verdict = review.review(evidence, write=False, ensemble=3)

    assert verdict["passed_audit"] is False
    assert verdict["close_call"] is True
    assert "[US-2] missing recovery" in verdict["skipped_flows"]
    assert verdict["recommendation"] == "redo-specific-flows"


def test_skeptic_rejection_skips_redundant_jurors_without_weakening_release_gate(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "run-input.json").write_text(json.dumps({"vision": "two stories"}))
    (evidence / "coverage.json").write_text(json.dumps([
        {"story": "US-1", "stop_reason": "coverage-complete", "tested": ["flow"], "yet_to_test": []},
        {"story": "US-2", "stop_reason": "coverage-complete", "tested": ["flow"], "yet_to_test": []},
    ]))
    (evidence / "run-final.json").write_text(json.dumps({"stories": [
        {"id": "US-1", "status": "passed", "steps": []},
        {"id": "US-2", "status": "passed", "steps": []},
    ]}))
    calls = []

    def reject(_evidence_dir, focused, _rubric, lens):
        calls.append((focused, lens))
        return {"passed_audit": False, "score": 2, "lens": lens,
                "skipped_flows": ["proof missing"], "unbacked_claims": [],
                "vague_reporting": [], "evidence_gaps": [], "summary": "reject",
                "recommendation": "reject"}

    monkeypatch.setattr(review, "_one_review", reject)
    verdict = review.review(evidence, write=False, ensemble=3)

    assert verdict["passed_audit"] is False
    assert len(calls) == 2
    assert all(lens == "skeptic" for _focused, lens in calls)
    assert verdict["juror_decisions"] == 2


def test_completed_story_jurors_resume_from_exact_evidence_checkpoint(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "run-input.json").write_text(json.dumps({"vision": "one story"}))
    (evidence / "coverage.json").write_text(json.dumps([
        {"story": "US-1", "stop_reason": "coverage-complete", "tested": ["flow"], "yet_to_test": []},
        {"story": "US-2", "stop_reason": "coverage-complete", "tested": ["flow"], "yet_to_test": []},
    ]))
    (evidence / "run-final.json").write_text(json.dumps({"stories": [
        {"id": "US-1", "status": "passed", "steps": []},
        {"id": "US-2", "status": "passed", "steps": []},
    ]}))
    calls = []

    def accept(_evidence_dir, _focused, _rubric, lens):
        calls.append(lens)
        return {"passed_audit": True, "score": 9, "lens": lens,
                "skipped_flows": [], "unbacked_claims": [], "vague_reporting": [],
                "evidence_gaps": [], "summary": "accept", "recommendation": "accept"}

    monkeypatch.setattr(review, "_one_review", accept)
    first = review.review(evidence, write=True, ensemble=3)
    assert first["passed_audit"] is True and len(calls) == 6
    assert (evidence / "audit-checkpoint.json").exists()

    second = review.review(evidence, write=True, ensemble=3)
    assert second["passed_audit"] is True
    assert len(calls) == 6
    assert second["checkpoint_reused"] == 6
