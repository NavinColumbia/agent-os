#!/usr/bin/env python3
"""customagents.py — let a TENANT (their CEO) define their OWN standing/custom agents.

The factory builds PRODUCTS; this lets a customer stand up a recurring, role-specialized agent of
their own — e.g. a weekly "market-updates" agent — that runs through the SAME governed line and reports
back into THEIR notification feed. The tenant's free-text instructions are UNTRUSTED: they're scanned
for prompt-injection and wrapped in a data envelope (sanitize.py) before any agent sees them, the agent
is pinned to a small allowlist of GOVERNED roles (no arbitrary capability), and it runs on the tenant's
own provider key (tenantproviders.build_kwargs). Recurring agents are wired into scheduler.py by id, so
the scheduler's shell command is literally `customagents.py run <id>`.

Two TIERS share this surface, and the split is structural, not cosmetic:
  * SYSTEM agents (Tier-A) — the standing org-chart roles (Controller + the governed fleet). They are
    DEFAULT and UNEDITABLE: surfaced READ-ONLY via system_agents(); they are NOT rows in custom_agents,
    so no define/run_now/toggle/delete path can mutate one. The Controller is the sole spawner
    (control-plane/roles/controller.yaml can_spawn:true).
  * CUSTOM agents (Tier-B) — the tenant's OWN standing agents (rows in custom_agents): editable, pinned
    to ALLOWED_ROLES, ownership-checked on every mutation. list_agents() tags these editable.
tiers(tid) returns both in one read for the Agents view.

    customagents.py run <id>        # execute one agent (the scheduler calls this)
    customagents.py list <tid>      # a tenant's defined agents
    customagents.py tiers <tid>     # system (read-only) + custom (editable) tiers as one payload
    customagents.py selftest        # offline-ish check (no real LLM spend; factory.agent monkeypatched)
Run with the agent-os venv python.
"""
import json
import re
import sys
import time
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit             # noqa: E402
import factory           # noqa: E402
import notifications     # noqa: E402
import orgview           # noqa: E402  (canonical standing org-chart = the Tier-A system roles)
import sanitize          # noqa: E402
import scheduler         # noqa: E402
import tenantproviders   # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

# Only GOVERNED roles a tenant may stand up — each has a manifest (must_never / allowed_paths) that
# constrains the agent. A tenant can NOT invent a role or pick a privileged one (builder/tech-lead/etc.).
ALLOWED_ROLES = ("research-growth", "marketing-growth", "technical-writer", "data-analyst")

VENV_PY = str(Path.home() / "projects" / "agent-os" / ".venv" / "bin" / "python")


def _ensure():
    """Mirror 32-customagents.sql so the API works even before a migration is applied."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS custom_agents (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT, name TEXT, role TEXT DEFAULT 'research-growth',
            instructions TEXT, trigger TEXT DEFAULT 'manual', interval_s INT, output TEXT DEFAULT 'report',
            product TEXT, enabled BOOLEAN DEFAULT true, last_run TIMESTAMPTZ, last_status TEXT,
            created_at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS custom_agent_runs (
            id BIGSERIAL PRIMARY KEY, agent_id BIGINT, tenant_id TEXT, started_at TIMESTAMPTZ DEFAULT now(),
            rc INT, cost_usd NUMERIC DEFAULT 0, output_ref TEXT, summary TEXT)""")
        c.commit()


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "agent").lower()).strip("-") or "agent"


def _tid_short(tid):
    return (tid or "tenant").split("-")[-1][:8] or "tenant"


def _load(agent_id):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, tenant_id, name, role, instructions, trigger, interval_s, output,
                              product, enabled FROM custom_agents WHERE id=%s""", (agent_id,))
        r = cur.fetchone()
    if not r:
        return None
    keys = ("id", "tenant_id", "name", "role", "instructions", "trigger", "interval_s", "output",
            "product", "enabled")
    return dict(zip(keys, r))


def define(tid, name, instructions, role="research-growth", trigger="manual", interval_s=None,
           output="report", product=None):
    """A tenant defines a standing/custom agent. Validates the role against the governed allowlist,
    scans the (untrusted) instructions for injection, stores the definition, and — if recurring — wires
    it into the scheduler so it fires every interval_s via `customagents.py run <id>`."""
    if role not in ALLOWED_ROLES:
        return {"error": f"role '{role}' not allowed; choose one of {list(ALLOWED_ROLES)}"}
    _ensure()
    flags = sanitize.scan(instructions)          # untrusted tenant text — flag obvious injection attempts
    if flags:
        audit.append(actor="sanitize", action="InjectionDetected", resource=tid,
                     decision="flagged", payload={"agent": name, "patterns": flags[:3]})
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO custom_agents (tenant_id, name, role, instructions, trigger,
                          interval_s, output, product)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (tid, name, role, instructions, trigger, interval_s, output, product))
        agent_id = cur.fetchone()[0]
        c.commit()
    if trigger == "recurring" and interval_s:
        scheduler.register(f"ca-{agent_id}",
                           f"{VENV_PY} {SCRIPTS / 'customagents.py'} run {agent_id}", int(interval_s))
    audit.append(actor="customagents", action="AgentDefined", resource=tid, decision="created",
                 payload={"agent_id": agent_id, "name": name, "role": role, "trigger": trigger,
                          "injection_flags": len(flags)})
    return {"agent_id": agent_id}


def run(agent_id):
    """Execute one custom agent through the governed factory on the TENANT's provider key, write its
    findings into the tenant's product workspace, notify the tenant, and record the run."""
    a = _load(agent_id)
    if not a:
        return {"error": f"no such custom agent {agent_id}"}
    tid, name, role, output = a["tenant_id"], a["name"], a["role"], a["output"]
    if not a["enabled"]:
        return {"agent_id": agent_id, "skipped": "disabled"}

    # per-tenant workspace repo (their own products dir, or a shared "<tid>-agents" workspace)
    repo_name = a["product"] or f"{_tid_short(tid)}-agents"
    repo = factory.PRODUCTS / repo_name
    (repo / "agents").mkdir(parents=True, exist_ok=True)

    # route to the tenant's connected provider (Claude/Codex, BYO key or subscription)
    bk = tenantproviders.build_kwargs(tid)
    factory._ctx.api_key = bk.get("api_key")
    factory._ctx.engine = (bk.get("engine") or "claude").lower()
    factory._ctx.codex_key = bk.get("provider_key") if factory._ctx.engine == "codex" else None
    factory._ctx.run = f"customagent-{agent_id}"
    factory._ctx.product = repo_name
    factory._ctx.stage = "CUSTOM-AGENT"

    ts = time.strftime("%Y%m%d-%H%M%S")
    started = time.time()
    safe_instructions = sanitize.wrap_untrusted(a["instructions"], label="TENANT AGENT INSTRUCTIONS")

    if output == "build":
        # the tenant wants a built artifact, not a report — drive the full product line
        r = factory.build_product(repo_name, a["instructions"], "lib", **bk)
        rc = 0 if r.get("result") == "LAUNCHED" else 1
        report_ref = str(repo)
        summary = f"{name}: build {r.get('result', 'UNKNOWN')} for {repo_name}"
    else:
        out_file = repo / "agents" / f"{_slug(name)}-{ts}.md"
        task = (
            f"You are running as a tenant's standing '{name}' agent. Do the work described in the "
            f"instructions below and WRITE YOUR FINDINGS to the file {out_file} (create it). Begin the "
            f"file with a one-line '# {name}' heading and a short dated summary, then the full findings. "
            f"Ground claims in real data where you can; be honest about what you don't know — do NOT "
            f"fabricate. Advisory only: do not ship, spend, publish, or act outside this repo.\n\n"
            f"INSTRUCTIONS:\n{safe_instructions}")
        r = factory.agent(role, str(repo), task)
        rc = r.get("rc", 1)
        report_ref = str(out_file) if out_file.exists() else None
        summary = (r.get("out") or "")[:1500] or f"{name}: run complete (rc={rc})"

    cost = float(r.get("cost_usd") or 0)
    status = "ok" if rc == 0 else "failed"

    # report back to the TENANT (passive changelog item in their feed) + a download pointer
    notifications.send(tid, "changelog", f"{name}: new update", summary[:1500],
                       level="passive", url=f"/download/{repo_name}")

    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE custom_agents SET last_run=now(), last_status=%s WHERE id=%s",
                    (status, agent_id))
        cur.execute("""INSERT INTO custom_agent_runs (agent_id, tenant_id, rc, cost_usd, output_ref, summary)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (agent_id, tid, rc, cost, report_ref, summary[:4000]))
        c.commit()
    audit.append(actor="customagents", action="AgentRun", resource=tid, decision=status,
                 payload={"agent_id": agent_id, "name": name, "rc": rc, "cost_usd": cost,
                          "elapsed_s": round(time.time() - started, 1)})
    return {"agent_id": agent_id, "report": report_ref}


def run_now(tid, agent_id):
    """Tenant-triggered run, ownership-checked (an agent only runs for the tenant that owns it)."""
    a = _load(agent_id)
    if not a or a["tenant_id"] != tid:
        return {"error": "not found or not owned by this tenant"}
    return run(agent_id)


def system_agents():
    """Tier-A SYSTEM agents — the standing org-chart roles (Controller + governed fleet), surfaced
    READ-ONLY. These are DEFAULT and UNEDITABLE: they are NOT rows in custom_agents, so no tenant route
    can create/run/toggle/delete one (`editable: False`). Sourced from the canonical standing hierarchy
    (orgview.ORG); the Controller is the sole spawner (control-plane/roles/controller.yaml can_spawn:true)
    — every other node is `can_spawn: False`. Manifest summary/title overlaid when a role manifest exists.
    No tenant_id: identical for everyone, so this is a pure read with no DB hit and no per-tenant state."""
    try:
        import governance
        manifest = governance.load_manifest
    except Exception:
        manifest = lambda _role: {}        # noqa: E731  (best-effort: manifest detail is optional)
    out = []
    for role, title, reports_to in orgview.ORG:
        m = manifest(role) or {}
        out.append({
            "role": role,
            "title": m.get("display_name") or title,
            "reports_to": reports_to,
            "summary": (m.get("summary") or "").strip() or None,
            "can_spawn": role == "controller",   # sole-spawner invariant, surfaced read-only
            "tier": "system",
            "editable": False,
        })
    return out


def list_agents(tid):
    """Tier-B CUSTOM agents — the tenant's OWN standing agents (rows they own). Tagged `editable: True`
    to distinguish them from the read-only system tier in the same Agents view."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, name, role, trigger, interval_s, output, enabled, last_run, last_status
                       FROM custom_agents WHERE tenant_id=%s ORDER BY id""", (tid,))
        rows = cur.fetchall()
    return [{"id": r[0], "name": r[1], "role": r[2], "trigger": r[3], "interval_s": r[4],
             "output": r[5], "enabled": r[6], "last_run": str(r[7]) if r[7] else None,
             "last_status": r[8], "tier": "custom", "editable": True} for r in rows]


def tiers(tid):
    """One read for the Agents view: the read-only SYSTEM tier, the tenant's editable CUSTOM tier, and
    the governed role allowlist a tenant may pick from when defining a custom agent."""
    return {"system": system_agents(), "custom": list_agents(tid), "roles": list(ALLOWED_ROLES)}


def toggle(tid, agent_id, enabled):
    a = _load(agent_id)
    if not a or a["tenant_id"] != tid:
        return {"error": "not found or not owned by this tenant"}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE custom_agents SET enabled=%s WHERE id=%s AND tenant_id=%s",
                    (bool(enabled), agent_id, tid))
        c.commit()
    # #12: a disabled recurring agent must also stop firing in the scheduler (pause, don't delete the
    # schedule so re-enabling resumes it). Best-effort: harmless no-op for non-recurring agents.
    se = getattr(scheduler, "set_enabled", None)
    if callable(se):
        try:
            se(f"ca-{agent_id}", bool(enabled))
        except Exception:
            pass
    audit.append(actor="customagents", action="AgentToggled", resource=tid,
                 decision="enabled" if enabled else "disabled", payload={"agent_id": agent_id})
    return {"agent_id": agent_id, "enabled": bool(enabled)}


def delete(tid, agent_id):
    a = _load(agent_id)
    if not a or a["tenant_id"] != tid:
        return {"error": "not found or not owned by this tenant"}
    # best-effort scheduler de-register if the scheduler exposes one; else just drop the row
    for fn in ("deregister", "unregister", "remove"):
        f = getattr(scheduler, fn, None)
        if callable(f):
            try:
                f(f"ca-{agent_id}")
            except Exception:
                pass
            break
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM custom_agents WHERE id=%s AND tenant_id=%s", (agent_id, tid))
        c.commit()
    audit.append(actor="customagents", action="AgentDeleted", resource=tid, decision="deleted",
                 payload={"agent_id": agent_id})
    return {"agent_id": agent_id, "deleted": True}


def _selftest():
    """No real LLM spend: monkeypatch factory.agent to a fake that ALSO writes the expected report file
    (parsed out of the task) so run() exercises the full path — DB rows, notification, ownership guard."""
    import billing
    real_agent = factory.agent

    def fake_agent(role, repo, task, *args, **kw):
        m = re.search(r"WRITE YOUR FINDINGS to the file (\S+)", task)
        if m:
            p = Path(m.group(1))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# market-updates\nmarket summary: prices steady this week.\n")
        return {"rc": 0, "out": "market summary: prices steady this week.", "cost_usd": 0.0}

    tid = billing.signup("ca-selftest", "free")["tenant_id"]
    agent_id = None
    try:
        factory.agent = fake_agent
        # bad role rejected
        bad = define(tid, "nope", "do x", role="builder")
        bad_role_ok = "error" in bad

        d = define(tid, "market-updates", "Summarize this week's market for our product.",
                   role="research-growth", trigger="manual", output="report")
        agent_id = d["agent_id"]

        # ownership guard: a FOREIGN tenant cannot run it
        guarded = run_now("some-other-tenant", agent_id)
        guard_ok = "error" in guarded

        res = run_now(tid, agent_id)
        report_written = bool(res.get("report")) and Path(res["report"]).exists()

        # Tier boundary: SYSTEM agents are default + UNEDITABLE; exactly one spawner (Controller);
        # the privileged spawner roles are NOT in the tenant's custom allowlist (can't be stood up).
        sys_agents = system_agents()
        sys_readonly = bool(sys_agents) and all(not s["editable"] and s["tier"] == "system"
                                                for s in sys_agents)
        sole_spawner = sum(1 for s in sys_agents if s["can_spawn"]) == 1
        no_privileged_custom = ("controller" not in ALLOWED_ROLES
                                and "builder" not in ALLOWED_ROLES)
        # The tenant's defined agent shows up in the CUSTOM tier, tagged editable.
        t = tiers(tid)
        custom_editable = (any(c["id"] == agent_id and c["editable"] and c["tier"] == "custom"
                               for c in t["custom"])
                           and t["system"] == sys_agents)
        tier_ok = sys_readonly and sole_spawner and no_privileged_custom and custom_editable

        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM custom_agent_runs WHERE agent_id=%s", (agent_id,))
            run_rows = cur.fetchone()[0]
            cur.execute("SELECT last_status FROM custom_agents WHERE id=%s", (agent_id,))
            last_status = cur.fetchone()[0]
            cur.execute("""SELECT count(*) FROM notifications WHERE tenant_id=%s AND category='changelog'""",
                        (tid,))
            notif_rows = cur.fetchone()[0]

        ok = (bad_role_ok and guard_ok and run_rows == 1 and last_status == "ok"
              and notif_rows >= 1 and report_written and tier_ok)
        print(f"bad-role-rejected={bad_role_ok} ownership-guard={guard_ok} runs={run_rows} "
              f"last_status={last_status} notifications={notif_rows} report={report_written}")
        print(f"tiers: system-readonly={sys_readonly} sole-spawner={sole_spawner} "
              f"no-privileged-custom={no_privileged_custom} custom-editable={custom_editable}")
        print("PASS: tenant custom-agent define/run via governed factory, reports back, audited ✅"
              if ok else "FAIL")
    finally:
        factory.agent = real_agent
        with psycopg.connect(DB) as c, c.cursor() as cur:
            if agent_id is not None:
                cur.execute("DELETE FROM custom_agent_runs WHERE agent_id=%s", (agent_id,))
            cur.execute("DELETE FROM custom_agents WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a:
        sys.exit("usage: customagents.py run <id> | list <tid> | tiers <tid> | selftest")
    if a[0] == "run" and len(a) > 1:
        print(json.dumps(run(int(a[1]))))
    elif a[0] == "list" and len(a) > 1:
        print(json.dumps(list_agents(a[1]), indent=2))
    elif a[0] == "tiers" and len(a) > 1:
        print(json.dumps(tiers(a[1]), indent=2))
    elif a[0] == "selftest":
        _selftest()
    else:
        sys.exit("usage: customagents.py run <id> | list <tid> | tiers <tid> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
