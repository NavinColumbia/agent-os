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
from datetime import datetime, timezone
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


# A finding a human has deliberately PARKED has been triaged — the whole point of the overdue clock is to
# catch items nobody has looked at. Continuing to page about a decision that was already made is how an
# alert channel gets ignored, so 'blocked' items are listed but never flagged overdue or escalated.
PARKED_STATES = ("blocked",)


def rank_findings(rows):
    """Pure (unit-tested): [(id, title, status, created_at_days_old)] -> ranked dicts, worst first."""
    out = []
    for row in rows:
        tid, title, status, age_days = row[:4]
        tenant = row[4] if len(row) > 4 else "platform"
        sev = _severity(title)
        parked = str(status or "").lower() in PARKED_STATES
        out.append({
            "id": tid, "severity": NAMES[sev], "_rank": sev,
            "area": _area(title), "title": _clean(title), "status": status,
            "tenant": tenant or "platform",
            "age_days": int(age_days), "parked": parked,
            "overdue": (not parked) and age_days > ESCALATE_AFTER_DAYS[sev],
        })
    out.sort(key=lambda d: (d["_rank"], -d["age_days"]))
    return out


def open_findings(limit=200, strict=False):
    """The live open backlog, worst first.

    Interactive reports remain fail-soft. Scheduled triage passes ``strict=True`` so database blindness is
    recorded as a failed schedule instead of a false-green empty board.
    """
    try:
        import aoscfg
        import psycopg
        with psycopg.connect(aoscfg.DB) as c, c.cursor() as cur:
            cur.execute(
                """SELECT id, title, status,
                          extract(epoch from (now() - created_at)) / 86400.0,
                          tenant
                     FROM task_board
                    WHERE status = ANY(%s) AND source LIKE %s
                    ORDER BY CASE
                               WHEN title ~* '^\\s*\\[critical\\]' THEN 0
                               WHEN title ~* '^\\s*\\[high\\]' THEN 1
                               WHEN title ~* '^\\s*\\[(med|medium)\\]' THEN 2
                               WHEN title ~* '^\\s*\\[low\\]' THEN 3
                               ELSE 4
                             END,
                             created_at ASC,
                             id ASC
                    LIMIT %s""", (list(OPEN_STATES), FINDING_SOURCES, limit))
            return rank_findings(cur.fetchall())
    except Exception:
        if strict:
            raise
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
    parked = [i for i in items if i["parked"]]
    tail = f" · {len(parked)} parked" if parked else ""
    lines = [f"Open findings: {len(items)} ({counts}) · {len(overdue)} past their review-by date{tail}"]
    for it in items[:max_lines]:
        flag = " ⚠ OVERDUE" if it["overdue"] else (" · parked" if it["parked"] else "")
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


_ESCALATED_AT = re.compile(r"\[escalated\s+(?P<at>\d{4}-\d\d-\d\dT[^\]]+)\]", re.I)


def _already_escalated(tid, notes_cache, now=None):
    """Whether an accepted escalation is still in cooldown.

    Old notes used the marker ``[escalated]`` without a timestamp.  Preserve those as accepted rather than
    suddenly replaying a historical backlog after an upgrade.  New notes carry an ISO timestamp and become
    eligible again after COOLDOWN_DAYS.  A failed delivery never writes either marker.
    """
    notes = notes_cache.get(tid) or ""
    stamps = list(_ESCALATED_AT.finditer(notes))
    if not stamps:
        return "[escalated]" in notes.lower()
    try:
        sent_at = datetime.fromisoformat(stamps[-1].group("at").replace("Z", "+00:00"))
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return (current - sent_at).total_seconds() < COOLDOWN_DAYS * 86400
    except (TypeError, ValueError, OverflowError):
        # An unparseable acceptance marker is evidence that a prior version acted.  Fail quiet instead of
        # turning malformed historical data into a page storm.
        return True


def _load_notes(items):
    import aoscfg
    import psycopg
    with psycopg.connect(aoscfg.DB) as c, c.cursor() as cur:
        cur.execute("SELECT id, coalesce(notes,'') FROM task_board WHERE id = ANY(%s)",
                    ([i["id"] for i in items] or [0],))
        return dict(cur.fetchall())


def sweep(notify_fn=None, dry=False):
    """Escalate overdue critical/high findings to the CEO. Scheduler data failures are explicit."""
    items = [i for i in open_findings(strict=True) if i["overdue"] and i["_rank"] <= 1]
    try:
        notes = _load_notes(items)
    except Exception as exc:
        raise RuntimeError(f"findings notes unavailable: {exc}") from exc

    fresh = [i for i in items if not _already_escalated(i["id"], notes)]
    if not fresh:
        return {"overdue": len(items), "escalated": 0, "note": "nothing new past threshold"}
    if dry:
        return {"overdue": len(items), "escalated": 0, "would_escalate": [i["id"] for i in fresh]}

    accepted = []
    accepted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for i in fresh:
        text = (f"Finding #{i['id']} is past its review-by date: {i['severity'].upper()} "
                f"({i['age_days']}d unlooked-at): {i['title'][:180]}\n\n"
                "Close it only with reproduction evidence and the verification result.")
        try:
            if notify_fn is not None:
                delivered = bool(notify_fn(text))
            else:
                import notifications
                # One tenant-scoped semantic outbox record per finding generation.  The operator pager only
                # receives notification routing metadata; tenant finding content stays in that tenant's feed.
                previous = notes.get(i["id"]) or ""
                generation = len(_ESCALATED_AT.findall(previous)) + (1 if "[escalated]" in previous.lower() else 0)
                context = f"finding-escalation:{i['id']}:{generation}"
                result = notifications.send(i.get("tenant") or "platform", "findings",
                                            f"Finding #{i['id']} needs review", text,
                                            level="urgent", url="/#findings", context_key=context)
                delivered = isinstance(result, dict) and bool(result.get("id"))
        except Exception:
            delivered = False
        if delivered and _note(i["id"], f"[escalated {accepted_at}] {i['age_days']}d open, never triaged — surfaced to the CEO"):
            accepted.append(i["id"])
    return {"overdue": len(items), "attempted": len(fresh), "escalated": len(accepted),
            "notified": bool(accepted), "ids": accepted,
            "failed_ids": [i["id"] for i in fresh if i["id"] not in accepted]}


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
    # a deliberately PARKED finding is triaged, so it must never read as overdue no matter how old
    parked_rows = [(9, "[critical] [x] parked on the owner's budget", "blocked", 500.0)]
    pr = rank_findings(parked_rows)[0]
    assert pr["parked"] is True and pr["overdue"] is False, pr
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
