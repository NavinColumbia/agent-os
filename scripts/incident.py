#!/usr/bin/env python3
"""incident.py — the reasoning incident-commander. When the rules-based responder has no safe fix for
a NOVEL failure, this spawns a real agent to diagnose it: it reads the recent logs + audit trail,
hypothesises a root cause, recommends ONE safe next action, and writes a root-cause analysis (RCA).

It DIAGNOSES and RECOMMENDS — it does not take arbitrary actions. The recommended action is only
executed if it falls in the responder's safe allowlist; otherwise it escalates to you WITH the RCA, so
a human decides with a written analysis in hand (not just a raw alert).

    incident.py investigate '<issue-json>'    # produce an RCA for an incident
    incident.py selftest                       # offline (context-gathering, no model call)
Run with the agent-os venv python. Uses the governed factory agent path (Codex-first by default).
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402
from dbpool import connection  # noqa: E402

INCIDENTS = ROOT / "docs" / "incidents"
_SERVICE_LOGS = Path(__file__).resolve().parents[1] / "logs" / "services"
LOGS = [str(_SERVICE_LOGS / name) for name in ("dashboard.log", "scheduler.log", "watchdog.log", "api.log")]
# Compatibility evidence from processes launched before shared service logging was introduced.
LOGS.append("/tmp/factory-splitbill2.log")


def _context(db_recent=12):
    """Gather what a human SRE would look at first: recent log tails + recent audit decisions."""
    parts = []
    for lp in LOGS:
        p = Path(lp)
        if p.exists():
            tail = p.read_text(errors="ignore").splitlines()[-12:]
            if tail:
                parts.append(f"### {lp}\n" + "\n".join(tail))
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("""SELECT to_char(ts,'HH24:MI:SS'), actor, action, resource, decision
                           FROM audit_log ORDER BY id DESC LIMIT %s""", (db_recent,))
            rows = [" ".join(str(x) for x in r) for r in cur.fetchall()]
        parts.append("### recent audit\n" + "\n".join(rows))
    except Exception as e:
        parts.append(f"(audit unavailable: {e})")
    return "\n\n".join(parts)


def investigate(issue, timeout=240):
    INCIDENTS.mkdir(parents=True, exist_ok=True)
    ctx = _context()
    prompt = (
        "You are the Incident Commander in a governed agent OS. An automated watchdog detected an "
        "incident that the rules-based responder could not auto-fix. Diagnose it.\n\n"
        f"INCIDENT: {json.dumps(issue)}\n\nRECENT CONTEXT (logs + audit):\n{ctx}\n\n"
        "Write a concise RCA in markdown with exactly these sections:\n"
        "## Summary (one line)\n## Most likely root cause\n## Evidence\n"
        "## Recommended next action (ONE concrete, safe step)\n## Needs human approval? (yes/no + why)\n"
        "Be specific and brief. Do not invent facts not supported by the context."
    )
    try:
        import factory
        prev = (getattr(factory._ctx, "run", None), getattr(factory._ctx, "product", None),
                getattr(factory._ctx, "stage", None))
        factory._ctx.run, factory._ctx.product, factory._ctx.stage = (
            f"incident-{int(time.time())}", "agent-os", "INCIDENT")
        r = factory.agent("incident-commander", str(ROOT), prompt, timeout=timeout,
                          tools=[], light=True)
        rca = ((r or {}).get("out_full") or (r or {}).get("out") or "").strip()
        if not rca:
            rca = f"(no analysis produced; agent result: {json.dumps(r, default=str)[:500]})"
    except Exception as e:
        rca = f"(incident analysis failed before producing an RCA: {e})"
    finally:
        try:
            factory._ctx.run, factory._ctx.product, factory._ctx.stage = prev
        except Exception:
            pass
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_sig = issue.get("sig", "incident").replace(":", "_").replace("/", "_")
    path = INCIDENTS / f"{ts}-{safe_sig}.md"
    path.write_text(f"# Incident RCA — {issue.get('msg','')}\n\n_generated {ts} by incident-commander_\n\n{rca}\n")
    lines = rca.splitlines()
    summary = ""
    for idx, line in enumerate(lines):          # prefer the line under "## Summary"
        if line.lower().startswith("## summary"):
            for nxt in lines[idx + 1:]:
                if nxt.strip() and not nxt.startswith("#"):
                    summary = nxt.strip()[:200]
                    break
            break
    if not summary:
        for line in lines:
            if line.strip() and not line.startswith("#"):
                summary = line.strip()[:200]
                break
    audit.append(actor="incident-commander", action="RCA", resource=issue.get("sig", ""),
                 decision="diagnosed", payload={"rca": str(path.name)})
    return {"rca_path": str(path), "summary": summary or rca[:200]}


def _main(a):
    if not a:
        sys.exit("usage: incident.py investigate '<issue-json>' | selftest")
    if a[0] == "investigate":
        print(json.dumps(investigate(json.loads(a[1])), indent=2))
    elif a[0] == "selftest":
        ctx = _context()
        ok = "recent audit" in ctx and len(ctx) > 50
        print(f"context gathered: {len(ctx)} chars; has audit: {'recent audit' in ctx}")
        print("PASS: incident context-gathering ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
