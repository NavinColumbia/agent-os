#!/usr/bin/env python3
"""accountability.py — who dropped the ball? Reconstruct agent handoffs and detect breakdowns.

The fleet already logs every agent->agent handoff in `conversations` (intents: ask/task/delegate/
review_request/test_request/conflict/reply/done/qa_green/...) and SLA waits in `waits`. What was MISSING is
the accountability layer on top: did a handoff actually get ACTED ON, or did reviewer1 send an issue to
dev1 and dev1 do nothing? This answers that — it matches each request-handoff to its resolution, flags the
DROPPED ones (and the responsible recipient), surfaces OVERDUE SLA waits, and scores each agent's
reliability. A scheduler sweep escalates dropped balls so the FLEET catches them itself, not the human.

    accountability.py report                 # fleet accountability summary
    accountability.py dropped [hours]        # handoffs raised but never acted on (the dropped balls)
    accountability.py sweep                  # detect + escalate dropped balls / overdue waits (cron)
    accountability.py selftest
Run with the agent-os venv python.
"""
import hashlib
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from dbpool import connection  # noqa: E402

# a request expects an action/answer back; a resolution closes it.
REQUEST = {"ask", "task", "delegate", "review_request", "test_request", "escalate", "conflict", "spec_ready"}
RESOLVE = {"reply", "done", "qa_green", "launch", "ack", "spec_ready", "resolved"}
GRACE_MIN = int(__import__("os").environ.get("AOS_HANDOFF_GRACE_MIN", "30"))   # under this = "in flight", not dropped


def _bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


SWEEP_LIMIT = _bounded_int("AOS_ACCOUNTABILITY_SWEEP_LIMIT", 40, 1, 200)
SWEEP_CONTEXT_LIMIT = _bounded_int("AOS_ACCOUNTABILITY_CONTEXT_LIMIT", 80, 5, 500)
DB_LOCK_TIMEOUT_MS = _bounded_int("AOS_ACCOUNTABILITY_DB_LOCK_TIMEOUT_MS", 500, 50, 5000)
DB_STATEMENT_TIMEOUT_MS = max(
    DB_LOCK_TIMEOUT_MS,
    _bounded_int("AOS_ACCOUNTABILITY_DB_STATEMENT_TIMEOUT_MS", 4000, 100, 30000),
)
_STATE_NAME = "scheduled"


def _set_db_timeouts(cur):
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{DB_LOCK_TIMEOUT_MS}ms",))
    cur.execute("SELECT set_config('statement_timeout', %s, true)",
                (f"{DB_STATEMENT_TIMEOUT_MS}ms",))


def _rows(window_hours):
    # Operator-wide report path: this intentionally sees the fleet fabric for aggregate accountability.
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT c.conversation_id, c.sender, c.recipient, c.intent, c.message_id, c.in_reply_to, c.ts,
                              COALESCE(c.tenant_id, ds.tenant_id, dr.tenant_id) AS tenant_id,
                              COALESCE(ds.product, dr.product) AS product
                       FROM conversations c
                       LEFT JOIN directory ds ON ds.agent_id = c.sender
                       LEFT JOIN directory dr ON dr.agent_id = c.recipient
                       WHERE c.ts > now() - (%s || ' hours')::interval
                       ORDER BY c.conversation_id, c.turn""", (window_hours,))
        return cur.fetchall()


def handoffs(window_hours=168):
    """Every request-handoff in the window, matched to whether/how it was resolved.
    Resolved = a later message in the same conversation FROM the recipient (or in_reply_to this one) with a
    resolving intent. Returns dicts incl resolved bool, resolver intent, age, and the responsible recipient."""
    import datetime
    rows = _rows(window_hours)
    by_conv = {}
    for cid, s, r, intent, mid, irt, ts, tenant_id, product in rows:
        by_conv.setdefault(cid, []).append({"s": s, "r": r, "intent": intent, "mid": mid, "irt": irt, "ts": ts,
                                            "tenant_id": tenant_id, "product": product})
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for cid, msgs in by_conv.items():
        used_resolvers = set()
        for i, m in enumerate(msgs):
            if m["intent"] not in REQUEST:
                continue
            # Does a LATER message resolve it? Prefer explicit replies. A generic later "done" from the
            # recipient can close ONE pending request only; otherwise one hand-back falsely resolves every
            # prior request in the conversation and hides dropped work.
            resolver = None
            resolver_key = None
            for j, later in enumerate(msgs[i + 1:], start=i + 1):
                if later["intent"] in RESOLVE and m["mid"] and later["irt"] == m["mid"]:
                    resolver = later["intent"]; resolver_key = later.get("mid") or f"idx:{j}"; break
            if resolver is None:
                for j, later in enumerate(msgs[i + 1:], start=i + 1):
                    key = later.get("mid") or f"idx:{j}"
                    if key in used_resolvers:
                        continue
                    if later["intent"] in RESOLVE and later["s"] == m["r"]:
                        resolver = later["intent"]; resolver_key = key; break
            if resolver_key:
                used_resolvers.add(resolver_key)
            age_min = int((now - m["ts"]).total_seconds() / 60)
            out.append({"conversation": cid, "tenant_id": m.get("tenant_id"), "product": m.get("product"),
                        "from": m["s"], "to": m["r"], "intent": m["intent"],
                        "ts": str(m["ts"]), "age_min": age_min, "resolved": resolver is not None,
                        "resolver": resolver, "responsible": m["r"]})
    return out


def dropped(window_hours=168, grace_min=GRACE_MIN):
    """Handoffs raised but NOT acted on past the grace period — the dropped balls + who's responsible."""
    return [h for h in handoffs(window_hours) if not h["resolved"] and h["age_min"] >= grace_min]


def overdue_waits():
    """SLA breaches: an agent is awaiting a reply that's past its reply_by deadline."""
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("""SELECT w.waiter, w.awaited, w.reply_by,
                                  COALESCE(w.tenant_id, dw.tenant_id, da.tenant_id) AS tenant_id,
                                  COALESCE(dw.product, da.product) AS product
                           FROM waits w
                           LEFT JOIN directory dw ON dw.agent_id = w.waiter
                           LEFT JOIN directory da ON da.agent_id = w.awaited
                           WHERE w.reply_by IS NOT NULL AND w.reply_by < now()""")
            return [{"waiter": w, "awaited": a, "reply_by": str(rb), "tenant_id": tid, "product": product}
                    for w, a, rb, tid, product in cur.fetchall()]
    except Exception:
        return []


def reliability(window_hours=168):
    """Per-agent: how many handoffs they RECEIVED vs resolved vs dropped — who can be trusted with work."""
    agg = {}
    for h in handoffs(window_hours):
        a = agg.setdefault(h["responsible"], {"agent": h["responsible"], "received": 0, "resolved": 0, "dropped": 0})
        a["received"] += 1
        a["resolved" if h["resolved"] else "dropped"] += 1
    for a in agg.values():
        a["reliability"] = round(a["resolved"] / a["received"], 2) if a["received"] else 1.0
    return sorted(agg.values(), key=lambda a: a["reliability"])


def report(window_hours=168):
    hs = handoffs(window_hours); dr = [h for h in hs if not h["resolved"] and h["age_min"] >= GRACE_MIN]
    rel = reliability(window_hours)
    return {"window_hours": window_hours, "handoffs": len(hs), "resolved": sum(1 for h in hs if h["resolved"]),
            "dropped": len(dr), "overdue_waits": len(overdue_waits()),
            "worst_offenders": [r for r in rel if r["dropped"] > 0][:5],
            "dropped_detail": dr[:20]}


def _load_sweep_state():
    """Read the durable two-stream cursor. Missing schema/DB is a hard scheduled degradation."""
    with connection() as c, c.cursor() as cur:
        _set_db_timeouts(cur)
        cur.execute("""SELECT handoff_conversation_id,handoff_turn,wait_waiter,wait_awaited,
                              next_stream,version
                         FROM accountability_sweep_state WHERE name=%s""", (_STATE_NAME,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("accountability sweep cursor row is missing; migration 71 is not ready")
    return {"handoff": (str(row[0] or ""), int(row[1] or 0)),
            "wait": (str(row[2] or ""), str(row[3] or "")),
            "next_stream": row[4] if row[4] in {"handoffs", "waits"} else "handoffs",
            "version": int(row[5] or 0)}


def _save_sweep_state(state, handoff_cursor, wait_cursor, next_stream):
    """Compare-and-swap both cursors so a concurrent manual sweep cannot move progress backward."""
    with connection() as c, c.cursor() as cur:
        _set_db_timeouts(cur)
        cur.execute("""UPDATE accountability_sweep_state
                          SET handoff_conversation_id=%s,handoff_turn=%s,
                              wait_waiter=%s,wait_awaited=%s,next_stream=%s,
                              version=version+1,updated_at=now()
                        WHERE name=%s AND version=%s""",
                    (handoff_cursor[0], int(handoff_cursor[1]), wait_cursor[0], wait_cursor[1],
                     next_stream, _STATE_NAME, int(state["version"])))
        changed = cur.rowcount
    if changed != 1:
        raise RuntimeError("accountability cursor changed concurrently; progress was not overwritten")


def _stream_budgets(limit, next_stream):
    """Split one global budget fairly; a one-item cadence alternates streams durably."""
    total = max(1, min(200, int(limit)))
    handoffs = total // 2
    waits = total // 2
    if total % 2:
        if next_stream == "waits":
            waits += 1
        else:
            handoffs += 1
    return handoffs, waits, ("waits" if next_stream == "handoffs" else "handoffs")


def _handoff_page(cursor, limit, window_hours=168, context_limit=SWEEP_CONTEXT_LIMIT):
    """Fetch a bounded request keyset and bounded later-message context for each request.

    The LATERAL lookup has its own hard row limit, so even a giant conversation cannot turn one scheduler
    tick into a seven-day materialization. A truncated unresolved context is reported as unknown, never as a
    dropped handoff.
    """
    if limit <= 0:
        return {"items": [], "cursor": tuple(cursor), "wrapped": False}

    def query(after):
        with connection() as c, c.cursor() as cur:
            _set_db_timeouts(cur)
            cur.execute("""WITH request_page AS MATERIALIZED (
                             SELECT c.conversation_id,c.turn,c.sender,c.recipient,c.intent,
                                    c.message_id,c.ts,c.tenant_id,
                                    ds.tenant_id AS sender_tenant,dr.tenant_id AS recipient_tenant,
                                    ds.product AS sender_product,dr.product AS recipient_product
                               FROM conversations c
                               LEFT JOIN directory ds ON ds.agent_id=c.sender
                               LEFT JOIN directory dr ON dr.agent_id=c.recipient
                              WHERE c.ts > now()-make_interval(hours => %s)
                                AND c.ts <= now()-make_interval(mins => %s)
                                AND c.intent=ANY(%s)
                                -- A broker transcript belongs to the production accountability graph only
                                -- when it has explicit tenant ownership or at least one endpoint is a known
                                -- directory agent. Historical test/legacy rows with two invented endpoints
                                -- cannot be routed safely and must not poison scheduler health forever.
                                AND COALESCE(c.tenant_id,ds.tenant_id,dr.tenant_id) IS NOT NULL
                                AND (c.conversation_id,c.turn)>(%s,%s)
                              ORDER BY c.conversation_id,c.turn
                              LIMIT %s
                           )
                           SELECT p.conversation_id,p.turn,p.sender,p.recipient,p.intent,p.message_id,p.ts,
                                  p.tenant_id,p.sender_tenant,p.recipient_tenant,
                                  p.sender_product,p.recipient_product,
                                  l.turn,l.sender,l.intent,l.message_id,l.in_reply_to
                             FROM request_page p
                             LEFT JOIN LATERAL (
                                  SELECT x.turn,x.sender,x.intent,x.message_id,x.in_reply_to
                                    FROM conversations x
                                   WHERE x.conversation_id=p.conversation_id AND x.turn>p.turn
                                   ORDER BY x.turn
                                   LIMIT %s
                             ) l ON TRUE
                            ORDER BY p.conversation_id,p.turn,l.turn""",
                        (int(window_hours), int(GRACE_MIN), list(REQUEST), after[0], int(after[1]),
                         int(limit), int(context_limit) + 1))
            return cur.fetchall()

    rows = query(cursor)
    wrapped = False
    if not rows and (cursor[0] or cursor[1]):
        rows = query(("", 0))
        wrapped = True
    grouped = {}
    for row in rows:
        key = (str(row[0]), int(row[1]))
        item = grouped.setdefault(key, {
            "conversation": str(row[0]), "turn": int(row[1]), "from": row[2], "to": row[3],
            "intent": row[4], "message_id": row[5], "ts": str(row[6]), "tenant_id": row[7],
            "sender_tenant": row[8], "recipient_tenant": row[9],
            "sender_product": row[10], "recipient_product": row[11], "context": [],
        })
        if row[12] is not None:
            item["context"].append({"turn": int(row[12]), "sender": row[13], "intent": row[14],
                                    "message_id": row[15], "in_reply_to": row[16]})
    items = list(grouped.values())
    for item in items:
        item["context_truncated"] = len(item["context"]) > context_limit
        item["context"] = item["context"][:context_limit]
    next_cursor = ((items[-1]["conversation"], items[-1]["turn"]) if items
                   else (("", 0) if wrapped else tuple(cursor)))
    return {"items": items, "cursor": next_cursor, "wrapped": wrapped}


def _wait_page(cursor, limit):
    """Fetch one bounded overdue-wait keyset, wrapping only after reaching the tail."""
    if limit <= 0:
        return {"items": [], "cursor": tuple(cursor), "wrapped": False}

    def query(after):
        with connection() as c, c.cursor() as cur:
            _set_db_timeouts(cur)
            cur.execute("""SELECT w.waiter,w.awaited,w.reply_by,w.tenant_id,
                                  dw.tenant_id,da.tenant_id,dw.product,da.product
                             FROM waits w
                             LEFT JOIN directory dw ON dw.agent_id=w.waiter
                             LEFT JOIN directory da ON da.agent_id=w.awaited
                            WHERE w.reply_by IS NOT NULL AND w.reply_by<now()
                              AND COALESCE(w.tenant_id,dw.tenant_id,da.tenant_id) IS NOT NULL
                              AND (w.waiter,w.awaited)>(%s,%s)
                            ORDER BY w.waiter,w.awaited LIMIT %s""",
                        (after[0], after[1], int(limit)))
            return cur.fetchall()

    rows = query(cursor)
    wrapped = False
    if not rows and (cursor[0] or cursor[1]):
        rows = query(("", ""))
        wrapped = True
    items = [{"waiter": r[0], "awaited": r[1], "reply_by": str(r[2]), "tenant_id": r[3],
              "waiter_tenant": r[4], "awaited_tenant": r[5],
              "waiter_product": r[6], "awaited_product": r[7]} for r in rows]
    next_cursor = ((str(items[-1]["waiter"]), str(items[-1]["awaited"])) if items
                   else (("", "") if wrapped else tuple(cursor)))
    return {"items": items, "cursor": next_cursor, "wrapped": wrapped}


def _item_scope(item, tenant_fields, product_fields):
    tenants = {str(item.get(k)) for k in tenant_fields if item.get(k)}
    if len(tenants) != 1:
        reason = "tenant ownership missing" if not tenants else "conflicting tenant ownership"
        raise ValueError(reason)
    products = {str(item.get(k)) for k in product_fields if item.get(k)}
    return next(iter(tenants)), (next(iter(products)) if len(products) == 1 else None)


def _classify_handoffs(items):
    """Bounded in-memory matching; each generic resolver can close at most one request in this page."""
    used = {}
    dropped_items, unknown = [], []
    for item in items:
        conv_used = used.setdefault(item["conversation"], set())
        resolver = None
        for later in item["context"]:
            key = later.get("message_id") or f"turn:{later['turn']}"
            if (later.get("intent") in RESOLVE and item.get("message_id")
                    and later.get("in_reply_to") == item.get("message_id")):
                resolver = key
                break
        if resolver is None:
            for later in item["context"]:
                key = later.get("message_id") or f"turn:{later['turn']}"
                if (key not in conv_used and later.get("intent") in RESOLVE
                        and later.get("sender") == item.get("to")):
                    resolver = key
                    break
        if resolver is not None:
            conv_used.add(resolver)
        elif item.get("context_truncated"):
            unknown.append(item)
        else:
            dropped_items.append(item)
    return dropped_items, unknown


def _semantic_key(kind, tenant_id, parts):
    raw = "|".join([kind, str(tenant_id), *(str(p or "") for p in parts)])
    return f"accountability:{kind}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def _route_management(item, kind, management_fn=None):
    if management_fn is None:
        import management
        management_fn = management.signal
    if kind == "handoff":
        tenant_id, product = _item_scope(
            item, ("tenant_id", "sender_tenant", "recipient_tenant"),
            ("sender_product", "recipient_product"))
        dedupe = _semantic_key(kind, tenant_id,
                               (item.get("message_id"), item["conversation"], item["turn"]))
        subject = f"Dropped {item['intent']} handoff to {item['to']}"
        state = {"conversation": item["conversation"], "turn": item["turn"],
                 "message_id": item.get("message_id"), "sender": item["from"],
                 "recipient": item["to"], "intent": item["intent"], "requested_at": item["ts"]}
        work_id, worker, trigger = item["conversation"], item["to"], "handoff_overdue"
    else:
        tenant_id, product = _item_scope(
            item, ("tenant_id", "waiter_tenant", "awaited_tenant"),
            ("waiter_product", "awaited_product"))
        dedupe = _semantic_key(kind, tenant_id,
                               (item["waiter"], item["awaited"], item["reply_by"]))
        subject = f"Overdue internal wait: {item['waiter']} awaiting {item['awaited']}"
        state = {"waiter": item["waiter"], "awaited": item["awaited"],
                 "reply_by": item["reply_by"]}
        work_id, worker, trigger = dedupe, item["awaited"], "internal_wait_overdue"
    return management_fn(dedupe, subject, trigger, state, tenant_id=tenant_id,
                         product=product, work_id=work_id, worker=worker,
                         manager_role="team-lead", progress=False)


def sweep(*, dry=False, limit=SWEEP_LIMIT, management_fn=None):
    """Bounded, fair scheduled accountability duty.

    Ordinary internal misses become durable tenant-owned management cases. They are never raw-paged to a
    human here; management may ask a person later only through a typed authority boundary. Every component
    failure is explicit and produces a degraded/nonzero CLI outcome instead of an empty false green.
    """
    result = {"status": "ok", "dry": bool(dry), "budget": max(1, min(200, int(limit))),
              "scanned": {"handoffs": 0, "waits": 0}, "dropped": 0, "overdue_waits": 0,
              "unknown_context": 0, "routed": 0, "errors": []}
    try:
        state = _load_sweep_state()
    except Exception as exc:
        result["status"] = "degraded"
        result["errors"].append({"component": "cursor_load", "error": str(exc)[:300]})
        return result

    handoff_budget, wait_budget, next_stream = _stream_budgets(result["budget"], state["next_stream"])
    handoff_page = {"items": [], "cursor": state["handoff"], "wrapped": False}
    wait_page = {"items": [], "cursor": state["wait"], "wrapped": False}
    try:
        handoff_page = _handoff_page(state["handoff"], handoff_budget)
    except Exception as exc:
        result["errors"].append({"component": "handoff_scan", "error": str(exc)[:300]})
    try:
        wait_page = _wait_page(state["wait"], wait_budget)
    except Exception as exc:
        result["errors"].append({"component": "wait_scan", "error": str(exc)[:300]})

    handoff_items = handoff_page["items"]
    wait_items = wait_page["items"]
    dropped_items, unknown = _classify_handoffs(handoff_items)
    result["scanned"] = {"handoffs": len(handoff_items), "waits": len(wait_items)}
    result["dropped"] = len(dropped_items)
    result["overdue_waits"] = len(wait_items)
    result["unknown_context"] = len(unknown)
    for item in unknown:
        result["errors"].append({"component": "handoff_context", "conversation": item["conversation"],
                                 "turn": item["turn"], "error": "bounded resolution context exhausted"})

    if not dry:
        for kind, items in (("handoff", dropped_items), ("wait", wait_items)):
            for item in items:
                try:
                    _route_management(item, kind, management_fn=management_fn)
                    result["routed"] += 1
                except Exception as exc:
                    ref = (f"{item.get('conversation')}:{item.get('turn')}" if kind == "handoff"
                           else f"{item.get('waiter')}->{item.get('awaited')}")
                    result["errors"].append({"component": f"{kind}_route", "item": ref,
                                             "error": str(exc)[:300]})
        try:
            _save_sweep_state(state, handoff_page["cursor"], wait_page["cursor"], next_stream)
        except Exception as exc:
            result["errors"].append({"component": "cursor_save", "error": str(exc)[:300]})

    if result["errors"]:
        result["status"] = "degraded"
    result["cursor"] = {"handoff": list(handoff_page["cursor"]), "wait": list(wait_page["cursor"]),
                        "next_stream": next_stream}
    return result


def _selftest():
    import os
    import directory
    suf = os.urandom(3).hex()
    conv_ok = f"c-ok-{suf}"; conv_drop = f"c-drop-{suf}"; conv_multi = f"c-multi-{suf}"
    tenant_id = f"acct-selftest-{suf}"
    product = f"accountability-{suf}"
    def ins(cid, s, r, intent, mid, hrs, in_reply_to=None):
        cur.execute("""INSERT INTO conversations
                         (conversation_id,sender,recipient,intent,message_id,in_reply_to,ts)
                       VALUES (%s,%s,%s,%s,%s,%s, now() - (%s||' hours')::interval)""",
                    (cid, s, r, intent, mid, in_reply_to, hrs))
    directory.register(f"reviewer@{suf}", "reviewer", product, "reviewing", tenant_id=tenant_id)
    directory.register(f"dev@{suf}", "builder", product, "fixing", tenant_id=tenant_id)
    directory.register(f"dev2@{suf}", "builder", product, "idle", tenant_id=tenant_id)
    directory.register(f"dev3@{suf}", "builder", product, "partial", tenant_id=tenant_id)
    directory.register(f"pm@{suf}", "product-manager", product, "scope", tenant_id=tenant_id)
    with connection() as c, c.cursor() as cur:
        # a RESOLVED handoff: reviewer asks dev, dev replies done
        ins(conv_ok, f"reviewer@{suf}", f"dev@{suf}", "review_request", f"m1-{suf}", 3)
        ins(conv_ok, f"dev@{suf}", f"reviewer@{suf}", "done", f"m2-{suf}", 2)
        # a DROPPED handoff: reviewer asks dev2, dev2 never responds (old enough to be past grace)
        ins(conv_drop, f"reviewer@{suf}", f"dev2@{suf}", "review_request", f"m3-{suf}", 5)
        # two requests to the SAME recipient, only one generic "done": only one request is resolved.
        # This guards the false-green where a single later response closed every prior request.
        ins(conv_multi, f"reviewer@{suf}", f"dev3@{suf}", "review_request", f"m4-{suf}", 5)
        ins(conv_multi, f"reviewer@{suf}", f"dev3@{suf}", "test_request", f"m5-{suf}", 5)
        ins(conv_multi, f"dev3@{suf}", f"reviewer@{suf}", "done", f"m6-{suf}", 4)
        cur.execute("""INSERT INTO waits(waiter, awaited, reply_by, tenant_id)
                       VALUES (%s,%s, now() - interval '10 minutes', %s)
                       ON CONFLICT (waiter, awaited)
                       DO UPDATE SET reply_by=EXCLUDED.reply_by, tenant_id=EXCLUDED.tenant_id""",
                    (f"dev2@{suf}", f"pm@{suf}", tenant_id))
    try:
        dr = dropped(window_hours=24)
        found_drop = any(h["to"] == f"dev2@{suf}" and h["conversation"] == conv_drop for h in dr)
        drop_scoped = any(h["to"] == f"dev2@{suf}" and h.get("tenant_id") == tenant_id
                          and h.get("product") == product for h in dr)
        not_flagged_ok = not any(h["conversation"] == conv_ok for h in dr)   # the resolved one must NOT be dropped
        multi = [h for h in handoffs(24) if h["conversation"] == conv_multi]
        multi_one_open = (len(multi) == 2 and sum(1 for h in multi if h["resolved"]) == 1
                          and sum(1 for h in multi if not h["resolved"]) == 1)
        ow = overdue_waits()
        wait_scoped = any(w["waiter"] == f"dev2@{suf}" and w["awaited"] == f"pm@{suf}"
                          and w.get("tenant_id") == tenant_id and w.get("product") == product for w in ow)
        rel = {r["agent"]: r for r in reliability(24)}
        dev2_dropped = rel.get(f"dev2@{suf}", {}).get("dropped", 0) == 1
        dev_resolved = rel.get(f"dev@{suf}", {}).get("resolved", 0) == 1
        rep = report(24)
        routed = []
        target_handoff = next(h for h in dr if h["conversation"] == conv_drop)
        target_handoff.update({"turn": 1, "message_id": f"m3-{suf}",
                               "sender_tenant": tenant_id, "recipient_tenant": tenant_id,
                               "sender_product": product, "recipient_product": product})
        _route_management(target_handoff, "handoff",
                          management_fn=lambda *args, **kwargs: routed.append((args, kwargs)) or {})
        sweep_routed = bool(routed) and routed[0][1].get("tenant_id") == tenant_id
        scoped_alert = bool(routed) and routed[0][1].get("product") == product
        ok = (found_drop and drop_scoped and wait_scoped and not_flagged_ok and multi_one_open
              and dev2_dropped and dev_resolved
              and rep["dropped"] >= 1 and sweep_routed and scoped_alert)
        print(f"dropped-found={found_drop} resolved-not-flagged={not_flagged_ok} "
              f"multi_one_open={multi_one_open} drop_scoped={drop_scoped} wait_scoped={wait_scoped} "
              f"dev2.dropped={dev2_dropped} dev.resolved={dev_resolved} "
              f"sweep_routed={sweep_routed} scoped_alert={scoped_alert}")
        print("PASS: accountability detects scoped dropped handoffs/overdue waits + routes tenant-owned management ✅"
              if ok else "FAIL")
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE conversation_id IN (%s,%s,%s)",
                        (conv_ok, conv_drop, conv_multi))
            cur.execute("DELETE FROM waits WHERE waiter=%s AND awaited=%s", (f"dev2@{suf}", f"pm@{suf}"))
            cur.execute("DELETE FROM directory WHERE product=%s", (product,))
    sys.exit(0 if ok else 1)



def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "report":
        print(json.dumps(report(int(a[1]) if len(a) > 1 else 168), indent=2))
    elif a[0] == "dropped":
        print(json.dumps(dropped(int(a[1]) if len(a) > 1 else 168), indent=2))
    elif a[0] == "reliability":
        print(json.dumps(reliability(), indent=2))
    elif a[0] == "sweep":
        result = sweep(dry="--dry" in a)
        print(json.dumps(result, indent=2))
        return 2 if result.get("status") != "ok" else 0
    else:
        print("usage: accountability.py report [hours] | dropped [hours] | reliability | sweep | selftest",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
