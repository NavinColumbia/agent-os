from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))


def test_legacy_unsealed_finding_requests_fresh_evidence_without_model_or_mutation(monkeypatch, tmp_path):
    import tools
    import qareview

    monkeypatch.setattr(tools, "_apply_tenant_ctx", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(qareview, "submit", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ValueError("finding-time evidence_provenance is required")))

    out = tools.qa_review({
        "tenant": "t", "repo": str(tmp_path), "_run_id": 9,
        "internal_review": {"review_id": "qa-review-1", "story": "US-1",
                            "finding": {"story": "US-1", "title": "legacy"}},
    })

    assert out["status"] == "done"
    assert out["result"]["fresh_evidence_required"] is True
    assert out["result"]["disposition"] is None


def test_explorer_findings_have_stable_content_addressed_ids():
    source = (ROOT / "scripts" / "orchestra" / "tools.py").read_text()
    assert 'item["finding_id"] = "qaf-"' in source
    assert 'identity["manifest_sha256"]' in source


def test_case_bound_review_claims_the_submitted_case_not_the_tenant_queue(monkeypatch, tmp_path):
    import qareview
    import tools

    claimed = []
    monkeypatch.setattr(tools, "_apply_tenant_ctx", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(qareview, "submit", lambda *_args, **_kwargs: {"case_id": "case-b"})
    monkeypatch.setattr(qareview, "get", lambda *_args: {"case_id": "case-b", "outcome": None})
    monkeypatch.setattr(qareview, "claim", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("case-bound actor must not claim the tenant's oldest arbitrary case")))

    def claim_case(tenant, case_id, claimant, **_kwargs):
        claimed.append((tenant, case_id, claimant))
        return {"case_id": case_id, "lease_token": "token-b"}

    monkeypatch.setattr(qareview, "claim_case", claim_case)
    monkeypatch.setattr(qareview, "adjudicate", lambda tenant, case_id, token, **_kwargs: {
        "case_id": case_id, "status": "resolved", "disposition": "verified_false_positive"})
    out = tools.qa_review({
        "tenant": "tenant-two-cases", "repo": str(tmp_path), "_run_id": 9,
        "internal_review": {"review_id": "review-b", "story": "US-2", "finding": {"story": "US-2"}},
    })

    assert len(claimed) == 1
    assert claimed[0][0:2] == ("tenant-two-cases", "case-b")
    assert claimed[0][2].startswith("qa-review:")
    assert out["result"]["case_id"] == "case-b"


def test_dev_fix_accepts_only_tenant_fenced_confirmed_adjudication(monkeypatch, tmp_path):
    import dev_loop
    import qareview
    import tools

    captured = {}
    monkeypatch.setattr(tools, "_apply_tenant_ctx", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(qareview, "get", lambda tenant, case_id: {
        "case_id": case_id, "review_id": "review-confirmed", "status": "resolved",
        "finding_id": "qaf-confirmed", "outcome": {
            "disposition": "confirmed_defect", "confidence": .99, "state_generation": 1}})

    def fake_fix(*_args, **kwargs):
        captured.update(kwargs)
        return {"fixed": True, "files": ["app.js"]}

    monkeypatch.setattr(dev_loop, "fix_bug", fake_fix)
    out = tools.dev_fix({
        "tenant": "tenant-a", "repo": str(tmp_path), "target_url": "http://app",
        "vision": "vision", "stories": [{"id": "US-1"}],
        "resume_triage_finding": {"finding_id": "fresh-current", "story": "US-1"},
        "resume_triage_receipt": {"finding_fingerprint": "exact-receipt"},
        "bug": {"story": "US-1", "finding_id": "qaf-confirmed",
                "_qa_adjudication": {"case_id": "case-confirmed",
                                      "review_id": "review-confirmed"}},
    })

    assert out["status"] == "done"
    assert captured["adjudication"]["disposition"] == "confirmed_defect"
    assert captured["adjudication"]["case_id"] == "case-confirmed"
    assert captured["resume_triage_finding"] == {
        "finding_id": "fresh-current", "story": "US-1"}
    assert captured["resume_triage_receipt"] == {"finding_fingerprint": "exact-receipt"}
