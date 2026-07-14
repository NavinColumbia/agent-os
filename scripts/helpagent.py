#!/usr/bin/env python3
"""helpagent.py — the in-app HELP ASSISTANT: answers a newly-onboarded CEO's "how do I…?" questions
and points them at the right screen.

A non-technical CEO who just signed up needs to *navigate*: "how do I connect a provider?", "where do I
see my spend?", "how do I pause a build?". This assistant answers ONLY from a KNOWLEDGE MAP of the app's
areas (embedded below as a Python constant — one entry per console nav screen, each with a plain-language
"what it's for" + "how to"). It grounds an LLM reply in that map and, when the answer lives on a screen,
emits a GOTO so the UI can deep-link the CEO straight there. No DB writes, no web server.

    helpagent.py ask <tid> "<question>"   # one help question -> friendly answer (+ maybe a suggested area)
    helpagent.py topics                   # the area keys + one-liners (for a no-LLM "suggested topics" UI)
    helpagent.py selftest
Run with the agent-os venv python.
"""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit       # noqa: E402  (imported for convention parity with orchestrator.py)
import factory     # noqa: E402

from aoscfg import ENV, DB

# KNOWLEDGE MAP — one entry per area in the console nav. The help assistant answers ONLY from this.
# Each: short "what it's for" + "how to". Keys are the GOTO targets the UI can deep-link to.
KNOWLEDGE = {
    "direct": {
        "name": "Direct / Chat",
        "for": "Describe a product idea in plain language and have the agent company build it for you.",
        "how": "Open Direct, type your idea in the chat; the orchestrator asks a clarifying question or "
               "two, then proposes a build you confirm with one tap.",
    },
    "cockpit": {
        "name": "Cockpit",
        "for": "Your live command center for a running build — workers, their chatter, progress, and the "
               "budget forecast, with pause/resume controls.",
        "how": "Open Cockpit to watch workers and progress; use the Pause button to halt a build and Resume "
               "to continue it, and check the budget forecast to see projected spend.",
    },
    "new_build": {
        "name": "New build & Templates",
        "for": "Kick off a fresh build from scratch or from a ready-made template.",
        "how": "Open New build, either write a charter or pick a Template to start from, then launch it.",
    },
    "projects": {
        "name": "Projects",
        "for": "The catalog of everything you've built — list, detail, and download.",
        "how": "Open Projects to see all your products; click one for its detail page where you can download "
               "the finished code.",
    },
    "fleet": {
        "name": "Fleet",
        "for": "A live view of every worker agent currently running across your builds.",
        "how": "Open Fleet to see active workers in real time and what each is doing right now.",
    },
    "observability": {
        "name": "Observability",
        "for": "Inspect what happened under the hood — runs, errors, and step-by-step trace replay.",
        "how": "Open Observability to browse runs, drill into errors, and replay a build's trace to see each "
               "step the agents took.",
    },
    "approvals": {
        "name": "Approvals",
        "for": "Decisions that are waiting on you — anything the agents paused to get your sign-off.",
        "how": "Open Approvals to review items awaiting you and approve or reject each one.",
    },
    "integrations": {
        "name": "Integrations",
        "for": "Connect outside services (the tools your products plug into).",
        "how": "Open Integrations and click Connect on a service to link it to your account.",
    },
    "billing": {
        "name": "Billing",
        "for": "Your plan and usage — see your spend and what tier you're on.",
        "how": "Open Billing to view your current plan, usage, and spend, and to upgrade if you need more.",
    },
    "settings": {
        "name": "Settings",
        "for": "Account preferences — AI consent, your model provider keys, and notification preferences.",
        "how": "Open Settings to give or review AI consent, manage model provider keys, and choose how you "
               "want to be notified.",
    },
    "status": {
        "name": "Status",
        "for": "The health of the platform and your services at a glance.",
        "how": "Open Status to check whether everything is operating normally.",
    },
    "providers": {
        "name": "Providers",
        "for": "Connect the AI model providers the agents run on — Claude and/or Codex keys.",
        "how": "Open Providers and paste your Claude or Codex API key to connect it; you can connect one or "
               "both.",
    },
    "onboarding": {
        "name": "Onboarding",
        "for": "The guided first-run setup that gets a new CEO up and running.",
        "how": "Open Onboarding to walk through the initial setup steps any time you want to revisit them.",
    },
}


def _knowledge_text():
    """Render the KNOWLEDGE MAP as a compact block to ground the LLM in."""
    return "\n".join(
        f"- {k} ({v['name']}): {v['for']} How to: {v['how']}"
        for k, v in KNOWLEDGE.items()
    )


def topics():
    """The area keys + a one-line description each — so the UI can show suggested help topics with no LLM call."""
    return [{"key": k, "name": v["name"], "description": v["for"]} for k, v in KNOWLEDGE.items()]


def ask(tid, question, api_key=None):
    """Answer ONE help question, grounded ONLY in the KNOWLEDGE MAP, and (when it maps to a screen)
    return the suggested area to deep-link the CEO to."""
    prompt = (
        "You are the in-app help assistant for agent-os. Answer ONLY from the app knowledge below, in 2-4 "
        "friendly plain sentences for a non-technical user. If the answer maps to a screen, end with a line "
        "'GOTO: <area-key>' using exactly one of these keys: " + ", ".join(KNOWLEDGE) + ".\n\n"
        "APP KNOWLEDGE:\n" + _knowledge_text() + "\n\n"
        f"USER QUESTION: {question}\n\nAnswer now:"
    )
    factory._ctx.api_key = api_key
    factory._ctx.product = None
    factory._ctx.run = "help"
    factory._ctx.stage = "HELP"
    r = factory.agent("technical-writer", str(factory.PRODUCTS), prompt, tools=[])
    out = (r.get("out") or "").strip() or "Sorry, I couldn't find that. Try the Onboarding screen to get started."

    suggested = None
    lines = out.splitlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("GOTO:"):
            cand = stripped.split(":", 1)[1].strip().strip("<>").strip()
            if cand in KNOWLEDGE:
                suggested = cand
            continue  # strip GOTO line from the answer regardless
        kept.append(line)
    answer = "\n".join(kept).strip()
    return {"answer": answer, "suggested_area": suggested}


def _selftest():
    """Mock factory.agent (NO real spend) and prove parse + GOTO-validation + topics."""
    real = factory.agent

    def fake_agent(role, repo, task, **k):
        return {"rc": 0, "out": "To connect your model provider, open Providers and paste your Claude or "
                                "Codex key.\nGOTO: providers"}

    factory.agent = fake_agent
    try:
        res = ask("t-help-selftest", "how do I add my codex key?")
        area_ok = res["suggested_area"] == "providers"
        no_goto = "GOTO" not in res["answer"]
        answer_ok = bool(res["answer"]) and "Providers" in res["answer"]

        t = topics()
        topics_ok = len(t) >= 10 and all("key" in e and "description" in e for e in t)
        keys_ok = all(e["key"] in KNOWLEDGE for e in t)

        # an unknown/garbage GOTO must validate to null and still strip the line
        factory.agent = lambda role, repo, task, **k: {"rc": 0, "out": "Here you go.\nGOTO: nonsense"}
        res2 = ask("t-help-selftest", "anything")
        null_ok = res2["suggested_area"] is None and "GOTO" not in res2["answer"]

        ok = area_ok and no_goto and answer_ok and topics_ok and keys_ok and null_ok
        print(f"area={area_ok} no-goto={no_goto} answer={answer_ok} topics>=10={topics_ok} "
              f"keys={keys_ok} bad-goto-null={null_ok}")
        print("PASS: help assistant (grounded answer -> validated GOTO -> topics) ✅" if ok else "FAIL")
    finally:
        factory.agent = real
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "ask" and len(a) > 2:
        print(json.dumps(ask(a[1], a[2]), indent=2))
    elif a[0] == "topics":
        print(json.dumps(topics(), indent=2))
    else:
        sys.exit('usage: helpagent.py ask <tid> "<question>" | topics | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
