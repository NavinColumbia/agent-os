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
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402

from aoscfg import ENV, DB

# a request expects an action/answer back; a resolution closes it.
REQUEST = {"ask", "task", "delegate", "review_request", "test_request", "escalate", "conflict", "spec_ready"}
RESOLVE = {"reply", "done", "qa_green", "launch", "ack", "spec_ready", "resolved"}
GRACE_MIN = int(__import__("os").environ.get("AOS_HANDOFF_GRACE_MIN", "30"))   # under this = "in flight", not dropped


def _rows(window_hours):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT conversation_id, sender, recipient, intent, message_id, in_reply_to, ts
                       FROM conversations WHERE ts > now() - (%s || ' hours')::interval
                       ORDER BY conversation_id, turn""", (window_hours,))
        return cur.fetchall()


def handoffs(window_hours=168):
    """Every request-handoff in the window, matched to whether/how it was resolved.
    Resolved = a later message in the same conversation FROM the recipient (or in_reply_to this one) with a
    resolving intent. Returns dicts incl resolved bool, resolver intent, age, and the responsible recipient."""
    import datetime
    rows = _rows(window_hours)
    by_conv = {}
    for cid, s, r, intent, mid, irt, ts in rows:
        by_conv.setdefault(cid, []).append({"s": s, "r": r, "intent": intent, "mid": mid, "irt": irt, "ts": ts})
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for cid, msgs in by_conv.items():
        for i, m in enumerate(msgs):
            if m["intent"] not in REQUEST:
                continue
            # does a LATER message resolve it? (recipient responds, or something replies to this message_id)
            resolver = None
            for later in msgs[i + 1:]:
                if later["intent"] in RESOLVE and (later["s"] == m["r"] or (m["mid"] and later["irt"] == m["mid"])):
                    resolver = later["intent"]; break
            age_min = int((now - m["ts"]).total_seconds() / 60)
            out.append({"conversation": cid, "from": m["s"], "to": m["r"], "intent": m["intent"],
                        "ts": str(m["ts"]), "age_min": age_min, "resolved": resolver is not None,
                        "resolver": resolver, "responsible": m["r"]})
    return out


def dropped(window_hours=168, grace_min=GRACE_MIN):
    """Handoffs raised but NOT acted on past the grace period — the dropped balls + who's responsible."""
    return [h for h in handoffs(window_hours) if not h["resolved"] and h["age_min"] >= grace_min]


def overdue_waits():
    """SLA breaches: an agent is awaiting a reply that's past its reply_by deadline."""
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT waiter, awaited, reply_by FROM waits
                           WHERE reply_by IS NOT NULL AND reply_by < now()""")
            return [{"waiter": w, "awaited": a, "reply_by": str(rb)} for w, a, rb in cur.fetchall()]
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


def sweep():
    """Cron: detect dropped balls + overdue waits and ESCALATE — so the fleet catches breakdowns itself."""
    dr = dropped(); ow = overdue_waits()
    if dr or ow:
        audit.append(actor="accountability", action="DroppedBallsDetected", resource="fleet",
                     decision="escalated", payload={"dropped": len(dr), "overdue_waits": len(ow),
                                                    "who": list({h["responsible"] for h in dr})[:10]})
        try:
            import notify
            notify.send(f"Accountability: {len(dr)} dropped handoff(s), {len(ow)} overdue wait(s). "
                        f"Responsible: {', '.join(sorted({h['responsible'] for h in dr}))[:200]}",
                        title="agent-os accountability", priority="high", tags="warning")
        except Exception:
            pass
    return {"dropped": len(dr), "overdue_waits": len(ow)}


def _selftest():
    import os
    suf = os.urandom(3).hex()
    conv_ok = f"c-ok-{suf}"; conv_drop = f"c-drop-{suf}"
    def ins(cid, s, r, intent, mid, hrs):
        cur.execute("""INSERT INTO conversations (conversation_id,sender,recipient,intent,message_id,ts)
                       VALUES (%s,%s,%s,%s,%s, now() - (%s||' hours')::interval)""",
                    (cid, s, r, intent, mid, hrs))
    with psycopg.connect(DB) as c, c.cursor() as cur:
        # a RESOLVED handoff: reviewer asks dev, dev replies done
        ins(conv_ok, f"reviewer@{suf}", f"dev@{suf}", "review_request", f"m1-{suf}", 3)
        ins(conv_ok, f"dev@{suf}", f"reviewer@{suf}", "done", f"m2-{suf}", 2)
        # a DROPPED handoff: reviewer asks dev2, dev2 never responds (old enough to be past grace)
        ins(conv_drop, f"reviewer@{suf}", f"dev2@{suf}", "review_request", f"m3-{suf}", 5)
        c.commit()
    try:
        dr = dropped(window_hours=24)
        found_drop = any(h["to"] == f"dev2@{suf}" and h["conversation"] == conv_drop for h in dr)
        not_flagged_ok = not any(h["conversation"] == conv_ok for h in dr)   # the resolved one must NOT be dropped
        rel = {r["agent"]: r for r in reliability(24)}
        dev2_dropped = rel.get(f"dev2@{suf}", {}).get("dropped", 0) == 1
        dev_resolved = rel.get(f"dev@{suf}", {}).get("resolved", 0) == 1
        rep = report(24)
        ok = found_drop and not_flagged_ok and dev2_dropped and dev_resolved and rep["dropped"] >= 1
        print(f"dropped-found={found_drop} resolved-not-flagged={not_flagged_ok} "
              f"dev2.dropped={dev2_dropped} dev.resolved={dev_resolved}")
        print("PASS: accountability detects dropped handoffs + who's responsible ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE conversation_id IN (%s,%s)", (conv_ok, conv_drop))
            c.commit()
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
        print(json.dumps(sweep(), indent=2))
    else:
        sys.exit("usage: accountability.py report [hours] | dropped [hours] | reliability | sweep | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
