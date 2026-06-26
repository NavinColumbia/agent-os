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
     "category": "ops", "trigger": "endpoint",
     "blurb": "A configurable agent that runs a multi-step back-office workflow on a trigger.",
     "charter_fragment": (
        "AGENTIC FEATURE — Workflow automation: add a configurable /agent/workflow endpoint that runs a "
        "multi-step agent workflow (the steps are defined by the operator) on a trigger, with an audit log of "
        "each run and a human-approval gate for any consequential action.")},
    {"slug": "event-agent-pipeline", "name": "Event → agent pipeline (async)", "audience": "staff",
     "surface": "event", "category": "ops", "trigger": "event",
     "blurb": "When something happens in your app (form submitted, email received, webhook), an agent picks "
              "it up off a queue and handles it async — like Kafka, but the workers are your AI agents.",
     "charter_fragment": (
        "AGENTIC FEATURE — Event→agent pipeline (the core async pattern): wire EVENT SOURCES in the product "
        "(a form-submit / inbound email / webhook / scheduled tick) to ENQUEUE an agent job on a durable queue "
        "(an 'agent_jobs' table with status pending|running|done|failed). A background WORKER drains the queue "
        "and runs an agent per job via the embedded agent-run hook (async, at-least-once, idempotent), then "
        "writes the result and fires a callback/notification. Include a staff view of the queue (depth, "
        "in-flight, failed/dead-letter, retry) and per-job audit. This is 'when X happens, an agent — acting "
        "as our async staff — does the work', the serverless/Kafka pattern with AI-agent workers.")},
]
_BY = {f["slug"]: f for f in CATALOG}

# the embedded hook spec — appended once when ANY agentic feature is chosen, so the product can call agents.
_HOOK = (
    "\n\nEMBEDDED AGENT-RUN HOOK (shared by the agentic features above): the product includes a small "
    "server-side `agent_run(role, task)` helper that invokes an agent through the platform's governed runtime "
    "using the TENANT'S OWN provider key (BYO), rate-limited and audited, and NEVER performs an irreversible "
    "action without a human-approval step. It supports BOTH sync (request/response) and ASYNC execution: for "
    "event/queue triggers it enqueues a durable job and a background worker drains it (at-least-once, "
    "idempotent by an idempotency key, with retry + dead-letter), then fires a callback/notification on "
    "completion. The product hosts the UI + event sources; the agent work runs through this hook — so the "
    "tenant embeds their AI fleet as the async 'staff' of their own app (the serverless/Kafka pattern, agent "
    "workers).")


def _norm(f):
    """Default a trigger type from the surface so every feature exposes how it's invoked."""
    t = f.get("trigger") or {"button": "manual", "widget": "manual", "endpoint": "endpoint",
                             "event": "event"}.get(f.get("surface"), "manual")
    return {**f, "trigger": t}


def catalog():
    return [_norm(f) for f in CATALOG]


def categories():
    return sorted({f["category"] for f in CATALOG})


def triggers():
    """The ways an agentic feature can be invoked — incl. the async event/queue (Kafka-with-agents) path."""
    return sorted({_norm(f)["trigger"] for f in CATALOG})


def get(slug):
    return _BY.get(slug, {"error": "unknown agentic feature"})


def charter_for(items):
    """Spec to APPEND to a build charter so the product ships with the agentic feature(s). Accepts catalog
    SLUGS *or* a FREE-TEXT description the CEO/controller wrote (custom feature + how it's invoked) — so we
    build exactly what they asked for, using the catalog only as a starting recommendation. Returns '' for
    'none'. Always appends the embedded agent-run hook when there's anything agentic."""
    if isinstance(items, str):
        text = items.strip()
        if not text or text.lower() in ("none", "-", "no", "n/a"):
            return ""
        frags = [_BY[s]["charter_fragment"] for s in _BY if s in text]      # any known features it names
        custom = "" if frags else f"AGENTIC FEATURE (as the CEO described it): {text}"
        body = "\n\n".join(frags) if frags else custom
        return body + _HOOK
    frags = [_BY[s]["charter_fragment"] for s in items if s in _BY]
    custom = [s for s in items if s not in _BY and str(s).strip()]
    parts = frags + ([f"AGENTIC FEATURE (as the CEO described it): {'; '.join(custom)}"] if custom else [])
    return ("\n\n".join(parts) + _HOOK) if parts else ""


def recommend(need):
    """Given a described need ('process uploads when a user submits', 'migrate clouds'), recommend an
    invocation ARCHITECTURE + the closest catalog feature(s) — the controller uses this to advise the CEO."""
    n = (need or "").lower()
    if any(w in n for w in ("when ", "on submit", "submitt", "email", "webhook", "incoming", "event",
                            "queue", "async", "background", "each time")):
        pattern = "event"
        rationale = ("This is event-driven — I'd recommend an ASYNC event→agent pipeline: the event enqueues a "
                     "durable job and an agent worker handles it in the background (at-least-once, retry, "
                     "dead-letter), then notifies. (Like Kafka, but the workers are your agents.)")
    elif any(w in n for w in ("every ", "daily", "weekly", "schedule", "cron", "periodic")):
        pattern = "schedule"
        rationale = "This is recurring — I'd run it on a SCHEDULE: a timer triggers an agent and reports back."
    elif any(w in n for w in ("button", "click", "on demand", "let me", "when i ", "when they click")):
        pattern = "manual"
        rationale = "This is on-demand — a BUTTON that triggers an agent and shows the result (sync) fits best."
    else:
        pattern = "endpoint"
        rationale = "I'd expose it as an ENDPOINT an agent backs; we can make it sync or async — your call."
    feats = [f["slug"] for f in catalog()
             if any(w in (f["name"] + " " + f["blurb"]).lower() for w in n.split() if len(w) > 4)][:3]
    return {"pattern": pattern, "rationale": rationale, "suggested_features": feats}


def _selftest():
    cat = catalog()
    has_migrate = any(f["slug"] == "cloud-migrate" for f in cat)
    frag = charter_for(["cloud-migrate", "support-triage"])
    wired = ("Migrate" in frag and "agent" in frag.lower() and "agent_run" in frag and "BYO" in frag
             and len(cat) >= 6 and len(categories()) >= 3)
    # the async event/queue (Kafka-with-agents) pattern is explicit
    evt = charter_for(["event-agent-pipeline"])
    async_ok = ("event" in triggers() and "queue" in evt.lower() and "async" in evt.lower()
                and "dead-letter" in evt.lower() and all("trigger" in f for f in cat))
    # FREE-TEXT (custom feature the CEO described) is built in verbatim + the hook
    free = charter_for("when a user uploads a file, an agent validates it async")
    freetext_ok = ("as the CEO described" in free and "agent_run" in free)
    # RECOMMENDATIONS: event-need -> async pipeline; button-need -> manual
    r1 = recommend("when a user submits the form, process it"); r2 = recommend("a button to clean my data")
    rec_ok = r1["pattern"] == "event" and r2["pattern"] == "manual" and r1["rationale"]
    unknown = get("nope").get("error") and charter_for("none") == ""   # 'none' -> nothing; custom text -> built
    ok = has_migrate and wired and async_ok and freetext_ok and rec_ok and unknown
    print(f"catalog={len(cat)} triggers={triggers()} migrate={has_migrate} async={async_ok} "
          f"freetext={freetext_ok} recommend(event->{r1['pattern']},button->{r2['pattern']})")
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
