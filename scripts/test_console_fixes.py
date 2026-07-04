#!/usr/bin/env python3
"""test_console_fixes.py — regression guard for QA-found console bugs (source-probe, no browser).

The agentic QA skeptic caught four subtle first-run bugs; each was fixed by a specific guard in console.py.
A future edit could silently drop any of these guards and reintroduce the bug (they're behavioural — the
browser crawler is heavy/skipped in the fast gate). This cheaply asserts each fix's guard is still present in
the source, so a regression fails the gate immediately.

    python test_console_fixes.py     # prints PASS / FAIL
Run with the agent-os venv python.
"""
import re
import sys
from pathlib import Path

SRC = (Path(__file__).resolve().parent / "console.py").read_text()
ok = True


def chk(cond, label):
    global ok
    print(("PASS" if cond else "FAIL") + f": {label}")
    ok = ok and bool(cond)


# 1) create-company: switchOrg must be async and reload ORGS (loadOrgs) before naming/navigating, else a
#    just-created company shows the 'company' placeholder and can be reset to home.
chk(re.search(r"async function switchOrg\(id\)\{[^}]*await loadOrgs\(\)", SRC),
    "switchOrg is async + awaits loadOrgs (create-company shows real name, not 'company' placeholder)")

# 2) #assistant route: 'assistant' must be in LABEL (so it deep-links/routes) and go() must alias it to
#    'controller' (so it actually renders the Assistant).
chk("assistant:'Assistant'" in SRC,
    "'assistant' is in LABEL (deep-linkable / hash-routable)")
chk(re.search(r"function go\(k\)\{if\(k==='assistant'\)k='controller'", SRC),
    "go() aliases 'assistant' -> 'controller' (Assistant route is not dead)")

# 3) cockpit async render race: cockpit does ~8 awaits; its terminal #view write must be guarded by CUR so a
#    stale late render can't clobber the view the user navigated to ('title Assistant / body Cockpit').
chk("if(CUR==='cockpit')$('#view').innerHTML=h" in SRC,
    "cockpit terminal render is CUR-guarded (late cockpit load can't clobber the next view)")

# 4) Activity empty-state must not contradict a just-started build (no 'No activity yet — Start a build').
chk("No activity yet" not in SRC and "appear as they spin up" in SRC,
    "Activity empty-state acknowledges an in-flight build (no cross-surface contradiction)")

print("PASS: all QA-found console fixes are still in place" if ok
      else "FAIL: a console fix guard regressed")
sys.exit(0 if ok else 1)
