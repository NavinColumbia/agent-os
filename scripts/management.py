#!/usr/bin/env python3
"""Durable, agentic management control plane.

This is the layer between a worker heartbeat and a CEO escalation.  A timer is a
review trigger, never a stop condition: state changes wake a case immediately and
quiet work gets a periodic manager review.  Each review is an AI decision, claimed
with a database lease so two scheduler ticks cannot hold overlapping "meetings".

Workers and managers communicate asynchronously.  A manager may request a report,
rebrief, retry, reassign, add help, open an internal incident, or escalate one tier.
Only a named boundary that genuinely needs a person may create an agent_request.
All state required to resume survives process and host restarts.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
import factory  # noqa: E402
from dbpool import connection  # noqa: E402

DEFAULT_REVIEW_S = max(30, int(os.environ.get("AOS_MANAGEMENT_REVIEW_S", "300")))
LEASE_S = max(120, int(os.environ.get("AOS_MANAGEMENT_LEASE_S", "900")))
# One bounded reasoning turn per scheduler invocation by default. This control
# plane observes expensive work; it must never become a new source of pressure.
MAX_BATCH = max(1, int(os.environ.get("AOS_MANAGEMENT_MAX_BATCH", "1")))
MAX_DUTY_DISCOVERY = max(1, int(os.environ.get("AOS_MANAGEMENT_DISCOVERY_MAX", "20")))
DUTY_SOURCES = (
    "pulse", "qa_dispute", "controller_internal", "actor", "question", "handoff", "schedule",
    "work_contract", "objective", "assurance", "incident",
)
MAX_DUTY_DISCOVERY = max(MAX_DUTY_DISCOVERY, len(DUTY_SOURCES))
DUTY_SOURCE_QUOTA = max(1, MAX_DUTY_DISCOVERY // len(DUTY_SOURCES))
MAX_INTERNAL_LEVEL = 3
MANAGEMENT_CHAIN = ("team-lead", "department-head", "chief-of-staff", "controller")
_ensured = False
_ensure_lock = threading.Lock()


def _bounded_int(name, default, minimum, maximum):
    """A malformed service environment must not disable the control plane."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


MANAGEMENT_DB_LOCK_TIMEOUT_MS = _bounded_int(
    "AOS_MANAGEMENT_DB_LOCK_TIMEOUT_MS", 500, 50, 5000)
MANAGEMENT_DB_STATEMENT_TIMEOUT_MS = max(
    MANAGEMENT_DB_LOCK_TIMEOUT_MS,
    _bounded_int("AOS_MANAGEMENT_DB_STATEMENT_TIMEOUT_MS", 3000, 50, 30000))
MANAGEMENT_SIGNAL_ATTEMPTS = _bounded_int(
    "AOS_MANAGEMENT_SIGNAL_ATTEMPTS", 3, 1, 5)
_RETRYABLE_DB_ERRORS = (
    psycopg.errors.LockNotAvailable,
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
)
VALID_ACTIONS = {
    "continue", "check_back", "request_status", "rebrief", "retry", "reassign",
    "add_help", "open_incident", "escalate_manager", "request_human", "resolve",
}
CONTROLLER_INTERNAL_TRIGGER = "controller_internal_management_orphaned"
QA_PERFORMANCE_TRIGGER = "qa_performance_stalled"


def _conn():
    return connection()


class ManagementDatabaseBusy(RuntimeError):
    """A management state change exhausted its bounded DB retry budget."""

    retryable = True


class ManagementDecisionUnavailable(RuntimeError):
    """No manager decision was actually produced; the durable case must be retried."""

    retryable = True


def _set_db_timeouts(cur, *, lock_ms=None, statement_ms=None):
    lock_ms = MANAGEMENT_DB_LOCK_TIMEOUT_MS if lock_ms is None else max(1, int(lock_ms))
    statement_ms = (MANAGEMENT_DB_STATEMENT_TIMEOUT_MS if statement_ms is None
                    else max(lock_ms, int(statement_ms)))
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{lock_ms}ms",))
    cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{statement_ms}ms",))


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with _conn() as c, c.cursor() as cur:
            _set_db_timeouts(cur, lock_ms=2000,
                             statement_ms=max(5000, MANAGEMENT_DB_STATEMENT_TIMEOUT_MS))
            cur.execute("""CREATE TABLE IF NOT EXISTS management_cases (
            case_id TEXT PRIMARY KEY,
            dedupe_key TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL DEFAULT '_platform',
            product TEXT,
            work_id TEXT,
            subject TEXT NOT NULL,
            worker TEXT,
            manager_role TEXT NOT NULL DEFAULT 'team-lead',
            management_level INT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            trigger TEXT NOT NULL,
            state JSONB NOT NULL DEFAULT '{}',
            semantic_state JSONB NOT NULL DEFAULT '{}',
            observation JSONB NOT NULL DEFAULT '{}',
            state_fingerprint TEXT NOT NULL,
            semantic_generation BIGINT NOT NULL DEFAULT 1,
            reviewed_generation BIGINT NOT NULL DEFAULT 0,
            last_event_generation BIGINT NOT NULL DEFAULT 0,
            last_observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            progress_seq BIGINT NOT NULL DEFAULT 0,
            last_progress_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            next_review_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            lease_owner TEXT,
            lease_until TIMESTAMPTZ,
            human_request_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ)""")
            cur.execute("""ALTER TABLE management_cases
                ADD COLUMN IF NOT EXISTS semantic_state JSONB NOT NULL DEFAULT '{}',
                ADD COLUMN IF NOT EXISTS observation JSONB NOT NULL DEFAULT '{}',
                ADD COLUMN IF NOT EXISTS semantic_generation BIGINT NOT NULL DEFAULT 1,
                ADD COLUMN IF NOT EXISTS reviewed_generation BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS last_event_generation BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS last_observed_at TIMESTAMPTZ NOT NULL DEFAULT now()""")
            cur.execute("ALTER TABLE management_cases DROP CONSTRAINT IF EXISTS management_cases_dedupe_key_key")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS management_cases_tenant_dedupe_uidx
                           ON management_cases(tenant_id,dedupe_key)""")
            cur.execute("""CREATE INDEX IF NOT EXISTS management_cases_due_idx
                       ON management_cases(status,next_review_at)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS management_events (
            event_id BIGSERIAL PRIMARY KEY, case_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT '_platform',
            event_type TEXT NOT NULL, payload JSONB NOT NULL DEFAULT '{}',
            semantic_generation BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("ALTER TABLE management_events ADD COLUMN IF NOT EXISTS semantic_generation BIGINT")
            cur.execute("""CREATE TABLE IF NOT EXISTS management_decisions (
            decision_id BIGSERIAL PRIMARY KEY, case_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT '_platform',
            manager_role TEXT NOT NULL, trigger TEXT NOT NULL,
            action TEXT NOT NULL, rationale TEXT, confidence DOUBLE PRECISION,
            decision JSONB NOT NULL DEFAULT '{}', semantic_generation BIGINT, target TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("""ALTER TABLE management_decisions
                           ADD COLUMN IF NOT EXISTS semantic_generation BIGINT,
                           ADD COLUMN IF NOT EXISTS target TEXT""")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS management_decisions_case_generation_uidx
                           ON management_decisions(case_id,semantic_generation)
                           WHERE semantic_generation IS NOT NULL""")
            cur.execute("""CREATE TABLE IF NOT EXISTS management_questions (
            question_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT '_platform',
            asker TEXT NOT NULL, recipient TEXT NOT NULL, question TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', answer TEXT,
            reply_by TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            answered_at TIMESTAMPTZ)""")
            cur.execute("""CREATE INDEX IF NOT EXISTS management_questions_open_idx
                       ON management_questions(status,reply_by)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS management_dispatches (
            dispatch_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            case_id TEXT NOT NULL, semantic_generation BIGINT NOT NULL,
            action TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            result JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(case_id,semantic_generation,action,target))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS management_duty_cursors (
            tenant_id TEXT NOT NULL DEFAULT '_platform', source TEXT NOT NULL,
            last_key TEXT NOT NULL DEFAULT '', updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY(tenant_id,source))""")
        _ensured = True


def _fingerprint(state: dict) -> str:
    raw = json.dumps(state or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


_VOLATILE_OBSERVATION_KEYS = {
    "age_s", "age_min", "beat_age_s", "silent_s", "overdue_s", "overdue_by",
    "elapsed_s", "observed_at", "checked_at", "updated_at",
}


def _split_observation(value):
    """Separate semantic facts from clock-derived observation telemetry.

    Callers may pass explicit ``semantic_state``/``observation`` to ``signal``;
    this recursive fallback protects older observers that still embed ages in
    their evidence document.
    """
    if isinstance(value, dict):
        semantic, observation = {}, {}
        for key, item in value.items():
            if (key in _VOLATILE_OBSERVATION_KEYS or key.endswith("_age_s")
                    or key.endswith("_overdue_s") or key.startswith("elapsed_")):
                observation[key] = item
                continue
            sem_item, obs_item = _split_observation(item)
            semantic[key] = sem_item
            if obs_item not in ({}, [], None):
                observation[key] = obs_item
        return semantic, observation
    if isinstance(value, list):
        semantic, observation = [], []
        for item in value:
            sem_item, obs_item = _split_observation(item)
            semantic.append(sem_item)
            observation.append(obs_item)
        return semantic, observation if any(x not in ({}, [], None) for x in observation) else []
    return value, None


def signal(dedupe_key: str, subject: str, trigger: str, state: dict | None = None,
           *, tenant_id: str | None = None, product: str | None = None,
           work_id: str | None = None, worker: str | None = None,
           manager_role: str = "team-lead", progress: bool = False,
           semantic_state: dict | None = None, observation: dict | None = None) -> dict:
    """Create/update a management case. A changed state wakes the manager now.

    Repeated identical heartbeats only refresh observation telemetry; they do not
    manufacture events or meetings. ``progress=True`` records real forward motion
    independently of the semantic generation.
    """
    _ensure()
    state = state or {}
    projected_semantic, projected_observation = _split_observation(state)
    semantic_state = projected_semantic if semantic_state is None else (semantic_state or {})
    observation = projected_observation if observation is None else (observation or {})
    tenant_id = tenant_id or "_platform"
    fp = _fingerprint({"trigger": trigger, "state": semantic_state})
    cid = f"mc-{hashlib.sha256(f'{tenant_id}:{dedupe_key}'.encode()).hexdigest()[:20]}"
    last_error = None
    for attempt in range(MANAGEMENT_SIGNAL_ATTEMPTS):
        try:
            with _conn() as c, c.cursor() as cur:
                # This timeout must be set in the SAME transaction as the
                # conflicting unique-key upsert. `_ensure` uses a separate
                # transaction, so its SET LOCAL cannot protect this operation.
                # Keep every attempt bounded, but widen the lock window after
                # confirmed contention. A fixed tiny window can exhaust all
                # retries before an already-committing owner gets scheduled on
                # a loaded host. The statement deadline remains the hard cap.
                lock_ms = min(
                    MANAGEMENT_DB_STATEMENT_TIMEOUT_MS,
                    MANAGEMENT_DB_LOCK_TIMEOUT_MS * (2 ** attempt),
                )
                _set_db_timeouts(
                    cur,
                    lock_ms=lock_ms,
                    statement_ms=MANAGEMENT_DB_STATEMENT_TIMEOUT_MS,
                )
                cur.execute("""INSERT INTO management_cases
                                 (case_id,dedupe_key,tenant_id,product,work_id,subject,worker,
                                  manager_role,trigger,state,semantic_state,observation,state_fingerprint,
                                  progress_seq)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (tenant_id,dedupe_key) DO UPDATE SET
                                 product=COALESCE(EXCLUDED.product,management_cases.product),
                                 work_id=COALESCE(EXCLUDED.work_id,management_cases.work_id),
                                 subject=EXCLUDED.subject,
                                 worker=COALESCE(EXCLUDED.worker,management_cases.worker),
                                 trigger=EXCLUDED.trigger,state=EXCLUDED.state,
                                 semantic_state=EXCLUDED.semantic_state,
                                 observation=EXCLUDED.observation,last_observed_at=now(),
                                 next_review_at=CASE
                                   WHEN management_cases.state_fingerprint <> EXCLUDED.state_fingerprint
                                   THEN now() ELSE management_cases.next_review_at END,
                                 status=CASE
                                   WHEN management_cases.state_fingerprint <> EXCLUDED.state_fingerprint
                                   THEN 'open' ELSE management_cases.status END,
                                 semantic_generation=management_cases.semantic_generation +
                                   CASE WHEN management_cases.state_fingerprint <> EXCLUDED.state_fingerprint
                                        THEN 1 ELSE 0 END,
                                 state_fingerprint=EXCLUDED.state_fingerprint,
                                 progress_seq=management_cases.progress_seq + EXCLUDED.progress_seq,
                                 last_progress_at=CASE WHEN EXCLUDED.progress_seq > 0 THEN now()
                                                       ELSE management_cases.last_progress_at END,
                                 updated_at=CASE
                                   WHEN management_cases.state_fingerprint <> EXCLUDED.state_fingerprint
                                   THEN now() ELSE management_cases.updated_at END
                               RETURNING case_id,status,next_review_at,semantic_generation""",
                            (cid, dedupe_key, tenant_id, product, work_id, subject, worker,
                             manager_role, trigger, json.dumps(state, default=str),
                             json.dumps(semantic_state, default=str),
                             json.dumps(observation, default=str), fp,
                             1 if progress else 0))
                row = cur.fetchone()
                cur.execute("""UPDATE management_cases
                                  SET last_event_generation=semantic_generation
                                WHERE case_id=%s AND last_event_generation<semantic_generation
                                RETURNING semantic_generation""", (row[0],))
                event_generation = cur.fetchone()
                if event_generation:
                    cur.execute("""INSERT INTO management_events
                                     (case_id,event_type,payload,semantic_generation)
                                   VALUES (%s,%s,%s,%s)""",
                                (row[0], trigger, json.dumps({"semantic_state": semantic_state,
                                   "observation": observation, "progress": bool(progress)}),
                                 event_generation[0]))
            # The case upsert and its event committed together. A timed-out
            # attempt rolled both back, so retry convergence emits one event.
            return {"case_id": row[0], "status": row[1], "next_review_at": str(row[2]),
                    "semantic_generation": row[3], "event_emitted": bool(event_generation)}
        except _RETRYABLE_DB_ERRORS as exc:
            last_error = exc
            if attempt + 1 < MANAGEMENT_SIGNAL_ATTEMPTS:
                time.sleep(min(0.2, 0.025 * (2 ** attempt)))
    raise ManagementDatabaseBusy(
        f"management case {cid} is contended; retry after the current owner commits") from last_error


def _duty_candidates(budget: dict, source: str, candidates: list[dict]) -> int:
    """Select a small semantic delta slice with a durable per-source cursor.

    Each candidate contains the keyword arguments for ``signal`` plus an optional
    ``cursor_key``. Existing identical fingerprints are removed in one semantic
    NOT EXISTS filter before the quota is spent, so a noisy fixed prefix can
    neither consume the turn nor starve a changed tail.
    """
    remaining = max(0, int(budget.get("remaining", 0)))
    if not candidates or remaining <= 0:
        return 0
    quota = min(DUTY_SOURCE_QUOTA, remaining)
    prepared = []
    for candidate in candidates:
        item = dict(candidate)
        state = item.get("state") or {}
        semantic, observation = _split_observation(state)
        if item.get("semantic_state") is not None:
            semantic = item["semantic_state"] or {}
        if item.get("observation") is not None:
            observation = item["observation"] or {}
        item["semantic_state"], item["observation"] = semantic, observation
        item["_fingerprint"] = _fingerprint({"trigger": item["trigger"], "state": semantic})
        item["_cursor_key"] = str(item.pop("cursor_key", "") or
                                  f"{item.get('tenant_id') or '_platform'}:{item['dedupe_key']}")
        prepared.append(item)
    prepared.sort(key=lambda x: x["_cursor_key"])

    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT last_key FROM management_duty_cursors WHERE tenant_id='_platform' AND source=%s",
                    (source,))
        row = cur.fetchone()
        last_key = row[0] if row else ""
        cur.execute("""SELECT candidate.tenant_id,candidate.dedupe_key,candidate.fingerprint
                         FROM unnest(%s::text[],%s::text[],%s::text[])
                           AS candidate(tenant_id,dedupe_key,fingerprint)
                        WHERE NOT EXISTS (
                          SELECT 1 FROM management_cases m
                           WHERE m.tenant_id=candidate.tenant_id
                             AND m.dedupe_key=candidate.dedupe_key
                             AND m.state_fingerprint=candidate.fingerprint)""",
                    ([str(x.get("tenant_id") or "_platform") for x in prepared],
                     [x["dedupe_key"] for x in prepared], [x["_fingerprint"] for x in prepared]))
        changed_keys = set(cur.fetchall())

    split = next((i for i, item in enumerate(prepared) if item["_cursor_key"] > last_key), len(prepared))
    ordered = prepared[split:] + prepared[:split]
    changed = [item for item in ordered
               if (str(item.get("tenant_id") or "_platform"), item["dedupe_key"],
                   item["_fingerprint"]) in changed_keys]
    selected = changed[:quota]
    selected_ids = {id(item) for item in selected}
    telemetry = [item for item in ordered if id(item) not in selected_ids][:quota - len(selected)]
    processed = selected + telemetry
    if processed:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO management_duty_cursors(tenant_id,source,last_key)
                           VALUES ('_platform',%s,%s)
                           ON CONFLICT (tenant_id,source) DO UPDATE
                             SET last_key=EXCLUDED.last_key,updated_at=now()""",
                        (source, processed[-1]["_cursor_key"]))
    for item in processed:
        item.pop("_fingerprint", None)
        item.pop("_cursor_key", None)
        signal(**item)
    budget["remaining"] = remaining - len(processed)
    return len(selected)


def ask_internal(case_id: str, asker: str, recipient: str, question: str,
                 reply_within_s: int = DEFAULT_REVIEW_S, send_fn: Callable | None = None) -> str:
    """Ask asynchronously and exactly once; never wait in-process.

    Orchestra workers are durable actors but are not necessarily registered in the corporate directory.
    Delivery therefore targets the tenant mailbox directly and uses the management generation as its
    idempotency boundary.  A crash after the question or message commit safely reuses both.
    """
    _ensure()
    wait_s = max(30, int(reply_within_s))
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT tenant_id,semantic_generation FROM management_cases
                       WHERE case_id=%s FOR UPDATE""", (case_id,))
        parent = cur.fetchone()
        if not parent:
            raise KeyError(f"management case {case_id!r} not found")
        tenant_id, generation = parent
        digest = hashlib.sha256(
            f"{tenant_id}:{case_id}:{generation}:{asker}:{recipient}:{question}".encode()).hexdigest()
        qid = f"mq-{digest[:20]}"
        cur.execute("""INSERT INTO management_questions
                         (question_id,case_id,tenant_id,asker,recipient,question,reply_by)
                       VALUES (%s,%s,%s,%s,%s,%s,now()+(%s||' seconds')::interval)
                       ON CONFLICT (question_id) DO NOTHING""",
                    (qid, case_id, tenant_id, asker, recipient, question, str(wait_s)))
        cur.execute("""UPDATE management_cases SET status='waiting_internal',
                         next_review_at=now()+(%s||' seconds')::interval,updated_at=now()
                       WHERE case_id=%s""", (str(wait_s), case_id))

    action = "internal_question"
    key = _dispatch_key({"tenant_id": tenant_id, "case_id": case_id,
                         "semantic_generation": generation}, action, recipient)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO management_dispatches
            (dispatch_key,tenant_id,case_id,semantic_generation,action,target)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING""",
                    (key, tenant_id, case_id, generation, action, recipient))
        cur.execute("SELECT status FROM management_dispatches WHERE dispatch_key=%s", (key,))
        already_delivered = cur.fetchone()[0] == "delivered"
    if already_delivered:
        return qid

    if send_fn:
        signature = inspect.signature(send_fn)
        accepts_key = ("idempotency_key" in signature.parameters
                       or any(p.kind == inspect.Parameter.VAR_KEYWORD
                              for p in signature.parameters.values()))
        kwargs = {"idempotency_key": key, "tenant_id": tenant_id} if accepts_key else {}
        result = send_fn(asker, recipient, "query", question, **kwargs)
        result = result if isinstance(result, dict) else {"sent": True}
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE management_dispatches
                              SET status='delivered',result=%s,updated_at=now()
                            WHERE dispatch_key=%s AND status='pending'""",
                        (json.dumps(result, default=str), key))
        return qid

    message_key = f"{key}:message"
    message_id = "dm-" + hashlib.sha256(f"{tenant_id}:{message_key}".encode()).hexdigest()[:24]
    result = {"sent": True, "message_id": message_id, "recipient": recipient,
              "question_id": qid}
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO conversations
            (conversation_id,message_id,intent,sender,recipient,content,tenant_id,idempotency_key)
            VALUES (%s,%s,'query',%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING""",
                    (f"dm-{asker}-{recipient}", message_id, asker, recipient,
                     json.dumps({"text": question, "question_id": qid}), tenant_id, message_key))
        cur.execute("""INSERT INTO inbox(subscriber,message_id,tenant_id)
                       VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (recipient, message_id, tenant_id))
        cur.execute("""UPDATE management_dispatches
                          SET status='delivered',result=%s,updated_at=now()
                        WHERE dispatch_key=%s AND status='pending'""",
                    (json.dumps(result), key))
    return qid


def answer(question_id: str, answer_text: str, responder: str | None = None) -> bool:
    """Durably answer a manager question and wake its case immediately."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE management_questions SET status='answered',answer=%s,answered_at=now()
                       WHERE question_id=%s AND status='open' RETURNING case_id,recipient""",
                    (answer_text, question_id))
        row = cur.fetchone()
        if not row:
            return False
        if responder and responder != row[1]:
            raise PermissionError(f"{responder} is not the requested respondent")
        cur.execute("SELECT semantic_state FROM management_cases WHERE case_id=%s FOR UPDATE", (row[0],))
        semantic = dict((cur.fetchone() or ({},))[0] or {})
        semantic["communication_answer"] = {"question_id": question_id,
                                              "responder": responder or row[1]}
        fp = _fingerprint({"trigger": "answer", "state": semantic})
        cur.execute("""UPDATE management_cases SET status='open',trigger='answer',
                         state=state || %s::jsonb,semantic_state=%s,state_fingerprint=%s,
                         semantic_generation=semantic_generation+1,
                         last_event_generation=semantic_generation+1,
                         next_review_at=now(),updated_at=now()
                       WHERE case_id=%s RETURNING semantic_generation""",
                    (json.dumps({"communication_answer": semantic["communication_answer"]}),
                     json.dumps(semantic), fp, row[0]))
        generation = cur.fetchone()[0]
        cur.execute("""INSERT INTO management_events(case_id,event_type,payload,semantic_generation)
                       VALUES (%s,'answer',%s,%s)""",
                    (row[0], json.dumps({"question_id": question_id}), generation))
    return True


def _claim(owner: str, limit: int = MAX_BATCH) -> list[dict]:
    """Short SKIP LOCKED claim. The model call never runs while a DB lock is held."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT m.case_id FROM management_cases m
                       JOIN tenants t ON t.tenant_id=m.tenant_id
                       WHERE m.status IN ('open','waiting_internal') AND m.next_review_at <= now()
                         AND m.reviewed_generation < m.semantic_generation
                         AND (m.lease_until IS NULL OR m.lease_until < now())
                       ORDER BY m.next_review_at,m.created_at
                       FOR UPDATE SKIP LOCKED LIMIT %s""", (int(limit),))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            return []
        cur.execute("""UPDATE management_cases SET lease_owner=%s,
                         lease_until=now()+(%s||' seconds')::interval
                       WHERE case_id=ANY(%s)
                       RETURNING case_id,tenant_id,product,work_id,subject,worker,manager_role,
                         management_level,status,trigger,state,semantic_state,observation,
                         state_fingerprint,semantic_generation,reviewed_generation,
                         progress_seq,last_progress_at""",
                    (owner, str(LEASE_S), ids))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _claim_one(case_id: str, owner: str) -> dict | None:
    """Lease exactly one case, regardless of its periodic review time."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT case_id FROM management_cases
                       WHERE case_id=%s AND status IN ('open','waiting_internal')
                         AND reviewed_generation < semantic_generation
                         AND (lease_until IS NULL OR lease_until < now())
                       FOR UPDATE SKIP LOCKED""", (case_id,))
        if not cur.fetchone():
            return None
        cur.execute("""UPDATE management_cases SET lease_owner=%s,
                         lease_until=now()+(%s||' seconds')::interval
                       WHERE case_id=%s
                       RETURNING case_id,tenant_id,product,work_id,subject,worker,manager_role,
                         management_level,status,trigger,state,semantic_state,observation,
                         state_fingerprint,semantic_generation,reviewed_generation,
                         progress_seq,last_progress_at""",
                    (owner, str(LEASE_S), case_id))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None


def _context(case: dict) -> dict:
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT question_id,asker,recipient,question,status,answer,reply_by,
                              reply_by < now() AS overdue
                       FROM management_questions WHERE case_id=%s
                       ORDER BY created_at DESC LIMIT 8""", (case["case_id"],))
        qs = [{"id": r[0], "asker": r[1], "recipient": r[2], "question": r[3],
               "status": r[4], "answer": r[5], "reply_by": str(r[6]), "overdue": r[7]}
              for r in cur.fetchall()]
        cur.execute("""SELECT event_type,payload,created_at FROM management_events
                       WHERE case_id=%s ORDER BY event_id DESC LIMIT 12""", (case["case_id"],))
        ev = [{"type": r[0], "payload": r[1], "at": str(r[2])} for r in cur.fetchall()]
        human = None
        cur.execute("""SELECT r.id,r.kind,r.question,r.status,r.answer,r.answered_at
                       FROM management_cases m LEFT JOIN agent_requests r ON r.id=m.human_request_id
                       WHERE m.case_id=%s""", (case["case_id"],))
        hr = cur.fetchone()
        if hr and hr[0] is not None:
            human = {"request_id": hr[0], "kind": hr[1], "question": hr[2],
                     "status": hr[3], "answer": hr[4],
                     "answered_at": str(hr[5]) if hr[5] else None}
    return {"case": {k: (str(v) if k == "last_progress_at" else v) for k, v in case.items()},
            "questions": qs, "human_request": human, "recent_events": ev}


def _parse_decision(result) -> dict:
    if isinstance(result, dict) and (result.get("failed") or result.get("rc", 0) not in (0, None)):
        reason = result.get("reason") or result.get("blocker") or "agent provider returned no decision"
        raise ManagementDecisionUnavailable(str(reason)[:300])
    if isinstance(result, dict) and "action" in result:
        d = dict(result)
    else:
        txt = ((result.get("out_full") or result.get("out") or "")
               if isinstance(result, dict) else str(result or ""))
        start, end = txt.find("{"), txt.rfind("}")
        try:
            d = json.loads(txt[start:end + 1]) if start >= 0 and end > start else {}
        except Exception:
            d = {}
    if not isinstance(d, dict) or not d.get("action"):
        raise ManagementDecisionUnavailable("manager returned malformed output without an action")
    action = str(d.get("action") or "").lower()
    if action not in VALID_ACTIONS:
        raise ManagementDecisionUnavailable(f"manager returned unsupported action {action!r}")
    d["action"] = action
    d["review_in_s"] = min(1800, max(30, int(d.get("review_in_s") or DEFAULT_REVIEW_S)))
    try:
        d["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0.5))))
    except Exception:
        d["confidence"] = 0.5
    return d


def _decide(case: dict, decide_fn: Callable | None = None) -> dict:
    context = _context(case)
    prompt = """You are the manager currently accountable for this work. Make one concrete decision.
The timer is a CHECK-IN, never a reason to stop useful work. Judge progress, elapsed silence, failures,
answers, risk, and authority. Resolve locally when confident; ask the worker asynchronously for facts when
needed; add help/reassign/retry if that is better; escalate one internal management tier if uncertain or
cross-team; request a human only for a named authority boundary that agents cannot possess.

Reply ONLY JSON with: action, rationale, confidence, review_in_s, message, target_role, human_boundary.
action must be one of continue, check_back, request_status, rebrief, retry, reassign, add_help,
open_incident, escalate_manager, request_human, resolve.
""" + json.dumps(context, default=str)[:12000]
    fn = decide_fn or (lambda role, task: factory.agent(role, str(SCRIPTS.parent), task,
                                                        spawner="controller", light=True))
    return _parse_decision(fn(case["manager_role"], prompt))


def _next_manager(level: int) -> tuple[int, str]:
    nxt = min(MAX_INTERNAL_LEVEL, int(level or 0) + 1)
    return nxt, MANAGEMENT_CHAIN[nxt]


def _resume_qa_dispute(case: dict, action: str, rationale: str, confidence) -> bool:
    """Route a manager decision to evidence collection, never treat the decision as evidence."""
    import qareview
    if (not _is_qa_dispute_case(case)
            or action not in {"continue", "retry", "reassign", "add_help", "rebrief", "resolve"}):
        return False
    state = dict(case.get("state") or {})
    review_id = str(state.get("review_id") or "")
    owner = _qa_process_owner(case, review_id)
    if not owner:
        return False
    run_id, coordinator_actor_id, record = owner
    collection = qareview.begin_evidence_collection(
        case["tenant_id"], state["case_id"],
        {"action": action, "rationale": str(rationale or "")[:1000],
         "confidence": confidence, "management_case_id": case.get("case_id")},
        actor=f"management:{case['manager_role']}")
    if collection.get("status") in {"resolved", "external_authority"}:
        return False
    story = state.get("story") or record.get("story") or (record.get("finding") or {}).get("story")
    payload = {"qa_evidence_recovery": {
        "review_id": review_id, "case_id": state["case_id"], "story": story,
        "action": action, "rationale": str(rationale or "")[:1000],
        "confidence": confidence, "management_case_id": case.get("case_id"),
        "state_generation": collection.get("state_generation")}}
    return _emit_qa_process_recovery(
        case, run_id, coordinator_actor_id, payload, "evidence")


def _resume_qa_performance(case: dict, action: str, rationale: str, confidence) -> bool:
    """Route a manager's process decision to the exact durable QA coordinator.

    Performance stalls do not have a sealed product-evidence dispute case, so sending a status question to a
    symbolic ``qa-manager`` mailbox can never receive an answer. The orchestra coordinator is the real owner;
    wake it with one idempotent event and let its single-writer step schedule the next evidence continuation.
    """
    if (not _is_qa_process_case(case, "performance")
            or action not in {"continue", "retry", "rebrief", "reassign", "add_help", "resolve"}):
        return False
    state = dict(case.get("state") or {})
    review_id = str(state.get("review_id") or "")
    tenant_id = str(case.get("tenant_id") or "")
    owner = _qa_process_owner(case, review_id)
    if not owner or not tenant_id:
        return False
    run_id, coordinator_actor_id, record = owner
    story = state.get("story") or record.get("story") or (record.get("finding") or {}).get("story")
    payload = {"qa_performance_recovery": {
        "review_id": review_id, "story": story, "action": action,
        "rationale": str(rationale or "")[:1000], "confidence": confidence,
        "management_case_id": case.get("case_id")}}
    return _emit_qa_process_recovery(case, run_id, coordinator_actor_id, payload, "performance")


def _is_qa_process_case(case: dict, kind: str | None = None) -> bool:
    """Recognize coordinator-owned QA process cases even after an overdue-question event changed trigger."""
    state = dict(case.get("state") or {})
    review_id = str(state.get("review_id") or "")
    trigger = str(case.get("trigger") or "")
    inferred = None
    if trigger == QA_PERFORMANCE_TRIGGER:
        inferred = "performance"
    elif trigger == "qa_capability_unavailable" or review_id.startswith("qa-capability-") \
            or bool(state.get("capabilities")):
        inferred = "capability"
    elif (review_id and not state.get("case_id")
          and str(case.get("work_id") or "").startswith("orchestra:")):
        # A sealed evidence dispute always carries its qad-* case id. A process
        # stall has no evidence-disposition case and is owned by the coordinator.
        inferred = "performance"
    return inferred == kind if kind else inferred is not None


def _is_qa_dispute_case(case: dict) -> bool:
    """Recognize a sealed dispute after communication telemetry has replaced its original trigger."""
    state = dict(case.get("state") or {})
    return (str(state.get("case_id") or "").startswith("qad-")
            and bool(str(state.get("review_id") or ""))
            and bool(str(case.get("tenant_id") or ""))
            and str(case.get("work_id") or "").startswith("orchestra:"))


def _qa_process_owner(case: dict, review_id: str):
    """Resolve the exact durable coordinator, recovering IDs lost from later communication telemetry."""
    state = dict(case.get("state") or {})
    try:
        run_id = int(state.get("run_id"))
    except (TypeError, ValueError):
        match = re.match(r"^orchestra:(\d+):", str(case.get("work_id") or ""))
        if not match:
            return None
        run_id = int(match.group(1))
    tenant_id = str(case.get("tenant_id") or "")
    if not review_id or not tenant_id:
        return None
    orchestra_dir = SCRIPTS / "orchestra"
    if str(orchestra_dir) not in sys.path:
        sys.path.insert(0, str(orchestra_dir))
    import store
    try:
        coordinator_actor_id = int(state.get("coordinator_actor_id"))
        coordinator = store.actor(coordinator_actor_id, tenant_id)
    except (TypeError, ValueError):
        coordinator = None
    if not coordinator or int(coordinator.get("run_id") or 0) != run_id \
            or coordinator.get("role") != "qa-coordinator":
        candidates = [item for item in store.actors(run_id, tenant_id)
                      if item.get("role") == "qa-coordinator"]
        coordinator = next((item for item in candidates if any(
            str((record or {}).get("review_id") or "") == review_id
            for record in ((item.get("memory") or {}).get("internal_reviews") or []))), None)
        if not coordinator and len(candidates) == 1:
            coordinator = candidates[0]
    if not coordinator:
        return None
    records = list(((coordinator.get("memory") or {}).get("internal_reviews") or []))
    record = next((item for item in records
                   if str((item or {}).get("review_id") or "") == review_id), {})
    return run_id, int(coordinator["actor_id"]), record


def _emit_qa_process_recovery(case, run_id, coordinator_actor_id, payload, kind) -> bool:
    orchestra_dir = SCRIPTS / "orchestra"
    if str(orchestra_dir) not in sys.path:
        sys.path.insert(0, str(orchestra_dir))
    import store
    tenant_id = str(case.get("tenant_id") or "")
    if not tenant_id:
        return False
    store.resume_run(run_id, tenant_id)
    emitted = store.emit_once(
        run_id, tenant_id, None, coordinator_actor_id, "context_update", payload,
        corr_id=(f"management-qa-{kind}:{case.get('case_id')}:"
                 f"{case.get('semantic_generation') or 1}"))
    return not bool((emitted or {}).get("error"))


def _resume_qa_capability(case: dict, action: str, rationale: str, confidence) -> bool:
    if (not _is_qa_process_case(case, "capability")
            or action not in {"continue", "retry", "rebrief", "reassign", "add_help", "resolve"}):
        return False
    state = dict(case.get("state") or {})
    review_id = str(state.get("review_id") or "")
    owner = _qa_process_owner(case, review_id)
    if not owner:
        return False
    run_id, coordinator_actor_id, record = owner
    story = state.get("story") or record.get("story") or (record.get("finding") or {}).get("story")
    payload = {"qa_capability_recovery": {
        "review_id": review_id, "story": story, "action": action,
        "rationale": str(rationale or "")[:1000], "confidence": confidence,
        "management_case_id": case.get("case_id")}}
    return _emit_qa_process_recovery(case, run_id, coordinator_actor_id, payload, "capability")


def _resume_controller_internal_wait(case: dict, action: str, rationale: str) -> bool:
    """Release a controller checkpoint only after an accountable manager acts.

    A QA worker can checkpoint on ``awaiting=internal_management`` while it
    hands a dispute to the QA management queue. If the worker dies before that
    durable dispute is created, the controller row otherwise has no possible
    wake-up source. The duty manager may retry/continue that orphaned handoff;
    active jobs or active QA disputes always win and make this a no-op.
    """
    if (case.get("trigger") != CONTROLLER_INTERNAL_TRIGGER
            or action not in {"continue", "retry", "rebrief", "resolve"}):
        return False
    state = case.get("state") or {}
    try:
        thread_id = int(state.get("thread_id"))
    except (TypeError, ValueError):
        return False
    with _conn() as c, c.cursor() as cur:
        _set_db_timeouts(cur)
        cur.execute("""SELECT tenant_id,phase,awaiting FROM controller_state
                        WHERE thread_id=%s FOR UPDATE""", (thread_id,))
        row = cur.fetchone()
        if not row or row[2] != "internal_management" or row[0] != case.get("tenant_id"):
            return False
        cur.execute("""SELECT EXISTS (SELECT 1 FROM controller_jobs
                        WHERE thread_id=%s AND status IN ('pending','running'))""", (thread_id,))
        if cur.fetchone()[0]:
            return False
        cur.execute("""SELECT EXISTS (SELECT 1 FROM qa_evidence_disputes
                        WHERE tenant_id=%s AND thread_id=%s
                          AND status NOT IN ('resolved','closed','cancelled'))""",
                    (case.get("tenant_id"), thread_id))
        if cur.fetchone()[0]:
            return False
        cur.execute("""UPDATE controller_state
                          SET awaiting=NULL,job_kind=NULL,job_started_at=NULL,job_eta_min=NULL,
                              job_status=NULL,job_sla_warned=false,job_sla_claimed_at=NULL,
                              job_sla_claim_token=NULL,
                              qa_checkpoint_count=CASE WHEN phase='TESTQA' THEN 0
                                                       ELSE qa_checkpoint_count END,
                              qa_last_completed=CASE WHEN phase='TESTQA' THEN NULL
                                                     ELSE qa_last_completed END,
                              qa_no_progress_count=CASE WHEN phase='TESTQA' THEN 0
                                                       ELSE qa_no_progress_count END,
                              updated_at=now()
                        WHERE thread_id=%s AND awaiting='internal_management'""", (thread_id,))
        if cur.rowcount != 1:
            return False
        cur.execute("""UPDATE management_cases
                          SET status='resolved',resolved_at=now(),lease_owner=NULL,lease_until=NULL,
                              state=state || %s::jsonb,updated_at=now()
                        WHERE case_id=%s""",
                    (json.dumps({"controller_resumed": True, "resume_action": action,
                                 "resume_rationale": rationale[:500]}), case["case_id"]))
        cur.execute("""UPDATE management_questions
                          SET status='cancelled',answered_at=now(),
                              answer='Controller checkpoint was resumed by management.'
                        WHERE case_id=%s AND status='open'""", (case["case_id"],))
        cur.execute("""INSERT INTO management_events(case_id,event_type,payload,semantic_generation)
                        VALUES (%s,'controller_resumed',%s,%s)""",
                    (case["case_id"], json.dumps({"thread_id": thread_id, "phase": row[1],
                                                  "action": action, "rationale": rationale[:500]}),
                     case.get("semantic_generation")))
    audit.append(actor=f"management:{case.get('manager_role') or 'duty-manager'}",
                 action="ControllerCheckpointResume", resource=str(thread_id), decision=action,
                 payload={"case_id": case["case_id"], "phase": row[1],
                          "rationale": rationale[:300]}, tenant_id=case.get("tenant_id"))
    return True


def _dispatch_key(case: dict, action: str, target: str) -> str:
    raw = f"{case['tenant_id']}:{case['case_id']}:{case['semantic_generation']}:{action}:{target}"
    return "md-" + hashlib.sha256(raw.encode()).hexdigest()


def _dispatch_staffing(case: dict, action: str, target: str, message: str,
                       send_fn: Callable | None = None) -> dict:
    """Converge every retry of one semantic management action on one dispatch.

    The production orchestration route uses the same key for its task, mailbox
    message, or hire row, so a crash after any one commit can safely replay.
    """
    key = _dispatch_key(case, action, target)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO management_dispatches
            (dispatch_key,tenant_id,case_id,semantic_generation,action,target)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING""",
                    (key, case["tenant_id"], case["case_id"], case["semantic_generation"],
                     action, target))
        cur.execute("""SELECT dispatch_key,status,result FROM management_dispatches
                       WHERE case_id=%s AND semantic_generation=%s AND action=%s AND target=%s""",
                    (case["case_id"], case["semantic_generation"], action, target))
        key, status, result = cur.fetchone()
    if status == "delivered":
        return dict(result or {})

    if send_fn:
        # Test/custom transports are expected to honor the supplied semantic key
        # when they accept it; the built-in durable route below is fully atomic.
        signature = inspect.signature(send_fn)
        accepts_key = ("idempotency_key" in signature.parameters
                       or any(p.kind == inspect.Parameter.VAR_KEYWORD
                              for p in signature.parameters.values()))
        if accepts_key:
            result = send_fn(case["manager_role"], target, "delegate", message,
                             idempotency_key=key, tenant_id=case["tenant_id"])
        else:
            result = send_fn(case["manager_role"], target, "delegate", message)
        result = result if isinstance(result, dict) else {"sent": True}
    else:
        import orchestrate
        result = orchestrate.request_collaborator(
            case["manager_role"], target, message, priority=2,
            tenant_id=case["tenant_id"], idempotency_key=key)

    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE management_dispatches SET status='delivered',result=%s,updated_at=now()
                       WHERE dispatch_key=%s""", (json.dumps(result, default=str), key))
    return result


def _send_worker_instruction(case: dict, action: str, message: str,
                             send_fn: Callable | None = None) -> dict:
    """Deliver a manager's non-question decision to its worker exactly once.

    A durable management decision is not operationally complete while it exists only in
    ``management_decisions``.  In particular, ``continue`` used to advance the case without telling the
    worker that had asked what to do.  Reuse the dispatch ledger as an outbox and give the conversation a
    deterministic tenant-scoped idempotency key, so replay after either commit cannot duplicate the message.
    """
    target = str(case.get("worker") or "").strip()
    tenant_id = str(case.get("tenant_id") or "").strip()
    if not target or not tenant_id:
        raise ValueError("worker instruction requires tenant and worker ownership")
    dispatch_action = f"{action}_instruction"
    key = _dispatch_key(case, dispatch_action, target)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO management_dispatches
            (dispatch_key,tenant_id,case_id,semantic_generation,action,target)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING""",
                    (key, tenant_id, case["case_id"], case["semantic_generation"],
                     dispatch_action, target))
        cur.execute("""SELECT status,result FROM management_dispatches
                       WHERE case_id=%s AND semantic_generation=%s AND action=%s AND target=%s""",
                    (case["case_id"], case["semantic_generation"], dispatch_action, target))
        status, prior = cur.fetchone()
    if status == "delivered":
        return dict(prior or {})

    sender = str(case.get("manager_role") or "manager")
    if send_fn:
        signature = inspect.signature(send_fn)
        accepts_key = ("idempotency_key" in signature.parameters
                       or any(p.kind == inspect.Parameter.VAR_KEYWORD
                              for p in signature.parameters.values()))
        kwargs = {"idempotency_key": key, "tenant_id": tenant_id} if accepts_key else {}
        result = send_fn(sender, target, "instruction", message, **kwargs)
        result = result if isinstance(result, dict) else {"sent": True}
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE management_dispatches
                              SET status='delivered',result=%s,updated_at=now()
                            WHERE dispatch_key=%s AND status='pending'""",
                        (json.dumps(result, default=str), key))
        return result

    message_key = f"{key}:message"
    message_id = "dm-" + hashlib.sha256(f"{tenant_id}:{message_key}".encode()).hexdigest()[:24]
    result = {"sent": True, "message_id": message_id, "recipient": target}
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO conversations
            (conversation_id,message_id,intent,sender,recipient,content,tenant_id,idempotency_key)
            VALUES (%s,%s,'instruction',%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING""",
                    (f"dm-{sender}-{target}", message_id, sender, target,
                     json.dumps({"text": message}), tenant_id, message_key))
        cur.execute("""INSERT INTO inbox(subscriber,message_id,tenant_id)
                       VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (target, message_id, tenant_id))
        cur.execute("""UPDATE management_dispatches
                          SET status='delivered',result=%s,updated_at=now()
                        WHERE dispatch_key=%s AND status='pending'""",
                    (json.dumps(result), key))
    return result


def _apply(case: dict, d: dict, send_fn: Callable | None = None,
           human_fn: Callable | None = None, alert_fn: Callable | None = None):
    action = d["action"]
    rationale = str(d.get("rationale") or "")[:2000]
    review_s = d["review_in_s"]
    level, role = int(case["management_level"] or 0), case["manager_role"]
    status = "open"
    human_request_id = None
    qa_process = _is_qa_process_case(case)
    qa_dispute = _is_qa_dispute_case(case)
    qa_owned = qa_process or qa_dispute
    if qa_owned and action == "request_status":
        # The evidence attempts and gap are already in the case. There is no separate mailbox worker to ask;
        # turn the check-in into an actionable rebrief to the owning durable coordinator.
        action = "rebrief"
        rationale = ("Manager requested a status rebrief; resume the exact story and report through its "
                     "durable evidence ledger. " + rationale)[:2000]

    if action == "continue" and case.get("worker") and not qa_process:
        message = d.get("message") or f"Continue the work: {rationale or 'management approved the next step'}"
        try:
            _send_worker_instruction(case, action, message, send_fn=send_fn)
        except Exception as e:
            action, rationale = "open_incident", f"manager instruction delivery failed: {e}"

    if (qa_owned
            and action in {"continue", "retry", "rebrief", "reassign", "add_help", "resolve"}):
        status = "resolved"
    elif (case.get("trigger") == CONTROLLER_INTERNAL_TRIGGER
            and action in {"continue", "retry", "rebrief", "resolve"}):
        # The durable controller row is the worker. Its exact fenced resume is
        # performed after this decision commits; there is nobody to synchronously
        # question, and manufacturing an async question would strand the case.
        status = "open"
    elif action in {"request_status", "rebrief", "retry"}:
        recipient = case.get("worker")
        if recipient:
            msg = d.get("message") or f"Manager check-in: {rationale or action}"
            ask_internal(case["case_id"], role, recipient, msg, review_s, send_fn=send_fn)
            status = "waiting_internal"
        else:
            action, rationale = "open_incident", "No worker is attached to this case; assign an owner"
    elif action in {"reassign", "add_help"}:
        target = d.get("target_role") or "operator"
        msg = d.get("message") or f"Assist with {case['subject']}: {rationale}"
        try:
            _dispatch_staffing(case, action, target, msg, send_fn=send_fn)
        except Exception as e:
            action, rationale = "open_incident", f"staffing dispatch failed: {e}"
    if action == "escalate_manager":
        level, role = _next_manager(level)
        if level == int(case["management_level"] or 0):
            action = "open_incident"
            rationale = rationale or "top internal management tier needs an incident investigation"
        review_s = 30
    if action == "open_incident":
        try:
            fn = alert_fn
            if fn is None:
                import alerts
                fn = alerts.raise_alert
            fn("management", "incident-commander",
               f"{case['subject']}: {rationale or 'manager requested investigation'}",
               "high", f"management:{case['case_id']}")
        except Exception as e:
            rationale = f"{rationale}; incident routing failed: {e}"[:2000]
    if action == "request_human":
        boundary = str(d.get("human_boundary") or "")
        if not case.get("tenant_id"):
            action = "open_incident"
            rationale = ("Cannot route a human request without tenant ownership. " + rationale)[:2000]
        else:
            question = d.get("message") or rationale or f"Decision needed for {case['subject']}"
            # Typed standing authority is the only component allowed to manufacture
            # a CEO gate. Unknown/uncertain/internal issues stay in management; spend
            # and business judgment use the tenant's durable delegation envelope.
            import authority
            kind = {
                "credential": "credential", "legal_judgment": "legal",
                "irreversible_action": "irreversible", "physical_action": "irreversible",
                "budget_authority": "spend", "missing_business_decision": "business",
                "external_action": "business", "approval": "business",
            }.get(boundary, "internal_recovery")
            proposal = {"question": question, "reason": rationale,
                        "confidence": d.get("confidence", 0.5),
                        "risk": d.get("risk", "medium"),
                        "reversible": d.get("reversible", boundary not in
                                            {"irreversible_action", "physical_action"}),
                        "amount_usd": d.get("amount_usd", 0),
                        "campaign_spent_usd": d.get("campaign_spent_usd", 0)}
            routed = authority.open_decision(
                case["tenant_id"], case.get("work_id") or case["case_id"], kind, proposal,
                correlation_id=f"management:{case['case_id']}:{case.get('state_fingerprint','')}:{boundary}",
                owner_role=role, review_after_s=review_s, request_human=human_fn)
            if routed["disposition"] == "human_required":
                human_request_id = routed.get("agent_request_id")
                status = "human_wait"
            elif routed["disposition"] == "manager_review":
                old_level = level
                level, role = _next_manager(level)
                action = "escalate_manager" if level > old_level else "open_incident"
                rationale = ("Standing authority routed this internally: " + routed["reason"] + ". "
                             + rationale)[:2000]
                review_s = 30
            else:
                action = "continue"
                rationale = ("Standing authority permits an agentic decision: " + routed["reason"] + ". "
                             + rationale)[:2000]
                status = "open"
    if action == "resolve":
        status = "resolved"

    manager_handoff = action == "escalate_manager"
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO management_decisions
                         (case_id,manager_role,trigger,action,rationale,confidence,decision,
                          semantic_generation,target)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (case_id,semantic_generation)
                         WHERE semantic_generation IS NOT NULL DO NOTHING""",
                    (case["case_id"], case["manager_role"], case["trigger"], action,
                     rationale, d.get("confidence"), json.dumps(d),
                     case["semantic_generation"], d.get("target_role")))
        cur.execute("""UPDATE management_cases SET status=%s,manager_role=%s,management_level=%s,
                         next_review_at=CASE WHEN %s THEN now()
                                             ELSE now()+(%s||' seconds')::interval END,
                         human_request_id=COALESCE(%s,human_request_id),lease_owner=NULL,lease_until=NULL,
                         reviewed_generation=GREATEST(reviewed_generation,%s),
                         semantic_generation=semantic_generation+CASE WHEN %s THEN 1 ELSE 0 END,
                         resolved_at=CASE WHEN %s='resolved' THEN now() ELSE NULL END,updated_at=now()
                       WHERE case_id=%s AND lease_owner=%s
                       RETURNING semantic_generation""",
                    (status, role, level, manager_handoff, str(review_s), human_request_id,
                     case["semantic_generation"], manager_handoff, status,
                     case["case_id"], case["lease_owner"]))
        updated = cur.fetchone()
        if qa_owned and status == "resolved" and updated:
            cur.execute("""UPDATE management_questions
                              SET status='cancelled',answered_at=now(),
                                  answer='Question retired: recovery routed to the durable QA coordinator.'
                            WHERE case_id=%s AND status='open'""", (case["case_id"],))
        if manager_handoff and updated:
            # Escalation is a new accountable decision turn, not merely a renamed owner on an already-reviewed
            # generation.  Advance and emit atomically so the next manager is immediately claimable exactly
            # once; a crash can neither lose the handoff nor manufacture an overlapping meeting.
            generation = int(updated[0])
            cur.execute("""UPDATE management_cases
                              SET last_event_generation=GREATEST(last_event_generation,%s)
                            WHERE case_id=%s""", (generation, case["case_id"]))
            cur.execute("""INSERT INTO management_events
                             (case_id,tenant_id,event_type,payload,semantic_generation)
                           VALUES (%s,%s,'manager_escalated',%s,%s)""",
                        (case["case_id"], case.get("tenant_id") or "_platform",
                         json.dumps({"from_role": case["manager_role"], "to_role": role,
                                     "rationale": rationale[:500]}), generation))
    audit.append(actor=f"management:{case['manager_role']}", action="ManagementDecision",
                 resource=case["case_id"], decision=action,
                 payload={"rationale": rationale[:300], "confidence": d.get("confidence"),
                          "next_review_s": review_s}, tenant_id=case.get("tenant_id"))
    try:
        _resume_qa_dispute(case, action, rationale, d.get("confidence"))
    except Exception as exc:
        audit.append(actor="management", action="QADisputeResume", resource=case["case_id"],
                     decision="failed", payload={"error": str(exc)[:300]},
                     tenant_id=case.get("tenant_id"))
    resumed_qa_performance = False
    try:
        resumed_qa_performance = _resume_qa_performance(
            case, action, rationale, d.get("confidence"))
    except Exception as exc:
        audit.append(actor="management", action="QAPerformanceResume", resource=case["case_id"],
                     decision="failed", payload={"error": str(exc)[:300]},
                     tenant_id=case.get("tenant_id"))
    resumed_qa_capability = False
    try:
        resumed_qa_capability = _resume_qa_capability(
            case, action, rationale, d.get("confidence"))
    except Exception as exc:
        audit.append(actor="management", action="QACapabilityResume", resource=case["case_id"],
                     decision="failed", payload={"error": str(exc)[:300]},
                     tenant_id=case.get("tenant_id"))
    resumed_controller = False
    try:
        resumed_controller = _resume_controller_internal_wait(case, action, rationale)
    except Exception as exc:
        audit.append(actor="management", action="ControllerCheckpointResume",
                     resource=case["case_id"], decision="failed",
                     payload={"error": str(exc)[:300]}, tenant_id=case.get("tenant_id"))
    if resumed_controller:
        status = "resolved"
    if resumed_qa_performance or resumed_qa_capability:
        status = "resolved"
    return {"case_id": case["case_id"], "action": action, "status": status,
            "manager_role": role, "next_review_s": review_s}


def discover_stalled_pulses(budget=None):
    """Turn silent work into immediate management cases; does not kill or park it."""
    try:
        import pulse
        rows = pulse.stalled()
    except Exception:
        return 0
    rows = [row for row in rows
            if str(row.get("stage") or "").lower() not in
            {"queued", "waiting_capacity", "admission_wait"}]
    candidates = [{"dedupe_key": f"pulse:{row['work_id']}",
                   "subject": row.get("label") or row["work_id"],
                   "trigger": "heartbeat_silent",
                   "state": {"stage": row.get("stage"), "progress": row.get("progress"),
                             "beat_age_s": row.get("beat_age_s"),
                             "expected_cadence_s": row.get("expected_cadence_s")},
                   "tenant_id": row.get("tenant_id"), "work_id": row["work_id"],
                   "worker": row["work_id"], "cursor_key": row["work_id"]} for row in rows]
    if budget is None:
        budget = {"remaining": MAX_DUTY_DISCOVERY}
    return _duty_candidates(budget, "pulse", candidates)


def _reconcile_queued_pulse_cases() -> int:
    """Close heartbeat incidents manufactured for work that is intentionally queued.

    Capacity queues need throughput/fairness management, not a status request to a worker that has not
    started. The queue owner remains durable in orchestra; resolving this false incident does not complete
    or drop the underlying work.
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE management_cases m
                          SET status='resolved',resolved_at=now(),lease_owner=NULL,lease_until=NULL,
                              trigger='queued_work_reconciled',
                              state=state || jsonb_build_object('queue_evidence',
                                  jsonb_build_object('stage',p.stage,'status',p.status,
                                                     'reconciled',true)),
                              updated_at=now()
                         FROM agent_pulse p
                        WHERE m.work_id=p.work_id AND m.trigger='heartbeat_silent'
                          AND m.status IN ('open','waiting_internal')
                          AND lower(coalesce(p.stage,'')) IN
                              ('queued','waiting_capacity','admission_wait')
                     RETURNING m.case_id""")
        reconciled = [row[0] for row in cur.fetchall()]
        for case_id in reconciled:
            cur.execute("""UPDATE management_questions
                              SET status='cancelled',answered_at=now(),
                                  answer='Question retired: work is queued and has not started.'
                            WHERE case_id=%s AND status='open'""", (case_id,))
            cur.execute("""INSERT INTO management_events(case_id,event_type,payload)
                           VALUES (%s,'queued_pulse_reconciled',
                                   '{"reason":"queued work is not heartbeat-silent"}'::jsonb)""",
                        (case_id,))
    return len(reconciled)


def _reconcile_terminal_questions() -> int:
    """Retire status questions whose addressed pulse can no longer answer.

    A terminal pulse is stronger evidence than an overdue reply: the underlying
    process has finished (or has already been conservatively reaped). Reopening
    the same communication case on every duty sweep would manufacture manager
    meetings for dead work. The owning controller/orchestra workflow remains
    responsible for retrying failed work; this case owns only the stale heartbeat
    question, so resolving it does not turn an operational failure into success.
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE management_questions q
                          SET status='cancelled', answered_at=now(),
                              answer='Question retired: addressed work pulse is terminal.'
                        FROM management_cases m, agent_pulse p
                       WHERE q.case_id=m.case_id AND q.status='open'
                         AND m.work_id=p.work_id
                         AND p.status NOT IN ('active','stalled')
                       RETURNING q.case_id,q.question_id,p.status""")
        reconciled = cur.fetchall()
        for case_id, question_id, pulse_status in reconciled:
            cur.execute("""UPDATE management_cases
                              SET status='resolved',resolved_at=now(),
                                  lease_owner=NULL,lease_until=NULL,
                                  trigger='terminal_work_reconciled',
                                  state=state || %s::jsonb,updated_at=now()
                            WHERE case_id=%s""",
                        (json.dumps({"terminal_work_evidence": {
                            "question_id": question_id, "pulse_status": pulse_status,
                            "reconciled": True}}), case_id))
            cur.execute("""INSERT INTO management_events(case_id,event_type,payload)
                           VALUES (%s,'terminal_question_reconciled',%s)""",
                        (case_id, json.dumps({"question_id": question_id,
                                             "pulse_status": pulse_status})))
    return len(reconciled)


def _reconcile_recovered_controller_cases() -> int:
    """Resolve an old worker incident once a newer durable generation is in flight."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE management_cases m
                          SET status='resolved',resolved_at=now(),
                              lease_owner=NULL,lease_until=NULL,
                              trigger='worker_recovered',
                              state=m.state || jsonb_build_object(
                                'recovered',true,'recovered_at',now()),
                              updated_at=now()
                        WHERE m.trigger='worker_state_changed'
                          AND m.status <> 'resolved'
                          AND (m.lease_until IS NULL OR m.lease_until < now())
                          AND (m.state->>'thread_id') ~ '^[0-9]+$'
                          AND EXISTS (
                                SELECT 1 FROM controller_jobs j
                                 WHERE j.thread_id=(m.state->>'thread_id')::bigint
                                   AND j.status IN ('pending','running')
                                   AND j.started_at > COALESCE(m.last_progress_at,m.created_at))
                        RETURNING m.case_id""")
        case_ids = [r[0] for r in cur.fetchall()]
        for case_id in case_ids:
            cur.execute("""INSERT INTO management_events(case_id,event_type,payload)
                           VALUES (%s,'control_recovered',%s)""",
                        (case_id, json.dumps({"source": "newer_controller_job"})))
    return len(case_ids)


def _reconcile_terminal_qa_dispute_cases() -> int:
    """Retire manager conversations whose durable QA dispute is already terminal.

    A dispute can resolve while one of its manager status questions is still open.  Without this join, the
    overdue-question sweep keeps waking paid managers to discuss evidence that already has an authoritative
    disposition.  The dispute row is the source of truth; cancellation here changes no QA verdict.
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE management_questions q
                          SET status='cancelled',answered_at=now(),
                              answer='Question retired: the durable QA evidence dispute is terminal.'
                        FROM management_cases m, qa_evidence_disputes d
                       WHERE q.case_id=m.case_id AND q.status='open'
                         AND d.tenant_id=m.tenant_id
                         AND d.review_id=m.state->>'review_id'
                         AND d.status IN ('resolved','closed','cancelled')
                       RETURNING q.case_id,q.question_id,d.case_id,d.status""")
        questions = cur.fetchall()
        cur.execute("""UPDATE management_cases m
                          SET status='resolved',resolved_at=now(),
                              lease_owner=NULL,lease_until=NULL,
                              trigger='terminal_qa_dispute_reconciled',
                              state=m.state || jsonb_build_object(
                                'terminal_qa_dispute',true,
                                'terminal_qa_status',d.status,
                                'terminal_qa_case_id',d.case_id,
                                'reconciled_at',now()),
                              updated_at=now()
                         FROM qa_evidence_disputes d
                        WHERE m.status <> 'resolved'
                          AND (m.lease_until IS NULL OR m.lease_until < now())
                          AND d.tenant_id=m.tenant_id
                          AND d.review_id=m.state->>'review_id'
                          AND d.status IN ('resolved','closed','cancelled')
                       RETURNING m.case_id,d.case_id,d.status""")
        reconciled = cur.fetchall()
        question_ids = {}
        for management_case_id, question_id, _qa_case_id, _status in questions:
            question_ids.setdefault(management_case_id, []).append(question_id)
        for management_case_id, qa_case_id, status in reconciled:
            cur.execute("""INSERT INTO management_events(case_id,event_type,payload)
                           VALUES (%s,'terminal_qa_dispute_reconciled',%s)""",
                        (management_case_id, json.dumps({
                            "qa_case_id": qa_case_id, "qa_status": status,
                            "cancelled_question_ids": question_ids.get(management_case_id, []),
                        })))
    return len(reconciled)


def discover_orphaned_internal_management(budget=None, thread_ids=None) -> int:
    """Create manager-owned cases for controller checkpoints with no owner."""
    scoped = None if thread_ids is None else [int(value) for value in thread_ids]
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT cs.thread_id,cs.tenant_id,cs.product,cs.phase,cs.execution_scope,
                              cs.updated_at,j.id,j.status,j.result,j.finished_at,
                              m.state->'recovery_evidence'
                         FROM controller_state cs
                         LEFT JOIN LATERAL (
                           SELECT id,status,result,finished_at FROM controller_jobs
                            WHERE thread_id=cs.thread_id ORDER BY id DESC LIMIT 1
                         ) j ON true
                         LEFT JOIN management_cases m
                           ON m.tenant_id=cs.tenant_id
                          AND m.dedupe_key='duty:controller-internal:'||cs.thread_id::text
                        WHERE cs.awaiting='internal_management'
                          AND (%s::bigint[] IS NULL OR cs.thread_id=ANY(%s))
                          AND NOT EXISTS (SELECT 1 FROM controller_jobs active
                                           WHERE active.thread_id=cs.thread_id
                                             AND active.status IN ('pending','running'))
                          AND NOT EXISTS (SELECT 1 FROM qa_evidence_disputes q
                                           WHERE q.tenant_id=cs.tenant_id
                                             AND q.thread_id=cs.thread_id
                                             AND q.status NOT IN ('resolved','closed','cancelled'))
                        ORDER BY cs.thread_id LIMIT 500""", (scoped, scoped))
        parked = cur.fetchall()
    candidates = []
    for (thread_id, tenant_id, product, phase, scope, updated_at, job_id,
         job_status, result, finished_at, recovery_evidence) in parked:
        result = result if isinstance(result, dict) else {}
        semantic = {"thread_id": thread_id, "phase": phase, "execution_scope": scope,
                    "awaiting": "internal_management", "active_job": False,
                    "active_qa_dispute": False,
                    "latest_job": {"id": job_id, "status": job_status,
                                   "error": result.get("error"),
                                   "engineering_hold": bool(result.get("engineering_hold"))}}
        if isinstance(recovery_evidence, dict):
            semantic["recovery_evidence"] = recovery_evidence
        candidates.append({
            "dedupe_key": f"duty:controller-internal:{thread_id}",
            "subject": f"Controller thread {thread_id} lost its internal-management handoff",
            "trigger": CONTROLLER_INTERNAL_TRIGGER, "state": semantic,
            "semantic_state": semantic,
            "observation": {"controller_updated_at": str(updated_at),
                            "job_finished_at": str(finished_at) if finished_at else None},
            "tenant_id": tenant_id, "product": product,
            "work_id": f"controller:{thread_id}:{phase}",
            "worker": None,
            "manager_role": "senior-qa-director" if phase == "TESTQA" else "duty-manager",
            "cursor_key": str(thread_id),
        })
    if budget is None:
        budget = {"remaining": MAX_DUTY_DISCOVERY}
    return _duty_candidates(budget, "controller_internal", candidates)


def duty_audit() -> dict:
    """Independent duty manager's evidence sweep across work and communications.

    This observer does not infer that elapsed time equals failure. It gathers durable
    evidence and wakes a manager to judge the situation. Dedupe keys plus case leases
    ensure repeated audits and line-manager reviews cannot overlap on the same issue.
    """
    budget = {"remaining": MAX_DUTY_DISCOVERY}
    evidence = {"silent_pulses": 0, "stale_actors": 0, "overdue_questions": 0,
                "dropped_handoffs": 0, "scheduler_failures": 0,
                "orphaned_control_cases": 0, "controller_internal_attention": 0,
                "provider_requests_reconciled": 0,
                "orphaned_tool_pulses_reconciled": 0,
                "terminal_questions_reconciled": 0,
                "terminal_qa_disputes_reconciled": 0,
                "queued_pulse_cases_reconciled": 0,
                "work_contract_attention": 0, "objective_attention": 0,
                "assurance_attention": 0, "incident_command_attention": 0,
                "qa_dispute_attention": 0}
    # Reconcile a durable tool pulse whose run/actor is already terminal before
    # treating its unanswered mailbox as a fresh management problem. Otherwise
    # the duty manager spends paid turns asking a worker that no longer exists.
    try:
        import pulse
        evidence["orphaned_tool_pulses_reconciled"] = pulse.reap_orphans()
    except Exception:
        pass
    try:
        evidence["queued_pulse_cases_reconciled"] = _reconcile_queued_pulse_cases()
    except Exception:
        pass
    evidence["silent_pulses"] = discover_stalled_pulses(budget)

    try:
        import agent_request
        evidence["provider_requests_reconciled"] = len(
            agent_request.reconcile_satisfied_provider_requests(limit=20))
    except Exception:
        pass

    # A terminal worker cannot answer an old status request. Reconcile before
    # selecting overdue questions so the same dead conversation cannot wake a
    # manager-model call on every scheduler tick.
    try:
        evidence["terminal_qa_disputes_reconciled"] = _reconcile_terminal_qa_dispute_cases()
    except Exception:
        pass

    try:
        evidence["terminal_questions_reconciled"] = _reconcile_terminal_questions()
    except Exception:
        pass

    # QA evidence disputes are internal management work. Unchanged uncertainty never pages the CEO and never
    # burns another identical reviewer turn; it remains visible here until the owning QA team collects new
    # evidence or a specifically named external authority answers.
    try:
        import qareview
        items = qareview.attention_evidence(limit=500)
        candidates = [{"dedupe_key": qareview.management_case_key(item["tenant_id"], item["review_id"]),
                       "subject": f"QA evidence dispute needs management action: {item['review_id']}",
                       "trigger": qareview.MANAGEMENT_TRIGGER,
                       "state": qareview.management_case_state(item),
                       "tenant_id": item.get("tenant_id"), "work_id": item.get("work_ref"),
                       "worker": "qa-manager", "manager_role": "senior-qa-director",
                       "cursor_key": f"{item.get('tenant_id')}:{item['review_id']}"}
                      for item in items]
        evidence["qa_dispute_attention"] = _duty_candidates(budget, "qa_dispute", candidates)
    except Exception:
        pass

    # An internal-management checkpoint must always have a durable manager
    # owner. If no live QA dispute and no active controller job owns it, create
    # one stable duty case. This detects the exact crash window between a QA
    # worker parking the controller and persisting its dispute handoff.
    try:
        evidence["controller_internal_attention"] = discover_orphaned_internal_management(budget)
    except Exception:
        pass

    # Controller recovery cases are subordinate to the durable controller row. A
    # test fixture, retired thread, or completed cleanup can remove that row after
    # signalling management. Likewise, a later live controller job is durable
    # proof that the failed generation recovered: keeping the old incident open
    # would make managers repeatedly discuss a failure that is no longer current.
    # Active leases are left alone so this audit cannot race a manager already
    # making a decision. A subsequent failure reopens the same dedupe key through
    # ``signal`` with its new evidence.
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE management_cases m
                              SET status='resolved',resolved_at=now(),
                                  state=m.state || jsonb_build_object(
                                    'orphan_reconciled',true,'reconciled_at',now()),
                                  updated_at=now()
                            WHERE m.trigger='worker_state_changed'
                              AND m.status <> 'resolved'
                              AND (m.lease_until IS NULL OR m.lease_until < now())
                              AND (m.state->>'thread_id') ~ '^[0-9]+$'
                              AND NOT EXISTS (
                                    SELECT 1 FROM controller_state s
                                     WHERE s.thread_id=(m.state->>'thread_id')::bigint)
                            RETURNING m.case_id""")
            orphaned_cases = [r[0] for r in cur.fetchall()]
            for case_id in orphaned_cases:
                cur.execute("""INSERT INTO management_events(case_id,event_type,payload)
                               VALUES (%s,'control_retired',%s)""",
                            (case_id, json.dumps({"source": "controller_state_absent"})))
        evidence["orphaned_control_cases"] = len(orphaned_cases)
    except Exception:
        pass

    try:
        evidence["controller_recoveries"] = _reconcile_recovered_controller_cases()
    except Exception:
        evidence["controller_recoveries"] = 0

    # Actor heartbeat silence: include assignment and exact silence/cadence evidence.
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT a.actor_id,a.name,a.role,a.assignment,a.tenant_id,r.run_id,
                                  EXTRACT(EPOCH FROM now()-a.last_active)::INT AS silent_s
                           FROM orchestra_actors a JOIN orchestra_runs r ON r.run_id=a.run_id
                           WHERE r.status='running' AND a.status='working'
                             AND a.last_active < now()-interval '10 minutes'
                           ORDER BY a.actor_id""")
            actors = cur.fetchall()
        candidates = [{"dedupe_key": f"duty:actor:{aid}",
                       "subject": f"{role} {name} stopped reporting",
                       "trigger": "actor_heartbeat_silent",
                       "state": {"actor_id": aid, "run_id": run_id, "assignment": assignment,
                                 "status_evidence": {"silent_s": silent_s, "threshold_s": 600}},
                       "tenant_id": tenant, "work_id": f"actor:{aid}", "worker": name,
                       "manager_role": "duty-manager", "cursor_key": str(aid)}
                      for aid, name, role, assignment, tenant, run_id, silent_s in actors]
        evidence["stale_actors"] = _duty_candidates(budget, "actor", candidates)
    except Exception:
        pass

    # An async question past reply_by is a communication breakdown. Wake its case;
    # never start a synchronous meeting or wait for the next nominal cadence.
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT q.question_id,q.case_id,q.asker,q.recipient,q.question,q.reply_by,
                                  EXTRACT(EPOCH FROM now()-q.reply_by)::INT AS overdue_s,
                                  m.tenant_id,m.dedupe_key,m.subject,m.product,m.work_id,m.worker,
                                  m.manager_role,m.semantic_state
                           FROM management_questions q JOIN management_cases m ON m.case_id=q.case_id
                           WHERE q.status='open' AND q.reply_by < now()
                             AND m.status IN ('open','waiting_internal')
                           ORDER BY q.question_id""")
            questions = cur.fetchall()
        candidates = []
        for (qid, _cid, asker, recipient, question, reply_by, overdue_s, tenant, dedupe,
             subject, product, work_id, worker, manager_role, semantic_state) in questions:
            semantic = dict(semantic_state or {})
            semantic["communication_evidence"] = {"question_id": qid, "asker": asker,
                "recipient": recipient, "question": question, "reply_by": str(reply_by),
                "status": "overdue"}
            candidates.append({"dedupe_key": dedupe, "subject": subject,
                "trigger": "internal_reply_overdue", "state": semantic,
                "semantic_state": semantic, "observation": {"overdue_s": overdue_s},
                "tenant_id": tenant, "product": product, "work_id": work_id,
                "worker": worker, "manager_role": manager_role, "cursor_key": qid})
        evidence["overdue_questions"] = _duty_candidates(budget, "question", candidates)
    except Exception:
        pass

    # Read the durable conversation transcript via accountability's explicit
    # request->resolution matcher. Restrict to recent work so installation does not
    # flood the queue with historical debt; old debt remains in accountability reports.
    try:
        import accountability
        handoffs = accountability.dropped(window_hours=24)
        # Conversation selftests and retired agents may leave transcript rows. A
        # duty manager may act only on a recipient that belongs to the durable org
        # directory; otherwise it would invent staffing work for a fixture/ghost.
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT agent_id FROM directory")
            known_agents = {r[0] for r in cur.fetchall()}
        handoffs = [h for h in handoffs if h.get("to") in known_agents]
        candidates = [{"dedupe_key": f"duty:handoff:{h['conversation']}:{h['to']}:{h['intent']}",
                       "subject": f"Unanswered {h['intent']} from {h['from']} to {h['to']}",
                       "trigger": "handoff_unanswered",
                       "state": {"conversation_id": h["conversation"], "from": h["from"],
                                 "to": h["to"], "intent": h["intent"],
                                 "age_min": h["age_min"], "resolved": False},
                       "tenant_id": h.get("tenant_id"), "product": h.get("product"),
                       "worker": h["to"], "manager_role": "duty-manager",
                       "cursor_key": f"{h['conversation']}:{h['to']}:{h['intent']}"}
                      for h in handoffs]
        evidence["dropped_handoffs"] = _duty_candidates(budget, "handoff", candidates)
    except Exception:
        pass

    # Repeated scheduler failure means the control mechanism itself is broken. The
    # scheduler's persisted failure_count/last_error are stronger evidence than logs.
    try:
        with _conn() as c, c.cursor() as cur:
            # Recovery is also a state change. Without this reconciliation, an old failure case keeps
            # waking managers forever even after the scheduler has recorded successful runs again.
            cur.execute("""UPDATE management_cases m
                              SET status='resolved',resolved_at=now(),lease_owner=NULL,lease_until=NULL,
                                  state=m.state || jsonb_build_object('recovered',true,'recovered_at',now()),
                                  updated_at=now()
                             FROM schedules s
                            WHERE m.dedupe_key='duty:schedule:'||s.name
                              AND m.status <> 'resolved' AND s.failure_count < 2
                            RETURNING m.case_id""")
            recovered_schedule_cases = [r[0] for r in cur.fetchall()]
            for case_id in recovered_schedule_cases:
                cur.execute("""INSERT INTO management_events(case_id,event_type,payload)
                               VALUES (%s,'control_recovered',%s)""",
                            (case_id, json.dumps({"source": "scheduler_success"})))
            cur.execute("""SELECT name,failure_count,last_error,last_run,next_run FROM schedules
                           WHERE enabled AND failure_count >= 2 ORDER BY name""")
            failures = cur.fetchall()
        candidates = [{"dedupe_key": f"duty:schedule:{name}",
                       "subject": f"Scheduler job {name} repeatedly failing",
                       "trigger": "control_mechanism_failure",
                       "state": {"schedule": name, "failure_count": count, "last_error": error,
                                 "last_run": str(last_run), "next_run": str(next_run)},
                       "semantic_state": {"schedule": name, "failure_count": count,
                                          "last_error": error},
                       "observation": {"last_run": str(last_run), "next_run": str(next_run)},
                       "worker": f"schedule:{name}", "manager_role": "duty-manager",
                       "cursor_key": name}
                      for name, count, error, last_run, next_run in failures]
        evidence["scheduler_failures"] = _duty_candidates(budget, "schedule", candidates)
        evidence["scheduler_recoveries"] = len(recovered_schedule_cases)
    except Exception:
        pass

    # Explicit work commitments are stronger than inferred task ownership. Review overdue substantive
    # check-ins, delegation offers without acknowledgement, and objectives lacking a continuity owner.
    try:
        import workcontracts
        workcontracts.ensure()
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT DISTINCT w.tenant_id FROM work_contracts w
                           JOIN tenants t ON t.tenant_id=w.tenant_id
                           WHERE w.status IN ('active','blocked')
                             AND COALESCE(w.constraints->>'execution_scope','production')='production'
                           ORDER BY w.tenant_id""")
            tenants = [r[0] for r in cur.fetchall()]
        items = []
        for tenant in tenants:
            items.extend((tenant, item) for item in workcontracts.attention_evidence(tenant, limit=1000))
        candidates = []
        for tenant, item in items:
            ref = item.get("contract_id") or item.get("delegation_id")
            candidates.append({"dedupe_key": f"duty:work-contract:{tenant}:{item['kind']}:{ref}",
                "subject": f"Work commitment needs management review: {item['kind']}",
                "trigger": "work_contract_attention", "state": item, "tenant_id": tenant,
                "work_id": f"work-contract:{ref}", "worker": item.get("accountable_owner"),
                "manager_role": "duty-manager", "cursor_key": f"{tenant}:{item['kind']}:{ref}"})
        evidence["work_contract_attention"] = _duty_candidates(budget, "work_contract", candidates)
    except Exception:
        pass

    # Strategic objectives tell management whether execution is still serving the
    # outcome. Only decision-relevant objective facts wake cases; healthy active
    # objectives remain visible through the portfolio without manufacturing work.
    try:
        import objectiveportfolio
        objectiveportfolio.ensure()
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT DISTINCT o.tenant_id FROM strategic_objectives o
                           JOIN tenants t ON t.tenant_id=o.tenant_id
                      LEFT JOIN work_contracts w ON w.contract_id=o.work_contract_id
                           WHERE o.state NOT IN ('achieved','abandoned')
                             AND COALESCE(w.constraints->>'execution_scope','production')='production'
                           ORDER BY o.tenant_id""")
            objective_tenants = [r[0] for r in cur.fetchall()]
        objective_items = []
        for tenant in objective_tenants:
            for item in objectiveportfolio.management_evidence(tenant):
                needs_judgment = (item.get("state") in {"at_risk", "blocked"}
                                  or int(item.get("blocking_dependencies") or 0) > 0
                                  or item.get("work_contract_status") == "blocked"
                                  or float(item.get("confidence") or 0) < 0.35)
                if not needs_judgment:
                    continue
                objective_items.append((tenant, item))
        candidates = [{"dedupe_key": f"duty:objective:{tenant}:{item['objective_id']}",
                       "subject": f"Objective needs management review: {item['title']}",
                       "trigger": "objective_attention", "state": item, "tenant_id": tenant,
                       "work_id": f"objective:{item['objective_id']}",
                       "worker": item.get("accountable_owner"), "manager_role": "duty-manager",
                       "cursor_key": f"{tenant}:{item['objective_id']}"}
                      for tenant, item in objective_items]
        evidence["objective_attention"] = _duty_candidates(budget, "objective", candidates)
    except Exception:
        pass

    # Independent assurance is a real delivery role, not a hidden table. Ready
    # reviews and reviews still awaiting evidence enter the duty manager's bounded
    # attention queue; elapsed time is included as evidence, never as a verdict.
    try:
        import assurance_learning
        assurance_learning.ensure()
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT review_id,tenant_id,subject_type,subject_id,work_contract_id,
                                  executor_id,manager_id,reviewer_id,status,created_at,
                                  EXTRACT(EPOCH FROM now()-created_at)::INT AS age_s
                           FROM assurance_reviews
                           WHERE status IN ('awaiting_evidence','ready')
                           ORDER BY review_id""")
            reviews = cur.fetchall()
        candidates = [{"dedupe_key": f"duty:assurance:{item['tenant_id']}:{item['review_id']}",
                       "subject": ("Independent assurance review needs attention: "
                                   f"{item['subject_type']} {item['subject_id']}"),
                       "trigger": "assurance_attention", "state": item,
                       "tenant_id": item["tenant_id"],
                       "work_id": f"assurance:{item['review_id']}", "worker": item["reviewer_id"],
                       "manager_role": "duty-manager",
                       "cursor_key": f"{item['tenant_id']}:{item['review_id']}"}
                      for item in ({"review_id": review_id, "subject_type": subject_type,
                          "subject_id": subject_id, "work_contract_id": contract_id,
                          "executor_id": executor, "manager_id": manager, "reviewer_id": reviewer,
                          "status": status, "created_at": str(created_at), "age_s": age_s,
                          "tenant_id": tenant}
                         for (review_id, tenant, subject_type, subject_id, contract_id, executor,
                              manager, reviewer, status, created_at, age_s) in reviews)]
        evidence["assurance_attention"] = _duty_candidates(budget, "assurance", candidates)
    except Exception:
        pass

    # Incident command owns operational truth; duty management only wakes the
    # accountable chain when a promised update, response acknowledgement, or
    # corrective-action deadline needs judgment. Time alone never changes status.
    try:
        import incidentcommand
        items = incidentcommand.attention_evidence(limit=1000)
        candidates = []
        for item in items:
            ref = item.get("action_id") or item["incident_id"]
            candidates.append({"dedupe_key": f"duty:incident-command:{item['kind']}:{ref}",
                "subject": f"Incident command needs review: {item['kind']}",
                "trigger": "incident_command_attention", "state": item,
                "tenant_id": item.get("tenant_id"), "work_id": f"incident:{item['incident_id']}",
                "worker": item.get("owner") or item.get("commander"),
                "manager_role": "duty-manager", "cursor_key": f"{item['kind']}:{ref}"})
        evidence["incident_command_attention"] = _duty_candidates(budget, "incident", candidates)
    except Exception:
        pass

    if any(evidence.values()):
        audit.append(actor="management:duty-manager", action="DutyAudit",
                     resource="company-control-plane", decision="cases_woken", payload=evidence)
    return evidence


def resume_human_answers():
    """Wake cases whose durable CEO request has been answered."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE management_cases m SET status='open',trigger='human_answered',
                         semantic_generation=semantic_generation+1,
                         last_event_generation=semantic_generation+1,
                         next_review_at=now(),updated_at=now()
                       FROM agent_requests r WHERE m.status='human_wait'
                         AND m.human_request_id=r.id AND r.status='answered'
                       RETURNING m.case_id,m.semantic_generation,r.id""")
        rows = cur.fetchall()
        for case_id, generation, request_id in rows:
            cur.execute("""INSERT INTO management_events
                             (case_id,event_type,payload,semantic_generation)
                           VALUES (%s,'human_answered',%s,%s)""",
                        (case_id, json.dumps({"request_id": request_id}), generation))
        return len(rows)


def sweep(*, decide_fn: Callable | None = None, send_fn: Callable | None = None,
          human_fn: Callable | None = None, alert_fn: Callable | None = None,
          limit: int = MAX_BATCH) -> list[dict]:
    """Discover, claim, decide and dispatch. Safe under concurrent scheduler ticks."""
    try:
        import qareview
        qareview.reconcile_authority_answers()
    except Exception:
        pass
    duty_audit()
    try:
        resume_human_answers()
    except Exception:
        pass
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    cases = _claim(owner, limit)
    out = []
    for case in cases:
        case["lease_owner"] = owner
        try:
            out.append(_apply(case, _decide(case, decide_fn), send_fn, human_fn, alert_fn))
        except Exception as e:
            # The decision engine failing is an internal incident, not a CEO gate. Release
            # the lease and retry soon so a transient provider/DB error cannot strand work.
            with _conn() as c, c.cursor() as cur:
                cur.execute("""UPDATE management_cases SET lease_owner=NULL,lease_until=NULL,
                                 next_review_at=now()+interval '30 seconds',updated_at=now()
                               WHERE case_id=%s AND lease_owner=%s""", (case["case_id"], owner))
            out.append({"case_id": case["case_id"], "action": "retry_decision", "error": str(e)[:300]})
    return out


def review_now(case_id: str, *, decide_fn: Callable | None = None,
               send_fn: Callable | None = None, human_fn: Callable | None = None,
               alert_fn: Callable | None = None) -> dict:
    """Run one targeted manager turn after an event-driven failure.

    This is the synchronous bridge for controllers: signal a durable case, then
    review it now and act on the returned status. If a periodic duty manager or
    another controller already owns the case, return ``busy`` immediately; the
    durable lease holder will finish it and no overlapping meeting/model call occurs.
    """
    owner = f"targeted:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    case = _claim_one(case_id, owner)
    if case is None:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT status,manager_role,lease_until FROM management_cases WHERE case_id=%s",
                        (case_id,))
            row = cur.fetchone()
        if not row:
            raise KeyError(f"management case {case_id!r} not found")
        return {"case_id": case_id, "action": "busy" if row[2] else "no_review",
                "status": row[0], "manager_role": row[1],
                "lease_until": str(row[2]) if row[2] else None}
    case["lease_owner"] = owner
    try:
        return _apply(case, _decide(case, decide_fn), send_fn, human_fn, alert_fn)
    except Exception:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE management_cases SET lease_owner=NULL,lease_until=NULL,
                             next_review_at=now()+interval '30 seconds',updated_at=now()
                           WHERE case_id=%s AND lease_owner=%s""", (case_id, owner))
        raise


def list_cases(status: str | None = None) -> list[dict]:
    _ensure()
    with _conn() as c, c.cursor() as cur:
        where, params = ("WHERE status=%s", (status,)) if status is not None else ("", ())
        cur.execute(f"""SELECT case_id,subject,worker,manager_role,management_level,status,trigger,
                               state,progress_seq,last_progress_at,next_review_at,human_request_id
                        FROM management_cases {where}
                        ORDER BY updated_at DESC LIMIT 200""", params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _main(argv):
    if not argv or argv[0] == "sweep":
        print(json.dumps(sweep(), indent=2, default=str))
    elif argv[0] == "list":
        print(json.dumps(list_cases(argv[1] if len(argv) > 1 else None), indent=2, default=str))
    elif argv[0] == "answer" and len(argv) >= 3:
        print(json.dumps({"answered": answer(argv[1], argv[2])}))
    else:
        raise SystemExit("usage: management.py sweep | list [status] | answer <question_id> <answer>")


if __name__ == "__main__":
    _main(sys.argv[1:])
