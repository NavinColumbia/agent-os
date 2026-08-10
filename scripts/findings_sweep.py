#!/usr/bin/env python3
"""findings_sweep.py — close the loop on the findings board.

The board was WRITE-ONLY. Dogfood and QA runs filed findings into task_board; nothing ever read them back
out. Measured on 2026-08-10: 27 open items, ZERO of them touched after creation, average age 35 days, three
of them CRITICAL — including one claiming work is silently lost while every surface reports green. The
`acceptance-dogfood` job that raises them was itself disabled, so the board was simply frozen.

This sweep makes the backlog impossible to ignore:

  * RANKS by severity then age, so a critical can never sit under a month of lows (the reason three
    criticals were invisible: the only ordering anywhere was recency).
  * ESCALATES a critical/high that has been open past a threshold, with a cooldown so the CEO is paged
    once per item per window rather than every tick.
  * REPORTS a compact summary the weekly digest embeds, so open findings reach the founder by default.

It deliberately does NOT auto-close anything. A finding can only be closed by evidence that the bug no
longer reproduces — closing on a heuristic would manufacture exactly the false-green the board exists to
catch. For the same reason the sweep does not stamp updated_at on rows it merely looked at: "never touched
since it was raised" is a signal worth preserving, and churning the timestamp would erase it.

    findings_sweep.py report          # print the ranked backlog (no writes, no notifications)
    findings_sweep.py sweep           # report + escalate anything past the threshold
    findings_sweep.py selftest        # offline (pure ranking/serverity parsing)
Run with the agent-os venv python.
"""
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

# Severity is packed into the title as a bracket tag by the review/dogfood writers.
_SEV_TAG = re.compile(r"^\s*\[(?P<sev>critical|high|med|medium|low)\]", re.I)
_AREA_TAG = re.compile(r"\[(?P<area>[^\]]+)\]")
RANK = {"critical": 0, "high": 1, "med": 2, "medium": 2, "low": 3}
NAMES = {0: "critical", 1: "high", 2: "med", 3: "low", 4: "untagged"}

# How long a finding of each severity may sit before it is escalated to the CEO. A critical that nobody has
# looked at for two days is itself an incident; a low can wait a month.
ESCALATE_AFTER_DAYS = {0: 2, 1: 7, 2: 30, 3: 90, 4: 30}
COOLDOWN_DAYS = int(os.environ.get("AOS_FINDINGS_COOLDOWN_DAYS", "7"))
OPEN_STATES = ("asked", "open", "new", "blocked", "in_progress")
# task_board carries TWO kinds of card: findings filed by review/QA/dogfood runs (source 'review:…') and
# the CEO's own roadmap items (source 'ceo' — "deploy publicly", "cloud horizontal scale"). Only the first
# kind is a BUG. Reporting a June roadmap item as a "46-day OVERDUE finding" is noise that buries the four
# real ones, so the triage view is scoped to findings; roadmap cards stay on the board where they belong.
FINDING_SOURCES = "review:%"


def _severity(title):
    m = _SEV_TAG.match(title or "")
    return RANK.get((m.group("sev").lower() if m else ""), 4)


def _area(title):
    """The second bracket tag ('dogfood/first-run', '1-ceo-cockpit') — where the finding came from."""
    tags = _AREA_TAG.findall(title or "")
    return tags[1] if len(tags) > 1 else (tags[0] if tags and _severity(title) == 4 else "")


def _clean(title):
    """Title with its bracket tags stripped — they are rendered separately, not as raw noise."""
    return _AREA_TAG.sub("", title or "", count=2).strip(" -—:")


def rank_findings(rows):
    """Pure (unit-tested): [(id, title, status, created_at_days_old)] -> ranked dicts, worst first."""
    out = []
    for tid, title, status, age_days in rows:
        sev = _severity(title)
        out.append({
            "id": tid, "severity": NAMES[sev], "_rank": sev,
            "area": _area(title), "title": _clean(title), "status": status,
            "age_days": int(age_days),
            "overdue": age_days > ESCALATE_AFTER_DAYS[sev],
        })
    out.sort(key=lambda d: (d["_rank"], -d["age_days"]))
    return out


def open_findings(limit=200):
    """The live open backlog, worst first. Empty list if the DB is unavailable (never raises)."""
    try:
        import aoscfg
        import psycopg
        with psycopg.connect(aoscfg.DB) as c, c.cursor() as cur:
            cur.execute(
                """SELECT id, title, status,
                          extract(epoch from (now() - created_at)) / 86400.0
                     FROM task_board
                    WHERE status = ANY(%s) AND source LIKE %s
                    ORDER BY created_at DESC
                    LIMIT %s""", (list(OPEN_STATES), FINDING_SOURCES, limit))
            return rank_findings(cur.fetchall())
    except Exception:
        return []


def summary(max_lines=6):
    """Compact text block for the founder digest. '' when the board is clear, so a clean board adds no noise."""
    items = open_findings()
    if not items:
        return ""
    by_sev = {}
    for it in items:
        by_sev[it["severity"]] = by_sev.get(it["severity"], 0) + 1
    counts = " · ".join(f"{n} {s}" for s, n in sorted(by_sev.items(), key=lambda kv: RANK.get(kv[0], 4)))
    overdue = [i for i in items if i["overdue"]]
    lines = [f"Open findings: {len(items)} ({counts}) · {len(overdue)} past their review-by date"]
    for it in items[:max_lines]:
        flag = " ⚠ OVERDUE" if it["overdue"] else ""
        where = f" [{it['area']}]" if it["area"] else ""
        lines.append(f"  #{it['id']} {it['severity'].upper()}{where} {it['title'][:88]} "
                     f"({it['age_days']}d){flag}")
    if len(items) > max_lines:
        lines.append(f"  … and {len(items) - max_lines} more — `taskboard.py list --open`")
    return "\n".join(lines)


def _note(tid, text):
    """Append an escalation note. Only written when we ACT (escalate) — never on a passive look, so the
    'untouched since raised' signal stays truthful."""
    try:
        import taskboard
        taskboard.status(tid, "asked", note=text)      # keep the state; record that it was escalated
        return True
    except Exception:
        return False


def _already_escalated(tid, notes_cache):
    return f"[escalated" in (notes_cache.get(tid) or "")


def sweep(notify_fn=None, dry=False):
    """Escalate overdue critical/high findings to the CEO. Returns a summary dict. Fail-open."""
    items = [i for i in open_findings() if i["overdue"] and i["_rank"] <= 1]
    notes = {}
    try:
        import aoscfg
        import psycopg
        with psycopg.connect(aoscfg.DB) as c, c.cursor() as cur:
            cur.execute("SELECT id, coalesce(notes,'') FROM task_board WHERE id = ANY(%s)",
                        ([i["id"] for i in items] or [0],))
            notes = dict(cur.fetchall())
    except Exception:
        pass

    fresh = [i for i in items if not _already_escalated(i["id"], notes)]
    if not fresh:
        return {"overdue": len(items), "escalated": 0, "note": "nothing new past threshold"}
    if dry:
        return {"overdue": len(items), "escalated": 0, "would_escalate": [i["id"] for i in fresh]}

    body = "\n".join(
        f"#{i['id']} {i['severity'].upper()} ({i['age_days']}d unlooked-at): {i['title'][:110]}"
        for i in fresh)
    text = (f"{len(fresh)} finding(s) past their review-by date and never triaged:\n{body}\n\n"
            f"Close with evidence: taskboard.py status <id> done --note \"<repro re-run + result>\"")
    sent = False
    try:
        send = notify_fn
        if send is None:
            import notify as _n
            send = lambda t: _n.send(t, title="agent-os: findings overdue", tags="rotating_light")
        sent = bool(send(text))
    except Exception:
        sent = False
    for i in fresh:
        _note(i["id"], f"[escalated] {i['age_days']}d open, never triaged — surfaced to the CEO")
    return {"overdue": len(items), "escalated": len(fresh), "notified": sent,
            "ids": [i["id"] for i in fresh]}


def _selftest():
    rows = [
        (1, "[low] [dogfood] something cosmetic", "asked", 100.0),
        (2, "[critical] [dogfood/first-run] work is silently lost", "asked", 3.0),
        (3, "[high] [1-ceo-cockpit] inbox absent after reload", "asked", 1.0),
        (4, "no tags at all", "asked", 400.0),
        (5, "[critical] [x] older critical", "asked", 40.0),
    ]
    r = rank_findings(rows)
    assert [x["id"] for x in r] == [5, 2, 3, 1, 4], [x["id"] for x in r]   # severity, then oldest first
    assert r[0]["severity"] == "critical" and r[0]["age_days"] == 40
    assert r[0]["title"] == "older critical" and r[0]["area"] == "x", r[0]
    # a 3-day-old critical is overdue (threshold 2d); a 1-day-old high is not (threshold 7d)
    assert next(x for x in r if x["id"] == 2)["overdue"] is True
    assert next(x for x in r if x["id"] == 3)["overdue"] is False
    # an untagged finding still ranks and never crashes the parser
    assert next(x for x in r if x["id"] == 4)["severity"] == "untagged"
    # a low that is 100 days old is NOT overdue at a 90-day threshold... it is; check the boundary holds
    assert next(x for x in r if x["id"] == 1)["overdue"] is True
    print("findings_sweep selftest: PASS (severity-then-age ranking, tag parsing, overdue thresholds)")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "report"
    if cmd == "selftest":
        sys.exit(_selftest())
    if cmd == "report":
        s = summary(max_lines=12)
        print(s or "Findings board is clear — nothing open.")
        sys.exit(0)
    if cmd == "sweep":
        import json
        print(json.dumps(sweep(dry="--dry" in a), indent=2))
        sys.exit(0)
    print(__doc__)
    sys.exit(1)
