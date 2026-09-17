import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qareview


def _sealed_review(tmp_path, suffix=""):
    repo = tmp_path / f"repo{suffix}"
    repo.mkdir()
    source = repo / "contract.py"
    source.write_text("def retries():\n    return 3\n")
    files = {"contract.py": hashlib.sha256(source.read_bytes()).hexdigest()}
    manifest = {"version": 1, "captured_at": 1.0, "repo": str(repo.resolve()),
                "git_head": None, "files": files}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / f"finding-repo-provenance-{digest[:20]}.json"
    path.write_bytes(raw)
    citation = {"path": "contract.py", "start_line": 1, "end_line": 2,
                "quote": "def retries():\n    return 3"}
    record = {"review_id": f"qa-review-{uuid.uuid4().hex}", "route": "qa-internal-management",
              "state": "pending_internal_management", "story": "US-1",
              "finding": {"story": "US-1", "bug": "retry behavior disputed",
                          "evidence_provenance": {"manifest_path": str(path),
                                                  "manifest_sha256": digest}},
              "triage": {"reviews": [{"verdict": "defect", "citations": [citation]}]},
              "reason": "reviewers disagreed"}
    return repo, record


@pytest.fixture
def durable_case(tmp_path):
    tenant = f"qareview-{uuid.uuid4().hex}"
    repo, record = _sealed_review(tmp_path)
    qareview.ensure()
    submitted = qareview.submit(tenant, record, repo=repo, thread_id=8)
    yield tenant, repo, record, submitted
    with qareview.connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM qa_evidence_dispute_reviews WHERE tenant_id=%s", (tenant,))
        cur.execute("DELETE FROM qa_evidence_disputes WHERE tenant_id=%s", (tenant,))


def test_submit_is_stable_and_rejects_changed_finding_replay(durable_case):
    tenant, repo, record, first = durable_case
    again = qareview.submit(tenant, record, repo=repo, thread_id=8,
                            run_id=77, coordinator_actor_id=88)
    assert again["case_id"] == first["case_id"] and again["duplicate"] is True
    routed = qareview.get(tenant, first["case_id"])
    assert routed["run_id"] == 77 and routed["coordinator_actor_id"] == 88
    changed = json.loads(json.dumps(record))
    changed["finding"]["bug"] = "a genuinely different disputed observation"
    with pytest.raises(ValueError, match="different immutable content"):
        qareview.submit(tenant, changed, repo=repo, thread_id=8)


def test_submit_reuses_case_when_only_triage_and_related_rollup_changed(durable_case):
    tenant, repo, record, first = durable_case
    replay = json.loads(json.dumps(record))
    replay["triage"] = {"reviews": [{"verdict": "uncertain", "reason": "later retry"}]}
    replay["finding"]["related_findings"] = [{"finding_id": "related-new", "severity": "low"}]
    replay["finding"]["related_finding_count"] = 1
    replay["finding"]["exploration_blocking"] = False

    again = qareview.submit(tenant, replay, repo=repo, thread_id=8)

    assert again["case_id"] == first["case_id"] and again["duplicate"] is True


def test_submit_reuses_case_when_resolved_adjudication_is_attached_to_finding(durable_case):
    """A coordinator may replay the observation with this case's derived outcome attached."""
    tenant, repo, record, first = durable_case
    replay = json.loads(json.dumps(record))
    replay["finding"]["_qa_adjudication"] = {
        "case_id": first["case_id"], "review_id": first["review_id"],
        "disposition": "confirmed_defect", "confidence": 0.99,
        "state_generation": 1, "rationale": "sealed evidence confirms the defect",
    }

    again = qareview.submit(tenant, replay, repo=repo, thread_id=8)

    assert again["case_id"] == first["case_id"] and again["duplicate"] is True


def test_submit_reuses_case_when_routing_reason_and_workflow_state_advance(durable_case):
    """Revision re-verification changes routing prose, not the stable disputed observation."""
    tenant, repo, record, first = durable_case
    replay = json.loads(json.dumps(record))
    replay["reason"] = "fresh current-revision finding lacks a usable sealed provenance boundary"
    replay["state"] = "review_scheduled"
    replay["triage"] = {"disposition": "revision_reverify_required", "reviews": []}
    replay["finding"]["_qa_adjudication"] = {
        "case_id": first["case_id"], "review_id": first["review_id"],
        "disposition": "confirmed_defect", "confidence": 0.99,
    }

    again = qareview.submit(tenant, replay, repo=repo, thread_id=8)

    assert again["case_id"] == first["case_id"] and again["duplicate"] is True


def test_lease_fencing_and_state_change_reclaim(durable_case):
    tenant, _repo, _record, submitted = durable_case
    claim = qareview.claim(tenant, "review-worker", lease_s=60)
    assert claim["case_id"] == submitted["case_id"]
    changed = qareview.state_changed(tenant, submitted["case_id"], {"evidence": "new"})
    assert changed["changed"] is True and changed["state_generation"] == 2
    with pytest.raises(qareview.LeaseLost):
        qareview.adjudicate(tenant, submitted["case_id"], claim["lease_token"],
                            review_fn=lambda *_: {})
    replacement = qareview.claim(tenant, "replacement", lease_s=60)
    assert replacement and replacement["state_generation"] == 2


def test_revision_supersedes_stale_dispute_without_erasing_audit_history(durable_case):
    tenant, _repo, _record, submitted = durable_case

    resolved = qareview.supersede_by_revision(
        tenant, submitted["case_id"], "revision-current",
        reason="the staff surface changed and requires current-revision evidence")

    assert resolved["status"] == "resolved" and resolved["changed"] is True
    assert resolved["outcome"]["disposition"] == "superseded_by_current_revision"
    assert resolved["outcome"]["product_revision"] == "revision-current"
    assert qareview.attention_evidence(limit=500) == [] or all(
        item["case_id"] != submitted["case_id"] for item in qareview.attention_evidence(limit=500))
    replay = qareview.supersede_by_revision(tenant, submitted["case_id"], "revision-current")
    assert replay["changed"] is False


def test_claim_case_never_leases_an_older_different_case(tmp_path):
    tenant = f"qareview-multi-{uuid.uuid4().hex}"
    repo_a, record_a = _sealed_review(tmp_path, "-a")
    repo_b, record_b = _sealed_review(tmp_path, "-b")
    submitted = []
    try:
        qareview.ensure()
        case_a = qareview.submit(tenant, record_a, repo=repo_a, thread_id=8)
        case_b = qareview.submit(tenant, record_b, repo=repo_b, thread_id=8)
        submitted.extend((case_a["case_id"], case_b["case_id"]))

        lease_b = qareview.claim_case(tenant, case_b["case_id"], "case-b-worker", lease_s=60)
        assert lease_b and lease_b["case_id"] == case_b["case_id"]
        lease_a = qareview.claim_case(tenant, case_a["case_id"], "case-a-worker", lease_s=60)
        assert lease_a and lease_a["case_id"] == case_a["case_id"]
        assert lease_a["lease_token"] != lease_b["lease_token"]
    finally:
        with qareview.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM qa_evidence_dispute_reviews WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM qa_evidence_disputes WHERE tenant_id=%s", (tenant,))


def test_same_semantic_state_does_not_revoke_lease_for_a_different_observer(durable_case):
    tenant, _repo, _record, submitted = durable_case
    first = qareview.state_changed(tenant, submitted["case_id"], {"revision": "abc"}, actor="worker")
    second = qareview.state_changed(tenant, submitted["case_id"], {"revision": "abc"}, actor="manager")
    assert first["changed"] is True and second["changed"] is False
    assert second["state_generation"] == first["state_generation"]


def test_senior_confirms_defect_from_sealed_evidence_and_replay_is_idempotent(durable_case):
    tenant, _repo, _record, submitted = durable_case
    claim = qareview.claim(tenant, "review-worker", lease_s=60)
    calls = []

    def reviewer(role, payload):
        calls.append(role)
        return {"verdict": "confirmed_defect", "confidence": 0.96,
                "rationale": "sealed contract requires three retries",
                "evidence_ids": [payload["sealed_evidence"][0]["evidence_id"]]}

    out = qareview.adjudicate(tenant, submitted["case_id"], claim["lease_token"], review_fn=reviewer)
    assert out["disposition"] == "confirmed_defect" and out["decided_by"] == qareview.ROLES[-1]
    assert calls == list(qareview.ROLES)
    replay = qareview.adjudicate(tenant, submitted["case_id"], "stale", review_fn=reviewer)
    assert replay["duplicate"] is True and replay["disposition"] == "confirmed_defect"
    assert calls == list(qareview.ROLES)


def test_model_reviewer_interprets_broad_population_language_causally(monkeypatch):
    import factory

    prompts = []
    monkeypatch.setattr(factory, "agent", lambda _role, _repo, prompt, **_kwargs: (
        prompts.append(prompt) or {"rc": 0, "out_full": '{"verdict":"uncertain"}'}))

    qareview._model_review("senior-qa-director", {"sealed_evidence": []})

    normalized = " ".join(prompts[0].split()) if prompts else ""
    assert "broad phrase such as \"all panels become populated\"" in normalized
    assert "when no story step created or submitted that entity" in normalized


def test_terminal_adjudication_wakes_the_exact_parked_runtime(monkeypatch, durable_case):
    tenant, _repo, _record, submitted = durable_case
    claim = qareview.claim(tenant, "review-worker", lease_s=60)
    wakes = []
    monkeypatch.setattr(qareview, "_wake_runtime", lambda tid, cid, state: (
        wakes.append((tid, cid, state)) or {"woken": True, "controller": True}))

    out = qareview.adjudicate(
        tenant, submitted["case_id"], claim["lease_token"],
        review_fn=lambda _role, payload: {
            "verdict": "confirmed_defect", "confidence": 0.99,
            "rationale": "sealed evidence proves the mismatch",
            "evidence_ids": [payload["sealed_evidence"][0]["evidence_id"]],
        })

    assert out["status"] == "resolved" and out["woken"] is True
    assert len(wakes) == 1
    assert wakes[0][2]["trigger"] == "terminal_adjudication"
    replay = qareview.adjudicate(
        tenant, submitted["case_id"], "stale-token", review_fn=lambda *_args: {})
    assert replay["duplicate"] is True and replay["woken"] is True
    assert len(wakes) == 2 and wakes[1][2]["trigger"] == "terminal_adjudication_replay"


def test_changed_post_finding_file_cannot_authorize_false_positive(durable_case):
    tenant, repo, _record, submitted = durable_case
    (repo / "contract.py").write_text("def retries():\n    return 1\n")
    claim = qareview.claim(tenant, "review-worker", lease_s=60)

    def reviewer(_role, payload):
        return {"verdict": "verified_false_positive", "confidence": 1.0,
                "rationale": "assertion without sealed proof",
                "evidence_ids": ["invented"] if not payload["sealed_evidence"] else []}

    out = qareview.adjudicate(tenant, submitted["case_id"], claim["lease_token"],
                              review_fn=reviewer, retry_after_s=30)
    assert out["status"] == "manager_review" and out["disposition"] is None
    assert qareview.get(tenant, submitted["case_id"])["outcome"] is None


def test_sealed_citation_expands_only_to_manifest_bound_helper_dependencies(tmp_path):
    repo = tmp_path / "js-repo"
    repo.mkdir()
    source = repo / "form.js"
    source.write_text("""const form = [checkboxField(\"Consent text\", \"consent\")];

function checkboxField(label, name) {
  const id = `field-${name}`;
  return el(\"label\", { for: id }, [el(\"input\", { id }), el(\"span\", {}, [label])]);
}

function el(tag, attrs, children) {
  return { tag, attrs, children };
}
""")
    files = {"form.js": hashlib.sha256(source.read_bytes()).hexdigest()}
    manifest = {"version": 1, "repo": str(repo.resolve()), "files": files}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    manifest_path = tmp_path / f"finding-repo-provenance-{digest[:20]}.json"
    manifest_path.write_bytes(raw)
    review = {"review_id": "qa-review-context", "route": "qa-internal-management",
              "finding": {"evidence_provenance": {
                  "manifest_path": str(manifest_path), "manifest_sha256": digest}},
              "triage": {"reviews": [{"citations": [{
                  "path": "form.js", "start_line": 1, "end_line": 1,
                  "quote": 'const form = [checkboxField("Consent text", "consent")];'}]}]}}
    case = {"repo": str(repo), "internal_review": review,
            "internal_review_digest": qareview._digest(review)}

    evidence = qareview._sealed_evidence(case)

    contexts = {item.get("symbol"): item for item in evidence
                if item.get("kind") == "sealed_dependency_context"}
    assert set(contexts) == {"checkboxField", "el"}
    assert 'for: id' in contexts["checkboxField"]["quote"]
    assert all(item["file_sha256"] == files["form.js"] for item in evidence)
    source.write_text(source.read_text() + "// changed after finding\n")
    assert qareview._sealed_evidence(case) == []


def test_authoritative_story_contract_is_sealed_as_scope_evidence(durable_case):
    tenant, _repo, _record, submitted = durable_case
    state = {
        "authoritative_story_contract": {
            "id": "US-1", "steps": ["Show the current retry count."],
            "expected_outcome": "The current count is visible.",
        },
        "contract_authority": (
            "The source story defines scope; a disputed finding cannot add requirements."),
    }
    qareview.state_changed(tenant, submitted["case_id"], state)
    case = qareview._case(tenant, submitted["case_id"])

    evidence = qareview._sealed_evidence(case)

    contract = next(item for item in evidence
                    if item.get("kind") == "authoritative_story_contract")
    assert contract["story"] == "US-1"
    assert contract["contract"]["steps"] == ["Show the current retry count."]
    assert contract["state_digest"] == case["state_digest"]


def test_grounded_fresh_browser_step_is_sealed_redacted_and_artifact_bound(tmp_path):
    repo, review = _sealed_review(tmp_path)
    artifacts = tmp_path / "browser-artifacts"
    artifacts.mkdir()
    screenshot = artifacts / "step.png"
    screenshot.write_bytes(b"browser pixels")
    finding_state = artifacts / "finding-state.json"
    finding_state.write_text('{"cookies":[]}')
    state = {
        "trigger": "fresh_independent_verification", "product_revision": "revision-abc",
        "fresh_result": {
            "story": "US-1", "recovery_scope": "focused", "stop_reason": "actionable-finding",
            "artifact_dir": str(artifacts), "steps_detail": [{
                "action": "fill idx=4 ='alice@example.com API_SECRET=plainsecret123456'",
                "expected": "Reject alice@example.com and +1 (415) 555-0198",
                "actual": "Published Bearer eyJhbGciOiJIUzI1Ni.payload.signature for alice@example.com",
                "verdict": "mismatch", "covers": ["reject pasted credentials"],
                "coverage_grounded": True,
                "bug": {"title": "API_SECRET=plainsecret123456 was published",
                        "finding_state_path": str(finding_state)},
                "screenshot": str(screenshot),
            }]}}
    case = {"repo": str(repo), "internal_review": review,
            "internal_review_digest": qareview._digest(review), "state": state,
            "state_digest": qareview._digest(state)}

    evidence = qareview._sealed_evidence(case)

    receipt = next(item for item in evidence if item.get("kind") == "sealed_fresh_browser_step")
    rendered = json.dumps(receipt)
    human_readable = json.dumps({
        key: receipt.get(key)
        for key in ("action", "expected", "actual", "bug")
    })
    assert receipt["story"] == "US-1" and receipt["coverage_grounded"] is True
    assert receipt["covers"] == ["reject pasted credentials"]
    assert receipt["action"] == "fill idx=4"
    assert receipt["screenshot_sha256"] == hashlib.sha256(screenshot.read_bytes()).hexdigest()
    assert receipt["finding_state_sha256"] == hashlib.sha256(finding_state.read_bytes()).hexdigest()
    assert "alice@example.com" not in human_readable and "415" not in human_readable
    assert str(artifacts) not in human_readable
    assert "plainsecret123456" not in human_readable and "eyJhbGci" not in human_readable
    assert "EMAIL-REDACTED" in human_readable and "REDACTED" in human_readable


def test_fresh_browser_evidence_rejects_ungrounded_labels_and_artifact_escape(tmp_path):
    repo, review = _sealed_review(tmp_path)
    artifacts = tmp_path / "browser-artifacts"
    artifacts.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"must not be admitted")
    state = {"fresh_result": {"story": "US-1", "artifact_dir": str(artifacts), "steps_detail": [
        {"actual": "model says it passed", "covers": ["label only"],
         "coverage_grounded": False, "screenshot": str(outside)},
        {"actual": "browser-grounded outcome", "covers": ["real aspect"],
         "coverage_grounded": True, "screenshot": str(outside)},
    ]}}
    case = {"repo": str(repo), "internal_review": review,
            "internal_review_digest": qareview._digest(review), "state": state,
            "state_digest": qareview._digest(state)}

    receipts = [item for item in qareview._sealed_evidence(case)
                if item.get("kind") == "sealed_fresh_browser_step"]

    assert len(receipts) == 1 and receipts[0]["covers"] == ["real aspect"]
    assert "screenshot_sha256" not in receipts[0] and str(outside) not in json.dumps(receipts[0])


def test_review_payload_never_sends_raw_fresh_result_pii_or_secrets(monkeypatch, durable_case):
    tenant, _repo, _record, submitted = durable_case
    state = {"trigger": "fresh_independent_verification", "rationale": "call +1 415 555 0198",
             "fresh_result": {"story": "US-1", "steps_detail": [{
                 "actual": "alice@example.com Bearer eyJhbGciOiJIUzI1Ni.payload.signature",
                 "covers": ["runtime behavior"], "coverage_grounded": True}]}}
    qareview.state_changed(tenant, submitted["case_id"], state)
    claim = qareview.claim(tenant, "review-worker", lease_s=60)
    payloads = []

    def reviewer(_role, payload):
        payloads.append(payload)
        return {"verdict": "uncertain", "confidence": 0.1, "rationale": "more proof needed"}

    qareview.adjudicate(tenant, submitted["case_id"], claim["lease_token"], review_fn=reviewer)

    rendered = json.dumps(payloads)
    assert "alice@example.com" not in rendered and "eyJhbGci" not in rendered
    assert "415 555" not in rendered and "fresh_result" not in payloads[0]["state"]


def test_story_specific_prompt_prefers_contract_actionable_receipt_and_direct_sources():
    import qareview

    evidence = [
        {"evidence_id": "contract", "kind": "authoritative_story_contract"},
        *[{"evidence_id": f"setup-{index}", "kind": "sealed_fresh_browser_step",
           "verdict": "matches_expected", "bug": ""} for index in range(12)],
        {"evidence_id": "mismatch", "kind": "sealed_fresh_browser_step",
         "verdict": "mismatch", "bug": "credential published"},
        *[{"evidence_id": f"source-{index}", "path": f"src/{index}.js"} for index in range(4)],
        *[{"evidence_id": f"context-{index}", "kind": "sealed_dependency_context"}
          for index in range(4)],
    ]

    selected = qareview._prompt_evidence(evidence)
    selected_ids = [item["evidence_id"] for item in selected]
    assert len(selected) == qareview.MAX_PROMPT_EVIDENCE
    assert selected_ids[:2] == ["contract", "mismatch"]
    assert set(f"source-{index}" for index in range(4)).issubset(selected_ids)
    assert not any(item.startswith("context-") for item in selected_ids)


def test_uncertainty_escalates_all_managers_without_ceo_gate(durable_case):
    tenant, _repo, _record, submitted = durable_case
    claim = qareview.claim(tenant, "review-worker", lease_s=60)
    calls = []

    def reviewer(role, _payload):
        calls.append(role)
        return {"verdict": "uncertain", "confidence": 0.2, "rationale": "insufficient evidence"}

    out = qareview.adjudicate(tenant, submitted["case_id"], claim["lease_token"], review_fn=reviewer)
    assert calls == list(qareview.ROLES)
    assert out["status"] == "manager_review" and out["disposition"] is None
    assert "CEO" not in json.dumps(out)
    assert qareview.claim(tenant, "poller", lease_s=60) is None, (
        "unchanged uncertainty must not replay the same cached management meeting")


def test_manager_instruction_fences_collection_until_fresh_evidence_arrives(durable_case):
    tenant, _repo, _record, submitted = durable_case
    collection = qareview.begin_evidence_collection(
        tenant, submitted["case_id"], {"action": "rebrief", "management_case_id": "mc-1"})
    assert collection["status"] == "manager_review" and collection["evidence_collection"] is True
    assert qareview.claim(tenant, "unchanged-review", lease_s=60) is None
    assert not any(item["case_id"] == submitted["case_id"]
                   for item in qareview.attention_evidence(limit=500))

    changed = qareview.state_changed(
        tenant, submitted["case_id"], {"trigger": "fresh_independent_verification",
                                        "artifact": "sealed-new-result"})
    assert changed["status"] == "pending"
    assert qareview.claim(tenant, "fresh-review", lease_s=60)["case_id"] == submitted["case_id"]


def test_cancellation_releases_the_lease_without_recording_a_fake_uncertain_review(durable_case):
    tenant, _repo, _record, submitted = durable_case
    claim = qareview.claim(tenant, "review-worker", lease_s=60)
    calls = []
    out = qareview.adjudicate(
        tenant, submitted["case_id"], claim["lease_token"],
        review_fn=lambda *_args: calls.append(1) or {}, should_stop=lambda: True)
    assert out["checkpoint_required"] is True and out["status"] == "pending"
    assert calls == []
    replacement = qareview.claim(tenant, "replacement", lease_s=60)
    assert replacement and replacement["case_id"] == submitted["case_id"]


def test_management_state_change_wakes_only_after_the_case_generation_changes(monkeypatch, durable_case):
    tenant, _repo, _record, submitted = durable_case
    wakes = []
    monkeypatch.setattr(qareview, "_wake_runtime",
                        lambda tid, cid, state: wakes.append((tid, cid, state)) or {"woken": True})
    first = qareview.resume_with_state(tenant, submitted["case_id"], {"manager": "retry"})
    second = qareview.resume_with_state(tenant, submitted["case_id"], {"manager": "retry"})
    assert first["changed"] is True and first["woken"] is True
    assert second["changed"] is False and second["woken"] is False
    assert len(wakes) == 1


def test_only_exact_runtime_current_evidence_resolution_can_supersede_a_dispute():
    memory = {"finding_resolutions": [
        {"review_id": "review-confirmed", "disposition": "confirmed_defect"},
        {"review_id": "review-old", "disposition": "superseded_by_current_revision",
         "rationale": "clean exact replay"},
        {"review_id": "review-fresh", "disposition": "superseded_by_fresh_evidence"},
    ]}

    old = qareview._runtime_superseding_resolution(memory, "review-old")
    fresh = qareview._runtime_superseding_resolution(memory, "review-fresh")

    assert old["rationale"] == "clean exact replay"
    assert fresh["disposition"] == "superseded_by_fresh_evidence"
    assert qareview._runtime_superseding_resolution(memory, "review-confirmed") is None
    assert qareview._runtime_superseding_resolution(memory, "missing") is None


def test_named_authority_link_is_durable_without_rewriting_sealed_input(durable_case):
    tenant, _repo, _record, submitted = durable_case
    linked = qareview.attach_authority(tenant, submitted["case_id"], 987654)
    assert linked["authority_decision_id"] == 987654
    assert qareview.get(tenant, submitted["case_id"])["authority_decision_id"] == 987654


def test_only_senior_can_name_typed_external_authority(durable_case):
    tenant, _repo, _record, submitted = durable_case
    claim = qareview.claim(tenant, "review-worker", lease_s=60)

    def reviewer(role, _payload):
        return {"verdict": "needs_named_external_authority", "confidence": 0.9,
                "rationale": "contract meaning requires its accountable owner",
                "external_authority": {"type": "customer_contract_owner",
                                       "name": "Acme policy owner",
                                       "question": "Does the policy require retry?"}}

    out = qareview.adjudicate(tenant, submitted["case_id"], claim["lease_token"], review_fn=reviewer)
    assert out["status"] == "external_authority"
    assert out["disposition"] == "needs_named_external_authority"
    assert out["external_authority"]["name"] == "Acme policy owner"
    resumed = qareview.state_changed(
        tenant, submitted["case_id"],
        {"external_authority_answer": {"by": "Acme policy owner", "answer": "three retries"}})
    assert resumed["changed"] is True and resumed["status"] == "pending"
    assert qareview.get(tenant, submitted["case_id"])["outcome"] is None


def test_crash_after_committed_review_reuses_that_tier_on_reclaim(durable_case):
    tenant, _repo, _record, submitted = durable_case
    first_claim = qareview.claim(tenant, "review-worker", lease_s=60)
    calls = []

    def reviewer(role, payload):
        calls.append(role)
        return {"verdict": "confirmed_defect", "confidence": 0.95,
                "rationale": "sealed proof", "evidence_ids": [payload["sealed_evidence"][0]["evidence_id"]]}

    with pytest.raises(RuntimeError, match="crash-window"):
        qareview.adjudicate(tenant, submitted["case_id"], first_claim["lease_token"],
                            review_fn=reviewer,
                            after_review=lambda tier, _review: (_ for _ in ()).throw(
                                RuntimeError("crash-window")) if tier == 0 else None)
    with qareview.connection() as c, c.cursor() as cur:
        cur.execute("UPDATE qa_evidence_disputes SET lease_until=now()-interval '1 second' "
                    "WHERE case_id=%s", (submitted["case_id"],))
    second_claim = qareview.claim(tenant, "replacement", lease_s=60)
    out = qareview.adjudicate(tenant, submitted["case_id"], second_claim["lease_token"],
                              review_fn=reviewer)
    assert out["disposition"] == "confirmed_defect"
    assert calls.count(qareview.ROLES[0]) == 1


def test_migration_has_tenant_rls_leases_and_no_generic_human_outcome():
    migration = (ROOT / "postgres" / "initdb" / "68-qa-evidence-disputes.sql").read_text()
    assert "FOR UPDATE SKIP LOCKED" not in migration  # claiming is a runtime transaction, not migration SQL
    assert "ENABLE ROW LEVEL SECURITY" in migration and "FORCE ROW LEVEL SECURITY" in migration
    assert "qa_evidence_disputes_tenant_idx" in migration
    source = (ROOT / "scripts" / "qareview.py").read_text()
    assert "FOR UPDATE SKIP LOCKED" in source and "lease_token" in source
    assert '"ceo", "founder", "human", "user"' in source
