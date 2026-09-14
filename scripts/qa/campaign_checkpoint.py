#!/usr/bin/env python3
"""Pure helpers for bounded, crash-resumable agentic-QA story campaigns.

The story admission limit is a batch-size safety control, never a coverage limit.  These helpers bind a
checkpoint to the complete enumerated manifest and select the next not-yet-observed stories.  The durable
orchestra coordinator remains the authority for story verdicts; the file checkpoint is the process-crash
locator and an operator-readable progress record.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


SCHEMA = "aos.qa.checkpoint/3"
# Evidence semantics are part of the proof, not an implementation detail. Incrementing this value forces a
# fresh campaign after a correctness bug in collection/adjudication is repaired, while retaining the v3
# full-manifest/checkpoint format.
EVIDENCE_POLICY_REVISION = 2
_REVISION_EXTENSIONS = {
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".html", ".css", ".json",
    ".md", ".sql", ".sh", ".yaml", ".yml", ".toml",
}
_REVISION_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "dist", "build", "coverage", "artifacts", "_versions",
}
_REVISION_EVIDENCE_DIRS = {"tests", "test", "docs", "tasks", ".agents", ".codex"}
_CONTROL_PLANE_MARKERS = {"PAUSED.html"}
_REVISION_GENERATED = {
    "docs/QA-CHECKPOINT.json", "docs/QA-VERDICT.json",
    # Financial circuit-breaker state is a control-plane marker, not a product build input.  appguard creates
    # and removes this file independently of the running QA target; hashing it erased already-proven coverage
    # when a long live campaign crossed its spend envelope even though no executable product file changed.
    "PAUSED.html",
}


def story_key(story: dict) -> str:
    """Return the runtime's durable story identity, failing closed on an unaddressable story."""
    key = str((story or {}).get("id") or (story or {}).get("title") or "").strip()
    if not key:
        raise ValueError("every QA story requires a non-empty id or title")
    return key


def canonical_manifest(stories) -> list[dict]:
    """Canonical full campaign input.

    Sorting makes harmless enumeration reordering resume the same campaign.  Duplicate runtime identities are
    rejected because ``story_status`` is keyed by this value and could otherwise fabricate tail coverage.
    """
    manifest = []
    seen = set()
    for story in stories or []:
        key = story_key(story)
        if key in seen:
            raise ValueError(f"duplicate QA story identity: {key}")
        seen.add(key)
        manifest.append({
            "key": key,
            "id": (story or {}).get("id"),
            "title": (story or {}).get("title"),
            "persona": (story or {}).get("persona"),
            "category": (story or {}).get("category"),
            "steps": (story or {}).get("steps"),
            "expected": ((story or {}).get("expected")
                         or (story or {}).get("expected_outcome")),
        })
    return sorted(manifest, key=lambda item: item["key"])


def campaign_signature(*, tenant, product, target_url, vision, repo, stories, thread_id=None) -> str:
    """Hash the complete tenant/product/revision-independent campaign contract, never just its first batch."""
    doc = {
        "schema": 3,
        "evidence_policy_revision": EVIDENCE_POLICY_REVISION,
        "tenant": str(tenant),
        "product": str(product),
        "target_url": str(target_url),
        "vision": str(vision),
        "repo": str(Path(repo).resolve()) if repo else None,
        "thread_id": str(thread_id) if thread_id is not None else None,
        "stories": canonical_manifest(stories),
    }
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def repo_revision(repo, *, max_files=4000, max_file_bytes=2_000_000,
                  include_control_markers=False, include_evidence_files=False) -> str | None:
    """Return a bounded content revision for the executable product/config tree.

    Git metadata is intentionally not required: exported product workspaces may have no usable repository.
    Tests, docs, agent instructions, and generated QA gate files are evidence/control inputs rather than
    browser-runtime inputs. Strengthening those files must not force already-proven user journeys to run again.
    ``include_evidence_files`` preserves the legacy hash for one-time checkpoint reconciliation.
    ``None`` is fail-closed evidence that a revision could not be established.
    """
    if not repo:
        return None
    root = Path(repo)
    generated = (_REVISION_GENERATED - _CONTROL_PLANE_MARKERS
                 if include_control_markers else _REVISION_GENERATED)
    try:
        paths = sorted(
            path for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
            and not any(part in _REVISION_SKIP_DIRS for part in path.relative_to(root).parts)
            and (include_evidence_files
                 or not path.relative_to(root).parts
                 or path.relative_to(root).parts[0] not in _REVISION_EVIDENCE_DIRS)
            and path.relative_to(root).as_posix() not in generated
            and path.suffix.lower() in _REVISION_EXTENSIONS
        )
    except OSError:
        return None
    if not paths or len(paths) > max(1, int(max_files)):
        return None
    digest = hashlib.sha256()
    for path in paths:
        try:
            if path.stat().st_size > int(max_file_bytes):
                return None
            rel = path.relative_to(root).as_posix().encode()
            raw = path.read_bytes()
        except OSError:
            return None
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def story_window(stories, batch_size, observed_story_ids=()) -> dict:
    """Select the next bounded unseen batch without ever returning the completed prefix again."""
    ordered = list(stories or [])
    # Validate identities even when no batching is requested.
    canonical_manifest(ordered)
    observed = {str(item) for item in (observed_story_ids or [])}
    outstanding = [(index, story) for index, story in enumerate(ordered)
                   if story_key(story) not in observed]
    size = int(batch_size or 0)
    active_pairs = outstanding if size <= 0 else outstanding[:max(1, size)]
    active_keys = [story_key(story) for _, story in active_pairs]
    deferred_pairs = outstanding[len(active_pairs):]
    return {
        "active_stories": [story for _, story in active_pairs],
        "active_story_ids": active_keys,
        "remaining_story_ids": [story_key(story) for _, story in outstanding],
        "deferred_story_ids": [story_key(story) for _, story in deferred_pairs],
        "next_index": (outstanding[0][0] if outstanding else len(ordered)),
        "observed_story_ids": [story_key(story) for story in ordered
                               if story_key(story) in observed],
        "total_stories": len(ordered),
    }


def checkpoint_document(*, run_id, signature, tenant, product, target_url, thread_id, stories,
                        story_status=None, batch_size=0, status="running") -> dict:
    """Build the v3 process locator/progress record from durable coordinator state."""
    known = {str(key): str(value) for key, value in dict(story_status or {}).items()}
    window = story_window(stories, batch_size, known)
    return {
        "schema": SCHEMA,
        "evidence_policy_revision": EVIDENCE_POLICY_REVISION,
        "run_id": int(run_id),
        "signature": str(signature),
        "tenant": str(tenant),
        "product": str(product),
        "target_url": str(target_url),
        "thread_id": str(thread_id) if thread_id is not None else None,
        "status": str(status),
        "batch_size": max(0, int(batch_size or 0)),
        "total_stories": window["total_stories"],
        "next_index": window["next_index"],
        "observed_story_ids": window["observed_story_ids"],
        "remaining_story_ids": window["remaining_story_ids"],
        "story_status": {key: known[key] for key in window["observed_story_ids"]},
        "updated_at": time.time(),
    }


def checkpoint_matches(checkpoint, *, signature, tenant, product, target_url, thread_id=None) -> bool:
    """Strict tenant/thread/product fence for attaching a process to an existing campaign."""
    cp = dict(checkpoint or {})
    return bool(
        cp.get("schema") == SCHEMA
        and cp.get("evidence_policy_revision") == EVIDENCE_POLICY_REVISION
        and cp.get("signature") == signature
        and str(cp.get("tenant")) == str(tenant)
        and str(cp.get("product")) == str(product)
        and str(cp.get("target_url")) == str(target_url)
        and (str(cp.get("thread_id")) if cp.get("thread_id") is not None else None)
        == (str(thread_id) if thread_id is not None else None)
    )


def write_checkpoint(path, document) -> Path:
    """Atomically publish a complete checkpoint; a crash yields the old or new document, never torn JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(dict(document), indent=2, sort_keys=True, default=str) + "\n").encode()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        try:
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)
    return target


_SUCCESS_VERDICTS = frozenset({"match", "pass", "passed", "success", "successful", "accepted", "ok"})


def _aspect_key(value) -> str:
    """Stable identity for a ledger assertion.

    Coverage text is a contract.  Case and harmless whitespace are normalized, but fuzzy/union matching is
    intentionally forbidden here: two narrow receipts must never silently satisfy a broader new assertion.
    """
    return " ".join(str(value or "").casefold().split())


def _record_passed(record) -> bool:
    record = record if isinstance(record, dict) else {}
    if record.get("bug") or record.get("infrastructure_error"):
        return False
    if record.get("coverage_grounded") is True:
        return True
    verdict = record.get("verdict")
    if isinstance(verdict, dict):
        if verdict.get("bug") or verdict.get("model_failed") or verdict.get("infrastructure_error"):
            return False
        if verdict.get("matches_expected") is True:
            return True
        return str(verdict.get("verdict") or "").casefold() in _SUCCESS_VERDICTS
    return str(verdict or "").casefold() in _SUCCESS_VERDICTS


def _mechanical_ledger_proof(item) -> bool:
    """Accept only self-identifying browser-engine receipts embedded in a coverage row.

    Model-authored coverage labels sometimes carry a ``proof`` mapping too.  Requiring an explicitly
    mechanical engine, action, and timestamp keeps those labels from becoming self-certifying while still
    preserving atomic dwell/inventory receipts whose full step may have been compacted away.
    """
    item = item if isinstance(item, dict) else {}
    proof = item.get("proof") if isinstance(item.get("proof"), dict) else {}
    engine = str(proof.get("engine") or "").casefold()
    action = str(proof.get("action_kind") or "").strip()
    recorded_at = proof.get("recorded_at")
    try:
        timestamped = float(recorded_at) > 0
    except (TypeError, ValueError):
        timestamped = False
    return bool(action and timestamped and (
        engine.startswith("mechanical-") or engine.startswith("browser-mechanical-")))


def proven_aspects(records) -> set[str]:
    """Return exact ledger identities backed by a successful durable action receipt."""
    proven = set()
    for raw in records or []:
        if not isinstance(raw, dict) or not _record_passed(raw):
            continue
        for field in ("covers", "demonstrated", "mechanically_proven"):
            for value in raw.get(field) or []:
                key = _aspect_key(value)
                if key:
                    proven.add(key)
    return proven


def evidence_diagnostics(result) -> dict:
    """Explain whether one story result is a clean, grounded release proof.

    A covered boolean is only an index into evidence, never evidence itself.  Every covered assertion must
    point either to a successful retained step receipt or to a narrowly recognized mechanical row receipt.
    """
    result = result if isinstance(result, dict) else {}
    coverage = [dict(item) for item in (result.get("coverage") or [])
                if isinstance(item, dict) and _aspect_key(item.get("aspect"))]
    proven = proven_aspects(result.get("steps_detail") or result.get("evidence_records") or [])
    uncovered = [str(item.get("aspect")) for item in coverage if not item.get("covered")]
    unproven = [str(item.get("aspect")) for item in coverage
                if item.get("covered") and _aspect_key(item.get("aspect")) not in proven
                and not _mechanical_ledger_proof(item)]
    bugs = result.get("bugs") or 0
    has_bugs = bool(len(bugs) if isinstance(bugs, (list, tuple, dict, set)) else bugs)
    missing_capabilities = list(result.get("missing_capabilities") or [])
    infrastructure_error = result.get("infrastructure_error")
    stop_reason = str(result.get("stop_reason") or "")
    complete = bool(
        stop_reason == "coverage-complete"
        and coverage
        and not uncovered
        and not unproven
        and not has_bugs
        and not missing_capabilities
        and not infrastructure_error
    )
    reasons = []
    if stop_reason != "coverage-complete":
        reasons.append("stop_reason")
    if not coverage:
        reasons.append("missing_coverage")
    if uncovered:
        reasons.append("uncovered_aspects")
    if unproven:
        reasons.append("covered_without_proof")
    if has_bugs:
        reasons.append("bugs")
    if missing_capabilities:
        reasons.append("missing_capabilities")
    if infrastructure_error:
        reasons.append("infrastructure_error")
    return {
        "complete": complete,
        "reasons": reasons,
        "coverage_total": len(coverage),
        "covered": len(coverage) - len(uncovered),
        "proven": len(coverage) - len(uncovered) - len(unproven),
        "uncovered_aspects": uncovered,
        "unproven_aspects": unproven,
    }


def result_evidence_complete(result) -> bool:
    return bool(evidence_diagnostics(result).get("complete"))


def reopen_unproven_coverage(ledger, records) -> tuple[list[dict], set[str]]:
    """Reopen covered checkpoint rows whose grounding receipt is no longer durable."""
    proven = proven_aspects(records)
    repaired, reopened = [], set()
    for raw in ledger or []:
        if not isinstance(raw, dict) or not _aspect_key(raw.get("aspect")):
            continue
        item = dict(raw)
        if (item.get("covered") and _aspect_key(item.get("aspect")) not in proven
                and not _mechanical_ledger_proof(item)):
            item["covered"] = False
            item["coverage_repaired"] = "covered-without-durable-proof"
            reopened.add(str(item.get("aspect")))
        repaired.append(item)
    return repaired, reopened


def compact_evidence_records(records, *, tail=24, max_records=600) -> list[dict]:
    """Bound navigation noise while never dropping the only retained proof for an aspect.

    Evidence-bearing rows are kept regardless of ``max_records``.  The cap applies to additional recent
    navigation context, which is useful to the next worker but is not release authority.
    """
    rows = [dict(item) for item in (records or []) if isinstance(item, dict)]
    if not rows:
        return []
    essential = set()
    first_proof_for = set()
    latest_proof_for = {}
    for index, row in enumerate(rows):
        if not _record_passed(row):
            continue
        aspects = {_aspect_key(value) for field in ("covers", "demonstrated", "mechanically_proven")
                   for value in (row.get(field) or []) if _aspect_key(value)}
        if aspects - first_proof_for:
            essential.add(index)
            first_proof_for.update(aspects)
        for aspect in aspects:
            latest_proof_for[aspect] = index
    essential.update(latest_proof_for.values())
    tail_count = max(0, int(tail or 0))
    selected = essential | set(range(max(0, len(rows) - tail_count), len(rows)))
    limit = max(1, int(max_records or 1))
    if len(selected) < limit:
        for index in range(len(rows) - 1, -1, -1):
            selected.add(index)
            if len(selected) >= limit:
                break
    return [rows[index] for index in sorted(selected)]
