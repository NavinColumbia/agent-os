#!/usr/bin/env python3
"""findings.py — close the loophole: a review finding is a GOVERNED, OWNED, TRACKED work-item, not text.

The bug the CEO caught: review agents produced findings as text back to the orchestrator (a human-like
single point), who manually triaged and could silently DROP one (e.g. "add real signup" got mis-bucketed
as gated). The fix: every finding is filed here, which (1) records it, (2) ROUTES it through the existing
governed fabric — orchestrate.request_collaborator checks the agent directory for an active agent of the
right role and assigns the work to them (or files a hire_request to spawn one), so it has an OWNER; (3)
puts it on the CEO task board for visibility; (4) is tracked to resolution, and accountability/sweep
escalates any finding left open too long. No finding can die in the orchestrator's head anymore.

C1 hardening (REBUILD-PLAN, quality-engine): resolution is now GATED ON EVIDENCE — "trust me, it's fixed"
no longer exists. resolve() requires a PASSING verification record in finding_verifications: for findings
filed by the agentic QA loop the ORIGINATING user story is re-run through the real explorer (an
independent observer — the fixer never grades its own homework); everything else gets an adversarial
qa-security AI verdict via factory.agent that defaults to BROKEN. And sweep() no longer just pages ntfy:
an overdue finding is RE-ROUTED to a senior owner (an AI decision) and surfaced on the CEO cockpit feed
(in-app notifications), so SLA breaches land where the CEO actually looks.

    findings.py file <source> <need_role> "<title>" ["detail"] [severity]   # file + auto-route to an owner
    findings.py open                       # the accountable backlog (open/routed, unresolved)
    findings.py verify <id>                # re-run the originating check -> a verification record
    findings.py resolve <id> [by]          # GATED: only lands on a passing verification record
    findings.py sweep                      # escalate findings past SLA (cron): senior re-route + feed
    findings.py selftest
Run with the agent-os venv python.
"""
import json
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit        # noqa: E402
import orchestrate  # noqa: E402
import taskboard    # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

SLA_HOURS = int(__import__("os").environ.get("AOS_FINDING_SLA_HOURS", "24"))
AOS_ROOT = Path.home() / "projects" / "agent-os"


def _conn(tenant_id=None):
    return tenant_connection(tenant_id) if tenant_id else connection()


def _tenant_for_product(product):
    if not product:
        return None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenant_products WHERE product=%s ORDER BY created_at DESC LIMIT 1",
                        (product,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _tenant_for_org(org_id):
    if not org_id:
        return None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM orgs WHERE id=%s", (int(org_id),))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _tenant_for_source(source, verify=None):
    if isinstance(verify, dict):
        tid = _tenant_for_product(verify.get("product")) or _tenant_for_org(verify.get("org"))
        if tid:
            return tid
    if isinstance(source, str) and ":" in source:
        prefix, product = source.split(":", 1)
        if prefix in {"qa", "qa-agentic", "audit"}:
            return _tenant_for_product(product)
    return None


def _tenant_for_finding(fid):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM findings WHERE id=%s", (int(fid),))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS findings (
            id BIGSERIAL PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, detail TEXT,
            severity TEXT NOT NULL DEFAULT 'med', need_role TEXT NOT NULL DEFAULT 'builder',
            status TEXT NOT NULL DEFAULT 'open',          -- open|routed|hire_pending|no_role|resolved|dropped
            owner TEXT, task_id BIGINT, hire_id BIGINT, board_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ, resolved_by TEXT)""")
        for col, typ in (("verify_check", "JSONB"),        # HOW to independently re-verify this finding
                         ("verification_id", "BIGINT"),    # the PASSING record that justified resolution
                         ("escalated_at", "TIMESTAMPTZ"),  # SLA breach -> senior re-route happened
                         ("escalated_to", "TEXT"),
                         # Stable identity supplied by an upstream durable workflow. A controller may
                         # checkpoint and resume many times; those process boundaries must not create a new
                         # company-wide work item for the same immutable observation.
                         ("dedupe_key", "TEXT"),
                         ("drop_reason", "TEXT")):
            cur.execute(f"ALTER TABLE findings ADD COLUMN IF NOT EXISTS {col} {typ}")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS findings_dedupe_key_uq
                       ON findings (tenant_id, source, dedupe_key)
                       WHERE dedupe_key IS NOT NULL""")
        # the VERIFICATION artifact resolve() gates on: an immutable record of an independent re-check.
        cur.execute("""CREATE TABLE IF NOT EXISTS finding_verifications (
            id BIGSERIAL PRIMARY KEY, finding_id BIGINT NOT NULL, kind TEXT NOT NULL,
            passed BOOLEAN NOT NULL, evidence JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("ALTER TABLE findings ADD COLUMN IF NOT EXISTS tenant_id TEXT")
        cur.execute("ALTER TABLE finding_verifications ADD COLUMN IF NOT EXISTS tenant_id TEXT")
        c.commit()


def file(source, need_role, title, detail="", severity="med", priority=5, verify=None, tenant_id=None,
         dedupe_key=None):
    """File a finding AND route it to an accountable owner via the governed fabric. Returns the disposition.

    `verify` (optional dict) records HOW this finding is independently re-verified at resolve time —
    e.g. {"kind": "qa_story", "target_url", "vision", "token", "org", "summary", "story", "product"}
    re-runs the originating user story through the real agentic explorer. Without it, resolution falls
    back to an adversarial qa-security AI verdict (still gated — never an unchecked 'resolved')."""
    _ensure()
    tenant_id = tenant_id or _tenant_for_source(source, verify)
    if not tenant_id:
        raise ValueError("finding tenant is required; platform work must explicitly use tenant_id='_platform'")
    stable_key = str(dedupe_key).strip()[:240] if dedupe_key is not None else None
    stable_key = stable_key or None
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO findings
                           (source, title, detail, severity, need_role, verify_check, tenant_id, dedupe_key)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, source, dedupe_key)
                           WHERE dedupe_key IS NOT NULL DO NOTHING
                       RETURNING id""",
                    (source, title, detail, severity, need_role, Jsonb(verify) if verify else None,
                     tenant_id, stable_key))
        row = cur.fetchone()
        if row is None:
            cur.execute("""SELECT id, status, owner, task_id, hire_id, board_id
                           FROM findings
                           WHERE tenant_id=%s AND source=%s AND dedupe_key=%s""",
                        (tenant_id, source, stable_key))
            existing = cur.fetchone()
            if existing is None:  # The unique winner must be visible in this transaction.
                raise RuntimeError("finding idempotency conflict winner is unavailable")
            fid, status, owner, task_id, hire_id, bid = existing
            audit.append(actor="findings", action="FindingDeduped", resource=str(fid),
                         decision="reused", payload={"source": source, "dedupe_key": stable_key},
                         tenant_id=tenant_id)
            return {"finding_id": fid, "status": status, "owner": owner, "routing": "deduped",
                    "task_id": task_id, "hire_id": hire_id, "board_id": bid,
                    "tenant_id": tenant_id, "deduped": True}
        fid = row[0]
    # ROUTE through the directory-aware router — assign to an active agent or spawn one (governed)
    _STATUS = {"routed_to_existing": "routed", "hire_requested_overloaded": "hire_pending",
               "hire_requested_spawn": "hire_pending", "no_exact_role_use_nearest": "routed",
               "no_role": "no_role"}
    r = orchestrate.request_collaborator(
        f"finding:{source}", need_role, title, priority, tenant_id=tenant_id,
        idempotency_key=f"finding:{fid}:initial:{need_role}")
    act = r.get("action")
    eff = r  # the disposition we actually persist (the re-routed result wins, if any)
    # if no exact role, re-route to the nearest role so it still gets an owner (don't drop it)
    if act == "no_exact_role_use_nearest" and r.get("suggested_role"):
        eff = orchestrate.request_collaborator(
            f"finding:{source}", r["suggested_role"], title, priority, tenant_id=tenant_id,
            idempotency_key=f"finding:{fid}:nearest:{r['suggested_role']}")
    # derive the real disposition from the EFFECTIVE result so r2's task_id/hire_id/status aren't lost
    eff_act = eff.get("action")
    status = _STATUS.get(eff_act, "open")
    owner = eff.get("assignee") or eff.get("suggested_role")
    task_id = eff.get("task_id")
    hire_id = eff.get("hire_id")
    # CEO visibility on the task board
    bid = None
    try:
        bid = taskboard.add(f"[{severity}] {title}", detail, source=f"review:{source}",
                            tenant=tenant_id or "platform")
    except Exception:
        pass
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE findings SET status=%s, owner=%s, task_id=%s, hire_id=%s, board_id=%s WHERE id=%s""",
                    (status, owner, task_id, hire_id, bid, fid))
    audit.append(actor="findings", action="FindingFiled", resource=str(fid), decision=status,
                 payload={"source": source, "need_role": need_role, "owner": owner, "routing": eff_act},
                 tenant_id=tenant_id)
    return {"finding_id": fid, "status": status, "owner": owner, "routing": eff_act,
            "task_id": task_id, "hire_id": hire_id, "board_id": bid, "tenant_id": tenant_id}


def verify(fid):
    """Independently RE-RUN the finding's originating story/check -> a verification RECORD (the artifact
    resolve() gates on). The verifier is never the fixer: a qa_story check re-runs the real agentic
    explorer against the live app; anything else gets an adversarial qa-security AI verdict via
    factory.agent. Any error, non-answer or refusal counts as FAIL — default BROKEN, never default green."""
    _ensure()
    fid = int(fid)
    tenant_id = _tenant_for_finding(fid)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT verify_check, title, detail FROM findings WHERE id=%s", (fid,))
        row = cur.fetchone()
    if not row:
        return {"verification_id": None, "passed": False, "error": f"no finding {fid}"}
    check = row[0] or {}
    if isinstance(check, str):
        try:
            check = json.loads(check)
        except Exception:
            check = {}
    title, detail = row[1], row[2]
    kind = check.get("kind") or "ai_review"
    passed, evidence = False, {}
    try:
        if kind == "qa_story":
            # re-run the ORIGINATING user story through the real state-based explorer (one round,
            # findings-filing off so a verification run can never recursively file more findings).
            qa_dir = str(SCRIPTS / "qa")
            if qa_dir not in sys.path:
                sys.path.insert(0, qa_dir)
            import qa_run as _qa
            rep = _qa.qa_run(check["target_url"], check.get("vision", ""), token=check.get("token"),
                             org=str(check.get("org", "0")), product_summary=check.get("summary", ""),
                             product=check.get("product", "app"), stories=[check["story"]],
                             max_rounds=1, max_steps=int(check.get("max_steps", 15)),
                             file_findings=False, audit_gate=True,
                             agentic=False, tenant_id=tenant_id)  # skeptical audit; no recursive QA org
            passed = bool(rep.get("passed"))
            evidence = {"report_md": rep.get("md"), "report_json": rep.get("json"),
                        "verdict": rep.get("verdict"), "qa_run_id": rep.get("qa_run_id")}
        else:
            # adversarial independent AI check — qa-security wears the evidence-or-it-didn't-happen brief.
            import factory
            prompt = (
                "You are qa-security, the ADVERSARIAL verifier (docs/STANDARDS-verification.md): default "
                "to BROKEN; your job is to find what is still wrong, not to confirm it works.\n\n"
                f"A fix is claimed for this finding:\nTITLE: {title}\nDETAIL: {detail or '(none)'}\n\n"
                "Independently verify the fix with concrete evidence (read the code, run the check, "
                "reproduce the original complaint). If you cannot obtain real evidence that it is fixed, "
                "it is NOT fixed.\n\nReply with ONLY a JSON object: "
                '{"passed": true|false, "evidence": "<what you actually observed>"}')
            res = factory.agent("qa-security", str(AOS_ROOT), prompt, light=True)
            out = (res.get("out_full") or res.get("out") or "") if isinstance(res, dict) else ""
            v = {}
            try:
                v = json.loads(out[out.index("{"): out.rindex("}") + 1])
            except Exception:
                v = {}
            passed = bool(isinstance(res, dict) and res.get("rc") == 0 and v.get("passed") is True)
            evidence = {"agent_rc": res.get("rc") if isinstance(res, dict) else None,
                        "verdict": v, "raw": out[:2000]}
    except Exception as e:
        passed, evidence = False, {"error": str(e)}
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO finding_verifications (finding_id, kind, passed, evidence, tenant_id)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (fid, kind, passed, Jsonb(evidence), tenant_id))
        vid = cur.fetchone()[0]
    audit.append(actor="findings", action="FindingVerified", resource=str(fid),
                 decision="pass" if passed else "fail", payload={"verification_id": vid, "kind": kind},
                 tenant_id=tenant_id)
    return {"verification_id": vid, "passed": passed, "kind": kind, "evidence": evidence}


def record_verification(fid, kind, passed, evidence=None, by="external-verifier"):
    """Record an independently gathered verification artifact.

    This is for evidence produced outside `verify()` itself, for example a deterministic browser bridge
    probe, a CI test run, or a staging smoke script. It still does NOT resolve the finding; callers must pass
    the returned verification_id to resolve(), which preserves the same gated close path.
    """
    _ensure()
    fid = int(fid)
    tenant_id = _tenant_for_finding(fid)
    evidence = evidence or {}
    if not isinstance(evidence, dict):
        evidence = {"evidence": evidence}
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM findings WHERE id=%s", (fid,))
        if not cur.fetchone():
            return {"verification_id": None, "passed": False, "error": f"no finding {fid}"}
        cur.execute("""INSERT INTO finding_verifications (finding_id, kind, passed, evidence, tenant_id)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (fid, kind or "external", bool(passed), Jsonb(evidence), tenant_id))
        vid = cur.fetchone()[0]
    audit.append(actor="findings", action="FindingVerified", resource=str(fid),
                 decision="pass" if passed else "fail",
                 payload={"verification_id": vid, "kind": kind or "external", "by": by},
                 tenant_id=tenant_id)
    return {"verification_id": vid, "passed": bool(passed), "kind": kind or "external",
            "evidence": evidence}


def resolve(fid, by="agent", verification_id=None):
    """GATED resolve: a finding only becomes 'resolved' on a PASSING verification record. With no
    verification_id the originating story/check is re-run RIGHT NOW (verify()); an existing record id
    must belong to this finding and have passed. A failing verification leaves the finding open (and is
    audited) — the fixer's word is never evidence."""
    _ensure()
    fid = int(fid)
    tenant_id = _tenant_for_finding(fid)
    if verification_id is not None:
        with _conn(tenant_id) as c, c.cursor() as cur:
            cur.execute("SELECT finding_id, passed FROM finding_verifications WHERE id=%s",
                        (int(verification_id),))
            row = cur.fetchone()
        if not row or int(row[0]) != fid:
            return {"finding_id": fid, "resolved": False, "verification_id": verification_id,
                    "reason": "verification record missing or belongs to a different finding"}
        v = {"verification_id": int(verification_id), "passed": bool(row[1])}
    else:
        v = verify(fid)
    if not v.get("passed"):
        audit.append(actor="findings", action="FindingResolveRefused", resource=str(fid),
                     decision="still_broken", payload={"by": by, "verification_id": v.get("verification_id")},
                     tenant_id=tenant_id)
        return {"finding_id": fid, "resolved": False, "verification_id": v.get("verification_id"),
                "reason": "verification did not pass — the originating check still fails"}
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE findings SET status='resolved', resolved_at=now(), resolved_by=%s,
                       verification_id=%s WHERE id=%s RETURNING board_id""",
                    (by, v["verification_id"], fid))
        row = cur.fetchone()
        # CLOSE THE CEO-VISIBLE MIRROR. task_board is where the CEO actually looks; resolving the finding
        # without closing its card left the board asserting the bug was still open. Measured 2026-08-10:
        # 16 findings resolved-with-evidence (some back on 2026-07-03) still showed as open work, so the
        # board reported a 23-item backlog when only 7 were real — and several of the loudest "HIGH,
        # 37 days overdue" rows had been fixed weeks earlier. A one-way mirror is worse than no mirror.
        if row and row[0]:
            cur.execute("""UPDATE task_board
                              SET status='done', updated_at=now(),
                                  notes = coalesce(notes,'') ||
                                          %s
                            WHERE id=%s AND status <> 'done'""",
                        (f"\n[done] finding {fid} resolved with verification "
                         f"{v['verification_id']} (by {by})", row[0]))
    audit.append(actor="findings", action="FindingResolved", resource=str(fid), decision="resolved",
                 payload={"by": by, "verification_id": v["verification_id"],
                          "board_id": (row[0] if row else None)}, tenant_id=tenant_id)
    return {"finding_id": fid, "status": "resolved", "resolved": True,
            "verification_id": v["verification_id"]}


def supersede_source(source, reason, *, by="system", tenant_id=None):
    """Auditably retire findings that an upstream durable workflow filed prematurely.

    This is intentionally distinct from ``resolve``: it makes no claim that a defect was fixed. It is for a
    whole, exact source whose work remains owned elsewhere (for example, observations accidentally fanned out
    during a resumable QA checkpoint). Linked CEO cards/tasks are closed so the mirrors cannot keep reporting
    work that this backlog no longer owns.
    """
    _ensure()
    source = str(source or "").strip()
    reason = str(reason or "").strip()
    if not source or not reason or not tenant_id:
        raise ValueError("source, reason, and tenant_id are required")
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id, task_id, hire_id, board_id FROM findings
                       WHERE tenant_id=%s AND source=%s
                         AND status NOT IN ('resolved','dropped') FOR UPDATE""",
                    (tenant_id, source))
        rows = cur.fetchall()
        ids = [row[0] for row in rows]
        task_ids = [row[1] for row in rows if row[1]]
        hire_ids = [row[2] for row in rows if row[2]]
        board_ids = [row[3] for row in rows if row[3]]
        if ids:
            cur.execute("""UPDATE findings
                           SET status='dropped', resolved_at=now(), resolved_by=%s, drop_reason=%s
                           WHERE tenant_id=%s AND id=ANY(%s)""",
                        (by, reason[:2000], tenant_id, ids))
        if task_ids:
            cur.execute("""UPDATE tasks SET status='done'
                           WHERE tenant_id=%s AND id=ANY(%s) AND status<>'done'""",
                        (tenant_id, task_ids))
        if hire_ids:
            cur.execute("""UPDATE hire_requests SET status='cancelled'
                           WHERE tenant_id=%s AND id=ANY(%s) AND status='open'""",
                        (tenant_id, hire_ids))
        if board_ids:
            cur.execute("""UPDATE task_board
                           SET status='done', updated_at=now(),
                               notes=coalesce(notes,'') || %s
                           WHERE id=ANY(%s) AND status<>'done'""",
                        (f"\n[done] superseded by {by}: {reason[:800]}", board_ids))
    audit.append(actor="findings", action="FindingSourceSuperseded", resource=source,
                 decision="dropped", payload={"by": by, "reason": reason[:1000], "count": len(ids)},
                 tenant_id=tenant_id)
    return {"source": source, "status": "dropped", "count": len(ids), "tenant_id": tenant_id}


def open_findings(tenant_id=None):
    """The accountable backlog: filed but not yet resolved — nothing here can be silently forgotten."""
    _ensure()
    extra = "AND tenant_id=%s" if tenant_id else ""
    args = [tenant_id] if tenant_id else []
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""SELECT id, source, title, severity, need_role, status, owner, escalated_to, tenant_id,
                              EXTRACT(EPOCH FROM now()-created_at)::int/3600 AS age_h
                       FROM findings WHERE status NOT IN ('resolved','dropped') {extra} ORDER BY created_at""",
                    args)
        return [{"id": i, "source": s, "title": t, "severity": sev, "need_role": nr, "status": st,
                 "owner": o, "escalated_to": esc, "tenant_id": tid, "age_hours": ah}
                for i, s, t, sev, nr, st, o, esc, tid, ah in cur.fetchall()]


def _senior_role(need_role, title):
    """AI decision (factory.agent): which SENIOR role takes over an overdue finding. Falls back to a
    deterministic seniority ladder so an SLA escalation can never silently fail on a model hiccup."""
    roles = sorted(orchestrate.known_roles())
    try:
        import factory
        prompt = (
            "An open defect finding blew past its SLA and must be RE-ROUTED to a MORE SENIOR owner who "
            "will drive it to closure.\n"
            f"Finding: {title!r} (current owner role: {need_role}).\n"
            f"Choose the single most appropriate senior role from this list: {', '.join(roles)}\n"
            'Reply with ONLY a JSON object: {"role": "<role-name>"}')
        res = factory.agent("controller", str(AOS_ROOT), prompt, light=True)
        out = (res.get("out_full") or res.get("out") or "") if isinstance(res, dict) else ""
        cand = json.loads(out[out.index("{"): out.rindex("}") + 1]).get("role", "")
        if cand in roles and cand != need_role:
            return cand
    except Exception:
        pass
    for cand in ("staff-engineer", "tech-lead", "incident-commander", "controller"):
        if cand in roles and cand != need_role:
            return cand
    return "controller"


def sweep(ids=None):
    """Cron: a finding past SLA cannot quietly rot. Each one is (1) RE-ROUTED to a senior owner — an AI
    decision via factory.agent, executed through the governed fabric so the senior actually gets the
    task; (2) surfaced on the CEO cockpit feed (in-app notifications — where the CEO looks); and (3)
    the operator is still paged via ntfy. `ids` optionally targets specific findings (tests/re-sweeps)."""
    _ensure()
    overdue = [f for f in open_findings() if (f["age_hours"] or 0) >= SLA_HOURS
               and (ids is None or f["id"] in ids)]
    escalated = []
    for f in overdue:
        tenant_id = f.get("tenant_id")
        if not tenant_id:
            raise RuntimeError(f"finding {f['id']} has no tenant; refusing cross-tenant escalation")
        with _conn(tenant_id) as c, c.cursor() as cur:
            cur.execute("SELECT escalated_at FROM findings WHERE id=%s", (f["id"],))
            row = cur.fetchone()
        if row and row[0]:
            continue                                   # already senior-owned; don't churn it again
        senior = _senior_role(f["need_role"], f["title"])
        r = orchestrate.request_collaborator(
            f"finding-escalation:{f['id']}", senior,
            f"[OVERDUE {f['age_hours']}h] {f['title']}", priority=2, tenant_id=tenant_id,
            idempotency_key=f"finding:{f['id']}:escalation:{senior}")
        owner = r.get("assignee") or senior
        with _conn(tenant_id) as c, c.cursor() as cur:
            cur.execute("""UPDATE findings SET owner=%s, escalated_at=now(), escalated_to=%s,
                           task_id=COALESCE(%s, task_id),
                           status=CASE WHEN %s='routed_to_existing' THEN 'routed' ELSE status END
                           WHERE id=%s""",
                        (owner, senior, r.get("task_id"), r.get("action"), f["id"]))
        try:                                           # the cockpit feed — visible in the console, not just ntfy
            import notifications
            notifications.send(tenant_id, "findings",
                               f"Finding #{f['id']} overdue — escalated to {senior}",
                               body=f"[{f['severity']}] {f['title']} (open {f['age_hours']}h, "
                                    f"was {f['owner'] or f['need_role']})",
                               level="standard")
        except Exception:
            pass
        audit.append(actor="findings", action="FindingEscalated", resource=str(f["id"]),
                     decision=senior, payload={"new_owner": owner, "routing": r.get("action")},
                     tenant_id=tenant_id)
        escalated.append({"id": f["id"], "to": senior, "owner": owner})
    if overdue:
        audit.append(actor="findings", action="FindingsOverdue", resource="backlog", decision="escalated",
                     payload={"count": len(overdue), "ids": [f["id"] for f in overdue][:20]})
        try:
            import notify
            notify.send(f"{len(overdue)} review finding(s) open past {SLA_HOURS}h with no resolution. "
                        f"Escalated to: {', '.join(sorted({e['to'] for e in escalated})) or '(already senior)'}",
                        title="agent-os findings", priority="high", tags="warning")
        except Exception:
            pass
    return {"open": len(open_findings()), "overdue": len(overdue), "escalated": escalated}


def _selftest():
    import os
    import types
    import billing
    import directory
    import factory
    import notify
    suf = os.urandom(3).hex()
    builder_id = f"builder@findings-{suf}"
    senior_id = f"staff-engineer@findings-{suf}"
    tenant_reg = billing.signup(f"findings-selftest-{suf}", "free")
    tenant_id = tenant_reg["tenant_id"]
    tenant_product = f"findings-tenant-{suf}"
    directory.register(builder_id, "builder", product=f"findings-{suf}", task="idle",
                       tenant_id="_platform")
    directory.register(senior_id, "staff-engineer", product=f"findings-{suf}", task="idle",
                       tenant_id="_platform")
    real_agent, real_notify = factory.agent, notify.send
    real_qa_mod = sys.modules.get("qa_run")
    # CONTAIN THE ROUTING. request_collaborator picks the least-loaded ACTIVE agent of the needed role out
    # of the live directory — which on a working install includes real builders. So the selftest's synthetic
    # findings were being dispatched to real builder sessions, which then worked fixture tickets. Confining
    # directory.find to this run's own agents keeps the REAL routing/hire logic under test (we still exercise
    # request_collaborator end to end) while making it impossible for a fixture to reach a live worker.
    real_find = directory.find

    def _confined_find(role=None, **kw):
        return [a for a in real_find(role=role, **kw) if str(a.get("agent_id", "")).endswith(f"findings-{suf}")]

    directory.find = _confined_find
    agent_roles, pages = [], []
    checks = {}
    try:
        with _conn(tenant_id) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO tenant_products (product, tenant_id)
                           VALUES (%s,%s) ON CONFLICT DO NOTHING""", (tenant_product, tenant_id))
        # 1) file a finding needing a 'builder' — must route to the live builder above (an OWNER), not vanish
        r = file(f"reviewer-{suf}", "builder", f"Add X to product {suf}", "details here", "high",
                 tenant_id="_platform")
        checks["routed_to_owner"] = bool(r["status"] == "routed" and r["owner"] == builder_id and r["task_id"])
        checks["on_taskboard"] = r["board_id"] is not None
        checks["in_backlog"] = any(f["id"] == r["finding_id"] for f in open_findings())
        rt = file(f"qa:{tenant_product}", "builder", f"Tenant bug {suf}", "tenant details", "high",
                  verify={"kind": "qa_story", "product": tenant_product,
                          "target_url": "http://tenant.test", "vision": "v",
                          "story": {"id": "US-T", "title": "Tenant story"}})
        with _conn(tenant_id) as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id, board_id FROM findings WHERE id=%s", (rt["finding_id"],))
            ftid, fbid = cur.fetchone()
            cur.execute("SELECT tenant FROM task_board WHERE id=%s", (fbid,))
            board_tenant = cur.fetchone()[0]
        checks["tenant_finding_scoped"] = bool(rt["tenant_id"] == tenant_id and ftid == tenant_id
                                               and board_tenant == tenant_id
                                               and any(f["id"] == rt["finding_id"]
                                                       for f in open_findings(tenant_id)))

        # 2) GATED RESOLVE — a FAILING independent verification must REFUSE resolution (default BROKEN)
        factory.agent = lambda role, repo, prompt, **k: (agent_roles.append(role) or {
            "rc": 0, "out": '{"passed": false, "evidence": "the button is still dead"}',
            "out_full": '{"passed": false, "evidence": "the button is still dead"}'})
        rr = resolve(r["finding_id"], by=builder_id)
        checks["resolve_refused_on_fail"] = bool(rr["resolved"] is False and rr["verification_id"]
                                                 and any(f["id"] == r["finding_id"] for f in open_findings()))
        # 3) a PASSING verification record lands the resolve, records the record id, clears the backlog
        factory.agent = lambda role, repo, prompt, **k: (agent_roles.append(role) or {
            "rc": 0, "out": '{"passed": true, "evidence": "re-ran the check; green"}',
            "out_full": '{"passed": true, "evidence": "re-ran the check; green"}'})
        rr2 = resolve(r["finding_id"], by=builder_id)
        checks["resolve_gated_on_pass"] = bool(rr2["resolved"] and rr2["verification_id"])
        checks["resolved_clears"] = not any(f["id"] == r["finding_id"] for f in open_findings())
        checks["adversarial_verifier"] = "qa-security" in agent_roles       # never the builder's own word
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT verification_id FROM findings WHERE id=%s", (r["finding_id"],))
            checks["verification_id_persisted"] = cur.fetchone()[0] == rr2["verification_id"]
            cur.execute("SELECT passed FROM finding_verifications WHERE id=%s", (rr2["verification_id"],))
            checks["verification_record_passed"] = cur.fetchone()[0] is True

        # 4) qa_story verification — resolve re-runs the ORIGINATING story via qa_run (findings-filing OFF)
        seen = {}
        fake = types.ModuleType("qa_run")

        def _fake_qa_run(url, vision, token=None, org="0", product_summary="", stories=None,
                         file_findings=True, **k):
            seen.update({"url": url, "story": (stories or [{}])[0].get("id"), "file_findings": file_findings})
            return {"passed": True, "md": "/tmp/x.md", "json": "/tmp/x.json",
                    "verdict": "ALL 1 STORIES PASSED", "qa_run_id": 1}
        fake.qa_run = _fake_qa_run
        sys.modules["qa_run"] = fake
        r2 = file(f"reviewer-{suf}", "builder", f"QA bug in {suf}", "explorer-found", "high",
                  verify={"kind": "qa_story", "target_url": "http://app.test", "vision": "v",
                          "story": {"id": "US-9", "title": "Sign in"}, "product": f"findings-{suf}"},
                  tenant_id="_platform")
        rr3 = resolve(r2["finding_id"], by=builder_id)
        checks["qa_story_rerun_gates"] = bool(rr3["resolved"] and seen.get("story") == "US-9"
                                              and seen.get("file_findings") is False
                                              and seen.get("url") == "http://app.test")

        # 5) SLA sweep — an overdue finding is RE-ROUTED to a senior owner + surfaces on the cockpit feed
        r3 = file(f"reviewer-{suf}", "builder", f"Never fixed in {suf}", "rotting", "high",
                  tenant_id="_platform")
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE findings SET created_at=now()-interval '48 hours' WHERE id=%s",
                        (r3["finding_id"],))
            c.commit()
        factory.agent = lambda role, repo, prompt, **k: {"rc": 0, "out": '{"role": "staff-engineer"}',
                                                         "out_full": '{"role": "staff-engineer"}'}
        notify.send = lambda *a, **k: pages.append(a)
        sw = sweep(ids=[r3["finding_id"]])
        esc = next((e for e in sw["escalated"] if e["id"] == r3["finding_id"]), None)
        checks["overdue_rerouted_senior"] = bool(esc and esc["to"] == "staff-engineer"
                                                 and esc["owner"] == senior_id)
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT escalated_at, escalated_to, owner FROM findings WHERE id=%s",
                        (r3["finding_id"],))
            ea, et, ow = cur.fetchone()
            checks["escalation_persisted"] = bool(ea and et == "staff-engineer" and ow == senior_id)
            cur.execute("""SELECT count(*) FROM notifications WHERE tenant_id='_platform'
                           AND category='findings' AND title LIKE %s""", (f"%#{r3['finding_id']}%",))
            checks["on_cockpit_feed"] = cur.fetchone()[0] == 1
        checks["ntfy_still_pages"] = len(pages) == 1
        # a second sweep must NOT churn it again (already senior-owned)
        checks["escalates_once"] = sweep(ids=[r3["finding_id"]])["escalated"] == []

        ok = all(checks.values())
        print(" ".join(f"{k}={v}" for k, v in checks.items()))
        print("PASS: filed -> routed -> GATED resolve (evidence, not claims) -> SLA senior re-route + "
              "cockpit feed ✅" if ok else "FAIL")
    finally:
        directory.find = real_find
        factory.agent, notify.send = real_agent, real_notify
        if real_qa_mod is not None:
            sys.modules["qa_run"] = real_qa_mod
        else:
            sys.modules.pop("qa_run", None)
        with _conn() as c, c.cursor() as cur:
            cur.execute("""DELETE FROM finding_verifications WHERE finding_id IN
                           (SELECT id FROM findings WHERE source=%s)""", (f"reviewer-{suf}",))
            cur.execute("DELETE FROM findings WHERE source=%s", (f"reviewer-{suf}",))
            cur.execute("""DELETE FROM finding_verifications WHERE finding_id IN
                           (SELECT id FROM findings WHERE source=%s)""", (f"qa:{tenant_product}",))
            cur.execute("DELETE FROM findings WHERE source=%s", (f"qa:{tenant_product}",))
            # Delete by REQUESTER, not just by synthetic assignee. request_collaborator routes to whatever
            # active agent of the right role it finds in the LIVE directory, which is often a real builder,
            # not our fixture one — those rows survived an assignee-scoped delete and real builder sessions
            # got dispatched on fixture work (observed: tasks 1175 and 1178 worked by
            # builder@f7bf9e-saas-rest-api and builder@623da4-saas-rest-api). The requester is ours no matter
            # who it was routed to, so it is the only handle that reliably reaches every leaked row.
            cur.execute("DELETE FROM tasks WHERE requester LIKE %s OR assignee IN (%s,%s)",
                        (f"finding%:reviewer-{suf}", builder_id, senior_id))
            cur.execute("DELETE FROM hire_requests WHERE requester LIKE %s", (f"finding%:reviewer-{suf}",))
            cur.execute("DELETE FROM directory WHERE agent_id IN (%s,%s)", (builder_id, senior_id))
            cur.execute("DELETE FROM task_board WHERE source=%s", (f"review:reviewer-{suf}",))
            cur.execute("DELETE FROM task_board WHERE source=%s", (f"review:qa:{tenant_product}",))
            cur.execute("""DELETE FROM notifications WHERE tenant_id='_platform' AND category='findings'
                           AND body LIKE %s""", (f"%{suf}%",))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tenant_id,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tenant_id,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "file" and len(a) >= 4:
        print(json.dumps(file(a[1], a[2], a[3], a[4] if len(a) > 4 else "", a[5] if len(a) > 5 else "med"), indent=2))
    elif a[0] == "open":
        print(json.dumps(open_findings(), indent=2))
    elif a[0] == "verify" and len(a) > 1:
        print(json.dumps(verify(int(a[1])), indent=2))
    elif a[0] == "record-verification" and len(a) > 4:
        try:
            evidence = json.loads(a[4])
        except Exception:
            evidence = {"note": a[4]}
        print(json.dumps(record_verification(int(a[1]), a[2], a[3].lower() in ("1", "true", "yes", "pass"),
                                             evidence, a[5] if len(a) > 5 else "external-verifier"),
                         indent=2))
    elif a[0] == "resolve" and len(a) > 1:
        print(json.dumps(resolve(int(a[1]), a[2] if len(a) > 2 else "human")))
    elif a[0] == "sweep":
        print(json.dumps(sweep(), indent=2))
    else:
        sys.exit('usage: findings.py file <source> <need_role> "<title>" ["detail"] [sev] | open | '
                 'verify <id> | record-verification <id> <kind> <pass?> <evidence-json> [by] | '
                 'resolve <id> [by] | sweep | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
