#!/usr/bin/env python3
"""dev_loop.py — the DEV-fix loop of agent-os's AGENTIC QA system.

Two mandates from the owner, both realized here as *pure AI-driven state loops* (no heuristics,
no hard-coded thresholds — EVERY decision is a `factory.agent` call, which itself retries 529/
overload and fails over to Codex):

  1. DEV IS THE FIRST QA.  Before a builder hands off its own work, it runs `dev_self_qa()` — an
     AI explorer (`qa_explorer.Explorer`) walks the ORIGINAL user stories against the live app and
     reports expected-vs-actual bugs. The builder fixes its own defects before anyone downstream
     ever sees them. Callable in-process (dev_self_qa) or as a CLI gate the factory's
     dev-is-first-QA hook shells out to:  `dev_loop.py self-qa <url> --vision ... --stories ...`
     (exit 0 clean / 1 non-blocking bugs / 2 blocking bugs).

  2. FIX-ON-BLOCKING-BUG.  When a blocking bug is found, `fix_bug()` runs a STATE-BASED loop —
     and the judge is NEVER allowed to grade prose or absence-of-evidence:
         observe(bug)                                   ── the failure + code context + vision
      -> AI DECIDES how to fix (staff-engineer plan)    ── how many dev agents, which files, roles
      -> ACT (spawn exactly that many dev agents)       ── role-specialized, in parallel;
                                                           ANY agent returning rc!=0 FAILS the attempt
      -> take the REAL git diff of what changed         ── evidence, not the plan's file list
      -> restart the app (tracked-PID kill + relaunch)  ── observe a FRESH process, never a stale one
      -> re-explore the failing story (MANDATORY)       ── what the app ACTUALLY does now; no
                                                           judge-without-repro path exists
      -> AI EVALUATES expected-vs-actual (is-it-fixed)  ── judged on the diff + fresh observation
      -> repeat until fixed or attempts exhausted.

    `target_url` and `stories` are therefore MANDATORY on fix_bug: a fix that was never re-observed
    against the live app cannot be judged fixed, period. If re-exploration itself fails, the
    attempt fails — the loop never substitutes "no observation" for "story passes".

The ORIGINAL VISION + EXPECTED behavior are threaded through every prompt so each AI call judges
against what the product was SUPPOSED to do, not just "does it crash". Cost is intentionally not a
concern: this file is deliberately maximally AI-driven.

  python dev_loop.py selftest   # offline wiring check — stubs factory.agent + Explorer, no API calls
  python dev_loop.py self-qa <target_url> --vision <text|@file> --stories <json|@file> [--token T] [--org O]

Run with the agent-os venv python.
"""
import hashlib
import difflib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent          # scripts/ (dev_loop.py lives in scripts/qa/)
sys.path.insert(0, str(SCRIPTS))
import factory  # noqa: E402  — the resilient LLM call (retries overload, fails over to Codex)
import governance  # noqa: E402  — writer-role authority is manifest-derived, never prompt-derived
from qa import campaign_checkpoint  # noqa: E402 — shared grounded story-completion contract

try:
    import audit  # noqa: E402  — best-effort provenance; never a hard dependency of the loop
except Exception:                                          # pragma: no cover
    audit = None

MAX_FIX_ATTEMPTS = int(os.environ.get("AOS_QA_MAX_FIX_ATTEMPTS", "3"))   # bounded observe->fix->judge cycles
MAX_FIX_AGENTS = int(os.environ.get("AOS_QA_MAX_FIX_AGENTS", "6"))       # ceiling on the AI's agent-count decision
HEALTH_TIMEOUT = int(os.environ.get("AOS_QA_HEALTH_TIMEOUT", "60"))      # seconds to wait for the app to come back
DIFF_LIMIT = int(os.environ.get("AOS_QA_DIFF_LIMIT", "14000"))           # chars of real git diff fed to the judge
VERIFICATION_JUDGE_CHAR_LIMIT = int(os.environ.get(
    "AOS_QA_VERIFICATION_JUDGE_CHAR_LIMIT", "12000"))
TRIAGE_MIN_CONFIDENCE = float(os.environ.get("AOS_QA_TRIAGE_MIN_CONFIDENCE", "0.8"))
MIN_EXTERNAL_STAGE_RUNWAY = float(os.environ.get("AOS_QA_MIN_EXTERNAL_RUNWAY_S", "600"))
# These are last-resort runaway fences, not delivery SLAs. Complex repair agents routinely make useful edits,
# run tests, inspect a residual, and continue beyond the old 5/9-minute ceilings. Those fixed cutoffs killed
# healthy 70%-complete work and forced the durable campaign to replay it. Keep generous defaults; operators
# can still tighten them explicitly, while the outer fenced job lease, process registry, and campaign cancel
# event remain the primary liveness/ownership controls.
READ_ONLY_STAGE_TIMEOUT = int(os.environ.get("AOS_QA_READ_ONLY_STAGE_TIMEOUT_S", "900"))
FIX_AGENT_STAGE_TIMEOUT = int(os.environ.get("AOS_QA_FIX_AGENT_STAGE_TIMEOUT_S", "1800"))
FIX_AGENT_CODEX_MODEL = os.environ.get("AOS_QA_FIX_CODEX_MODEL", "gpt-5.6-sol")
FIX_AGENT_REASONING_EFFORT = os.environ.get("AOS_QA_FIX_REASONING_EFFORT", "high")
PROVENANCE_FILE_LIMIT = int(os.environ.get("AOS_QA_PROVENANCE_FILE_LIMIT", "2000"))
TRIAGE_CAPSULE_CHAR_LIMIT = int(os.environ.get("AOS_QA_TRIAGE_CAPSULE_CHAR_LIMIT", "36000"))
TRIAGE_CAPSULE_FILE_LIMIT = int(os.environ.get("AOS_QA_TRIAGE_CAPSULE_FILE_LIMIT", "10"))
TRIAGE_CAPSULE_WINDOW_LINES = int(os.environ.get("AOS_QA_TRIAGE_CAPSULE_WINDOW_LINES", "7"))
FIX_WRITER_ROLES = (
    "staff-engineer", "builder", "frontend-engineer", "backend-engineer",
    "fullstack-engineer", "mobile-engineer", "data-engineer", "security-redteam",
)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _as_list(x):
    if x is None:
        return []
    return list(x) if isinstance(x, (list, tuple)) else [x]


def _audit(action, payload):
    if audit is None:
        return
    try:
        audit.append(actor="qa:dev_loop", action=action, resource="dev_loop",
                     decision="ran", payload=payload)
    except Exception:
        pass


def _ai_json(role, repo, prompt, *, api_key=None, default=None, timeout=None, retries=None,
             compact=False, isolated_repo=False, codex_model=None, reasoning_effort=None):
    """One AI DECISION that must return JSON. Delegates to the resilient factory.agent (overload retry +
    Codex failover) and parses the object out of its reply. Returns `default` only if the model produced
    nothing parseable — the point of the system is that a real decision is always an AI call."""
    if api_key is not None:
        try:
            factory._ctx.api_key = api_key
        except Exception:
            pass
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if retries is not None:
        kwargs["retries"] = retries
    if compact:
        kwargs["compact"] = True
    if codex_model is not None:
        kwargs["codex_model"] = codex_model
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    # Structured judges receive all admissible evidence in their prompt. Running them from the product
    # checkout nevertheless lets an agentic CLI recursively rediscover the entire repository, repeatedly
    # replaying hundreds of thousands of input tokens for a decision that should inspect a bounded dossier.
    # An empty temporary cwd is an enforcement boundary, not a prompt suggestion: the reviewer can reason
    # deeply over the supplied evidence, but cannot wander through unrelated product files. Product/tenant
    # budget context remains on factory._ctx and exact citations are still verified against ``repo`` below.
    if isolated_repo:
        with tempfile.TemporaryDirectory(prefix="aos-qa-judge-") as decision_cwd:
            res = factory.agent(role, decision_cwd, prompt, **kwargs)
    else:
        res = factory.agent(role, str(repo), prompt, **kwargs)
    text = (res or {}).get("out_full") or (res or {}).get("out") or ""
    data = factory._extract_json(text)
    if not data and default is not None:
        return dict(default)
    return data or {}


def _repo_file_hashes(repo) -> dict:
    """Bounded source/test/config hashes used to fence a finding to its observation-time worktree."""
    root = Path(repo)
    skip_dirs = {".git", "node_modules", ".venv", "dist", "build", "coverage", "artifacts"}
    generated = {"docs/QA-CHECKPOINT.json", "docs/QA-VERDICT.json"}
    allowed = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".html", ".css", ".json",
               ".md", ".sql", ".sh", ".yaml", ".yml", ".toml"}
    try:
        paths = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()
                       and not any(part in skip_dirs for part in p.relative_to(root).parts)
                       and p.relative_to(root).as_posix() not in generated
                       and p.suffix.lower() in allowed)
        # A partial manifest cannot prove that an unrecorded test was absent at finding time. Refuse to
        # create a provenance boundary rather than silently weakening the revision fence.
        if len(paths) > PROVENANCE_FILE_LIMIT:
            return {}
    except Exception:
        return {}
    hashes = {}
    for path in paths:
        try:
            if path.stat().st_size <= 2_000_000:
                hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            continue
    return hashes


def capture_finding_provenance(repo, artifact_dir) -> dict | None:
    """Persist a sealed, content-only repository manifest beside browser evidence.

    The artifact is outside the product checkout. A later fixer can therefore prove that a cited source/test
    file is byte-for-byte the version QA saw when it filed the finding; a post-finding test rewrite cannot
    bootstrap its own defect verdict.
    """
    try:
        root = Path(repo).resolve(strict=True)
        files = _repo_file_hashes(root)
        if not files:
            return None
        git_head = None
        try:
            p = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                               text=True, timeout=15)
            git_head = p.stdout.strip() if p.returncode == 0 else None
        except Exception:
            pass
        doc = {"version": 1, "captured_at": time.time(), "repo": str(root),
               "git_head": git_head, "files": files}
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        out = Path(artifact_dir) / f"finding-repo-provenance-{digest[:20]}.json"
        # Content-addressing prevents a later QA pass in the same evidence directory from overwriting an
        # older finding boundary. Atomic replace + fsync makes a crash yield either no usable reference or
        # the complete sealed manifest, never a reference to a torn write.
        tmp = out.with_name(f".{out.name}.{os.getpid()}.{time.time_ns()}.tmp")
        with tmp.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        tmp.replace(out)
        try:
            dir_fd = os.open(str(out.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
        return {"manifest_path": str(out), "manifest_sha256": digest,
                "captured_at": doc["captured_at"], "git_head": git_head, "file_count": len(files)}
    except Exception:
        return None


def _provenance_ref(bug):
    """Prefer the original finding boundary; a resumed residual is not allowed to redefine its baseline."""
    item = bug
    for _ in range(4):
        if not isinstance(item, dict):
            break
        ref = item.get("evidence_provenance")
        if isinstance(ref, dict):
            return ref
        item = item.get("recovery_trigger")
    return None


def _load_finding_provenance(repo, bug_or_ref) -> tuple[dict | None, dict | None]:
    ref = bug_or_ref if isinstance(bug_or_ref, dict) and bug_or_ref.get("manifest_path") \
        else _provenance_ref(bug_or_ref)
    if not ref:
        return None, None
    try:
        path = Path(ref["manifest_path"])
        if (not path.name.startswith("finding-repo-provenance-") or path.suffix != ".json"
                or not path.is_file() or path.stat().st_size > 1_000_000):
            return None, None
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.get("manifest_sha256"):
            return None, None
        doc = json.loads(raw)
        if doc.get("version") != 1 or Path(doc.get("repo") or "").resolve() != Path(repo).resolve():
            return None, None
        if not isinstance(doc.get("files"), dict) or not doc["files"]:
            return None, None
        return ref, doc
    except Exception:
        return None, None


def _changed_files_since_finding(repo, bug_or_ref) -> list[str]:
    """Recover a content-delta receipt when an interrupted writer's result envelope was lost.

    The finding manifest is content-addressed outside the product checkout. Comparing it with the current
    bounded manifest proves which files changed across that sealed boundary without trusting agent prose.
    Generated control markers are not product mutations and are excluded.
    """
    _ref, manifest = _load_finding_provenance(repo, bug_or_ref)
    if not manifest:
        return []
    current = _repo_file_hashes(repo)
    if not current:
        return []
    before = dict(manifest.get("files") or {})
    generated = {"PAUSED.html", "docs/QA-CHECKPOINT.json", "docs/QA-VERDICT.json"}
    return sorted(path for path in set(before) | set(current)
                  if path not in generated and before.get(path) != current.get(path))


def _deadline_value(deadline=None):
    """Resolve a live management lease while retaining numeric-deadline compatibility."""
    try:
        return deadline() if callable(deadline) else deadline
    except Exception:
        return None


def _external_stage_has_runway(deadline=None, cancel_event=None, required=None) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        return False
    required = MIN_EXTERNAL_STAGE_RUNWAY if required is None else max(0.0, float(required))
    live_deadline = _deadline_value(deadline)
    return live_deadline is None or (live_deadline - time.time()) >= required


def _verified_repo_citations(repo, citations, *, provenance=None, limit=8) -> list:
    """Admit exact citations only when the whole file matches the sealed finding-time manifest."""
    try:
        root = Path(repo).resolve(strict=True)
    except Exception:
        return []
    ref, manifest = _load_finding_provenance(repo, provenance or {})
    if not manifest:
        return []
    verified = []
    for raw in _as_list(citations)[:limit]:
        if not isinstance(raw, dict):
            continue
        rel = str(raw.get("path") or "").strip().replace("\\", "/")
        if not rel or Path(rel).is_absolute():
            continue
        try:
            path = (root / rel).resolve(strict=True)
            path.relative_to(root)                         # rejects traversal and symlink escapes
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if manifest["files"].get(str(path.relative_to(root))) != file_sha:
                continue                                   # modified/new after the finding is non-authoritative
            start, end = int(raw.get("start_line")), int(raw.get("end_line"))
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if start < 1 or end < start or end - start >= 40 or end > len(lines):
                continue
            excerpt = "\n".join(lines[start - 1:end])
            quote = str(raw.get("quote") or "").replace("\r\n", "\n")
            if not quote or quote != excerpt:
                continue
            verified.append({"path": str(path.relative_to(root)), "start_line": start,
                             "end_line": end, "quote": excerpt,
                             "sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                             "file_sha256": file_sha, "manifest_sha256": ref["manifest_sha256"]})
        except Exception:
            continue
    return verified


_CAPSULE_STOP_WORDS = {
    "about", "after", "again", "against", "before", "being", "browser", "cannot", "complete",
    "could", "current", "defect", "demonstrated", "expected", "finding", "from", "have", "into",
    "must", "only", "original", "other", "product", "repository", "should", "story", "that", "their",
    "there", "these", "they", "this", "through", "under", "using", "verdict", "vision", "what", "when",
    "where", "which", "with", "would",
}

_CAPSULE_CALL_STOP_WORDS = {
    "assert", "catch", "describe", "filter", "finally", "foreach", "includes", "join", "map",
    "match", "object", "optionalarray", "promise", "reduce", "replace", "return", "slice", "some",
    "sort", "split", "string", "test", "throw", "trim",
}


def _referenced_definition_anchors(lines, primary_anchors, *, limit=8):
    """Follow local helper calls from relevant windows to their definitions in the same sealed file.

    Lexical retrieval often finds the call site that contains the story's language but misses the helper that
    proves its semantics (for example ``checkboxField(...)`` versus its distant ``for``/``id`` implementation).
    This bounded one-hop expansion is deterministic and remains inside the already authoritative file.
    """
    context = []
    for anchor in primary_anchors or []:
        start, end = max(1, int(anchor) - 2), min(len(lines), int(anchor) + 2)
        context.extend(lines[start - 1:end])
    symbols = {
        match for match in re.findall(r"(?<![\w$])([A-Za-z_$][\w$]{3,})\s*\(", "\n".join(context))
        if match.lower() not in _CAPSULE_CALL_STOP_WORDS
    }
    found = []
    for symbol in sorted(symbols, key=lambda value: (-len(value), value.lower())):
        escaped = re.escape(symbol)
        patterns = (
            rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{escaped}\b",
            rf"^\s*(?:export\s+)?(?:const|let|var)\s+{escaped}\s*=",
            rf"^\s*(?:async\s+)?{escaped}\s*\([^)]*\)\s*\{{",
            rf"^\s*(?:export\s+)?class\s+{escaped}\b",
        )
        definition = next((line_no for line_no, line in enumerate(lines, 1)
                           if any(re.search(pattern, line) for pattern in patterns)), None)
        if definition is not None and definition not in found:
            found.append(definition)
        if len(found) >= max(1, int(limit)):
            break
    return found


def _triage_evidence_capsule(repo, bug, code_context, vision, provenance_ref, provenance) -> str:
    """Build a deterministic, bounded source/test/spec dossier from the sealed finding revision.

    The capsule contains a bounded authoritative-file index plus exact, line-addressed windows selected from
    finding/vision identifiers. It never admits a file whose current hash differs from the sealed manifest.
    Missing evidence therefore produces ``uncertain`` rather than silently widening repository access or
    accepting a model assertion.
    """
    if not provenance_ref or not provenance:
        return "NO SEALED PROVENANCE: a definitive verdict is not permitted."
    try:
        root = Path(repo).resolve(strict=True)
    except Exception:
        return "REPOSITORY UNAVAILABLE: a definitive verdict is not permitted."

    raw_query = "\n".join((str(vision)[:12000], json.dumps(bug, default=str)[:12000],
                             json.dumps(code_context, default=str)[:12000]))
    words = re.findall(r"[A-Za-z][A-Za-z0-9_.:-]{3,}", raw_query)
    # Preserve identifiers and long domain terms first, then stable lexical order. This makes selection
    # reproducible while keeping generic prose from matching every README paragraph.
    ranked_terms = sorted(
        {word.lower().strip("._:-") for word in words
         if len(word.strip("._:-")) >= 4 and word.lower().strip("._:-") not in _CAPSULE_STOP_WORDS},
        key=lambda value: (-("_" in value or "-" in value or any(ch.isdigit() for ch in value)),
                           -len(value), value),
    )[:48]
    # Story IDs and code-shaped/long domain terms carry much more retrieval signal than prose such as
    # ``status`` or ``private``. Weight them without removing the ordinary terms: the capsule stays capable of
    # finding a plain-language contract, while a large generic test file can no longer win merely by repeating
    # common words hundreds of times.
    story_markers = {
        str(value).strip().lower()
        for value in ((bug or {}).get("story"), (bug or {}).get("story_id"))
        if str(value or "").strip()
    }
    high_signal_terms = {
        term for term in ranked_terms
        if "_" in term or "-" in term or any(ch.isdigit() for ch in term) or len(term) >= 10
    }
    path_hints = {
        match.replace("\\", "/").strip("'\"`()[]{}.,:;")
        for match in re.findall(r"(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+", raw_query)
    }
    manifest_files = provenance.get("files") or {}
    authoritative = []
    candidates = []
    for rel, sealed_sha in sorted(manifest_files.items()):
        try:
            path = (root / rel).resolve(strict=True)
            path.relative_to(root)
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != sealed_sha:
                continue
            text_value = payload.decode("utf-8", errors="replace")
        except Exception:
            continue
        rel = str(Path(rel)).replace("\\", "/")
        authoritative.append(rel)
        lower_path, lower_text = rel.lower(), text_value.lower()
        path_score = sum((18 if term in high_signal_terms else 6)
                         for term in ranked_terms if term in lower_path)
        path_score += sum(80 for marker in story_markers if marker in lower_path)
        explicit = rel in path_hints or any(rel.endswith("/" + hint) or hint.endswith("/" + rel)
                                             for hint in path_hints)
        if explicit:
            path_score += 1000
        hit_lines = []
        line_scores = []
        for line_no, line in enumerate(text_value.splitlines(), 1):
            lower_line = line.lower()
            hits = sum((3 if term in high_signal_terms else 1)
                       for term in ranked_terms if term in lower_line)
            hits += sum(7 for marker in story_markers if marker in lower_line)
            if hits:
                hit_lines.append(line_no)
                line_scores.append((hits, line_no))
        contract_bonus = 5 if any(part in lower_path for part in
                                  ("test", "spec", "contract", "readme", "docs/")) else 0
        # Reward distinct query concepts and the strongest local co-occurrence, not raw repetition count.
        # Otherwise an 80-line generic fixture mentioning "status audit metadata" on every line outranks the
        # three-line handler containing the exact story ID and keyboard action.
        distinct_signal = sum((3 if term in high_signal_terms else 1)
                              for term in ranked_terms if term in lower_text)
        distinct_signal += sum(7 for marker in story_markers if marker in lower_text)
        strongest_line = max((item[0] for item in line_scores), default=0)
        score = (path_score + contract_bonus + min(120, distinct_signal * 4)
                 + min(80, strongest_line * 4))
        if score:
            candidates.append((score, explicit, rel, text_value.splitlines(), hit_lines, line_scores))

    index_budget = max(1000, min(16000, TRIAGE_CAPSULE_CHAR_LIMIT // 4))
    indexed = []
    for rel in authoritative:
        trial = json.dumps(indexed + [rel], ensure_ascii=False, separators=(",", ":"))
        if len(trial) > index_budget:
            break
        indexed.append(rel)
    index_json = json.dumps(indexed, ensure_ascii=False, separators=(",", ":"))
    header = (
        "SEALED EVIDENCE CAPSULE v1\n"
        f"manifest_sha256={provenance_ref.get('manifest_sha256')}\n"
        f"authoritative_file_count={len(authoritative)}\n"
        f"indexed_file_count={len(indexed)}\n"
        f"index_omitted_file_count={max(0, len(authoritative) - len(indexed))}\n"
        "AUTHORITATIVE_FILE_INDEX_JSON=" + index_json + "\n"
        "Each EVIDENCE_WINDOWS_JSONL record contains raw complete lines from the named original file. "
        "Use its start_line/end_line and copy text exactly into a citation quote. Files absent from the "
        "index or windows are not evidence; if the windows are insufficient, return uncertain.\n"
        "EVIDENCE_WINDOWS_JSONL:\n"
    )
    budget = max(0, TRIAGE_CAPSULE_CHAR_LIMIT - len(header))
    records = []
    used = 0
    for _score, explicit, rel, lines, hit_lines, line_scores in sorted(
            candidates, key=lambda row: (-row[0], not row[1], row[2]))[:TRIAGE_CAPSULE_FILE_LIMIT]:
        if not lines:
            continue
        # Highest-signal matches win, but emit windows in source order so a reviewer sees coherent code.
        anchors = [line for _hits, line in sorted(line_scores, key=lambda row: (-row[0], row[1]))[:5]]
        if explicit and not anchors:
            anchors = [1]
        anchors = list(dict.fromkeys(
            anchors + _referenced_definition_anchors(lines, anchors, limit=5)))
        spans = []
        radius = max(2, TRIAGE_CAPSULE_WINDOW_LINES)
        for anchor in sorted(anchors):
            start, end = max(1, anchor - radius), min(len(lines), anchor + radius)
            if spans and start <= spans[-1][1] + 1:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
        for start, end in spans[:4]:
            record = json.dumps({"path": rel, "start_line": start, "end_line": end,
                                 "text": "\n".join(lines[start - 1:end])},
                                ensure_ascii=False, separators=(",", ":")) + "\n"
            if used + len(record) > budget:
                continue
            records.append(record)
            used += len(record)
    if not records:
        return header + "(no relevant authoritative windows selected; return uncertain)\n"
    return header + "".join(records)


def _triage_finding(bug, code_context, vision, *, repo, api_key=None, deadline=None,
                    cancel_event=None, authorized_changed_files=None) -> dict:
    """Run two independent, read-only reviews before any product mutation is permitted."""
    item = bug or {}
    observed_cmd = str(((item.get("action") or {}).get("cmd") or "")).strip().lower()
    recovery_cmd = str(((((item.get("recovery_trigger") or {}).get("action") or {}).get("cmd"))
                        or "")).strip().lower()
    finding_text = " ".join(str(item.get(key) or "") for key in
                            ("title", "detail", "bug", "expected")).casefold()
    read_only_actions = {"inspect_surfaces", "dwell_surfaces", "scroll", "viewport", "wait",
                         "reload", "goto", "back", "forward", "noop"}
    effectful_actions = {"click", "tap", "touch", "pen", "press", "hold", "burst",
                         "timed_transition", "scenario_matrix", "case_matrix"}
    # A residual from a focused verifier occasionally inherited an effectful expectation from its historical
    # trigger even though the *current* journey executed only inspect/reload actions. Source code proving that
    # acknowledgement works cannot prove the absent click occurred. Never spend two frontier reviews and then
    # authorize a writer from that evidence mismatch; rerun the bounded interaction with an actual action.
    if (isinstance(item.get("recovery_trigger"), dict)
            and observed_cmd in read_only_actions and recovery_cmd in effectful_actions
            and re.search(r"\b(?:acknowledg|approv|publish|send|submit|retry|drain|focus|keyboard|pointer|mouse)",
                          finding_text)
            and not item.get("prior_action_receipts")):
        result = {
            "disposition": "revision_reverify_required", "may_mutate": False,
            "reason": (
                "the residual claims an effectful transition, but its sealed current action is read-only and "
                "contains no prior effectful action receipt; perform one fresh bounded interaction before "
                "any repository review or product mutation"
            ),
            "reviews": [], "evidence_provenance": _provenance_ref(item),
            "changed_since_finding": [], "authorized_changed_files": [],
            "unattributed_changed_since_finding": [], "evidence_contract_recheck": True,
        }
        _audit("FindingTriage", {"disposition": result["disposition"], "may_mutate": False,
                                  "reason": "missing_effectful_action_receipt"})
        return result
    provenance_ref, provenance = _load_finding_provenance(repo, bug)
    current_hashes = _repo_file_hashes(repo) if provenance else {}
    changed_since_finding = sorted(
        path for path in set((provenance or {}).get("files") or {}) | set(current_hashes)
        if (provenance or {}).get("files", {}).get(path) != current_hashes.get(path))[:80]
    authorized = {
        str(Path(str(path or "")).as_posix()).lstrip("./")
        for path in _as_list(authorized_changed_files)
        if str(path or "").strip() and not Path(str(path or "")).is_absolute()
        and ".." not in Path(str(path or "")).parts
    }
    unattributed_changes = [path for path in changed_since_finding if path not in authorized]
    # A sealed finding says what was true at observation time; it does not say whether a later product
    # revision still has the defect.  Sending both frontier reviewers through the old capsule before noticing
    # global worktree drift caused the coordinator to adjudicate and redispatch the same stale finding over and
    # over.  Drift grants no mutation authority, but it *does* require one bounded, read-only reproduction on
    # the current revision.  If the mismatch persists, that browser pass emits a new sealed provenance boundary
    # which crosses this gate normally; if it is gone, complete browser coverage can retire the stale report.
    if unattributed_changes:
        result = {
            "disposition": "revision_reverify_required", "may_mutate": False,
            "reason": (
                "repository files changed after the finding-time evidence boundary; reverify the exact "
                "finding read-only on the current revision before dismissal or mutation"
            ),
            "reviews": [], "evidence_provenance": provenance_ref,
            "changed_since_finding": changed_since_finding,
            "authorized_changed_files": sorted(authorized),
            "unattributed_changed_since_finding": unattributed_changes,
        }
        _audit("FindingTriage", {"disposition": result["disposition"], "may_mutate": False,
                                  "changed_files": len(unattributed_changes)})
        return result
    capsule = _triage_evidence_capsule(
        repo, bug, code_context, vision, provenance_ref, provenance)
    base = (
        "You are one of two INDEPENDENT, READ-ONLY reviewers deciding whether a QA finding describes a "
        "real product defect or whether the observed behavior is the repository's intended contract. "
        "Do not edit files, change repository/process state, ask another agent, or rely on the other reviewer. "
        "You MUST inspect source, tests, and specifications through the SEALED EVIDENCE CAPSULE supplied "
        "below. It is the complete admissible dossier for this review. Do not run shell/filesystem commands "
        "or seek additional repository context; if the capsule is insufficient, return uncertain. "
        "A browser observation alone cannot "
        "override an explicit source/test contract. Conversely, intended implementation alone does not "
        "erase a demonstrated mismatch with the stated vision. Interpret broad population language causally: "
        "'all panels become populated' means "
        "each panel renders and reflects the story actions, not that every independent entity counter becomes "
        "nonzero. A zero approval/ticket/notification count is valid when no story step created or submitted "
        "that entity; only a specific causal creation requirement makes its absence a defect. Quantifiers bind "
        "only their named noun phrase: 'exactly one approval ticket and blocker' does not impose an exactly-one "
        "audit-event constraint. Distinct request, denial, and human-decision audit projections are not duplicate "
        "tickets. Likewise, do not invent cross-panel parity: a filtered approvals/blockers governance history may "
        "truthfully be empty while broader operational diagnostics contain enquiry/job/domain audits, unless the "
        "story explicitly requires the same records on both surfaces. If evidence is incomplete or conflicts, "
        "return uncertain. Every defect or false_positive verdict MUST cite exact current repository lines; "
        "copy the complete cited line range verbatim.\n\n"
        f"ORIGINAL VISION:\n{vision}\n\n"
        f"FINDING:\n{json.dumps(bug, default=str)[:6000]}\n\n"
        f"SUPPLIED CODE CONTEXT (untrusted orientation only):\n"
        f"{json.dumps(code_context, default=str)[:6000]}\n\n"
        f"SEALED FINDING-TIME PROVENANCE: {json.dumps(provenance_ref, default=str)[:1000]}\n"
        f"FILES CHANGED/CREATED/REMOVED SINCE THE FINDING (NON-AUTHORITATIVE): "
        f"{json.dumps(changed_since_finding)}\n"
        "A citation to any non-authoritative file will be rejected mechanically. If the finding has no sealed "
        "provenance, no definitive verdict is possible; return uncertain.\n\n"
        f"{capsule}\n\n"
        "Reply with ONLY a JSON object: "
        '{"verdict":"defect|false_positive|uncertain","confidence":<0..1>,'
        '"reason":"<evidence-based reason>","citations":[{"path":"repo/relative/path",'
        '"start_line":1,"end_line":2,"quote":"exact text of those complete lines"}]}'
    )
    lenses = (
        "Contract lens: prioritize executable tests, handler call parameters, and documented acceptance behavior.",
        "Adversarial lens: try to falsify the finding and also try to falsify the claimed intended behavior.",
    )
    # These reviewers are deliberately independent and read-only, so serial execution bought no additional
    # safety while adding two full frontier-model latencies to every finding. Start them together against the
    # exact same immutable capsule, then restore their stable lens order before applying the unchanged
    # two-reviewer/citation policy below. A single stage runway is sufficient because both calls share the
    # same wall-clock interval; if it is unavailable, checkpoint before starting either call.
    if not _external_stage_has_runway(
            deadline, cancel_event, required=READ_ONLY_STAGE_TIMEOUT + 30):
        result = {"disposition": "checkpoint_required", "may_mutate": False,
                  "reason": "insufficient safety runway for the independent reviewers",
                  "reviews": [], "evidence_provenance": provenance_ref,
                  "checkpoint_required": True}
        _audit("FindingTriage", {"disposition": "checkpoint_required", "may_mutate": False,
                                  "completed_reviewers": 0})
        return result

    def review_one(index, lens):
        try:
            raw = _ai_json("reviewer", repo, base + f"\n\nYOUR REVIEW LENS #{index}: {lens}",
                           api_key=api_key,
                           timeout=READ_ONLY_STAGE_TIMEOUT, retries=0,
                           compact=True,
                           isolated_repo=True,
                           default={"verdict": "uncertain", "confidence": 0.0,
                                    "reason": "reviewer returned no parseable decision", "citations": []})
        except Exception as e:
            raw = {"verdict": "uncertain", "confidence": 0.0,
                   "reason": f"reviewer unavailable: {e}", "citations": []}
        verdict = str(raw.get("verdict") or "uncertain").strip().lower()
        if verdict not in {"defect", "false_positive", "uncertain"}:
            verdict = "uncertain"
        citations = _verified_repo_citations(repo, raw.get("citations"), provenance=provenance_ref)
        if verdict != "uncertain" and not citations:
            verdict = "uncertain"                         # model assertion without repo proof is not a verdict
        try:
            confidence = float(raw.get("confidence") or 0.0)
            confidence = 0.0 if confidence != confidence else max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < TRIAGE_MIN_CONFIDENCE:
            confidence, verdict = 0.0, "uncertain"
        return {"reviewer": index, "verdict": verdict, "confidence": confidence,
                "reason": str(raw.get("reason") or "")[:1000], "citations": citations}

    review_by_index = {}
    with ThreadPoolExecutor(max_workers=len(lenses), thread_name_prefix="qa-triage") as pool:
        futures = {pool.submit(review_one, index, lens): index
                   for index, lens in enumerate(lenses, 1)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                review_by_index[index] = future.result()
            except Exception as e:  # defensive: one crashed reviewer can never erase the other one's evidence
                review_by_index[index] = {
                    "reviewer": index, "verdict": "uncertain", "confidence": 0.0,
                    "reason": f"reviewer unavailable: {e}", "citations": [],
                }
    reviews = [review_by_index[index] for index in range(1, len(lenses) + 1)]

    verdicts = [r["verdict"] for r in reviews]
    distinct = {(c["path"], c["start_line"], c["end_line"])
                for r in reviews for c in r["citations"]}
    if verdicts == ["defect", "defect"]:
        disposition, may_mutate = "confirmed_defect", True
        reason = "two independent reviewers confirmed a repository-grounded defect"
    elif verdicts == ["false_positive", "false_positive"] and len(distinct) >= 2:
        disposition, may_mutate = "false_positive", False
        reason = "two independent reviewers dismissed the finding with distinct verified citations"
    else:
        disposition, may_mutate = "internal_review_required", False
        reason = ("reviewers disagreed or lacked sufficient verifiable repository evidence; "
                  "product mutation is fail-closed pending internal review")
    result = {"disposition": disposition, "may_mutate": may_mutate, "reason": reason,
              "reviews": reviews, "evidence_provenance": provenance_ref,
              "finding_fingerprint": _finding_fingerprint(bug),
              "changed_since_finding": changed_since_finding,
              "authorized_changed_files": sorted(authorized),
              "unattributed_changed_since_finding": unattributed_changes}
    _audit("FindingTriage", {"disposition": disposition, "may_mutate": may_mutate,
                              "review_verdicts": verdicts, "verified_citations": len(distinct)})
    return result


def _triage_citations_current(repo, triage) -> bool:
    """Revision fence: all admitted evidence must still match immediately before the disposition is used."""
    expected = []
    current = []
    provenance = (triage or {}).get("evidence_provenance")
    for review in (triage or {}).get("reviews") or []:
        for citation in review.get("citations") or []:
            expected.append((citation.get("path"), citation.get("start_line"), citation.get("end_line"),
                             citation.get("sha256")))
            refreshed = _verified_repo_citations(repo, [citation], provenance=provenance)
            if refreshed:
                item = refreshed[0]
                current.append((item["path"], item["start_line"], item["end_line"], item["sha256"]))
    return bool(expected) and expected == current


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ground truth #1 — the REAL git diff (the judge never grades agent prose)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _is_git_repo(repo) -> bool:
    try:
        p = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=15)
        return p.returncode == 0 and p.stdout.strip() == "true"
    except Exception:
        return False


def _dirty_paths(repo) -> list:
    """Repo-relative paths git currently sees as modified/untracked (mirrors factory._changed_paths)."""
    try:
        p = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                           capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return []
        out = []
        for line in (p.stdout or "").splitlines():
            if not line.strip():
                continue
            path = line[3:]
            if " -> " in path:                             # rename/copy: the destination is the written path
                path = path.split(" -> ", 1)[1]
            out.append(path.strip().strip('"'))
        return out
    except Exception:
        return []


def _worktree_snapshot(repo) -> dict:
    """Content hash of every currently-dirty/untracked file — taken BEFORE spawning fix agents so the
    attempt's changed-file set is *this attempt's* real footprint, not pre-existing worktree dirt."""
    snap = {}
    for rel in _dirty_paths(repo):
        f = Path(repo) / rel
        try:
            snap[rel] = hashlib.sha1(f.read_bytes()).hexdigest()
        except Exception:
            snap[rel] = "<unreadable-or-deleted>"
    return snap


def _changed_since(repo, before: dict) -> list:
    """Repo-relative files whose content actually changed since the `before` snapshot (new, modified,
    or reverted/deleted). This — not the plan's file list — is what the judge sees."""
    after = _worktree_snapshot(repo)
    changed = [rel for rel, h in after.items() if before.get(rel) != h]
    changed += [rel for rel in before if rel not in after]  # dirty-before, gone-now (revert/delete)
    return sorted(set(changed))


def _git_diff(repo, paths: list, limit: int = DIFF_LIMIT) -> str:
    """The REAL unified diff of `paths` against HEAD (tracked), plus full-content pseudo-diffs for new
    untracked files. This is the evidence the fix judge grades — never the dev agents' own claims."""
    if not paths:
        return ""
    chunks = []
    try:
        p = subprocess.run(["git", "-C", str(repo), "diff", "HEAD", "--"] + list(paths),
                           capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and p.stdout:
            chunks.append(p.stdout)
        covered = {ln[6:].strip() for ln in (p.stdout or "").splitlines() if ln.startswith("+++ b/")}
        for rel in paths:                                  # untracked new files don't appear in diff HEAD
            if rel in covered:
                continue
            f = Path(repo) / rel
            if f.exists():
                q = subprocess.run(["git", "-C", str(repo), "diff", "--no-index", "--", "/dev/null", rel],
                                   capture_output=True, text=True, timeout=30)
                chunks.append(q.stdout or f"NEW FILE {rel} (content unavailable)")
            else:
                chunks.append(f"DELETED/REVERTED: {rel}")
    except Exception as e:
        chunks.append(f"(diff error: {e})")
    return "\n".join(chunks)[:limit]


def _directory_snapshot(repo) -> dict:
    """Bounded content snapshot for product directories that are not Git checkouts.

    Live product workspaces are sometimes exported without ``.git``. Treating those as having no diff made
    every otherwise-successful fixer fail closed and repeat its entire browser QA loop. Snapshot ordinary
    source/test/config files so the judge still receives concrete before/after evidence without mutating or
    reverting any pre-existing user work.
    """
    root = Path(repo)
    snap = {}
    skip_dirs = {".git", "node_modules", ".venv", "dist", "build", "coverage", "artifacts"}
    allowed = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".html", ".css", ".json",
               ".md", ".sql", ".sh", ".yaml", ".yml", ".toml"}
    try:
        files = sorted(p for p in root.rglob("*") if p.is_file()
                       and not any(part in skip_dirs for part in p.relative_to(root).parts)
                       and p.suffix.lower() in allowed)
    except Exception:
        return snap
    for path in files[:2000]:
        try:
            if path.stat().st_size > 2_000_000:
                continue
            raw = path.read_bytes()
            rel = str(path.relative_to(root))
            snap[rel] = {"sha1": hashlib.sha1(raw).hexdigest(),
                         "text": raw.decode("utf-8", "replace")}
        except Exception:
            continue
    return snap


def _directory_changed_since(repo, before: dict) -> tuple[list, dict]:
    after = _directory_snapshot(repo)
    changed = [rel for rel, item in after.items()
               if (before.get(rel) or {}).get("sha1") != item.get("sha1")]
    changed += [rel for rel in before if rel not in after]
    return sorted(set(changed)), after


def _directory_diff(before: dict, after: dict, paths: list, limit: int = DIFF_LIMIT) -> str:
    chunks = []
    for rel in paths:
        old = (before.get(rel) or {}).get("text", "").splitlines(keepends=True)
        new = (after.get(rel) or {}).get("text", "").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        if sum(len(x) for x in chunks) >= limit:
            break
    return "".join(chunks)[:limit]


def _snapshot_before_nongit_mutation(repo, tenant, finding, attempt) -> dict:
    """Create a durable product version before an AI writer touches an exported workspace.

    A directory-only product has no Git object database to recover interrupted edits from.  An in-memory
    before/after snapshot is sufficient for judging a completed writer, but disappears if its process is
    cancelled after writing and before returning.  Production QA therefore records a tenant-owned tar version
    before every non-Git mutation attempt.  Unscoped unit/dev callers retain the existing in-memory behavior;
    a tenant-scoped writer fails closed unless the snapshot covers the exact repository path.
    """
    if not tenant:
        return {"status": "unscoped", "reason": "no tenant-scoped production mutation"}
    root = Path(repo).expanduser().resolve()
    product = root.name
    finding_id = str((finding or {}).get("finding_id") or "unidentified")[:80]
    try:
        import versions
        registered = (Path(versions.factory.PRODUCTS) / product).expanduser().resolve()
        if registered != root:
            return {"status": "failed", "reason": "version registry path does not match repair repo"}
        saved = versions.snapshot(
            str(tenant), product,
            label=f"pre-dev-fix {finding_id} attempt {int(attempt)}")
        if not isinstance(saved, dict) or saved.get("error") or not saved.get("snapshot_path"):
            return {"status": "failed", "reason": str((saved or {}).get("error") or
                                                        "version snapshot returned no path")[:300]}
        receipt = {"status": "created", "version": saved.get("version"),
                   "snapshot_path": saved.get("snapshot_path"), "product": product,
                   "finding_id": finding_id, "attempt": int(attempt)}
        _audit("PreMutationSnapshot", receipt)
        return receipt
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:240]}"}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ground truth #2 — restart by TRACKED PID (never `pkill -f <pattern>`, which can murder bystanders)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
_TRACKED = {}                                              # cmd-key -> Popen of the instance WE launched


def _cmd_key(cmd, cwd) -> str:
    return json.dumps([list(cmd) if isinstance(cmd, (list, tuple)) else str(cmd), str(cwd or "")])


def _kill_pid(pid, proc=None, grace: float = 5.0) -> bool:
    """Terminate the process GROUP we created (start_new_session=True => pgid == pid): SIGTERM, wait up
    to `grace`, then SIGKILL. Reaps via the Popen handle when we hold it. Returns True when it is gone."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except Exception:
            return False
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc is not None:
            if proc.poll() is not None:
                return True
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=grace)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def restart_target(cmd, *, health_url=None, cwd=None, env=None, timeout=HEALTH_TIMEOUT,
                   pid=None) -> dict:
    """Restart/reset the app under test: kill the OLD instance by its TRACKED PID, relaunch, then wait
    until healthy. This is what makes the fix loop observe a *fresh* process rather than a stale one
    still serving the old (buggy) code.

      cmd        — argv list (or shell string) that launches the app.
      health_url — URL polled until it returns 2xx (readiness gate). If None, we just wait a beat and
                   trust the launch (best we can do without a health endpoint).
      pid        — an EXTERNALLY-tracked pid of the old instance (e.g. the builder launched the app
                   itself and knows the pid). When omitted, we kill the pid WE launched last time for
                   this exact cmd+cwd — and if we never launched it, we kill NOTHING: a first launch
                   must never take out an unrelated process the way `pkill -f <pattern>` could.

    Returns {restarted, healthy, pid, killed_old, detail}.
    """
    argv = cmd if isinstance(cmd, (list, tuple)) else None
    shell = None if argv else str(cmd)
    key = _cmd_key(cmd, cwd)

    # 1) KILL the old instance — by tracked PID only, never by pattern.
    killed = False
    if pid is not None:
        killed = _kill_pid(int(pid))
    else:
        old = _TRACKED.pop(key, None)
        if old is not None:
            killed = _kill_pid(old.pid, proc=old)
    if killed:
        time.sleep(float(os.environ.get("AOS_QA_KILL_SETTLE", "1.0")))   # let sockets/pidfiles clear

    # 2) RELAUNCH (its own session/process group so the NEXT restart can killpg exactly this tree).
    try:
        proc = subprocess.Popen(
            list(argv) if argv else shell, shell=bool(shell), cwd=cwd,
            env=({**os.environ, **env} if env else None),
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return {"restarted": False, "healthy": False, "pid": None, "killed_old": killed,
                "detail": f"relaunch failed: {e}"}
    _TRACKED[key] = proc                                    # the pid the NEXT restart will kill

    # 3) WAIT for health.
    healthy, detail = _wait_healthy(health_url, timeout)
    _audit("RestartTarget", {"killed_old": killed, "pid": proc.pid, "healthy": healthy})
    return {"restarted": True, "healthy": healthy, "pid": proc.pid, "killed_old": killed,
            "detail": detail or f"launched pid={proc.pid}, old_killed={killed}"}


def _wait_healthy(health_url, timeout) -> tuple:
    if not health_url:
        time.sleep(float(os.environ.get("AOS_QA_SETTLE", "2.0")))
        return True, "no health_url — waited and assumed up"
    import urllib.request
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=5) as r:
                if 200 <= r.status < 300:
                    return True, f"healthy: {health_url} -> {r.status}"
                last = f"{r.status}"
        except Exception as e:
            last = str(e)
        time.sleep(1.0)
    return False, f"unhealthy after {timeout}s (last: {last})"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# dev-as-first-QA
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _latest_resume_checkpoint(story, repo, evidence_root=None, product=None):
    """Return coverage plus portable browser state from a revision-fenced checkpoint.

    A bounded worker rotation must not make dev verification start its browser ledger from zero.  Conversely,
    evidence captured before a repository mutation is stale and must never be reused.  Comparing checkpoint
    time with the newest repo file gives this recovery path a fail-closed revision fence without coupling the
    product to the orchestra database.
    """
    try:
        default_evidence = evidence_root is None
        if default_evidence:
            import artifacts
            # Live checkpoints are written to the native staging root. Recursively globbing the historical
            # Windows-visible tree on every worker handoff enters WSL p9_client_rpc for tens of seconds or
            # minutes before a browser can even start. Explicit sealed paths still work independently; make
            # the broad legacy recovery scan an operator-invoked migration path, never the latency default.
            evidence_roots = [Path(artifacts.root())]
            if os.environ.get("AOS_QA_SCAN_LEGACY_RESUME", "").strip().lower() in {
                    "1", "true", "yes", "on"}:
                evidence_roots.extend(
                    Path(value) for value in artifacts.search_roots()
                    if Path(value) not in evidence_roots)
        else:
            evidence_roots = [Path(evidence_root)]
        story_name = story.get("title", story.get("name", "")) if isinstance(story, dict) else str(story)
        repo_path = Path(repo) if repo else None
        generated = {"docs/QA-CHECKPOINT.json", "docs/QA-VERDICT.json"}
        non_runtime_roots = {"tests", "docs", "node_modules", ".git", "coverage"}
        repo_mtime = max((p.stat().st_mtime for p in repo_path.rglob("*")
                          if p.is_file()
                          and str(p.relative_to(repo_path)) not in generated
                          and p.relative_to(repo_path).parts[0] not in non_runtime_roots), default=0.0) \
            if repo_path and repo_path.exists() else 0.0
        if default_evidence:
            # Tool workers use product/story/pid-named roots, while older direct
            # explorers used qa-explorer/<timestamp>. Search both bounded prefixes.
            product_name = str(product or (repo_path.name if repo_path else "")).strip()
            safe_product = re.sub(r"[^a-zA-Z0-9_.-]+", "-", product_name).strip("-")
            candidates = []
            for root in evidence_roots:
                candidates += list((root / "qa-explorer").glob("*/checkpoint.json"))
                if safe_product:
                    candidates += list(root.glob(
                        f"qa-explorer-{safe_product}-*/**/checkpoint.json"))
        else:
            candidates = [path for root in evidence_roots for path in root.glob("*/checkpoint.json")]
        candidates = sorted(set(candidates), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates:
            data = json.loads(path.read_text())
            if data.get("story") != story_name:
                continue
            if float(data.get("ts") or path.stat().st_mtime) <= repo_mtime:
                continue
            tested = list(data.get("tested") or [])
            coverage = [dict(item) for item in (data.get("coverage") or [])
                        if isinstance(item, dict) and item.get("aspect")]
            steps_detail = campaign_checkpoint.compact_evidence_records(
                data.get("steps_detail") or [])
            if steps_detail and coverage:
                coverage, _reopened = campaign_checkpoint.reopen_unproven_coverage(
                    coverage, steps_detail)
                tested = [str(item.get("aspect")) for item in coverage if item.get("covered")]
            state_value = data.get("resume_state_path")
            state_path = Path(state_value) if state_value else path.parent / "storage-state.json"
            # A completed, revision-fenced checkpoint is the strongest possible
            # recovery evidence. The old ``yet_to_test`` requirement perversely
            # discarded it and reran the entire browser story after every shift.
            # Even before the first coverage aspect closes, restored form/queue state avoids replaying many
            # setup actions. Assertions may be empty; state continuity is still valuable and safe.
            if state_path.is_file():
                result = {"covered": tested, "coverage": coverage,
                          "resume_state_path": str(state_path)}
                if steps_detail:
                    result["steps_detail"] = steps_detail
                return result
    except Exception:
        pass
    return None


def _latest_resume_coverage(story, repo, evidence_root=None):
    """Compatibility view: never return coverage unless its portable browser state also exists."""
    found = _latest_resume_checkpoint(story, repo, evidence_root)
    return list(found["covered"]) if found else []


def dev_self_qa(target_url, vision, stories, *, token=None, org="0", api_key=None,
                max_steps=None, deadline=None, cancel_event=None,
                stop_on_actionable_bug=False, resume_repo=None, return_report=False,
                browser_state_path=None, resume_covered=None, resume_coverage=None,
                resume_steps_detail=None,
                scope_run_id=None, scope_tenant=None):
    """A builder QAs its OWN work before handoff (dev = first QA). An AI EXPLORER (qa_explorer.Explorer)
    walks each ORIGINAL user story against the LIVE app at `target_url`, holding the `vision` in context so
    it judges expected-vs-actual — and returns every bug it finds. Returning a non-empty list means the
    builder is NOT ready to hand off and should fix its own defects first.

    This is the entry point the factory's dev-is-first-QA hook calls: hand it the served URL + the
    ORIGINAL stories, get the bugs back. Also exposed as the `self-qa` CLI subcommand (see _main) so a
    hook can shell out and gate on the exit code.

    Contract with qa_explorer:
        Explorer(target_url, vision, token=None, org="0") with .explore(story) -> list[bug-dict].
    Each bug-dict is expected to carry at least {story, expected, actual, severity, blocking}.
    (Explorer routes its own factory.agent calls; token/org seed the browser's tenant auth.)
    """
    import qa_explorer                                     # sibling QA module (imported lazily on purpose)
    initial_state = (str(browser_state_path) if browser_state_path
                     and Path(browser_state_path).is_file() else None)
    explorer_args = {"token": token, "org": org}
    if scope_run_id is not None:
        explorer_args["scope_run_id"] = scope_run_id
    if scope_tenant is not None:
        explorer_args["scope_tenant"] = scope_tenant
    if initial_state:
        explorer_args["resume_state_path"] = initial_state
    explorer = qa_explorer.Explorer(target_url, vision, **explorer_args)
    bugs = []
    story_reports = []
    story_list = _as_list(stories)
    # A dev-fix continuation owns exactly one focused story. Its browser state and coverage ledger are an
    # inseparable checkpoint: carrying either one alone can replay mutations or credit evidence against the
    # wrong state. Multi-story callers keep using the ordinary per-story resume lookup below.
    explicit_resume = bool(initial_state and len(story_list) == 1)

    def cancelled():
        live_deadline = _deadline_value(deadline)
        return ((cancel_event is not None and cancel_event.is_set())
                or (live_deadline is not None and time.time() >= live_deadline))

    def legacy_bugs(items):
        """Old Explorer stubs returned bugs directly; live Explorer returns step records and calls on_bug."""
        return [item for item in _as_list(items) if isinstance(item, dict)
                and not item.get("resolved")
                and (item.get("kind") == "bug" or "bug" in item or "blocking" in item)]

    try:
        for story in story_list:
            if cancelled():
                raise TimeoutError("dev self-QA cancelled by run safety deadline")
            try:
                explore_kw = {"max_steps": max_steps, "on_bug": bugs.append, "deadline": deadline,
                              "cancel_event": cancel_event}
                inherited_records = []
                if explicit_resume:
                    resume_contract_matches = True
                    if story.get("category") == "focused-regression" and resume_coverage:
                        # Focused contracts are synthesized code, so a rolling template upgrade can change
                        # their exact four acceptance rows while an actor still holds the old ledger. Carry
                        # receipts only when every retained row belongs to one of the current atomic steps.
                        current_steps = [" ".join(str(step).casefold().split())
                                         for step in (story.get("steps") or []) if str(step).strip()]
                        retained = [" ".join(str(item.get("aspect") or "").casefold().split())
                                    for item in resume_coverage if isinstance(item, dict)
                                    and str(item.get("aspect") or "").strip()]
                        resume_contract_matches = bool(
                            current_steps and retained
                            and all(any(step in aspect for step in current_steps)
                                    for aspect in retained))
                    if resume_contract_matches and resume_covered:
                        explore_kw["resume_covered"] = list(resume_covered)
                    if resume_contract_matches and resume_coverage:
                        explore_kw["resume_coverage"] = [dict(item) for item in resume_coverage
                                                          if isinstance(item, dict) and item.get("aspect")]
                    if resume_contract_matches and resume_steps_detail:
                        inherited_records = campaign_checkpoint.compact_evidence_records(
                            resume_steps_detail)
                        explore_kw["resume_steps_detail"] = inherited_records
                if stop_on_actionable_bug:
                    explore_kw["stop_on_actionable_bug"] = True
                # Full release stories retain their exact browser/coverage checkpoint across rotations. A
                # focused regression is a tiny contract synthesized from the current finding template; carrying
                # an older focused ledger across a runtime upgrade can make the historical broken output remain
                # a requirement even after the template is corrected. Recreate those four bounded steps fresh.
                if resume_repo and story.get("category") != "focused-regression":
                    resume = _latest_resume_checkpoint(
                        story, resume_repo, product=Path(resume_repo).name)
                    if resume:
                        explore_kw["resume_covered"] = resume["covered"]
                        explore_kw["resume_coverage"] = resume.get("coverage")
                        if resume.get("steps_detail"):
                            explore_kw["resume_steps_detail"] = campaign_checkpoint.compact_evidence_records(
                                resume["steps_detail"])
                        # Explorer was already constructed above, so dev self-QA cannot safely retrofit a
                        # context-level Playwright state.  Recreate it once with state restored before boot.
                        explorer.close()
                        resumed_args = {"token": token, "org": org,
                                        "resume_state_path": resume["resume_state_path"]}
                        if scope_run_id is not None:
                            resumed_args["scope_run_id"] = scope_run_id
                        if scope_tenant is not None:
                            resumed_args["scope_tenant"] = scope_tenant
                        explorer = qa_explorer.Explorer(target_url, vision, **resumed_args)
                records = explorer.explore(story, **explore_kw)
                # ``Explorer.explore`` returns actions performed by this process generation. Its coverage
                # ledger can also contain already-proved rows restored with the exact browser checkpoint.
                # Evidence completion must therefore adjudicate the matching inherited receipts together
                # with new actions. Looking only at ``records`` made a 4/4 resumed verifier report 0/4 proven,
                # checkpoint, and immediately relaunch forever despite retaining all sealed proof.
                evidence_records = campaign_checkpoint.compact_evidence_records(
                    inherited_records + list(records or []))
                coverage = [dict(item) for item in (getattr(explorer, "coverage", None) or [])
                            if isinstance(item, dict)]
                stop_reason = getattr(explorer, "stop_reason", None)
                infrastructure_error = getattr(explorer, "infrastructure_error", None)
                missing_capabilities = list(getattr(explorer, "missing_capabilities", None) or [])
                story_identity = (story.get("id") or story.get("title") or story.get("name")
                                  if isinstance(story, dict) else str(story))
                story_open_bugs = [item for item in (getattr(explorer, "bugs", None) or [])
                                   if not item.get("resolved")]
                evidence_result = {
                    "stop_reason": stop_reason, "bugs": len(story_open_bugs),
                    "coverage": coverage, "steps_detail": evidence_records,
                    "infrastructure_error": infrastructure_error,
                    "missing_capabilities": missing_capabilities,
                }
                story_reports.append({
                    "story": story_identity,
                    "stop_reason": stop_reason,
                    "steps": len(records or []),
                    "covered": sum(1 for item in coverage if item.get("covered")),
                    "coverage_total": len(coverage),
                    "complete": campaign_checkpoint.result_evidence_complete(evidence_result),
                    "evidence_diagnostics": campaign_checkpoint.evidence_diagnostics(evidence_result),
                    "infrastructure_error": infrastructure_error,
                    "missing_capabilities": missing_capabilities,
                    "resume_state_path": getattr(explorer, "resume_state_path", None),
                    "coverage": coverage,
                    # A coverage label is an index, not evidence. Preserve the compact sealed receipts with
                    # the browser state so a durable fixer rotation can prove inherited rows instead of
                    # reopening 4/4 coverage and replaying the same focused browser journey forever.
                    "steps_detail": evidence_records,
                })
            except TypeError as e:                        # tolerate legacy/stub Explorer signatures
                if "cancel_event" in str(e):
                    # Intermediate Explorer contract: deadline + on_bug, but no cooperative Event parameter.
                    explorer.explore(story, max_steps=max_steps, on_bug=bugs.append, deadline=deadline)
                    continue
                try:
                    found = explorer.explore(story)
                except TypeError:
                    found = explorer.explore(stories=[story])
                bugs.extend(legacy_bugs(found))
    finally:
        try:
            explorer.close()
        except Exception:
            pass
    bugs = legacy_bugs(bugs)
    # ``tools.qa_explore`` seals ordinary release findings after Explorer returns. A fixer invokes Explorer
    # directly, so its fresh residuals previously lost that boundary on the way back to ``finding_gate``.
    # The gate then (correctly) refused mutation and scheduled another review/browser cycle forever. Seal the
    # current-revision residuals here as well; this is the same content-only manifest, outside the checkout.
    artifact_dir = getattr(explorer, "artifact_dir", None)
    provenance = (capture_finding_provenance(resume_repo, artifact_dir)
                  if resume_repo and artifact_dir else None)
    if provenance:
        for item in bugs:
            item.setdefault("evidence_provenance", provenance)
            if not item.get("finding_id"):
                identity = {key: item.get(key) for key in (
                    "story", "bug", "detail", "url", "expected", "action")}
                identity["manifest_sha256"] = provenance.get("manifest_sha256")
                item["finding_id"] = "qaf-" + hashlib.sha256(
                    json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:24]
    complete = bool(story_reports and len(story_reports) == len(story_list)
                    and all(item.get("complete") for item in story_reports))
    latest = story_reports[-1] if len(story_reports) == 1 else {}
    report = {"bugs": bugs, "complete": complete, "stories": story_reports,
              "resume_state_path": latest.get("resume_state_path"),
              "coverage": latest.get("coverage") or [],
              "steps_detail": latest.get("steps_detail") or []}
    _audit("DevSelfQA", {"target": target_url, "stories": len(story_list), "bugs": len(bugs),
                          "complete": complete})
    return report if return_report else bugs


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# fix-on-blocking-bug  (the state-based loop)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _plan_fix(bug, code_context, vision, *, repo, api_key=None) -> dict:
    """AI DECISION #1 — a staff engineer designs the fix: HOW MANY dev agents to spawn, and for each the
    ROLE (by area: frontend/backend/…) and the exact FILES + task it owns. This is the first thing that
    happens; the loop then spawns exactly the agents this plan names."""
    prompt = (
        "You are the staff engineer triaging a BLOCKING bug in a product built by an agent fleet. Decide "
        "the SMALLEST correct set of dev agents to fix it — how many, and for each choose a "
        f"manifest-backed writer ROLE from {', '.join(FIX_WRITER_ROLES)}, the exact FILES it should touch, "
        "and its concrete TASK. Base the "
        "count on the true blast radius of the fix; do not pad it.\n\n"
        f"ORIGINAL VISION (what the product is meant to be):\n{vision}\n\n"
        f"THE BUG (expected vs actual):\n{json.dumps(bug, default=str)[:6000]}\n\n"
        f"CODE CONTEXT (relevant files / current behavior):\n{json.dumps(code_context, default=str)[:8000]}\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"agents": [{"role": "<area-role>", "files": ["path", ...], "task": "<what this agent fixes>"}], '
        '"rationale": "<why this many, this split>"}'
    )
    # Planning is still pre-mutation work. Use the reviewer role because its manifest denies Bash/Edit/Write;
    # the fix agents below are the first actors authorized to touch the product checkout.
    plan = _ai_json("reviewer", repo, prompt, api_key=api_key,
                    timeout=READ_ONLY_STAGE_TIMEOUT, retries=0,
                    compact=True,
                    codex_model=os.environ.get("AOS_QA_PLANNER_CODEX_MODEL", "gpt-5.6-terra"),
                    reasoning_effort=os.environ.get("AOS_QA_PLANNER_REASONING_EFFORT", "high"),
                    default={"agents": [], "rationale": "no parseable repair plan"})
    agents = plan.get("agents") or []
    agents = [item for item in agents if isinstance(item, dict)][:MAX_FIX_AGENTS]
    plan["agents"] = agents
    return plan


def _authorized_fix_writer_role(requested):
    """Resolve a planner specialty label to an actual manifest-authorized code writer.

    Unknown roles never acquire authority. They are mapped onto a known writer while the requested specialty
    remains explicit in the task and result audit. Existing roles are accepted only with ``can_modify_code``.
    """
    role = str(requested or "").strip().lower()
    aliases = {
        "frontend": "frontend-engineer", "front-end": "frontend-engineer",
        "backend": "backend-engineer", "back-end": "backend-engineer",
        "fullstack": "fullstack-engineer", "full-stack": "fullstack-engineer",
        "mobile": "mobile-engineer", "data": "data-engineer",
        "security": "security-redteam", "security-engineer": "security-redteam",
        "security-governance": "security-redteam", "appsec": "security-redteam",
    }
    candidate = aliases.get(role, role)
    try:
        if (candidate in FIX_WRITER_ROLES and governance.load_manifest(candidate)
                and governance.flags(candidate).get("can_modify_code")):
            return candidate
    except Exception:
        pass
    return "staff-engineer"


def _spawn_fix_agents(plan, bug, vision, *, repo, api_key=None) -> dict:
    """ACT — spawn EXACTLY the AI-decided number of dev agents, each role-specialized to its area, IN
    PARALLEL. Every agent's factory.agent RESULT is checked: rc!=0 / failed / crashed means that agent
    did NOT fix anything, and the whole attempt FAILS (a crashed or refused dev agent must never be
    indistinguishable from a successful fix). Returns {ok, results, failures}."""
    agents = plan["agents"]

    def _one(spec):
        requested_role = spec.get("role") or "builder"
        role = _authorized_fix_writer_role(requested_role)
        files = _as_list(spec.get("files"))
        task = (
            f"SPECIALTY REQUESTED BY THE REPAIR PLAN: {requested_role}. "
            f"Execute it under the manifest-authorized writer identity {role}.\n\n"
            f"A blocking bug must be fixed so the product matches its VISION.\n\n"
            f"VISION:\n{vision}\n\n"
            f"BUG (expected vs actual):\n{json.dumps(bug, default=str)[:4000]}\n\n"
            f"YOUR SCOPE — own these files: {files or 'the relevant source under src/'}\n"
            f"YOUR TASK: {spec.get('task') or 'Implement the fix.'}\n\n"
            f"Fix the ROOT CAUSE (not the symptom), keep existing behavior intact, and verify your change.\n\n"
            "RETRIEVAL BOUNDARY: start with the named files and their direct imports/tests. Do not enumerate "
            "or search the whole repository unless evidence in those files proves the defect crosses that boundary. "
            "EXECUTION BOUNDARY: this repair writer runs in a filesystem-only sandbox. It cannot bind or "
            "connect to localhost, launch Chromium, or inspect the already-running app server. Do not spend "
            "time retrying those operations or searching for alternate browser binaries. Run the strongest "
            "available unit/integration/mounted-DOM tests inside this repo and report that limitation once. "
            "The outer QA coordinator owns the post-repair live-browser restart and full user-story proof."
        )
        if api_key is not None:
            try:
                factory._ctx.api_key = api_key
            except Exception:
                pass
        # The planner has already supplied the specialty, exact files, bug dossier, and execution contract.
        # Reattaching the generic role charter/company memory made a small repair rediscover the whole product
        # before editing (multi-minute xhigh turns with six-figure command output). Keep the strongest coding
        # model, but use the self-contained compact prompt and a high-reasoning default; operators can raise the
        # effort for unusually broad fixes without slowing every ordinary QA correction.
        res = factory.agent(
            role, str(repo), task, timeout=FIX_AGENT_STAGE_TIMEOUT, retries=0, compact=True,
            codex_model=FIX_AGENT_CODEX_MODEL, reasoning_effort=FIX_AGENT_REASONING_EFFORT)
        rc = (res or {}).get("rc")
        failed = res is None or bool((res or {}).get("failed")) or (rc is not None and rc != 0)
        return {"role": role, "requested_role": requested_role, "rc": rc, "failed": failed,
                "blocker": (res or {}).get("blocker"), "planned_files": files}

    results = []
    workers = min(len(agents), int(os.environ.get("AOS_FLEET_WORKERS", "5")))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for fut in as_completed([ex.submit(_one, s) for s in agents]):
            try:
                results.append(fut.result())
            except Exception as e:                         # a crashed spawn IS a failed agent
                results.append({"role": "?", "rc": None, "failed": True, "blocker": str(e)[:300],
                                "planned_files": []})
    failures = [r for r in results if r["failed"]]
    return {"ok": not failures, "results": results, "failures": failures}


def _bounded_text(value, *, head=1200, tail=500):
    """Retain the outcome-bearing beginning and final state of a verbose browser receipt."""
    rendered = str(value or "")
    if len(rendered) <= head + tail:
        return rendered
    return rendered[:head] + f"\n...<{len(rendered) - head - tail} chars omitted>...\n" + rendered[-tail:]


def _verification_judge_capsule(verification, bug=None, *, limit=VERIFICATION_JUDGE_CHAR_LIMIT) -> dict:
    """Build a small story-specific proof dossier without positional JSON truncation.

    Explorer receipts can exceed 60k characters because each action carries network, accessibility, and input
    telemetry. Taking the first N characters hides late transition proof (often the exact action that fixed the
    finding). Keep completion/coverage diagnostics and one successful proof row for every covered contract
    aspect, plus any finding-bearing row. This reduces judge input while preserving the evidence that matters.
    """
    if not isinstance(verification, dict):
        return {"available": False}
    stories = [dict(item) for item in (verification.get("stories") or [])
               if isinstance(item, dict)]
    primary = stories[-1] if len(stories) == 1 else verification
    coverage = [dict(item) for item in (verification.get("coverage")
                                        or primary.get("coverage") or [])
                if isinstance(item, dict) and item.get("aspect")]
    records = campaign_checkpoint.compact_evidence_records(
        verification.get("steps_detail") or primary.get("steps_detail") or [], tail=8)

    def aspect_key(value):
        return " ".join(str(value or "").casefold().split())

    selected = set()
    for aspect in coverage:
        wanted = aspect_key(aspect.get("aspect"))
        matches = []
        for index, record in enumerate(records):
            proved = {
                aspect_key(value)
                for field in ("covers", "demonstrated", "mechanically_proven")
                for value in (record.get(field) or [])
            }
            if wanted and wanted in proved:
                matches.append(index)
        if matches:
            selected.add(matches[-1])
    selected.update(index for index, record in enumerate(records) if record.get("bug"))
    if records and not selected:
        selected.update(range(max(0, len(records) - 4), len(records)))

    proofs = []
    for index in sorted(selected):
        record = records[index]
        action = record.get("action")
        if isinstance(action, dict):
            action = {key: action.get(key) for key in (
                "cmd", "target", "target_text", "targets", "value", "key") if action.get(key) is not None}
        verdict = record.get("verdict")
        if isinstance(verdict, dict):
            verdict = {key: verdict.get(key) for key in (
                "matches_expected", "verdict", "reason") if verdict.get(key) is not None}
        proofs.append({
            "action": action,
            "covers": record.get("covers") or record.get("demonstrated")
                      or record.get("mechanically_proven") or [],
            "expected": _bounded_text(record.get("expected"), head=700, tail=0),
            "actual": _bounded_text(record.get("actual"), head=1400, tail=500),
            "verdict": verdict,
            "bug": record.get("bug"),
        })

    capsule = {
        "complete": verification.get("complete"),
        "story_reports": [{
            key: item.get(key) for key in (
                "story", "complete", "stop_reason", "steps", "covered", "coverage_total",
                "evidence_diagnostics", "infrastructure_error", "missing_capabilities")
            if item.get(key) not in (None, [], {})
        } for item in stories],
        "coverage": [{
            "aspect": item.get("aspect"), "covered": bool(item.get("covered")),
            "proof": item.get("proof"),
        } for item in coverage],
        "proof_records": proofs,
        "missing_capabilities": verification.get("missing_capabilities") or [],
        "infrastructure_error": verification.get("infrastructure_error"),
        "finding_focus": {key: (bug or {}).get(key) for key in (
            "story", "title", "detail", "expected", "action") if (bug or {}).get(key) is not None},
    }
    rendered = json.dumps(capsule, default=str)
    if len(rendered) <= max(1000, int(limit)):
        return capsule
    # Preserve every coverage row, but progressively shrink verbose actuals rather than slicing the JSON and
    # accidentally deleting a later proof row altogether.
    for proof in proofs:
        proof["actual"] = _bounded_text(proof.get("actual"), head=550, tail=180)
        proof["expected"] = _bounded_text(proof.get("expected"), head=320, tail=0)
    rendered = json.dumps(capsule, default=str)
    if len(rendered) <= max(1000, int(limit)):
        return capsule
    for proof in proofs:
        proof["actual"] = _bounded_text(proof.get("actual"), head=260, tail=80)
        proof.pop("expected", None)
    return capsule


def _judge_fixed(bug, vision, changed_files, diff_text, residual_bugs, restart, *,
                 repo, api_key=None, verification=None, prior_mutation_receipt=False,
                 mutation_receipt_source=None) -> dict:
    """AI DECISION #3 — evaluate expected-vs-actual against the ORIGINAL VISION: is the bug ACTUALLY fixed?
    Fed ONLY ground truth: the REAL git diff of what changed (never the dev agents' prose) and the FRESH
    post-restart observation from re-exploring the failing story. Fail-closed: if the model is unsure it
    should say not-fixed so the loop tries again."""
    changed_provenance = (
        "from the sealed finding-time manifest versus current content delta"
        if mutation_receipt_source == "sealed_finding_delta" else
        "from the exact fenced prior-worker mutation receipt" if prior_mutation_receipt else
        "from the current git/directory snapshot")
    rendered_diff = diff_text or (
        "(NOT RECONSTRUCTIBLE ACROSS THE PROCESS HANDOFF — use the exact fenced changed-file receipt "
        "together with the complete fresh verification; this is not evidence of no mutation)"
        if prior_mutation_receipt else "(EMPTY — no verifiable change was made)")
    rendered_verification = (
        json.dumps(_verification_judge_capsule(verification, bug), default=str)
        if verification is not None else "(legacy caller; unavailable)")
    prompt = (
        "You are the staff engineer VERIFYING a fix. Judge strictly against the ORIGINAL VISION and the "
        "bug's EXPECTED behavior — expected-vs-actual, not merely 'it no longer crashes'. Be adversarial; "
        "if you are not confident it is truly fixed, say so. Ground rules: (a) the GIT DIFF below is the "
        "ONLY change evidence. An empty diff normally means no fix; on an idempotent retry after an interrupted "
        "attempt, however, an exact fenced prior-worker file receipt plus a COMPLETE clean FRESH VERIFICATION "
        "may prove the already-applied fix. Never accept an empty diff if any residual bug or incomplete "
        "verification remains; (b) the FRESH VERIFICATION below is "
        "the app's ACTUAL behavior after a "
        "restart — any residual bug matching the original story means NOT fixed.\n\n"
        f"ORIGINAL VISION:\n{vision}\n\n"
        f"THE BUG that was supposed to be fixed:\n{json.dumps(bug, default=str)[:4000]}\n\n"
        f"FILES ACTUALLY CHANGED ({changed_provenance}; not an agent prose claim):\n"
        f"{json.dumps(changed_files, default=str)[:2000]}\n\n"
        f"REAL GIT DIFF of those changes:\n{rendered_diff}\n\n"
        f"APP RESTART: {json.dumps(restart, default=str)[:500]}\n\n"
        f"FRESH VERIFICATION CAPSULE (every contract row plus its outcome-bearing proof; verbose browser "
        f"telemetry compacted, never positionally truncated):\n"
        f"{rendered_verification}\n\n"
        f"FRESH OBSERVATION — bugs the explorer STILL sees after restart, re-exploring the failing story "
        f"(empty means the story now passes):\n{json.dumps(residual_bugs, default=str)[:6000]}\n\n"
        "Reply with ONLY a JSON object: "
        '{"fixed": <true|false>, "confidence": <0..1>, "reason": "<evidence-based justification>"}'
    )
    # Verification is read-only; use the sandboxed reviewer role and bound the call to the same stage budget
    # as the independent finding reviews.
    verdict = _ai_json("reviewer", repo, prompt, api_key=api_key,
                       timeout=READ_ONLY_STAGE_TIMEOUT, retries=0,
                       compact=True,
                       default={"fixed": False, "confidence": 0.0, "reason": "no verdict"})
    verdict["fixed"] = bool(verdict.get("fixed"))
    return verdict


def _finding_browser_state_path(finding):
    """Return the portable browser snapshot paired with a sealed finding, when retained.

    New findings carry an immutable ``finding-state-*`` snapshot captured at the reporting boundary.  The
    historical ``storage-state.json`` is only a moving continuation cursor, so accept it for compatibility
    only when its mtime is close enough to the finding screenshot to be plausibly paired. No coverage or
    verdict crosses this boundary—only browser input state.
    """
    item = finding or {}
    candidates = []
    for key in ("finding_state_path", "resume_state_path"):
        explicit = item.get(key)
        if explicit:
            candidates.append((Path(str(explicit)), True))
    screenshot = item.get("screenshot") or item.get("shot")
    shot_path = Path(str(screenshot)) if screenshot else None
    if screenshot:
        if shot_path.parent.name == "screenshots":
            candidates.append((shot_path.parent.parent / "storage-state.json", False))
    artifact_dir = item.get("artifact_dir")
    if artifact_dir:
        candidates.append((Path(str(artifact_dir)) / "storage-state.json", False))
    manifest = ((item.get("evidence_provenance") or {}).get("manifest_path")
                if isinstance(item.get("evidence_provenance"), dict) else None)
    if manifest:
        candidates.append((Path(str(manifest)).parent / "storage-state.json", False))
    seen = set()
    for candidate, exact in candidates:
        try:
            resolved = str(candidate.resolve())
            if resolved in seen or not candidate.is_file():
                continue
            seen.add(resolved)
            if candidate.name != "storage-state.json" and not candidate.name.startswith("finding-state-"):
                continue
            if not exact and shot_path and shot_path.is_file():
                # A cursor saved materially after the screenshot includes later story mutations. Restoring
                # it can manufacture a false regression, as when later acknowledgements altered US011 state.
                if candidate.stat().st_mtime - shot_path.stat().st_mtime > 5.0:
                    continue
            return str(candidate)
        except OSError:
            continue
    return None


_FOCUSED_REPRO_CONTRACT_VERSION = 9


def _observational_recheck_steps(restored_state=False):
    """Keep a read-only finding read-only during focused verification.

    Surface inspection, viewport, scroll and no-op findings name observation
    boundaries, not controls.  Calling a heading/panel a "corrected user
    control" made the explorer search for a clickable element, then wander
    into unrelated setup when none existed.
    """
    return [
        ("Inspect the restored finding-time state; do not load or seed an unrelated story fixture."
         if restored_state else
         "Load only the fixture and prerequisite state needed for this exact finding."),
        ("Confirm only the finding-named prerequisites and surfaces that are observable; do not create or "
         "submit a duplicate record."),
        ("Perform the reported read-only observation exactly once using its named targets, viewport, or "
         "scroll boundary; inspect the surface directly and do not click or submit a control."),
        ("Verify the corrected expected behavior on only the finding-named surfaces. Do not require "
         "co-rendered siblings to be absent unless the finding itself names illegal exposure."),
    ]


def _form_enter_submission_recheck(finding):
    """Recognize Enter on a complete valid form, the semantic opposite of blank-form validation."""
    item = dict(finding or {})
    action = item.get("action") or {}
    if str(action.get("cmd") or "").strip().lower() != "press":
        return False
    key = str(action.get("value") or action.get("key") or "").strip().lower()
    if key not in {"enter", "return"}:
        return False
    text = " ".join(str(item.get(name) or "") for name in
                    ("title", "detail", "bug", "expected")).casefold()
    form_context = re.search(
        r"\b(?:form|field|name|email|phone|postcode|date|age|consent|enquiry|submit|submission)\b",
        text)
    complete_context = (
        re.search(r"\bno\s+(?:required\s+)?fields?\s+(?:were?\s+|are\s+|is\s+)?"
                  r"(?:blank|empty|missing|invalid)\b", text)
        or re.search(r"\ball\s+required\s+fields?\s+(?:were?\s+|are\s+|is\s+|had\s+been\s+)?"
                     r"(?:filled|complete|completed|valid|populated|provided)\b", text)
        or re.search(r"\b(?:fully|correctly)\s+(?:filled|complete|completed|valid|populated)\s+"
                     r"(?:form|enquiry|submission)\b", text)
        or re.search(r"\bvalid\s+(?:completed\s+)?(?:form|enquiry|submission)\b", text)
    )
    return bool(form_context and complete_context)


def _form_enter_validation_recheck(finding):
    """Recognize an Enter-key form-validation defect before generic focus routing.

    Focus language is common to both form validation and post-action list controls.  Treating every such
    finding as an acknowledgement-style interaction produced an impossible focused story asking a blank
    enquiry form to leave a second equivalent action open.  The action plus validation vocabulary is a
    tighter boundary and lets the browser prove the actual disabled-submit/field-error contract directly.
    """
    item = dict(finding or {})
    action = item.get("action") or {}
    if str(action.get("cmd") or "").strip().lower() != "press":
        return False
    key = str(action.get("value") or action.get("key") or "").strip().lower()
    if key not in {"enter", "return"}:
        return False
    text = " ".join(str(item.get(name) or "") for name in
                    ("title", "detail", "bug", "expected")).casefold()
    if _form_enter_submission_recheck(item):
        return False
    return bool(
        re.search(r"\b(?:form|field|name|email|phone|postcode|date|age|consent|enquiry|submit)\b", text)
        # This specialized contract hard-codes a blank Name field. Generic validation/error vocabulary is
        # insufficient: "no required fields empty triggered validation" describes a valid submission defect
        # and is the semantic opposite of this branch.
        and re.search(r"\b(?:blank|empty|missing|incomplete)\b", text)
        and re.search(r"\b(?:required|invalid|validation|error|message|feedback|disabled)\b", text)
    )


def _form_enter_validation_steps():
    return [
        "Inspect the restored finding-time form state; do not load or seed an unrelated story fixture.",
        ("Focus the blank required Name field while the enquiry remains incomplete and confirm Send enquiry "
         "is disabled; do not populate or submit the form."),
        ("Press Enter exactly once from the blank Name field so the current revision runs its form-validation "
         "attempt; do not click Send enquiry."),
        ("Verify a clear field-specific Name error, accessible invalid/error association, visible recoverable "
         "focus, and no enquiry, agent job, audit event, or notification persisted."),
    ]


def _form_enter_submission_steps():
    return [
        ("Open the public enquiry form at the finding's target; browser storage does not preserve typed DOM "
         "values, so do not assume the historical fields are still populated."),
        ("Populate every required enquiry field with safe fixture values, select required consent, and confirm "
         "Send enquiry is enabled; do not submit yet."),
        ("Focus the finding-named Name field and press Enter exactly once; do not click Send enquiry or issue "
         "a second submit action."),
        ("Verify one enquiry is accepted with the success confirmation and expected single downstream side "
         "effects, with no required-field validation error and no duplicate enquiry."),
    ]


def _reset_storage_recheck_steps():
    """Exact browser contract for a fresh-state reopen/console finding."""
    return [
        ("Use the currently open target as the baseline for one fresh-state reset; do not load a fixture or "
         "submit a business record."),
        ("Perform exactly one fresh-state reset that clears browser storage and reopens the pinned target URL."),
        ("After that fresh-state reset, wait for the reopened page to settle and inspect its scoped network "
         "receipts without clicking or submitting any control."),
        ("Verify the fresh-state reset completed on the target with no console error and no failed or HTTP "
         "4xx/5xx request."),
    ]


def _normalize_focused_repro_contract(story):
    """Upgrade a durable focused story created by an older controller generation.

    Tool arguments survive controller restarts by design.  That means a rolling code upgrade can otherwise
    keep replaying an already-serialized unsafe assertion even after the story generator was corrected.  Keep
    this migration deliberately narrow: it only removes the historical blanket absence requirement and does
    not reinterpret arbitrary user-authored stories.
    """
    normalized = dict(story or {})
    if str(normalized.get("category") or "") != "focused-regression":
        return normalized
    steps = [str(step) for step in _as_list(normalized.get("steps"))]
    legacy = (
        "Verify the corrected expected behavior after the transition on only surfaces named by the source "
        "story or finding; confirm controls restricted to a different state are absent."
    )
    replacement = (
        "Verify corrected behavior only for finding-named controls. Do not require co-rendered siblings "
        "to be absent unless the finding itself names illegal exposure."
    )
    normalized["steps"] = [replacement if step.strip() == legacy else step for step in steps]
    focused_finding = dict(normalized.get("focused_finding") or {})
    reported = focused_finding.get("action") or {}
    reported_cmd = str(reported.get("cmd") or "").strip().lower()
    if reported_cmd in {"reset_storage", "resetstorage"}:
        normalized["steps"] = _reset_storage_recheck_steps()
    if (reported_cmd in {"inspect_surfaces", "inspect_landmarks", "inspectlandmarks",
                         "scroll", "viewport", "noop"}
            and not focused_finding.get("browser_state_restored")):
        normalized["steps"] = _observational_recheck_steps(
            bool(focused_finding.get("browser_state_restored")))
        focused_finding["observational_recheck"] = True
        normalized["focused_finding"] = focused_finding
    if reported_cmd == "reload":
        generic = (
            "Exercise the corrected user control required by the expected behavior; do not repeat a historical "
            "observation, scroll, or diagnostic action."
        )
        reload_step = (
            "Reload the restored page once so the current revision re-evaluates and re-renders the exact "
            "persisted finding-time state; do not submit a duplicate record."
        )
        normalized["steps"] = [reload_step if step.strip() == generic else step
                               for step in normalized["steps"]]
    interaction_text = " ".join(str(focused_finding.get(key) or "")
                                for key in ("detail", "expected")).casefold()
    interaction_cmd = reported_cmd
    form_enter_submission_recheck = bool(
        focused_finding.get("browser_state_restored")
        and _form_enter_submission_recheck(focused_finding)
    )
    form_enter_recheck = bool(
        focused_finding.get("browser_state_restored")
        and not form_enter_submission_recheck
        and _form_enter_validation_recheck(focused_finding)
    )
    interaction_recheck = bool(
        focused_finding.get("browser_state_restored")
        and not form_enter_recheck and not form_enter_submission_recheck
        and interaction_cmd in {"click", "tap", "touch", "pen", "press", "hold", "burst"}
        and re.search(r"\b(?:focus|focused|keyboard|pointer|mouse|outline|indicator)\b",
                      interaction_text)
    )
    if form_enter_submission_recheck:
        normalized["steps"] = _form_enter_submission_steps()
        focused_finding["form_enter_submission_recheck"] = True
        focused_finding["form_enter_validation_recheck"] = False
        focused_finding["interaction_mechanics_recheck"] = False
        normalized["focused_finding"] = focused_finding
    elif form_enter_recheck:
        normalized["steps"] = _form_enter_validation_steps()
        focused_finding["form_enter_submission_recheck"] = False
        focused_finding["form_enter_validation_recheck"] = True
        focused_finding["interaction_mechanics_recheck"] = False
        normalized["focused_finding"] = focused_finding
    elif interaction_recheck:
        normalized["steps"] = [
            "Inspect the restored finding-time state; do not load or seed an unrelated story fixture.",
            ("Identify one currently open finding-named action control while at least one equivalent action "
             "will remain open; do not activate either control yet."),
            ("Perform exactly one fresh finding-named action, then verify its status transition and that focus "
             "moves to a remaining enabled action with a distinct visible focus indicator."),
            ("Verify the finding-named status, focus, and privacy outcome after that action. Do not require "
             "co-rendered siblings to be absent unless the finding itself names illegal exposure."),
        ]
        focused_finding["interaction_mechanics_recheck"] = True
        normalized["focused_finding"] = focused_finding
    normalized["_qa_focused_contract_version"] = _FOCUSED_REPRO_CONTRACT_VERSION
    return normalized


def _focused_repro_stories(stories, bug, browser_state_path=None):
    """Turn a broad release story into the exact defect reproduction used inside a fixer.

    The coordinator owns full-story regression after a repair. Replaying every sibling clause inside the
    fixer is slower and can miss the actual shape (for example testing ``token=`` while the sealed finding is
    about a bare ``sk_live_`` token). Preserve the durable story identity, but bind this browser to the exact
    finding, action, adjudication rationale, and expected behavior.
    """
    raw_source = (_as_list(stories) or [{}])[0] or {}
    # ``fix_bug`` has always documented both plain story strings and structured story dictionaries. The
    # focused-regression synthesizer must preserve that compatibility instead of feeding a string to dict(),
    # which aborts every post-fix browser verification before the Explorer can start.
    source = (dict(raw_source) if isinstance(raw_source, dict)
              else {"title": str(raw_source), "goal": str(raw_source)})
    finding = dict(bug or {})
    detail = str(finding.get("detail") or finding.get("bug") or finding.get("title") or "reported defect")
    raw_expected = str(finding.get("expected") or "the reported mismatch no longer occurs")
    action = json.dumps(finding.get("action") or {}, default=str, sort_keys=True)
    adjudication = json.dumps(finding.get("_qa_adjudication") or {}, default=str, sort_keys=True)
    # A story-level expected outcome may contain several semicolon-delimited sibling journeys.  A focused
    # repair must prove the clause that actually matches the sealed defect, then leave the complete outcome
    # to the coordinator's full-story regression.  Only narrow when one clause is an unambiguous lexical
    # match; ties retain the original contract rather than guessing.
    clause_stopwords = {
        "after", "area", "before", "claim", "claims", "content", "expected", "finding", "only",
        "public", "publish", "published", "remains", "reported", "result", "should", "story", "their",
        "there", "these", "this", "through", "verified", "visible", "while", "with", "without",
        "the", "and", "that", "then", "from", "into", "when", "where", "which",
    }

    def focus_tokens(value):
        """Normalize temporal/compound defect language for expected-clause matching.

        A finding may say ``10-second`` while the contract says ``10 seconds``. Keeping the hyphenated form
        as one token made a generic prerequisite clause ("set Timeout and fill the form") outscore the actual
        broken outcome ("remain pending for 10 seconds"). This lightweight normalization is only a narrowing
        aid; ambiguous scores still retain the full original contract.
        """
        normalized = str(value or "").casefold().replace("_", " ").replace("-", " ").replace("/", " ")
        tokens = set()
        for term in re.findall(r"[a-z0-9]+", normalized):
            if term in clause_stopwords or (len(term) < 2 and not term.isdigit()):
                continue
            if len(term) > 5 and term.endswith("ed"):
                term = term[:-2]
            elif len(term) > 4 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
                term = term[:-1]
            tokens.add(term)
        return tokens

    focus_text = (" ".join(str(finding.get(key) or "") for key in ("title", "detail", "bug"))
                  + " " + action)
    focus_terms = focus_tokens(focus_text)
    expected_clauses = [clause.strip() for clause in re.split(r";\s*", raw_expected) if clause.strip()]
    expected = raw_expected
    if len(expected_clauses) > 1 and focus_terms:
        clause_term_sets = [focus_tokens(clause) for clause in expected_clauses]
        # Terms repeated by every sibling ("submission", "Agent", etc.) are weak signals. Weight each shared
        # term by inverse sibling frequency so the defect's distinctive outcome/temporal language wins.
        term_frequency = {
            term: sum(term in clause_terms for clause_terms in clause_term_sets)
            for term in set().union(*clause_term_sets)
        }
        clause_scores = [sum(1.0 / term_frequency[term] for term in (focus_terms & clause_terms))
                         for clause_terms in clause_term_sets]
        ranked_scores = sorted(clause_scores, reverse=True)
        if ranked_scores[0] > 0 and ranked_scores[0] > ranked_scores[1] + 0.25:
            expected = expected_clauses[clause_scores.index(ranked_scores[0])]
    source_steps = [str(step).strip() for step in _as_list(source.get("steps")) if str(step).strip()]
    finding_terms = {
        term for term in re.findall(
            r"[a-z0-9_:-]{4,}",
            " ".join((str(finding.get(key) or "") for key in
                      ("title", "detail", "bug"))).lower()
            + " " + raw_expected.lower()
            + " " + action.lower())
        if term not in {"after", "area", "before", "claim", "claims", "content", "expected", "finding",
                        "only", "public", "publish", "published", "remains", "reported", "result", "should",
                        "story", "their", "there", "these", "this", "through", "verified", "visible", "while",
                        "with", "without"}
    }
    ranked_clues = sorted(
        ((len(finding_terms & set(re.findall(r"[a-z0-9_:-]{4,}", step.lower()))), index, step)
         for index, step in enumerate(source_steps[1:], start=1)),
        key=lambda item: (-item[0], item[1]))
    # The first source step normally establishes the fixture. Add at most two semantically relevant later
    # steps so a final action such as reload has the input journey that created its prerequisite state. This
    # fixes token/publication verification without expanding the worker back into every sibling assertion.
    source_clues = source_steps[:1]
    source_clues.extend(step for score, _index, step in ranked_clues[:2] if score > 0)
    setup_clue = " ".join(source_clues)[:1800]
    # The durable fixer can restore a checkpoint supplied by its actor result even when that path was not
    # embedded in the original sealed finding.  Pass that transport-level path into the story contract too;
    # otherwise the browser is genuinely restored while the planner is told to re-prove the hidden triggering
    # value and burns its action budget scrolling for evidence that privacy correctly keeps out of the DOM.
    restored_state = browser_state_path or _finding_browser_state_path(finding)
    reported_action = finding.get("action") or {}
    recovery_action = (((finding.get("recovery_trigger") or {}).get("action") or {})
                       if isinstance(finding.get("recovery_trigger"), dict) else {})
    finding_outcome_text = f"{detail} {expected}".casefold()
    if (str(reported_action.get("cmd") or "").strip().lower()
            in {"inspect_surfaces", "dwell_surfaces", "scroll", "viewport", "wait", "reload", "noop"}
            and str(recovery_action.get("cmd") or "").strip().lower()
            in {"click", "tap", "touch", "pen", "press", "hold", "burst"}
            and re.search(r"\b(?:focus|focused|keyboard|pointer|mouse|outline|indicator)\b",
                          finding_outcome_text)):
        # A focused residual can only evaluate the interaction mechanics it inherited by actually replaying
        # that historical trigger. Preserve the current inspect finding as context, but bind the next focused
        # contract to the source action instead of asking a static reload to manufacture its effect.
        reported_action = recovery_action
        action = json.dumps(reported_action, default=str, sort_keys=True)
    reported_cmd = str(reported_action.get("cmd") or "").strip().lower()
    reported_target = str(
        reported_action.get("target_text") or reported_action.get("target") or "").strip()
    # ``diagnose_incomplete`` is a runner conclusion, not a user-facing control.  The sealed snapshot already
    # contains the triggering input, so demanding a new click while simultaneously forbidding a duplicate
    # submission creates an impossible ledger clause.  Reloading is the real corrected transition here: it
    # makes the current revision re-evaluate and re-render the persisted finding-time state.
    diagnostic_recheck = bool(restored_state and reported_cmd == "diagnose_incomplete")
    viewport_recheck = bool(restored_state and reported_cmd == "viewport")
    effectful_transition = reported_cmd in {
        "click", "tap", "touch", "pen", "press", "hold", "burst",
        "timed_transition", "scenario_matrix", "case_matrix", "diagnose_incomplete",
    }
    # A post-action focus/keyboard/pointer defect cannot be verified by restoring the post-action checkpoint
    # and merely reloading it: that static replay never performs the action whose mechanics are in dispute.
    # Reuse one currently-open equivalent control, preserve another as the focus destination, and execute the
    # named action exactly once. This is a fresh verification action, not a duplicate business record.
    interaction_mechanics_recheck = bool(
        restored_state and effectful_transition
        and not _form_enter_submission_recheck({**finding, "action": reported_action})
        and not _form_enter_validation_recheck({**finding, "action": reported_action})
        and re.search(r"\b(?:focus|focused|keyboard|pointer|mouse|outline|indicator)\b",
                      finding_outcome_text)
    )
    form_enter_submission_recheck = bool(
        restored_state
        and _form_enter_submission_recheck({**finding, "action": reported_action})
    )
    form_enter_validation_recheck = bool(
        restored_state
        and not form_enter_submission_recheck
        and _form_enter_validation_recheck({**finding, "action": reported_action})
    )
    # A sealed finding is often captured after a successful mutating action. The form may intentionally reset,
    # while the disputed blocker/audit/status row remains in the restored checkpoint. For explicitly settled
    # presentation defects, replaying Publish/Submit creates duplicates and tests another record; reload current
    # code and inspect the exact persisted outcome instead. Transient/action mechanics keep the active path.
    settled_outcome_recheck = bool(
        restored_state
        and reported_cmd in {"click", "tap", "touch", "pen", "press", "hold", "burst",
                             "timed_transition", "scenario_matrix", "case_matrix"}
        and re.search(r"\b(?:settled|recorded|persisted|rendered|displayed|shown|exposed?)\b",
                      finding_outcome_text)
        and re.search(r"\b(?:blockers?|audits?|diagnostics?|statuses|status|messages?|records?|rows?|history|outcomes?)\b",
                      finding_outcome_text)
        and not re.search(r"\b(?:pending|loading|spinner|busy|dwell|duration|seconds?|minutes?)\b",
                          finding_outcome_text)
        and not interaction_mechanics_recheck
        and not form_enter_validation_recheck
        and not form_enter_submission_recheck)
    # ``inspect_surfaces``/``scroll``/``viewport`` are observation boundaries, not business mutations. A focused replay
    # used to synthesize a generic "exercise the corrected control" step for them; on an already-pending
    # approval snapshot the planner duly approved and sent the follow-up, destroying the very pending state it
    # was meant to verify. Reloading is the only transition needed to make current code re-render persisted
    # finding-time state, after which the named surfaces can be inspected read-only.
    observational_recheck = reported_cmd in {
        "inspect_surfaces", "inspect_landmarks", "inspectlandmarks", "scroll", "viewport", "noop"}
    reload_recheck = bool(restored_state and reported_cmd == "reload")
    passive_recheck = bool(restored_state and (
        diagnostic_recheck or observational_recheck or settled_outcome_recheck or reload_recheck))
    transient_terms = focus_tokens(f"{detail} {expected}")
    transient_recheck = bool(
        restored_state
        and effectful_transition
        and transient_terms & {"pending", "loading", "spinner", "waiting", "busy", "progress",
                               "sending", "saving", "submitting", "processing", "retrying"}
        and transient_terms & {"second", "minute", "duration", "dwell", "stable", "stability",
                               "latency", "timeout"}
    )
    if restored_state and not (transient_recheck or interaction_mechanics_recheck
                               or form_enter_submission_recheck):
        # The exact state and reported read-only action already define this verifier's boundary. Broad source
        # journey text often names every sibling control in the release story; retaining it here caused a
        # viewport label recheck to click Drain queue and report an empty queue as a new product defect.
        setup_clue = ""
    setup_step = ((
        "Inspect the restored finding-time browser state and confirm the exact prerequisite state for this "
        "finding; do not load or seed an unrelated story fixture."
    ) if restored_state else (
        "Establish only the minimum valid product state and prerequisites needed for this exact finding. "
        "Prefer an existing purpose-built seed, load, demo, or fixture control when the interface offers one; "
        "do not manually invent a record through a workflow that cannot create it."
    ))
    if setup_clue:
        setup_step += (
            f" Source-story journey clues: {setup_clue} Use only the parts needed by this finding and skip its "
            "unrelated sibling assertions."
        )
    reset_storage_recheck = reported_cmd in {"reset_storage", "resetstorage"}
    if reset_storage_recheck:
        focused_steps = _reset_storage_recheck_steps()
    elif form_enter_submission_recheck:
        focused_steps = _form_enter_submission_steps()
    elif form_enter_validation_recheck:
        focused_steps = _form_enter_validation_steps()
    elif observational_recheck and not restored_state:
        focused_steps = _observational_recheck_steps(bool(restored_state))
    elif transient_recheck:
        focused_steps = [
            "Inspect the restored finding-time state; do not load or seed an unrelated story fixture.",
            ("Recreate one fresh, distinct triggering user action"
             + (f" for {reported_target}" if reported_target else "")
             + " with the finding-named prerequisites; do not reuse an existing record or idempotency key."),
            "Perform the exact action once, verify its transient busy/pending safeguards, and dwell for the finding's stated duration without forcing completion.",
            "After the dwell, verify the corrected expected outcome and confirm the fresh action created no duplicate side effect.",
        ]
    elif interaction_mechanics_recheck:
        focused_steps = [
            "Inspect the restored finding-time state; do not load or seed an unrelated story fixture.",
            ("Identify one currently open finding-named action control while at least one equivalent action "
             "will remain open; do not activate either control yet."),
            ("Perform exactly one fresh finding-named action, then verify its status transition and that focus "
             "moves to a remaining enabled action with a distinct visible focus indicator."),
            ("Verify the finding-named status, focus, and privacy outcome after that action. Do not require "
             "co-rendered siblings to be absent unless the finding itself names illegal exposure."),
        ]
    else:
        focused_steps = [
            ("Inspect the restored finding-time state; do not load or seed an unrelated story fixture."
             if restored_state else
             "Load only the fixture and prerequisite state needed for this exact finding."),
            ("Confirm the restored state contains the finding's exact triggering input and prerequisite state; "
             "do not create or submit a duplicate record."
            if restored_state and not (diagnostic_recheck or settled_outcome_recheck) else
             "Confirm only the finding-named restored prerequisites that are observable; do not require a "
             "blocker, hidden value, or duplicate record unless the finding explicitly names it."
             if diagnostic_recheck or settled_outcome_recheck else
             "Recreate and submit the finding's exact triggering input without requiring the historical broken output."),
            ("Reapply the reported finding viewport dimensions once so the current revision reflows the "
             "restored state at the exact tested width and height; do not activate any sibling control."
            if viewport_recheck else
            "Reload the restored page once so the current revision re-evaluates and re-renders the exact "
             "persisted finding-time state; do not submit a duplicate record."
            if passive_recheck else
            "Exercise the corrected user control required by the expected behavior; do not repeat a historical "
            "observation, scroll, or diagnostic action."),
            ("Verify corrected behavior only for finding-named controls. Do not require co-rendered siblings "
             "to be absent unless the finding itself names illegal exposure."),
        ]

    restored_instruction = (
        "Browser storage does not retain typed form-control values. Rebuild one minimal valid enquiry with safe "
        "fixture values, then press Enter once from the finding-named field and prove exactly one submission. "
        if form_enter_submission_recheck else
        "The exact finding-time blank-form state is restored. Press Enter once from the named field to run "
        "the current validation path; do not populate or submit the form and prove that no business record or "
        "side effect is created. "
        if form_enter_validation_recheck else
        "The exact finding-time browser state is restored as context, but the defect concerns a transient state "
        "that cannot survive a checkpoint. Recreate one fresh distinct triggering action and do not reuse an "
        "existing record or idempotency key. "
        if transient_recheck else
        "The exact finding-time browser state is restored as context, but interaction mechanics require one "
        "fresh action on a currently open equivalent control. Leave another equivalent action open as the "
        "focus destination and do not create a duplicate business record. "
        if interaction_mechanics_recheck else
        "The exact finding-time browser state is restored; inspect it directly and do not load or seed a fixture "
        "labeled for another story. Treat that restored state as the already-created exact triggering input: do "
        "not submit a duplicate record merely to recreate it. "
        if restored_state else
        "Prefer a purpose-built seed, load, demo, or fixture and use only the relevant clues. "
    )

    source.update({
        "title": f"Focused reproduction — {source.get('title') or finding.get('story') or 'QA defect'}",
        "category": "focused-regression",
        # These four steps are already the deliberately bounded decomposition. The generic release-story
        # parser must not split commas inside finding detail or JSON action text into dozens of pseudo-steps.
        "_qa_atomic_steps": True,
        "_qa_focused_contract_version": _FOCUSED_REPRO_CONTRACT_VERSION,
        # Keep decision-critical detail in the goal, while the durable ledger below stays short enough for an
        # evaluator to credit one grounded milestone at a time. Embedding paragraphs/JSON inside each aspect made
        # a complete denial+reload journey remain 0/4 forever because no single demonstrated string matched.
        "goal": (
            "Recreate the exact triggering input and verify the corrected expected behavior for the sealed QA "
            "finding, not unrelated sibling behavior. Historical mismatch (targeting context only; do not require "
            f"it to reappear): {detail} Reported final action: {action}. Correct expected behavior: {expected}. "
            f"Source-story journey clues: {setup_clue or '(none)'}. "
            + restored_instruction
            + "Only entities and actions explicitly named by the historical mismatch, reported final action, "
              "or narrowed corrected behavior are in scope; never exercise a sibling control from the broad "
              "source story. Judge each requirement on its named surface: staff/internal "
              "administration records are not public exposure."
        ),
        "steps": focused_steps,
        "expected": expected,
        "expected_outcome": expected,
        "focused_finding": {"detail": detail, "expected": expected,
                            "action": reported_action, "adjudication": adjudication,
                            "browser_state_restored": bool(restored_state),
                            "reset_storage_recheck": reset_storage_recheck,
                            "form_enter_submission_recheck": form_enter_submission_recheck,
                            "form_enter_validation_recheck": form_enter_validation_recheck,
                            "transient_recheck": transient_recheck,
                            "interaction_mechanics_recheck": interaction_mechanics_recheck,
                            "settled_outcome_recheck": settled_outcome_recheck,
                            "observational_recheck": observational_recheck},
    })
    return [_normalize_focused_repro_contract(source)]


def _adjudication_applies(current_bug, adjudication):
    """Bind senior authority to the exact finding it decided, never to a later residual."""
    current_id = str((current_bug or {}).get("finding_id") or "").strip()
    decided_id = str((adjudication or {}).get("finding_id") or "").strip()
    return bool(current_id and decided_id and current_id == decided_id
                and (adjudication or {}).get("disposition") == "confirmed_defect"
                and (adjudication or {}).get("case_id") and (adjudication or {}).get("review_id"))


def _finding_fingerprint(finding) -> str:
    """Bind a durable triage receipt to one exact finding and sealed repository boundary."""
    item = finding or {}
    identity = {
        key: item.get(key)
        for key in ("finding_id", "story", "title", "detail", "bug", "expected", "action")
    }
    identity["manifest_sha256"] = (_provenance_ref(item) or {}).get("manifest_sha256")
    return hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _recovery_triage_receipt_applies(repo, current_bug, receipt) -> bool:
    """Accept a prior confirmed gate only to enter read-only recovery verification.

    The receipt cannot authorize a later residual: that finding crosses ``finding_gate`` again below. Exact
    finding identity, sealed provenance, and still-verifiable citations prevent actor/tool input from turning
    an unrelated historical review into mutation authority.
    """
    return bool(
        isinstance(receipt, dict)
        and receipt.get("disposition") == "confirmed_defect"
        and receipt.get("may_mutate") is True
        and receipt.get("finding_fingerprint") == _finding_fingerprint(current_bug)
        and _triage_citations_current(repo, receipt)
    )


def fix_bug(bug, code_context, vision, *, target_url, stories, repo=None, restart_cmd=None,
            health_url=None, token=None, org="0", api_key=None, max_steps=None,
            deadline=None, cancel_event=None,
            scope_run_id=None, scope_tenant=None,
            adjudication=None,
            resume_triage_finding=None,
            resume_triage_receipt=None,
            resume_changed_files=None,
            resume_change_diff=None,
            resume_state_path=None, resume_covered=None, resume_coverage=None,
            resume_steps_detail=None,
            resume_existing=False,
            max_attempts=MAX_FIX_ATTEMPTS) -> dict:
    """Fix ONE blocking bug via the state-based loop (observe -> AI plans -> spawn dev agents -> real git
    diff -> restart -> RE-EXPLORE the failing story -> AI judges), repeating up to `max_attempts` until
    the AI judge confirms the fix against the ORIGINAL VISION.

    `target_url` and `stories` are MANDATORY: every attempt re-explores a focused reproduction of the failing
    finding against the
    live app, and the judge only ever sees that fresh observation plus the real diff. There is no
    judge-without-repro path — an attempt whose agents fail (rc!=0), whose app won't come back healthy,
    or whose re-exploration errors is a FAILED attempt, never a judged pass.

      bug          — the failure dict (expected vs actual), typically from dev_self_qa / the explorer.
      code_context — relevant code/state; if a dict it may carry {"repo": <path>} used to locate the app.
      vision       — the ORIGINAL product vision + expected behavior (held in context for every AI call).
      target_url   — REQUIRED: the live app to re-observe after each attempt.
      stories      — REQUIRED: the failing story/stories to re-explore (usually just the bug's story).
      restart_cmd  — argv/str to relaunch the app after the fix (restart_target, tracked-PID kill). When
                     None the target is managed externally (e.g. a dev server the builder owns) — the
                     mandatory re-exploration still observes the live app either way.
      token/org    — tenant auth for the explorer's browser session.

    Returns {fixed: bool, files: [...repo-relative, from git...], attempts, verdict, plan, restart,
             residual, attempts_log}.
    """
    if not target_url:
        raise ValueError("fix_bug: target_url is mandatory — a fix that is never re-observed against "
                         "the live app cannot be judged fixed")
    stories = _as_list(stories)
    if not stories:
        raise ValueError("fix_bug: stories is mandatory — the failing story must be re-explored after "
                         "every fix attempt (no judge-without-repro path)")
    if repo is None and isinstance(code_context, dict):
        repo = code_context.get("repo")
    repo = repo or str(factory.PRODUCTS)
    in_git = _is_git_repo(repo)

    # A current-revision focused reproduction can finish and yield a newly sealed residual immediately before
    # the two independent repository reviewers run out of shift runway.  That residual is itself the durable
    # input to triage.  Replaying the browser on every worker generation adds no authority and can consume an
    # entire shift rediscovering the same finding, so resume directly at the read-only triage gate.
    triage_resumed = isinstance(resume_triage_finding, dict) and bool(resume_triage_finding)
    if triage_resumed:
        original_bug = bug
        bug = dict(resume_triage_finding)
        bug.setdefault("recovery_trigger", original_bug)

    # A writer may finish immediately before the QA shift checkpoints. The next process can re-observe that
    # already-applied repair, but it cannot reconstruct a pre-mutation directory snapshot. Carry the exact
    # changed-file receipt from the prior fenced tool result so impact-scoped regression does not silently
    # degrade into either "no revision" or a blind full-corpus retest after recovery verification succeeds.
    all_files = []
    for raw in _as_list(resume_changed_files):
        rel = str(raw or "").strip().replace("\\", "/")
        if rel and not Path(rel).is_absolute() and ".." not in Path(rel).parts and rel not in all_files:
            all_files.append(rel)
    attempts, attempts_log, mutation_snapshots = 0, [], []
    change_diff = str(resume_change_diff or "")[:DIFF_LIMIT]
    plan, verdict, restart, residual, verification = None, {"fixed": False}, None, [], None
    triage = None

    def focused_qa(current_bug):
        """Run the exact reproduction and preserve completion separately from an empty bug list.

        ``[]`` used to ambiguously mean either a clean coverage-complete proof or a cancelled/stalled browser.
        New explorers return the sealed coverage/stop report; list-shaped test doubles and rolling legacy
        workers remain compatible but cannot manufacture a false incomplete flag.
        """
        finding_state_path = _finding_browser_state_path(current_bug)
        # A prior focused worker may have changed business state before checkpointing (for example approving
        # and sending while it was meant to inspect a pending approval). Its transport checkpoint is useful
        # only when no sealed finding snapshot exists. Prefer the immutable finding-adjacent input, and never
        # carry a coverage ledger that was earned against a different browser state.
        focused_state_path = finding_state_path or resume_state_path
        same_resume_state = bool(
            resume_state_path and focused_state_path
            and Path(resume_state_path).resolve() == Path(focused_state_path).resolve())
        observed = dev_self_qa(
            target_url, vision,
            _focused_repro_stories(stories, current_bug, browser_state_path=focused_state_path),
            token=token, org=org, api_key=api_key, max_steps=max_steps,
            deadline=deadline, cancel_event=cancel_event,
            stop_on_actionable_bug=True, resume_repo=repo, return_report=True,
            browser_state_path=focused_state_path,
            resume_covered=(resume_covered if same_resume_state else None),
            resume_coverage=(resume_coverage if same_resume_state else None),
            resume_steps_detail=(resume_steps_detail if same_resume_state else None),
            scope_run_id=scope_run_id, scope_tenant=scope_tenant)
        if isinstance(observed, dict) and isinstance(observed.get("bugs"), list):
            return list(observed["bugs"]), observed
        return _as_list(observed), None

    def finding_gate(current_bug):
        """Return (triage, terminal_result). Every no-mutation acceptance and writer path crosses here."""
        if _adjudication_applies(current_bug, adjudication):
            # tools.dev_fix resolves this reference from the tenant-scoped durable QA case before passing it
            # here. The manager has already adjudicated the immutable finding; re-running two stochastic
            # reviewers would reopen the same case and can never add authority. Mutation still must survive
            # the normal diff, restart, fresh-browser, and independent fix-judge gates below.
            changed = _changed_files_since_finding(repo, current_bug)
            authorized = set(all_files if resume_existing else [])
            unattributed = [path for path in changed if path not in authorized]
            if unattributed:
                return ({
                    "disposition": "revision_reverify_required", "may_mutate": False,
                    "reason": (
                        "the exact finding was adjudicated, but the product revision changed afterward; "
                        "reverify it read-only before using that decision"
                    ),
                    "adjudication": adjudication,
                    "evidence_provenance": (current_bug or {}).get("evidence_provenance"),
                    "changed_since_finding": changed,
                    "authorized_changed_files": sorted(authorized),
                    "unattributed_changed_since_finding": unattributed,
                    "reviews": [],
                }, None)
            return ({"disposition": "confirmed_defect", "may_mutate": True,
                     "reason": "durable senior QA adjudication confirmed this exact finding",
                     "adjudication": adjudication,
                     "evidence_provenance": (current_bug or {}).get("evidence_provenance"),
                     "changed_since_finding": changed,
                     "authorized_changed_files": sorted(authorized),
                     "unattributed_changed_since_finding": []}, None)
        decision = _triage_finding(
            current_bug, code_context, vision, repo=repo, api_key=api_key,
            deadline=deadline, cancel_event=cancel_event,
            # A prior generation can finish its authorized writer immediately before checkpointing. The
            # durable tool reducer carries that exact repo-relative mutation receipt. It permits only fresh
            # recovery verification of those attributed changes; any additional drift remains fail-closed.
            authorized_changed_files=(all_files if resume_existing else None))
        if decision["disposition"] == "checkpoint_required":
            gated_verdict = {"fixed": False, "confidence": 0.0, "reason": decision["reason"],
                             "checkpoint_required": True}
            return decision, {
                "fixed": False, "files": [], "attempts": 0, "verdict": gated_verdict, "plan": None,
                "restart": restart, "residual": residual, "attempts_log": attempts_log,
                "triage": decision, "checkpoint_required": True, "internal_review_required": False}
        if decision["disposition"] == "revision_reverify_required":
            return decision, None
        if (decision["disposition"] != "internal_review_required"
                and not _triage_citations_current(repo, decision)):
            decision["disposition"], decision["may_mutate"] = "internal_review_required", False
            decision["reason"] = ("repository evidence changed after review; product mutation and "
                                  "no-mutation acceptance are fail-closed pending internal review")
        if decision["disposition"] == "false_positive":
            gated_verdict = {
                "fixed": True, "confidence": min(r["confidence"] for r in decision["reviews"]),
                "reason": decision["reason"], "resolved_without_mutation": True}
            return decision, {
                "fixed": True, "resolved": True, "resolved_without_mutation": True,
                "files": [], "attempts": 0, "verdict": gated_verdict,
                "plan": {"agents": [], "rationale": "verified false positive; no repair permitted"},
                "restart": restart, "residual": residual, "attempts_log": attempts_log,
                "triage": decision, "internal_review_required": False}
        if not decision["may_mutate"]:
            gated_verdict = {"fixed": False, "confidence": 0.0, "reason": decision["reason"],
                             "internal_review_required": True}
            return decision, {
                "fixed": False, "files": [], "attempts": 0, "verdict": gated_verdict, "plan": None,
                "restart": restart, "residual": residual, "attempts_log": attempts_log,
                "triage": decision, "internal_review_required": True,
                "review_route": "qa-internal-review"}
        return decision, None

    def preserve_triage_checkpoint(payload, current_bug):
        """Attach the exact sealed finding whose reviewer stage must resume without another browser run."""
        if isinstance(payload, dict) and payload.get("checkpoint_required"):
            payload = dict(payload)
            payload["resume_triage_finding"] = dict(current_bug or {})
        return payload

    # The finding-time boundary must be checked BEFORE recovery QA. Otherwise a resumed worker can accept an
    # empty residual after an interested predecessor rewrote source/tests, bypassing the exact dispute gate
    # that would have rejected that post-finding mutation. This also prevents an unproved ``resolved`` flag
    # from becoming a citation-free false-positive dismissal.
    # Once an authorized writer has produced a fenced mutation receipt, repeating the exact same two sealed
    # finding-time reviewers on every browser checkpoint adds no authority. Reuse their identity-bound receipt
    # only to enter fresh read-only recovery QA; any residual finding is independently gated again below.
    if (resume_existing and all_files
            and _recovery_triage_receipt_applies(repo, bug, resume_triage_receipt)):
        triage = dict(resume_triage_receipt)
        triage["reused_for_recovery_verification"] = True
    else:
        triage, terminal = finding_gate(bug)
        if terminal is not None:
            return preserve_triage_checkpoint(terminal, bug)

    revision_reverified = False
    if triage.get("disposition") == "revision_reverify_required":
        restart = {"restarted": False, "healthy": None,
                   "detail": "repository revision changed; running focused read-only re-verification"}
        try:
            residual, verification = focused_qa(bug)
        except Exception as e:
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": f"current-revision verification interrupted ({e}); no mutation started",
                       "checkpoint_required": True}
            return {"fixed": False, "files": [], "attempts": 0, "verdict": verdict, "plan": None,
                    "restart": restart, "residual": [],
                    "attempts_log": [{"attempt": 0, "failed_stage": "revision-reverify",
                                      "error": str(e)[:300]}],
                    "triage": triage, "checkpoint_required": True,
                    "internal_review_required": False}
        if not residual:
            # Unlike the legacy resume judge, revision drift is deciding whether an old finding may be
            # retired at all.  A list-shaped/unknown report therefore cannot mean clean: require the explorer's
            # explicit sealed completion bit and coverage ledger.
            if not isinstance(verification, dict) or verification.get("complete") is not True:
                verdict = {"fixed": False, "confidence": 0.0,
                           "reason": "current-revision verification is incomplete; checkpoint for continuation",
                           "checkpoint_required": True}
                return {"fixed": False, "files": [], "attempts": 0, "verdict": verdict,
                        "plan": None, "restart": restart, "residual": [],
                        "attempts_log": [{"attempt": 0, "failed_stage": "revision-reverify-incomplete",
                                          "verification": verification}],
                        "triage": triage, "checkpoint_required": True,
                        "internal_review_required": False,
                        "resume_state_path": (verification or {}).get("resume_state_path"),
                        "coverage": (verification or {}).get("coverage") or [],
                        "steps_detail": (verification or {}).get("steps_detail") or []}
            verdict = {
                "fixed": True, "confidence": 0.99,
                "reason": "complete focused browser verification on the current revision found no residual defect",
                "resolved_without_mutation": True,
                "acceptance_basis": "current_revision_complete_reverification",
            }
            return {"fixed": True, "resolved": True, "resolved_without_mutation": True,
                    "files": [], "attempts": 0, "verdict": verdict,
                    "plan": {"agents": [], "rationale": "stale finding no longer reproduces"},
                    "restart": restart, "residual": [],
                    "attempts_log": [{"attempt": 0, "changed": [], "residual": 0,
                                      "revision_reverification": True}],
                    "triage": triage, "internal_review_required": False,
                    "recovery_verified": True}
        rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        observed = max(
            (item for item in residual if isinstance(item, dict)),
            key=lambda item: (bool(item.get("blocking")),
                              rank.get(str(item.get("severity") or "").lower(), 0)),
            default=None)
        if not observed:
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": "current-revision verification returned no sealed actionable finding",
                       "internal_review_required": True}
            return {"fixed": False, "files": [], "attempts": 0, "verdict": verdict,
                    "plan": None, "restart": restart, "residual": residual,
                    "attempts_log": [{"attempt": 0, "failed_stage": "revision-reverify-evidence"}],
                    "triage": triage, "internal_review_required": True,
                    "review_route": "qa-internal-review"}
        prior_bug = bug
        bug = dict(observed)
        bug["recovery_trigger"] = prior_bug
        triage, terminal = finding_gate(bug)
        if terminal is not None:
            return preserve_triage_checkpoint(terminal, bug)
        if triage.get("disposition") == "revision_reverify_required":
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": "fresh current-revision finding lacks a usable sealed provenance boundary",
                       "internal_review_required": True}
            return {"fixed": False, "files": [], "attempts": 0, "verdict": verdict,
                    "plan": None, "restart": restart, "residual": residual,
                    "attempts_log": [{"attempt": 0, "failed_stage": "revision-reverify-provenance"}],
                    "triage": triage, "internal_review_required": True,
                    "review_route": "qa-internal-review"}
        revision_reverified = True

    mutation_receipt_source = "prior_worker" if all_files else None
    if resume_existing and not all_files:
        recovered_files = _changed_files_since_finding(repo, bug)
        if recovered_files:
            all_files.extend(recovered_files)
            mutation_receipt_source = "sealed_finding_delta"

    # A fixer can be interrupted after its dev agent committed the code but while independent browser QA is
    # still running. On the next durable generation, verify the exact failing story only AFTER the original
    # finding passed its sealed evidence gate. If the observed product is already correct, the bounded
    # read-only judge may accept it; repeating repository mutation is neither safer nor more intelligent.
    if resume_existing and not revision_reverified and not triage_resumed:
        restart = {"restarted": False, "healthy": None,
                   "detail": "durable fixer resumed; verifying existing product state before mutation"}
        try:
            residual, verification = focused_qa(bug)
        except Exception as e:
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": f"resume verification interrupted ({e}); no mutation started"}
            return {"fixed": False, "files": [], "attempts": 0, "verdict": verdict, "plan": None,
                    "restart": restart, "residual": [],
                    "attempts_log": [{"attempt": 0, "failed_stage": "resume-verify",
                                      "error": str(e)[:300]}]}
        if not residual:
            if verification is not None and not verification.get("complete"):
                verdict = {"fixed": False, "confidence": 0.0,
                           "reason": "fresh recovery verification is incomplete; checkpoint for continuation",
                           "checkpoint_required": True}
                return {"fixed": False, "files": all_files, "attempts": 0, "verdict": verdict,
                        "plan": None, "restart": restart, "residual": [],
                        "attempts_log": [{"attempt": 0, "failed_stage": "resume-verify-incomplete",
                                          "verification": verification}],
                        "triage": triage, "checkpoint_required": True,
                        "internal_review_required": False,
                        "resume_state_path": verification.get("resume_state_path"),
                        "coverage": verification.get("coverage") or [],
                        "steps_detail": verification.get("steps_detail") or []}
            if not _external_stage_has_runway(
                    deadline, cancel_event, required=READ_ONLY_STAGE_TIMEOUT + 30):
                verdict = {"fixed": False, "confidence": 0.0,
                           "reason": "insufficient runway for recovery verification; checkpoint",
                           "checkpoint_required": True}
                return {"fixed": False, "files": [], "attempts": 0, "verdict": verdict, "plan": None,
                        "restart": restart, "residual": residual, "attempts_log": attempts_log,
                        "triage": triage, "checkpoint_required": True,
                        "internal_review_required": False}
            verdict = _judge_fixed(
                bug, vision, all_files, "", residual, restart, repo=repo, api_key=api_key,
                verification=verification, prior_mutation_receipt=bool(all_files),
                mutation_receipt_source=mutation_receipt_source)
            # A complete explorer run is the direct observation gate: every bounded clause was exercised and
            # it reported zero residual bugs.  When that proof is paired with an exact fenced/sealed mutation
            # receipt, a model review may add caution but must not send the already-clean repository back to a
            # writer merely because the handoff cannot reconstruct a textual pre-mutation diff.  That failure
            # mode caused an unbounded verify -> rewrite -> verify loop.  Concrete residual bugs, incomplete
            # coverage, or a missing receipt still fail closed above and can never take this path.
            complete_recovery_proof = bool(
                all_files
                and isinstance(verification, dict)
                and verification.get("complete") is True
                and not residual
            )
            if complete_recovery_proof and not verdict.get("fixed"):
                advisory_verdict = dict(verdict)
                verdict = {
                    "fixed": True,
                    "confidence": max(0.95, float(advisory_verdict.get("confidence") or 0.0)),
                    "reason": (
                        "accepted by sealed recovery gate: exact mutation receipt plus complete fresh "
                        "coverage with zero residual bugs"
                    ),
                    "acceptance_basis": "sealed_complete_recovery",
                    "reviewer_advisory": advisory_verdict,
                }
            attempts_log.append({"attempt": 0, "changed": [], "residual": 0,
                                 "fixed": verdict["fixed"], "recovery_verification": True})
            _audit("FixBugRecoveryVerification", {"fixed": verdict["fixed"], "residual": 0})
            if verdict["fixed"]:
                return {"fixed": True, "files": all_files, "attempts": 0, "verdict": verdict, "plan": None,
                        "restart": restart, "residual": [], "attempts_log": attempts_log,
                        "triage": triage, "recovery_verified": True}
        else:
            # The fresh browser is ground truth.  A resumed fixer may discover that its original defect is
            # gone but a different release-blocking failure in the same story remains.  Plan against the
            # observed residual, not the stale triggering report; otherwise a worker can repeat an already
            # applied mutation while ignoring the defect it just proved.  Prefer blocking and higher-severity
            # evidence while retaining the original report as provenance for the staff engineer.
            rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
            observed = max(
                (item for item in residual if isinstance(item, dict)),
                key=lambda item: (bool(item.get("blocking")),
                                  rank.get(str(item.get("severity") or "").lower(), 0)),
                default=None)
            if observed:
                prior_bug = bug
                bug = dict(observed)
                bug["recovery_trigger"] = prior_bug
                # Confirmation of the old report is not authority to mutate for an arbitrary newly observed
                # residual. Fence and independently review the actual bug that would drive the repair plan.
                triage, terminal = finding_gate(bug)
                if terminal is not None:
                    return preserve_triage_checkpoint(terminal, bug)
        live_deadline = _deadline_value(deadline)
        if ((cancel_event is not None and cancel_event.is_set())
                or (live_deadline is not None and time.time() >= live_deadline)):
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": "resume verification reached the QA safety deadline; no mutation started"}
            return {"fixed": False, "files": [], "attempts": 0, "verdict": verdict, "plan": None,
                    "restart": restart, "residual": residual, "attempts_log": attempts_log}
    while attempts < max_attempts:
        if not _external_stage_has_runway(
                deadline, cancel_event, required=READ_ONLY_STAGE_TIMEOUT + 30):
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": "insufficient safety runway for repair planning; checkpoint for continuation",
                       "checkpoint_required": True}
            break
        # observe -> AI DECIDES the fix
        plan = _plan_fix(bug, code_context, vision, repo=repo, api_key=api_key)
        if not plan.get("agents"):
            attempts += 1
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": ("confirmed defect received no actionable repair plan; mutation is "
                                  "fail-closed pending internal review"),
                       "internal_review_required": True}
            attempts_log.append({"attempt": attempts, "failed_stage": "planning",
                                 "internal_review_required": True})
            break
        if not _external_stage_has_runway(deadline, cancel_event):
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": "repair plan completed without enough runway to launch a writer; checkpoint",
                       "checkpoint_required": True}
            break
        attempts += 1
        if not in_git and scope_tenant:
            snapshot_receipt = _snapshot_before_nongit_mutation(
                repo, scope_tenant, bug, attempts)
            mutation_snapshots.append(snapshot_receipt)
            if snapshot_receipt.get("status") != "created":
                verdict = {"fixed": False, "confidence": 0.0,
                           "reason": ("non-Git repair has no durable pre-mutation snapshot: "
                                      f"{snapshot_receipt.get('reason') or 'unknown snapshot failure'}"),
                           "checkpoint_required": True}
                attempts_log.append({"attempt": attempts, "failed_stage": "pre-mutation-snapshot",
                                     "snapshot": snapshot_receipt})
                break
        before = _worktree_snapshot(repo) if in_git else _directory_snapshot(repo)
        # ACT — spawn exactly the AI-decided number of role-specialized dev agents
        spawn = _spawn_fix_agents(plan, bug, vision, repo=repo, api_key=api_key)
        if not spawn["ok"]:                                # rc!=0 / crashed agent -> the attempt FAILS
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": f"{len(spawn['failures'])}/{len(spawn['results'])} spawned fix agent(s) "
                                 f"failed (rc!=0) — a crashed/refused agent is not a fix"}
            attempts_log.append({"attempt": attempts, "failed_stage": "spawn",
                                 "failures": spawn["failures"]})
            _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                     "fixed": False, "stage": "spawn-failed",
                                     "failures": len(spawn["failures"])})
            continue
        # GROUND TRUTH — what actually changed, straight from git (never the plan's file list)
        if in_git:
            changed = _changed_since(repo, before)
            diff_text = _git_diff(repo, changed)
        else:
            changed, after = _directory_changed_since(repo, before)
            diff_text = _directory_diff(before, after, changed)
        if diff_text:
            separator = f"\n\n--- repair attempt {attempts} ---\n" if change_diff else ""
            # Keep the beginning and end of a multi-attempt receipt: the first attempt establishes intent and
            # the latest attempt usually contains the correction. Individual unified diffs are already bounded.
            combined_diff = change_diff + separator + diff_text
            if len(combined_diff) > DIFF_LIMIT:
                half = max(1, DIFF_LIMIT // 2)
                combined_diff = (combined_diff[:half]
                                 + "\n...<bounded intermediate mutation diff>...\n"
                                 + combined_diff[-half:])
            change_diff = combined_diff[:DIFF_LIMIT]
        all_files.extend(f for f in changed if f not in all_files)
        # restart/reset the app (tracked-PID kill + relaunch) so we observe a FRESH process
        if restart_cmd:
            restart = restart_target(restart_cmd, health_url=health_url,
                                     cwd=repo if isinstance(repo, str) else None)
            if not restart.get("healthy"):
                verdict = {"fixed": False, "confidence": 0.0,
                           "reason": f"app failed to come back healthy after restart: "
                                     f"{restart.get('detail')}"}
                attempts_log.append({"attempt": attempts, "failed_stage": "restart", "restart": restart})
                _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                         "fixed": False, "stage": "restart-failed"})
                continue
        else:
            restart = {"restarted": False, "healthy": None,
                       "detail": "no restart_cmd — target managed externally; re-exploring live URL"}
        # MANDATORY re-observation — re-run the failing story against the live app
        try:
            focused_state_path = _finding_browser_state_path(bug)
            observed = dev_self_qa(
                target_url, vision,
                _focused_repro_stories(stories, bug, browser_state_path=focused_state_path),
                token=token, org=org, api_key=api_key, max_steps=max_steps,
                deadline=deadline, cancel_event=cancel_event, return_report=True,
                browser_state_path=focused_state_path,
                scope_run_id=scope_run_id, scope_tenant=scope_tenant)
            if isinstance(observed, dict) and isinstance(observed.get("bugs"), list):
                residual, verification = list(observed["bugs"]), observed
            else:
                residual, verification = _as_list(observed), None
        except Exception as e:
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": f"re-exploration failed ({e}) — no observation, no pass"}
            attempts_log.append({"attempt": attempts, "failed_stage": "re-explore", "error": str(e)[:300]})
            _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                     "fixed": False, "stage": "reexplore-failed"})
            continue
        # AI EVALUATES expected-vs-actual against the vision, on diff + fresh observation only
        if not _external_stage_has_runway(
                deadline, cancel_event, required=READ_ONLY_STAGE_TIMEOUT + 30):
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": "insufficient runway for post-fix verification; checkpoint",
                       "checkpoint_required": True}
            attempts_log.append({"attempt": attempts, "changed": changed,
                                 "failed_stage": "verification-checkpoint"})
            break
        if verification is not None and not verification.get("complete") and not residual:
            verdict = {"fixed": False, "confidence": 0.0,
                       "reason": "post-fix browser verification is incomplete",
                       "checkpoint_required": True}
            attempts_log.append({"attempt": attempts, "changed": changed,
                                 "failed_stage": "verification-incomplete",
                                 "verification": verification})
            break
        verdict = _judge_fixed(bug, vision, changed, diff_text, residual, restart,
                               repo=repo, api_key=api_key, verification=verification)
        attempts_log.append({"attempt": attempts, "changed": changed, "residual": len(residual),
                             "fixed": verdict["fixed"]})
        _audit("FixBugAttempt", {"attempt": attempts, "agents": len(plan["agents"]),
                                 "fixed": verdict["fixed"], "files": all_files})
        if verdict["fixed"]:
            break

    return {"fixed": bool(verdict.get("fixed")), "files": all_files, "attempts": attempts,
            "verdict": verdict, "plan": plan, "restart": restart, "residual": residual,
            "attempts_log": attempts_log, "triage": triage,
            "change_diff": change_diff,
            "mutation_snapshots": mutation_snapshots,
            "resume_state_path": (verification.get("resume_state_path")
                                  if isinstance(verification, dict) else None),
            "coverage": (verification.get("coverage") or []
                         if isinstance(verification, dict) else []),
            "steps_detail": (verification.get("steps_detail") or []
                             if isinstance(verification, dict) else []),
            "internal_review_required": bool(verdict.get("internal_review_required")),
            "checkpoint_required": bool(verdict.get("checkpoint_required")),
            "review_route": ("qa-internal-review" if verdict.get("internal_review_required") else None)}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# offline selftest — stubs factory.agent + qa_explorer.Explorer; NO real API calls, deterministic
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile
    import types
    checks = {}
    real_agent = factory.agent
    os.environ["AOS_QA_SETTLE"] = "0.1"                    # fast no-health-url waits
    os.environ["AOS_QA_KILL_SETTLE"] = "0.1"

    # a stub Explorer module: records every story re-explored; returns whatever `residual_script` says.
    explored = []
    residual_script = {"bugs": []}

    class _FakeExplorer:
        def __init__(self, url, vision, token=None, org="0", **kw):
            self.url, self.token, self.org = url, token, org
            self.coverage = []
            self.stop_reason = None
            self.infrastructure_error = None
            self.missing_capabilities = []
            self.resume_state_path = None
        def explore(self, story, max_steps=None, on_bug=None, deadline=None, cancel_event=None, **kw):
            explored.append(story)
            aspect = "focused story behavior is correct"
            self.coverage = [{"aspect": aspect, "covered": True}]
            self.stop_reason = "coverage-complete"
            found = list(residual_script["bugs"])
            if on_bug:
                for item in found:
                    on_bug(dict(item))
            # Match the modern Explorer contract: observations are step receipts; bugs travel through on_bug.
            return [{"step": 0, "matched": not found, "covers": [aspect]}]
        def close(self):
            pass

    fake_mod = types.ModuleType("qa_explorer")
    fake_mod.Explorer = _FakeExplorer
    sys.modules["qa_explorer"] = fake_mod

    # a REAL throwaway git repo — the diff the judge sees must be genuine, not prose.
    tmp = Path(tempfile.mkdtemp(prefix="devloop-selftest-"))
    subprocess.run(["git", "init", "-q", str(tmp)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "qa@test"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "qa"], check=True)
    app = tmp / "app.py"
    app.write_text("def login():\n    return 500\n")
    subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "init"], check=True)

    evidence_tmp = Path(tempfile.mkdtemp(prefix="devloop-evidence-selftest-"))
    bug = {"story": "log in", "expected": "dashboard", "actual": "500 error", "blocking": True,
           "evidence_provenance": capture_finding_provenance(tmp, evidence_tmp)}
    vision = "A todo app that lets users log in."
    sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]

    # ── (1) MANDATORY ARGS: no judge-without-repro path exists.
    try:
        fix_bug(bug, {"repo": str(tmp)}, vision)           # no target_url/stories at all
        checks["mandatory_omitted"] = False
    except TypeError:
        checks["mandatory_omitted"] = True
    try:
        fix_bug(bug, {"repo": str(tmp)}, vision, target_url="", stories=["log in"])
        checks["mandatory_empty_url"] = False
    except ValueError:
        checks["mandatory_empty_url"] = True
    try:
        fix_bug(bug, {"repo": str(tmp)}, vision, target_url="http://x", stories=[])
        checks["mandatory_empty_stories"] = False
    except ValueError:
        checks["mandatory_empty_stories"] = True

    # ── (2) HAPPY PATH: AI plans N agents -> exactly N spawned -> REAL diff judged -> restart+re-explore
    #        happen EVERY attempt -> fixed. The judge prompt must carry the actual git diff content.
    PLANNED = 3
    calls, judge_prompts = [], []

    def fake_agent(role, repo, task, **kw):
        calls.append(role)
        if role == "reviewer" and "SMALLEST correct set of dev agents" in task:
            plan = {"agents": [{"role": "staff-engineer", "files": ["src/imaginary%d.py" % i],
                                "task": f"fix part {i}"} for i in range(PLANNED)],
                    "rationale": "three agents"}
            return {"out_full": "here is the plan " + json.dumps(plan), "rc": 0}
        if role == "reviewer" and "VERIFYING a fix" in task:
            judge_prompts.append(task)
            return {"out_full": json.dumps({"fixed": True, "confidence": 0.95,
                                            "reason": "diff fixes login; story passes"}), "rc": 0}
        if role == "reviewer":
            excerpt = app.read_text().strip()
            return {"out_full": json.dumps({"verdict": "defect", "confidence": 0.95,
                                             "reason": "repository behavior contradicts the story",
                                             "citations": [{"path": "app.py", "start_line": 1,
                                                            "end_line": len(excerpt.splitlines()),
                                                            "quote": excerpt}]}), "rc": 0}
        # a spawned dev agent: ONE of them actually edits the code (the real footprint)
        if "YOUR TASK: fix part 0" in task:
            app.write_text("def login():\n    return 'dashboard'  # FIXED-SENTINEL\n")
        return {"out_full": "done", "rc": 0}

    factory.agent = fake_agent
    explored.clear()
    residual_script["bugs"] = []
    try:
        res = fix_bug(bug, {"repo": str(tmp)}, vision, target_url="http://127.0.0.1:1",
                      stories=["log in"], restart_cmd=sleeper)
    finally:
        factory.agent = real_agent
    checks["spawn_exact"] = calls.count("staff-engineer") == PLANNED
    checks["fixed"] = res["fixed"] is True and res["attempts"] == 1
    # diff-based judging: files come from GIT (the actually-edited file), never the plan's prose list
    checks["files_from_git"] = res["files"] == ["app.py"]
    checks["plan_prose_ignored"] = not any("imaginary" in f for f in res["files"])
    checks["judge_saw_real_diff"] = bool(judge_prompts) and "FIXED-SENTINEL" in judge_prompts[0] \
        and "app.py" in judge_prompts[0]
    # always-restart + always-re-explore: the attempt restarted a fresh process AND re-ran the story
    checks["restarted"] = bool(res["restart"] and res["restart"]["restarted"]
                               and res["restart"]["pid"])
    checks["reexplored"] = (len(explored) == 1 and isinstance(explored[0], dict)
                              and explored[0].get("category") == "focused-regression"
                              and "log in" in explored[0].get("title", ""))
    happy_pid = (res.get("restart") or {}).get("pid")

    # ── (3) rc!=0 FAILS THE ATTEMPT: a crashed/refused dev agent is never judged as a fix.
    calls2, judge2 = [], []

    def failing_agent(role, repo, task, **kw):
        calls2.append(role)
        if role == "reviewer" and "SMALLEST correct set of dev agents" in task:
            return {"out_full": json.dumps({"agents": [{"role": "builder", "files": [], "task": "fix"}],
                                             "rationale": "one"}), "rc": 0}
        if role == "reviewer" and "VERIFYING a fix" in task:
            judge2.append(task)
            return {"out_full": json.dumps({"fixed": True, "confidence": 1.0, "reason": "??"}), "rc": 0}
        if role == "reviewer":
            excerpt = app.read_text().strip()
            return {"out_full": json.dumps({"verdict": "defect", "confidence": 0.95,
                                             "reason": "repository evidence",
                                             "citations": [{"path": "app.py", "start_line": 1,
                                                            "end_line": len(excerpt.splitlines()),
                                                            "quote": excerpt}]}), "rc": 0}
        return {"out_full": "I refuse / crashed", "rc": 1, "failed": True}

    factory.agent = failing_agent
    explored.clear()
    bug["evidence_provenance"] = capture_finding_provenance(tmp, evidence_tmp)
    try:
        res2 = fix_bug(bug, {"repo": str(tmp)}, vision, target_url="http://127.0.0.1:1",
                       stories=["log in"], max_attempts=2)
    finally:
        factory.agent = real_agent
    checks["rc_nonzero_fails"] = res2["fixed"] is False and res2["attempts"] == 2 \
        and not judge2 \
        and all(a.get("failed_stage") == "spawn" for a in res2["attempts_log"])
    checks["rc_nonzero_no_explore"] = explored == []       # a failed attempt is never "observed fixed"

    # ── (4) RE-EXPLORATION FAILURE fails the attempt (no observation => no pass, judge never asked).
    calls3, judge3 = [], []

    def ok_agent(role, repo, task, **kw):
        calls3.append(role)
        if role == "reviewer" and "SMALLEST correct set of dev agents" in task:
            return {"out_full": json.dumps({"agents": [{"role": "builder", "files": [], "task": "fix"}],
                                             "rationale": "one"}), "rc": 0}
        if role == "reviewer" and "VERIFYING a fix" in task:
            judge3.append(task)
            return {"out_full": json.dumps({"fixed": True, "confidence": 1.0, "reason": "??"}), "rc": 0}
        if role == "reviewer":
            excerpt = app.read_text().strip()
            return {"out_full": json.dumps({"verdict": "defect", "confidence": 0.95,
                                             "reason": "repository evidence",
                                             "citations": [{"path": "app.py", "start_line": 1,
                                                            "end_line": len(excerpt.splitlines()),
                                                            "quote": excerpt}]}), "rc": 0}
        return {"out_full": "done", "rc": 0}

    class _BoomExplorer(_FakeExplorer):
        def explore(self, story):
            raise RuntimeError("browser died")

    fake_mod.Explorer = _BoomExplorer
    factory.agent = ok_agent
    bug["evidence_provenance"] = capture_finding_provenance(tmp, evidence_tmp)
    try:
        res3 = fix_bug(bug, {"repo": str(tmp)}, vision, target_url="http://127.0.0.1:1",
                       stories=["log in"], max_attempts=1)
    finally:
        factory.agent = real_agent
        fake_mod.Explorer = _FakeExplorer
    checks["reexplore_error_fails"] = res3["fixed"] is False and not judge3 \
        and res3["attempts_log"][0]["failed_stage"] == "re-explore"

    # ── (5) TRACKED-PID RESTART: kills exactly the pid it launched; never a pattern-kill of bystanders.
    bystander = subprocess.Popen(sleeper, start_new_session=True)   # same cmdline — pkill -f would kill it
    try:
        r1 = restart_target(sleeper)                       # first launch of this key: kills NOTHING
        checks["first_launch_kills_nothing"] = r1["restarted"] and r1["killed_old"] is False
        r2 = restart_target(sleeper)                       # second: kills EXACTLY r1's pid
        old_gone = False
        try:
            os.kill(r1["pid"], 0)
        except ProcessLookupError:
            old_gone = True
        bystander_alive = bystander.poll() is None
        checks["tracked_pid_killed"] = r2["killed_old"] is True and old_gone and r2["pid"] != r1["pid"]
        checks["bystander_survives"] = bystander_alive
        _kill_pid(r2["pid"], proc=_TRACKED.pop(_cmd_key(sleeper, None), None))   # cleanup
    finally:
        try:
            os.killpg(bystander.pid, signal.SIGKILL)
            bystander.wait(timeout=5)
        except Exception:
            pass
    if happy_pid:                                          # cleanup the happy-path restart's process
        _kill_pid(happy_pid, proc=_TRACKED.pop(_cmd_key(sleeper, str(tmp)), None))

    # ── (6) dev_self_qa: runs the Explorer over every story, returns the bugs (the self-QA gate).
    explored.clear()
    residual_script["bugs"] = [{"story": "s", "expected": "works", "actual": "broken",
                                "severity": "high", "blocking": True}]
    bugs = dev_self_qa("http://127.0.0.1:8080", "A todo app.", ["story-1", "story-2"], token="T", org="7")
    checks["selfqa"] = len(bugs) == 2 and explored == ["story-1", "story-2"] \
        and all(b["blocking"] for b in bugs)

    # Modern Explorer returns STEP records and reports actual bugs only through on_bug. Step records must
    # never be misclassified as residual defects (that caused every clean fix to repeat all attempts).
    class _ModernExplorer:
        def __init__(self, *a, **k): pass
        def explore(self, story, max_steps=None, on_bug=None, deadline=None):
            explored.append(story)
            if story == "broken":
                on_bug({"kind": "bug", "story": story, "blocking": True})
            return [{"step": 0, "matched": True}, {"step": 1, "matched": True}]
        def close(self): pass
    fake_mod.Explorer = _ModernExplorer
    explored.clear()
    modern_bugs = dev_self_qa("http://127.0.0.1:8080", "v", ["clean", "broken"], max_steps=3)
    checks["selfqa_steps_not_bugs"] = modern_bugs == [
        {"kind": "bug", "story": "broken", "blocking": True}] and explored == ["clean", "broken"]

    # Exported product directories may intentionally have no .git metadata. They still need concrete,
    # bounded before/after evidence so a valid fix does not become an unfinishable three-attempt loop.
    nongit = Path(tempfile.mkdtemp(prefix="dev-loop-nongit-"))
    try:
        nf = nongit / "app.js"
        nf.write_text("export const ready = false;\n")
        ns = _directory_snapshot(nongit)
        nf.write_text("export const ready = true;\n")
        nc, na = _directory_changed_since(nongit, ns)
        nd = _directory_diff(ns, na, nc)
        checks["nongit_diff_evidence"] = nc == ["app.js"] and "ready = false" in nd \
            and "ready = true" in nd
    finally:
        shutil.rmtree(nongit, ignore_errors=True)

    del sys.modules["qa_explorer"]
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(evidence_tmp, ignore_errors=True)

    ok = all(checks.values())
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print("PASS: dev-fix loop wired — mandatory repro, rc!=0 fails, real-diff judging, tracked-PID "
          "restart, always re-explore ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# CLI — `self-qa` is the shell entry point for the factory's dev-is-first-QA hook.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _read_arg(v):
    """CLI values may be inline text/JSON or @/path/to/file."""
    if isinstance(v, str) and v.startswith("@"):
        return Path(v[1:]).read_text()
    return v


def _cli_self_qa(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="dev_loop.py self-qa",
                                 description="Dev-is-first-QA gate: explore the ORIGINAL stories against "
                                             "the served build; exit 0 clean, 1 bugs, 2 blocking bugs.")
    ap.add_argument("target_url")
    ap.add_argument("--vision", required=True, help="product vision text, or @file")
    ap.add_argument("--stories", required=True, help="JSON list of stories, or @file")
    ap.add_argument("--token", default=None)
    ap.add_argument("--org", default="0")
    a = ap.parse_args(argv)
    vision = _read_arg(a.vision)
    stories = json.loads(_read_arg(a.stories))
    bugs = dev_self_qa(a.target_url, vision, stories, token=a.token, org=a.org)
    blocking = [b for b in bugs if isinstance(b, dict) and b.get("blocking")]
    print(json.dumps({"target": a.target_url, "stories": len(_as_list(stories)),
                      "bugs": bugs, "blocking": len(blocking),
                      "ready_to_handoff": not bugs}, default=str, indent=2))
    sys.exit(2 if blocking else (1 if bugs else 0))


# public API
__all__ = ["fix_bug", "dev_self_qa", "restart_target"]


def _main(argv):
    if argv and argv[0] == "selftest":
        _selftest()
    if argv and argv[0] == "self-qa":
        _cli_self_qa(argv[1:])
    print(__doc__)
    print("commands: selftest | self-qa <target_url> --vision <text|@file> --stories <json|@file>")
    sys.exit(0)


if __name__ == "__main__":
    _main(sys.argv[1:])
