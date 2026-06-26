#!/usr/bin/env python3
"""agentfeatures.py — AGENTIC FEATURES embedded INTO the user's product (not hosted by us).

Key distinction: we don't run dashboards/monitoring FOR a tenant's product — while building their product we
BUILD those surfaces (and agentic capabilities) INTO it, hosted on their side. This is the catalog of
ready-made agentic features a CEO can have wired into their app — a button/endpoint/widget that, when their
own user or staff triggers it, runs an agent fleet (on the CEO's keys) to do real work. Example: a
"Migrate AWS→GCP" button that spins up a migration agent. Picked features fold their charter fragment into
the product's build charter, so the shipped product includes the feature + a callback hook to invoke agents.

    agentfeatures.py catalog
    agentfeatures.py charter <slug1,slug2,...>     # the spec fragment appended to a build
    agentfeatures.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

# audience: who triggers it in THEIR app. surface: how it appears. charter_fragment: appended to the build
# spec so the factory builds the feature INTO the product, incl. an /agent-run callback that invokes an
# agent (their keys) — the product hosts the UI; the agent work runs through the governed runtime.
CATALOG = [
    {"slug": "cloud-migrate", "name": "Cloud migration button", "audience": "staff", "surface": "button",
     "category": "infra",
     "blurb": "A “Migrate” button (AWS↔GCP↔Azure) that runs an agent to translate IaC and plan the move.",
     "charter_fragment": (
        "AGENTIC FEATURE — Cloud migration: add a staff-facing 'Migrate' control. When clicked with a source "
        "and target cloud (e.g. AWS→GCP), POST to an endpoint /agent/migrate that invokes a migration agent "
        "which reads the current infra (Terraform/IaC), produces an equivalent target-cloud IaC plan, and "
        "returns a diff + steps for human approval (do NOT auto-apply). Include a results panel showing the "
        "plan and a 'request approval' action. The agent runs via the embedded agent-run hook on the tenant's keys.")},
    {"slug": "support-triage", "name": "Support-ticket triage", "audience": "staff", "surface": "endpoint",
     "category": "ops",
     "blurb": "Incoming tickets auto-triaged by an agent (category, priority, suggested reply).",
     "charter_fragment": (
        "AGENTIC FEATURE — Support triage: add an endpoint /agent/triage that takes a support message and runs "
        "an agent to classify category + priority and draft a suggested reply, returned for a human to send. "
        "Surface a staff inbox view that shows the agent's triage per ticket.")},
    {"slug": "smart-search", "name": "Agentic search/assistant", "audience": "external", "surface": "widget",
     "category": "ux",
     "blurb": "An in-app assistant your users chat with; an agent answers grounded in your app's data.",
     "charter_fragment": (
        "AGENTIC FEATURE — In-app assistant: add a chat widget for end users and an endpoint /agent/ask that "
        "runs an agent answering ONLY from the app's own data (no fabrication), citing what it used. Include a "
        "rate limit and a clear 'AI-generated' label.")},
    {"slug": "content-moderation", "name": "Content moderation", "audience": "external", "surface": "endpoint",
     "category": "trust",
     "blurb": "User-generated content screened by an agent before it goes live.",
     "charter_fragment": (
        "AGENTIC FEATURE — Moderation: add an endpoint /agent/moderate that an agent uses to screen "
        "user-submitted content for policy violations, returning allow/flag/block + a reason; flagged items go "
        "to a staff review queue (never silently dropped).")},
    {"slug": "data-cleanup", "name": "Data-cleanup job", "audience": "staff", "surface": "button",
     "category": "ops",
     "blurb": "A button that runs an agent to dedupe/normalise/repair a chosen dataset.",
     "charter_fragment": (
        "AGENTIC FEATURE — Data cleanup: add a staff 'Clean dataset' button that POSTs to /agent/cleanup; an "
        "agent proposes dedupe/normalise/repair changes as a preview diff for human approval before applying.")},
    {"slug": "report-generator", "name": "AI report generator", "audience": "staff", "surface": "button",
     "category": "ops",
     "blurb": "Generate a narrative report from your data on demand via an agent.",
     "charter_fragment": (
        "AGENTIC FEATURE — Report generator: add a 'Generate report' button → /agent/report; an agent composes "
        "a narrative report from the app's data with the key numbers, downloadable as markdown/PDF.")},
    {"slug": "workflow-automation", "name": "Custom workflow agent", "audience": "staff", "surface": "endpoint",
     "category": "ops",
     "blurb": "A configurable agent that runs a multi-step back-office workflow on a trigger.",
     "charter_fragment": (
        "AGENTIC FEATURE — Workflow automation: add a configurable /agent/workflow endpoint that runs a "
        "multi-step agent workflow (the steps are defined by the operator) on a trigger, with an audit log of "
        "each run and a human-approval gate for any consequential action.")},
]
_BY = {f["slug"]: f for f in CATALOG}

# the embedded hook spec — appended once when ANY agentic feature is chosen, so the product can call agents.
_HOOK = (
    "\n\nEMBEDDED AGENT-RUN HOOK (shared by the agentic features above): the product must include a small "
    "server-side `agent_run(role, task)` helper that invokes an agent through the platform's governed runtime "
    "using the TENANT'S OWN provider key (BYO), is rate-limited and audited, and NEVER performs an "
    "irreversible action without a human-approval step. The product hosts the UI; the agent work runs through "
    "this hook — so the tenant embeds their AI fleet into their own app.")


def catalog():
    return CATALOG


def categories():
    return sorted({f["category"] for f in CATALOG})


def get(slug):
    return _BY.get(slug, {"error": "unknown agentic feature"})


def charter_for(slugs):
    """Combined spec fragment to APPEND to a build charter so the product ships with these agentic features."""
    if isinstance(slugs, str):
        slugs = [s.strip() for s in slugs.split(",") if s.strip()]
    frags = [_BY[s]["charter_fragment"] for s in slugs if s in _BY]
    if not frags:
        return ""
    return "\n\n".join(frags) + _HOOK


def _selftest():
    cat = catalog()
    has_migrate = any(f["slug"] == "cloud-migrate" for f in cat)
    frag = charter_for(["cloud-migrate", "support-triage"])
    wired = ("Migrate" in frag and "agent" in frag.lower() and "agent_run" in frag and "BYO" in frag
             and len(cat) >= 6 and len(categories()) >= 3)
    unknown = get("nope").get("error") and charter_for(["nope"]) == ""
    ok = has_migrate and wired and unknown
    print(f"catalog={len(cat)} categories={len(categories())} migrate={has_migrate} fragment_wires_hook={('agent_run' in frag)}")
    print("PASS: agentic-feature catalog + build-charter fragments (embedded in the product) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "catalog":
        print(json.dumps(catalog(), indent=2))
    elif a[0] == "charter" and len(a) > 1:
        print(charter_for(a[1]))
    else:
        sys.exit("usage: agentfeatures.py catalog | charter <slug,slug> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
