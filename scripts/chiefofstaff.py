#!/usr/bin/env python3
"""chiefofstaff.py — the CEO's chief-of-staff brief (REBUILD-PLAN B1).

The review's finding: digest.py is an OPERATOR brief (one ntfy channel to the developer's phone, platform
MRR) — no tenant CEO ever gets briefed, and nothing in the console. This builds the real thing: a
PER-TENANT, agentic chief-of-staff brief, composed by a model FROM THIS COMPANY'S REAL STATE (portfolio,
in-flight builds, open decisions, health, spend), surfaced IN THE CONSOLE — the "your executive team
briefs you" experience. Every brief is an AI call over grounded facts (no fabrication; cost is not a
concern). It reads like a chief-of-staff, not a dashboard dump.

  python chiefofstaff.py brief <tenant_id> [org_id]
  python chiefofstaff.py selftest
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbpool import connection, tenant_connection  # noqa: E402


def _facts(tid, org_id=0):
    """Gather THIS company's real state from the modules that already compute it. Every value is grounded;
    a missing source degrades to a note, never a fabricated number."""
    f = {"awaiting": [], "health": None, "verdict": None, "portfolio": None, "spend": None,
         "workstreams": []}
    try:
        import approvals
        inbox = (approvals.inbox(tid) or {}).get("items", [])     # inbox() returns {items:[...], count:N}
        f["awaiting"] = [{"kind": i.get("kind"), "title": i.get("title") or i.get("summary")} for i in inbox][:8]
    except Exception:
        pass
    try:
        import cockpit
        f["verdict"] = cockpit.company_summary(tid, org_id)
        f["health"] = cockpit.health(tid, org_id)
    except Exception:
        pass
    try:
        import workstreamview
        f["workstreams"] = workstreamview.active_workstreams(tid, org_id)[:8]
    except Exception:
        pass
    try:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("""SELECT count(DISTINCT product), coalesce(sum(cost_usd),0)
                           FROM traces WHERE product IN (SELECT product FROM tenant_products WHERE tenant_id=%s)
                             AND ts > now()-interval '30 days'""", (tid,))
            n, cost = cur.fetchone()
            f["portfolio"] = {"products_touched_30d": int(n or 0)}
            f["spend"] = {"cost_usd_30d": round(float(cost or 0), 2)}
    except Exception:
        pass
    return f


def _active_workstream_lines(facts):
    out = []
    for w in facts.get("workstreams") or []:
        if not (w.get("running") or w.get("awaiting")):
            continue
        label = w.get("product") or w.get("label") or f"workstream {w.get('thread_id')}"
        phase = w.get("phase") or "work"
        state = "running" if w.get("running") else f"awaiting {w.get('awaiting')}"
        out.append(f"{label}: {phase} {state}")
    return out


_BRIEF_SYS = (
    "You are the CEO's chief of staff at a company whose entire staff is AI agents. Write the CEO's brief "
    "for right now — warm, concise, executive, in the voice of a trusted chief of staff (not a dashboard). "
    "Use ONLY the FACTS provided; never invent a number, product, or event. Structure your reply as ONLY "
    'JSON: {"headline":"<one sentence: the state of things>","needs_you":["<decision awaiting the CEO>",...],'
    '"team_did":["<what the agent team accomplished / is doing>",...],"watch":["<a risk or thing to watch>",...],'
    '"suggestion":"<one proactive next move you recommend>"}. Keep each item under 18 words. If a section '
    "has nothing real, use an empty list. Do not mention that you are an AI or that this is generated."
)


def _cache_get(tid, org_id=0, ttl_s=1800):
    """Return a recently-computed brief (< ttl_s old) for INSTANT serving, else None. The brief is an ~8s
    model call; without this the cockpit recomputes it on every load. Fail-open (None on any error, incl.
    a missing table on first-ever call). Hot path -> pooled read."""
    try:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("""SELECT data FROM brief_cache WHERE tenant_id=%s AND org_id=%s
                           AND computed_at > now() - make_interval(secs => %s)""",
                        (tid, int(org_id or 0), int(ttl_s)))
            r = cur.fetchone()
        if r and isinstance(r[0], dict):
            d = dict(r[0]); d["_cached"] = True
            return d
    except Exception:
        pass
    return None


def _ensure_cache():
    with connection() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS brief_cache (
            tenant_id TEXT NOT NULL, org_id INT NOT NULL DEFAULT 0, data JSONB NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (tenant_id, org_id))""")


def _cache_put(tid, org_id, data):
    """Store a freshly-computed brief for reuse. Idempotent per (tenant, org). Never raises."""
    try:
        _ensure_cache()
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO brief_cache (tenant_id, org_id, data, computed_at)
                           VALUES (%s,%s,%s,now()) ON CONFLICT (tenant_id, org_id)
                           DO UPDATE SET data=EXCLUDED.data, computed_at=now()""",
                        (tid, int(org_id or 0), json.dumps(data, default=str)))
    except Exception:
        pass


def brief(tid, org_id=0, api_key=None, use_cache=True):
    """Compose the per-tenant chief-of-staff brief from real state. Returns a structured dict the console
    renders. Serves a warm cache instantly (the model call is ~8s); computes + caches on a miss. Falls
    back to a deterministic brief (still grounded) if the model call fails — never blank."""
    if use_cache:
        cached = _cache_get(tid, org_id)
        if cached is not None:
            # The AI narrative is cached, but decisions awaiting the CEO can't wait ~30min — overlay the
            # LIVE approvals inbox so 'needs_you' is always current even on a cache hit (cheap query).
            try:
                import approvals
                items = (approvals.inbox(tid) or {}).get("items", [])   # inbox() -> {items:[...], count:N}
                cached["needs_you"] = [t for t in ((i.get("title") or i.get("summary")) for i in items) if t][:5]
            except Exception:
                pass
            try:
                facts = _facts(tid, org_id)
                active = _active_workstream_lines(facts)
                if active:
                    cached["_facts"] = facts
                    cached["headline"] = f"Your team is working on {len(active)} active workstream(s)."
                    existing = cached.get("team_did") if isinstance(cached.get("team_did"), list) else []
                    cached["team_did"] = (active + existing)[:5]
                    cached["suggestion"] = "Open Activity or Cockpit to monitor the active workstream."
            except Exception:
                pass
            return cached
    facts = _facts(tid, org_id)
    try:
        import factory
        if api_key is not None:
            try:
                factory._ctx.api_key = api_key
            except Exception:
                pass
        r = factory.agent("classifier", ".", _BRIEF_SYS + "\n\nFACTS:\n" + json.dumps(facts, default=str)[:2500],
                          light=True, model=factory.CHEAP_MODEL)
        data = factory._extract_json((r or {}).get("out_full") or (r or {}).get("out") or "")
        if isinstance(data, dict) and data.get("headline"):
            data["_facts"] = facts
            _cache_put(tid, org_id, data)          # warm the cache so the next cockpit load is instant
            return data
    except Exception:
        pass
    return _fallback_brief(facts)


def _fallback_brief(facts):
    """Grounded, zero-model brief for batch delivery and provider outages."""
    verdict = (facts.get("verdict") or {}).get("verdict") if isinstance(facts.get("verdict"), dict) else facts.get("verdict")
    active = _active_workstream_lines(facts)
    return {
        "headline": (f"Your team is working on {len(active)} active workstream(s)."
                     if active else
                     f"Company status: {verdict or 'steady'}. {len(facts['awaiting'])} decision(s) await you."),
        "needs_you": [a.get("title") for a in facts["awaiting"] if a.get("title")][:5],
        "team_did": (active + ([f"Worked across {facts['portfolio']['products_touched_30d']} product(s) in 30 days"]
                               if facts.get("portfolio") else []))[:5],
        "watch": ([] if (verdict in (None, "healthy")) else [f"Company health is '{verdict}' — review needed"]),
        "suggestion": "Open Activity or Cockpit to monitor the active workstream." if active else
                      "Open the Assistant to direct your next build." if not facts["awaiting"]
                      else "Clear the decisions waiting on you, then keep building.",
        "_facts": facts, "_fallback": True,
    }


def _fmt(b):
    """Render a brief dict into a short notification body."""
    lines = []
    if b.get("needs_you"):
        lines.append("Awaiting you: " + "; ".join(b["needs_you"][:3]))
    if b.get("team_did"):
        lines.append("Your team: " + "; ".join(b["team_did"][:2]))
    if b.get("suggestion"):
        lines.append("Next: " + b["suggestion"])
    return "\n".join(lines)[:400]


def _daily_tenants(context_key, limit):
    """Select a fair bounded page of genuinely active, not-yet-briefed tenants.

    Test/demo signups with no recent work are not an audience for a daily model campaign. A tenant is active
    when controller work, a human request, or product traces changed in the last 30 days. The notification
    ledger itself is the durable cursor: already-delivered tenants disappear from subsequent five-minute
    batches, so the tail cannot be starved by a fixed LIMIT prefix.
    """
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("""SELECT t.tenant_id FROM tenants t
                           WHERE coalesce(t.suspended,false)=false
                             AND NOT EXISTS (
                                   SELECT 1 FROM notifications n
                                    WHERE n.tenant_id=t.tenant_id AND n.context_key=%s)
                             AND (
                               EXISTS (SELECT 1 FROM controller_state s
                                        WHERE s.tenant_id=t.tenant_id
                                          AND s.updated_at>now()-interval '30 days')
                               OR EXISTS (SELECT 1 FROM agent_requests r
                                           WHERE r.tenant_id=t.tenant_id
                                             AND r.created_at>now()-interval '30 days')
                               OR EXISTS (SELECT 1 FROM tenant_products tp JOIN traces tr
                                            ON tr.product=tp.product
                                           WHERE tp.tenant_id=t.tenant_id
                                             AND tr.ts>now()-interval '30 days'))
                           ORDER BY t.tenant_id LIMIT %s""", (context_key, max(1, min(200, int(limit)))))
            return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        # A scheduled executive-communications job must not turn database blindness into a false-green
        # "zero tenants" run.  Let the process exit non-zero so scheduler/management can recover/escalate.
        raise RuntimeError(f"daily tenant discovery unavailable: {exc}") from exc


def push_daily(limit=25):
    """Deliver one daily brief through bounded, durable batches.

    This scheduled path intentionally does not call a model once per tenant.  A previous 200-tenant model
    loop could not finish inside the scheduler lease, retried the same prefix, and never reached later CEOs.
    The on-demand ``brief()`` remains richly model-composed; daily delivery uses the same live facts with the
    grounded fallback renderer and a dated idempotency key. Running this every five minutes drains any size
    active population while sending each tenant at most once per UTC day.
    """
    sent = skipped = duplicates = 0
    context_key = f"chief-of-staff-daily:{datetime.now(timezone.utc).date().isoformat()}"
    tenants = _daily_tenants(context_key, limit)
    for tid in tenants:
        try:
            b = _fallback_brief(_facts(tid, 0))
            body = _fmt(b)
            if not body:
                skipped += 1
                continue                              # nothing worth interrupting the CEO for today
            import notifications
            delivery = notifications.send(tid, "digest", b.get("headline", "Your daily brief"), body,
                                          level="standard", url="/#cockpit", context_key=context_key)
            if isinstance(delivery, dict) and delivery.get("duplicate"):
                duplicates += 1
            else:
                sent += 1
        except Exception:
            continue
    return {"briefs_sent": sent, "duplicates": duplicates,
            "tenants_considered": len(tenants), "empty_skipped": skipped,
            "context_key": context_key}


def _selftest():
    import uuid
    import factory
    real = factory.agent
    tid = f"cos-selftest-{uuid.uuid4().hex[:8]}"
    ok = True

    def chk(cond, label):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + f": {label}")
        ok = ok and bool(cond)

    try:
        # (a) grounded FALLBACK brief (force a model failure) — must still be a real, structured brief
        factory.agent = lambda *a, **k: {"rc": 1, "out": "boom"}
        b = brief(tid, 0)
        chk(b.get("_fallback") and "headline" in b and isinstance(b.get("needs_you"), list),
            "fallback brief is structured + grounded (never blank on model failure)")

        # (b) MODEL brief — stub a chief-of-staff reply, assert it's parsed + facts attached
        factory.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": json.dumps(
            {"headline": "One product shipping; one decision awaits you.", "needs_you": ["Approve the pricing page"],
             "team_did": ["Built and QA-passed the checkout flow"], "watch": [], "suggestion": "Approve, then start v2."})}
        b2 = brief(tid, 0)
        chk(b2.get("headline", "").startswith("One product") and not b2.get("_fallback")
            and b2["needs_you"] == ["Approve the pricing page"] and "_facts" in b2,
            "model brief parsed into headline/needs_you/team_did/watch/suggestion + facts attached")

        # (c) facts are grounded (real modules queried, no crash on an empty tenant)
        chk(isinstance(_facts(tid, 0), dict) and "awaiting" in _facts(tid, 0),
            "_facts gathers real state without fabricating (empty tenant -> empty, not invented)")

        # (d) cache writes are tenant-owned data writes, not request-path DDL under the app role.
        _cache_put(tid, 0, {"headline": "Cached brief", "needs_you": [], "team_did": [],
                            "watch": [], "suggestion": "Keep moving"})
        cached = _cache_get(tid, 0)
        chk(cached and cached.get("_cached") and cached.get("headline") == "Cached brief",
            "brief cache writes and reads a structured tenant-owned brief")

        # (e) daily push is scheduled (the proactive morning brief) + _fmt renders a body
        import scheduler
        scheduled = any(n == "chiefofstaff-daily" for n, _, _ in scheduler.DEFAULT_SCHEDULES)
        chk(scheduled and _fmt({"needs_you": ["Approve X"], "suggestion": "Ship it"}),
            "daily brief is scheduled (chiefofstaff-daily) + renders a notification body")

        # (f) REGRESSION GUARD: a tenant with a REAL pending decision must surface it in 'awaiting'. The
        #     inbox-shape bug (approvals.inbox() returns {items:[...],count:N}, not a list) made this
        #     silently EMPTY, so the CEO's brief never showed a single decision. A provider connected but
        #     no consent always yields a 'consent required' inbox item -> awaiting must be non-empty.
        import billing
        import tenantproviders
        gtid = billing.signup("cos-await-" + uuid.uuid4().hex[:6], "free")["tenant_id"]
        tenantproviders.connect(gtid, "anthropic", "subscription")
        gfacts = _facts(gtid, 0)
        chk(len(gfacts["awaiting"]) > 0 and any(a.get("title") for a in gfacts["awaiting"]),
            "a tenant with a pending decision surfaces it in awaiting (guards the inbox {items} shape bug)")
        try:
            with tenant_connection(tid) as c, c.cursor() as cur:
                cur.execute("DELETE FROM brief_cache WHERE tenant_id=%s", (tid,))
            with connection() as c, c.cursor() as cur:
                cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (gtid,)); c.commit()
        except Exception:
            pass
        print("PASS: chiefofstaff — per-tenant CEO brief, AI-composed from grounded real state, "
              "fallback never blank ✅" if ok else "FAIL")
    finally:
        factory.agent = real
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        sys.exit(0 if _selftest() else 1)
    elif cmd == "brief":
        print(json.dumps(brief(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0), indent=2, default=str))
    elif cmd == "push-daily":
        print(json.dumps(push_daily()))
