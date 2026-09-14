#!/usr/bin/env python3
"""approvals.py — the tenant-facing Approvals / Decisions inbox (Area 8 governance core).

Everything that is waiting on a HUMAN decision, scoped to one tenant, aggregated into a single
list so nothing sits unseen: products that were paused (a PAUSED.html landed, or the kill_switch
halted them), open hire_requests an agent raised, a pending AI-consent gate that blocks builds,
and dead-lettered tasks that exhausted retries and need a person to retry or drop them. decide()
applies the SAFE side of each decision and records it in the tamper-evident audit chain.

    approvals.py json <tenant_id>     # the inbox as JSON
    approvals.py selftest
Run with the agent-os venv python.  Data/logic module only — binds no server.
"""
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import consent  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

PRODUCTS = Path.home() / "projects" / "products"

# Optional collaborators — present in this factory, but degrade gracefully if absent.
try:
    import killswitch  # noqa: E402
except Exception:       # pragma: no cover
    killswitch = None


def _conn(tid=None):
    return tenant_connection(tid) if tid else connection()


def _tenant_products(tid):
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
        return [r[0] for r in cur.fetchall()]


def _paused_apps(tid):
    """A tenant product is 'paused' if it has a PAUSED.html OR a kill_switch row (scoped or global)."""
    prods = _tenant_products(tid)
    if not prods:
        return []
    halted = {}
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT scope, reason FROM kill_switch WHERE scope IN ('global', %s) OR scope = ANY(%s)",
                    (tid, prods))
        for scope, reason in cur.fetchall():
            halted[scope] = reason or "halted via kill_switch"
    out = []
    global_reason = halted.get("global")
    for p in prods:
        reason = None
        if (PRODUCTS / p / "PAUSED.html").exists():
            reason = "PAUSED.html present (auto-paused)"
        elif p in halted:
            reason = halted[p]
        elif global_reason is not None:
            reason = f"global halt: {global_reason}"
        if reason:
            out.append({"product": p, "reason": reason})
    return out


def _blocked_builds(tid):
    """Builds that BLOCKED at QA/REVIEW and still need a human call. A blocked build records a
    ProductComplete audit row with decision LIKE 'BLOCKED%'; it's considered RESOLVED (and drops off
    the inbox) once a LATER ProductComplete row for the same product reads decision='LAUNCHED'."""
    prods = _tenant_products(tid)
    if not prods:
        return []
    out = []
    with tenant_connection(tid) as c, c.cursor() as cur:
        # The most recent ProductComplete per product over the last 7 days; surface it only if that
        # latest verdict is still BLOCKED (a since-LAUNCHED build has a newer, higher-id LAUNCHED row).
        cur.execute("""
            SELECT DISTINCT ON (resource) resource, decision, payload
              FROM audit_log
             WHERE action='ProductComplete'
               AND resource = ANY(%s)
               AND ts >= now() - interval '7 days'
             ORDER BY resource, id DESC
        """, (prods,))
        for resource, decision, payload in cur.fetchall():
            if not (decision or "").startswith("BLOCKED"):
                continue
            blocker = ""
            if isinstance(payload, dict):
                blocker = (payload.get("blocker") or payload.get("error") or "")
            out.append({"product": resource, "decision": decision, "blocker": blocker[:240]})
    return out


def _hire_requests(tid):
    """Open hire requests OWNED BY THIS TENANT. WAS a cross-tenant leak (no filter -> every CEO saw every
    tenant's hires). NULL-tenant rows are shared-pool/platform hires with no owning company — surfaced to no
    CEO inbox (an operator concern), never to a random tenant."""
    try:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("""SELECT id, requester, need_role, reason FROM hire_requests
                           WHERE status='open' AND tenant_id = %s ORDER BY id""", (tid,))
            return [{"id": r[0], "requester": r[1], "need_role": r[2], "reason": r[3] or ""}
                    for r in cur.fetchall()]
    except Exception:
        return []            # fail-closed (e.g. pre-migration column absent): show nothing, never leak


def _dead_letters(tid):
    """Dead-lettered tasks OWNED BY THIS TENANT, scoped by the product embedded in the task's assignee
    ('<role>@<product>') matched against the tenant's products. WAS A CROSS-TENANT LEAK: the docstring
    claimed scoping but the query was `WHERE status='dead'` with NO tenant filter, so EVERY CEO's inbox
    showed EVERY tenant's (and the platform's) dead-lettered tasks. Truly orphaned dead tasks (no derivable
    owner) are a PLATFORM concern surfaced via tasksweep dead-letter depth / the operator — never dumped
    into a random tenant's inbox."""
    prods = set(_tenant_products(tid))
    if not prods:
        return []
    with _conn(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT id, title, role, last_error, attempts, assignee FROM tasks
                       WHERE status='dead' ORDER BY id""")
        rows = cur.fetchall()
    out = []
    for r in rows:
        assignee = r[5] or ""
        prod = assignee.split("@", 1)[1] if "@" in assignee else ""
        if prod and prod in prods:                        # only THIS tenant's own dead-letters
            out.append({"id": r[0], "title": r[1] or "", "role": r[2] or "",
                        "last_error": r[3] or "", "attempts": r[4] or 0})
    return out


def _controller_gates(tid, execution_scope=None):
    """Controller workstreams parked on a CEO gate.

    Proactive comms already pages these, but the linked Approvals screen must also show the actual item and
    provide a response path. Otherwise a CEO sees an urgent notification that opens to "All clear".
    """
    try:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("""SELECT thread_id, phase, awaiting, product
                           FROM controller_state
                           WHERE tenant_id=%s
                             AND (%s::text IS NULL OR execution_scope=%s)
                             AND awaiting IN ('user_feedback','user_approval','credentials')
                             AND phase <> 'DELIVER'
                             AND NOT EXISTS (
                                   SELECT 1 FROM controller_jobs cj
                                   WHERE cj.thread_id=controller_state.thread_id AND cj.status='cancelled'
                                     AND cj.id = (SELECT max(id) FROM controller_jobs
                                                  WHERE thread_id=controller_state.thread_id))
                           ORDER BY updated_at DESC""", (tid, execution_scope, execution_scope))
            rows = cur.fetchall()
        try:
            import killswitch
            rows = [r for r in rows if not killswitch.is_halted(f"thread-{r[0]}").get("halted")]
        except Exception:
            pass
        out = []
        for thread_id, phase, awaiting, product in rows:
            what = {"user_approval": "pick a direction or approve the plan",
                    "user_feedback": "review and respond",
                    "credentials": "connect a model provider"}.get(awaiting, "respond")
            out.append({
                "id": f"ceo_gate:{thread_id}",
                "kind": "ceo_decision",
                "ref": str(thread_id),
                "title": f"Your build is waiting on you ({phase})",
                "detail": f"{product or 'This workstream'} needs you to {what}.",
                "severity": "high",
                "action_label": "Respond",
            })
        return out
    except Exception:
        return []


def _retry_build(product):
    """Re-run a blocked build the same way the self-heal sweep does: a DETACHED resume process
    (`factory.py build <product> "" <kind>`) that skips the SPEC/BUILD checkpoints and re-runs QA/
    REVIEW/LAUNCH. Returns True if a process was actually launched; False if we fell back to the
    audit-marker-only path (a resume sweep / operator picks it up). Never raises."""
    # Acceptance/selftests must never launch a detached build.  The old
    # approvals selftest did exactly that and then used an unsafe broad pkill
    # to clean it up, turning a no-spend scorecard into live process pressure.
    if os.environ.get("AOS_SELFTEST", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    try:
        import subprocess
        import factory
        kind = factory._detect_kind(factory.PRODUCTS / product)
        log = open(f"/tmp/retry-{product}.log", "a")
        # empty charter is intentional on resume: CHARTER.md already exists and is preserved.
        subprocess.Popen([sys.executable, str(SCRIPTS / "factory.py"), "build", product, "", kind],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(SCRIPTS.parent))
        return True
    except Exception:        # heavy/unavailable -> just leave the BuildRetryRequested marker
        return False


def inbox(tid, execution_scope=None):
    """Aggregate everything awaiting a human decision for this tenant into ONE list."""
    items = []

    for p in _paused_apps(tid):
        items.append({
            "id": f"paused_app:{p['product']}",
            "kind": "paused_app",
            "ref": p["product"],
            "title": f"Resume or retire '{p['product']}'",
            "detail": p["reason"],
            "severity": "high",
            "action_label": "Approve resume",
        })

    if not consent.require_consent(tid):
        st = consent.state(tid)
        items.append({
            "id": f"consent:{tid}",
            "kind": "consent",
            "ref": tid,
            "title": "AI consent required before builds can run",
            "detail": f"{st['provider']} · disclosure {st['version']} not yet accepted",
            "severity": "high",
            "action_label": "Approve consent",
        })

    for b in _blocked_builds(tid):
        detail = b["decision"] + (f" — {b['blocker']}" if b["blocker"] else "")
        items.append({
            "id": b["product"],
            "kind": "blocked_build",
            "ref": b["product"],
            "title": f"Build needs you: {b['product']}",
            "detail": detail,
            "severity": "high",
            "action_label": "Retry build",
        })

    for h in _hire_requests(tid):
        items.append({
            "id": f"hire_request:{h['id']}",
            "kind": "hire_request",
            "ref": h["id"],
            "title": f"Approve hiring a '{h['need_role']}'",
            "detail": f"requested by {h['requester']}" + (f": {h['reason']}" if h["reason"] else ""),
            "severity": "med",
            "action_label": "Approve / Deny",
        })

    items.extend(_controller_gates(tid, execution_scope=execution_scope))

    # AI -> CEO questions: an agent hit something only the CEO can answer (a choice, a credential, a
    # judgment call) and PAUSED. Surfacing them here is the 'ask-the-CEO' loop (B1) — without this the
    # question only push-notified and the CEO had nowhere in-product to answer, so the phase stayed blocked.
    try:
        import agent_request
        for r in (agent_request.open_requests(tid, execution_scope=execution_scope) or []):
            items.append({
                "id": f"question:{r['id']}",
                "kind": "question",
                "ref": r["id"],
                "title": r.get("question") or "Your AI team needs your input",
                "detail": "Your fleet paused on this and is waiting for your answer to continue.",
                "severity": "med",
                "action_label": "Answer",
            })
    except Exception:
        pass

    for d in _dead_letters(tid):
        items.append({
            "id": f"dead_letter:{d['id']}",
            "kind": "dead_letter",
            "ref": d["id"],
            "title": f"Dead-lettered task: {d['title'][:80] or d['role'] or ('#' + str(d['id']))}",
            "detail": f"failed after {d['attempts']} attempts — {d['last_error'][:160]}",
            "severity": "med",
            "action_label": "Retry / Drop",
        })

    return {"items": items, "count": len(items)}


def decide(tid, kind, ref, verdict):
    """Apply the SAFE side of a human decision and record it in the audit chain."""
    verdict = (verdict or "").lower()

    if kind == "consent":
        if verdict == "approve":
            consent.record(tid)
        else:
            consent.revoke(tid)

    elif kind == "paused_app":
        # OWNERSHIP: only the owning tenant may resume/unpause its product (cross-tenant write guard).
        if ref not in _tenant_products(tid):
            raise ValueError(f"product {ref!r} is not owned by tenant {tid}")
        if verdict == "approve":
            if killswitch is not None:
                killswitch.resume(ref)            # clear scoped kill_switch row
            (PRODUCTS / ref / "PAUSED.html").unlink(missing_ok=True)
        # 'deny'/'retire' is a no-op here beyond the audit note (retirement is a separate flow).

    elif kind == "hire_request":
        new = "fulfilled" if verdict == "approve" else "denied"
        with tenant_connection(tid) as c, c.cursor() as cur:
            # OWNERSHIP: only the owning tenant may decide its hire (cross-tenant write guard).
            cur.execute("UPDATE hire_requests SET status=%s WHERE id=%s AND tenant_id=%s",
                        (new, int(ref), tid))
            if cur.rowcount == 0:
                raise ValueError(f"hire_request {ref} is not owned by tenant {tid}")

    elif kind == "blocked_build":
        if ref not in _tenant_products(tid):
            raise ValueError(f"product {ref!r} is not owned by tenant {tid}")
        if verdict in ("approve", "retry"):
            queued = _retry_build(ref)
            audit.append(actor="approvals", action="BuildRetryRequested", resource=ref,
                         decision="retry", payload={"tenant": tid, "relaunched": queued}, tenant_id=tid)
            return {"ok": True, "queued": True, "kind": kind, "ref": ref, "verdict": verdict}
        else:  # 'deny'/'drop'
            audit.append(actor="approvals", action="BuildAbandoned", resource=ref,
                         decision="abandon", payload={"tenant": tid}, tenant_id=tid)
            return {"ok": True, "kind": kind, "ref": ref, "verdict": verdict}

    elif kind == "dead_letter":
        with _conn(tid) as c, c.cursor() as cur:
            # OWNERSHIP: the dead task's product (embedded in assignee '<role>@<product>') must be the
            # tenant's — else a tenant could retry/drop ANOTHER tenant's task by id (cross-tenant write).
            cur.execute("SELECT assignee FROM tasks WHERE id=%s", (int(ref),))
            row = cur.fetchone()
            prod = (row[0].split("@", 1)[1] if row and row[0] and "@" in row[0] else "")
            if not prod or prod not in _tenant_products(tid):
                raise ValueError(f"dead_letter {ref} is not owned by tenant {tid}")
            if verdict == "retry":
                cur.execute("""UPDATE tasks SET status='pending', not_before=now(), last_error=NULL
                               WHERE id=%s""", (int(ref),))
            else:  # 'drop'
                cur.execute("UPDATE tasks SET status='done' WHERE id=%s", (int(ref),))
            c.commit()

    else:
        raise ValueError(f"unknown decision kind: {kind}")

    audit.append(actor="approvals", action="DecisionApplied", resource=f"{kind}:{ref}",
                 decision=verdict, payload={"tenant": tid, "kind": kind, "ref": str(ref)}, tenant_id=tid)
    return {"ok": True, "kind": kind, "ref": ref, "verdict": verdict}


def _selftest():
    # Standalone `approvals.py selftest` must be no-spend too; do not rely on a
    # parent harness to provide the marker before exercising retry approval.
    os.environ["AOS_SELFTEST"] = "1"
    import billing
    reg = billing.signup("approvals-selftest", "free")
    tid = reg["tenant_id"]
    hire_id = None
    dead_id = None
    blk_product = f"approvals-selftest-blocked-{tid}"
    try:
        # An item we fully control: an OPEN hire_request (global, surfaces in every tenant's inbox).
        with _conn(tid) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO hire_requests (requester, need_role, reason, status, tenant_id)
                           VALUES (%s,%s,%s,'open',%s) RETURNING id""",
                        ("approvals-selftest", "qa-bot", "selftest hire", tid))
            hire_id = cur.fetchone()[0]
            # A dead-lettered task OWNED BY THIS TENANT (assignee '<role>@<product>' where product is the
            # tenant's) — so it surfaces for THIS tenant only, not globally (tests the isolation fix).
            cur.execute("""INSERT INTO tasks
                              (tenant_id,assignee,requester,role,title,status,attempts,max_retry)
                           VALUES (%s,%s,'approvals-selftest','qa-bot','selftest dead task','dead',3,3)
                           RETURNING id""", (tid, f"qa-bot@{blk_product}"))
            dead_id = cur.fetchone()[0]
            # A blocked build the tenant owns, recorded as a ProductComplete BLOCKED_AT_REVIEW row.
            cur.execute("""INSERT INTO tenant_products (product, tenant_id)
                           VALUES (%s,%s) ON CONFLICT DO NOTHING""", (blk_product, tid))
            c.commit()
        audit.append(actor="factory:controller", action="ProductComplete", resource=blk_product,
                     decision="BLOCKED_AT_REVIEW",
                     payload={"blocker": "reviewer still REQUEST-CHANGES", "tenant": tid},
                     tenant_id=tid)

        box = inbox(tid)
        kinds = {i["kind"] for i in box["items"]}
        blocked_present = any(i["kind"] == "blocked_build" and i["ref"] == blk_product
                              for i in box["items"])
        hire_present = any(i["kind"] == "hire_request" and i["ref"] == hire_id for i in box["items"])
        dead_present = any(i["kind"] == "dead_letter" and i["ref"] == dead_id for i in box["items"])
        consent_present = "consent" in kinds       # fresh tenant -> consent must be required

        # ISOLATION GUARD: a DIFFERENT tenant must NOT see this tenant's dead-letter (guards the cross-tenant
        # leak — _dead_letters used to return ALL dead tasks platform-wide for every tenant).
        other_tid = billing.signup("approvals-selftest-other", "free")["tenant_id"]
        other_box = inbox(other_tid)["items"]
        isolated = (not any(i["kind"] == "dead_letter" and i["ref"] == dead_id for i in other_box)
                    and not any(i["kind"] == "hire_request" and i["ref"] == hire_id for i in other_box))

        # Resolve the hire_request and verify it leaves the inbox.
        decide(tid, "hire_request", hire_id, "approve")
        with _conn(tid) as c, c.cursor() as cur:
            cur.execute("SELECT status FROM hire_requests WHERE id=%s", (hire_id,))
            hire_status = cur.fetchone()[0]
        hire_resolved = hire_status == "fulfilled" and \
            not any(i["kind"] == "hire_request" and i["ref"] == hire_id for i in inbox(tid)["items"])

        # Retry the dead task and verify it's no longer dead.
        decide(tid, "dead_letter", dead_id, "retry")
        with _conn(tid) as c, c.cursor() as cur:
            cur.execute("SELECT status FROM tasks WHERE id=%s", (dead_id,))
            task_status = cur.fetchone()[0]
        dead_resolved = task_status == "pending" and \
            not any(i["kind"] == "dead_letter" and i["ref"] == dead_id for i in inbox(tid)["items"])

        # Approve consent and verify the gate item clears.
        decide(tid, "consent", tid, "approve")
        consent_resolved = consent.require_consent(tid) and \
            not any(i["kind"] == "consent" for i in inbox(tid)["items"])

        # Retry the blocked build: decide() returns ok+queued and writes a BuildRetryRequested audit.
        blk_decided = decide(tid, "blocked_build", blk_product, "approve")
        with _conn(tid) as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM audit_log
                           WHERE action='BuildRetryRequested' AND resource=%s""", (blk_product,))
            retry_audited = cur.fetchone()[0] >= 1
        blocked_retried = blk_decided.get("ok") and retry_audited
        # A LATER LAUNCHED ProductComplete means it's since-fixed -> the item must DISAPPEAR.
        audit.append(actor="factory:controller", action="ProductComplete", resource=blk_product,
                     decision="LAUNCHED", payload={"stages": 5}, tenant_id=tid)
        blocked_cleared = not any(i["kind"] == "blocked_build" and i["ref"] == blk_product
                                  for i in inbox(tid)["items"])

        ok = (hire_present and dead_present and consent_present and blocked_present and isolated and
              hire_resolved and dead_resolved and consent_resolved and
              blocked_retried and blocked_cleared)
        print(f"surfaced: hire={hire_present} dead={dead_present} consent={consent_present} "
              f"blocked={blocked_present} tenant_isolated={isolated} | resolved: hire={hire_resolved} "
              f"dead={dead_resolved} consent={consent_resolved} blocked_retry={blocked_retried} "
              f"blocked_cleared={blocked_cleared}")
        print("PASS: approvals inbox aggregates + decide() resolves each kind ✅" if ok else "FAIL")
        rc = 0 if ok else 1
    finally:
        # AOS_SELFTEST makes _retry_build model/process-free, so cleanup never
        # needs broad command-line matching or process signalling.
        import shutil
        shutil.rmtree(PRODUCTS / blk_product, ignore_errors=True)
        Path(f"/tmp/retry-{blk_product}.log").unlink(missing_ok=True)
        with _conn(tid) as c, c.cursor() as cur:
            if dead_id is not None:
                cur.execute("DELETE FROM tasks WHERE id=%s", (dead_id,))
            if hire_id is not None:
                cur.execute("DELETE FROM hire_requests WHERE id=%s", (hire_id,))
            try:
                cur.execute("DELETE FROM tenants WHERE name='approvals-selftest-other'")
            except Exception:
                pass
            # NOTE: never DELETE from audit_log — it is an append-only tamper-evident chain; deleting rows
            # breaks `audit.py verify`. The few test rows are harmless append-only entries.
            cur.execute("DELETE FROM traces WHERE product=%s OR run_id=%s",
                        (blk_product, f"build-{blk_product}"))
            cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(rc)


def _main(argv):
    import json
    if not argv or argv[0] == "selftest":
        _selftest()
    elif argv[0] == "json" and len(argv) > 1:
        print(json.dumps(inbox(argv[1]), indent=2, default=str))
    else:
        sys.exit("usage: approvals.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
