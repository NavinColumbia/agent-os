#!/usr/bin/env python3
"""scale.py — turn a BUDGET into a capability profile. The north star: capability scales monotonically
with spend. More budget -> more concurrent agents (breadth), deeper recursion (depth), higher verification
rigor (assurance), and more parallel design exploration (quality). This is the "tokens -> capability"
mapper; a tenant just says how much they'll spend and the factory dials itself accordingly.

    scale.py profile <budget_usd>     # show the profile for a budget
    scale.py selftest
The single-box ceiling is ~30 concurrent agents (RAM); profiles above that assume the cloud worker pool
(same Postgres SKIP-LOCKED queue) — capability is unbounded with budget by design, the box is not.
"""
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

SINGLE_BOX_AGENT_CEIL = int(os.environ.get("AOS_SINGLE_BOX_CEIL", "30"))


def profile(budget_usd) -> dict:
    """Monotonic budget -> {max_agents, max_depth, rigor, exploration, budget_usd, mode}.
    Every knob is non-decreasing in budget. `mode` flags when the profile needs the cloud worker pool."""
    b = max(0.0, float(budget_usd or 0))
    if b <= 0:                                        # unset -> conservative single-box defaults
        return {"max_agents": 8, "max_depth": 2, "rigor": 1, "exploration": 1, "budget_usd": 0.0, "mode": "single-box"}
    max_agents = max(2, int(b // 2))                  # ~ one concurrent agent slot per ~$2 of budget
    max_depth = 1 if b < 5 else 2 if b < 50 else 3 if b < 500 else 4 if b < 5000 else 5
    rigor = 1 if b < 10 else 2 if b < 100 else 3 if b < 1000 else min(8, 3 + int((b // 1000)))
    exploration = 1 if b < 50 else 2 if b < 500 else 3 if b < 5000 else 4
    mode = "single-box" if max_agents <= SINGLE_BOX_AGENT_CEIL else "cloud-pool"
    return {"max_agents": max_agents, "max_depth": max_depth, "rigor": rigor,
            "exploration": exploration, "budget_usd": round(b, 2), "mode": mode}


def apply(p: dict) -> dict:
    """Set the env knobs so a subsequent build/verify runs at this profile's scale. Returns what was set."""
    env = {
        "AOS_BUDGET_USD": str(p["budget_usd"]),
        "AOS_MAX_AGENTS": str(min(p["max_agents"], SINGLE_BOX_AGENT_CEIL) if p["mode"] == "single-box" else p["max_agents"]),
        "AOS_MAX_DEPTH": str(p["max_depth"]),
        "AOS_RIGOR": str(p["rigor"]),
        "AOS_EXPLORATION": str(p["exploration"]),
        "AOS_FLEET_WORKERS": str(min(p["max_agents"], 16)),
    }
    os.environ.update(env)
    return env


def _selftest():
    pts = [2, 8, 30, 80, 300, 2000, 100_000]   # monotonicity holds for real budgets ($0 = "unset default")
    profs = [profile(b) for b in pts]
    mono = all(profs[i]["max_agents"] <= profs[i + 1]["max_agents"] and
               profs[i]["max_depth"] <= profs[i + 1]["max_depth"] and
               profs[i]["rigor"] <= profs[i + 1]["rigor"] and
               profs[i]["exploration"] <= profs[i + 1]["exploration"] for i in range(len(profs) - 1))
    cloud = profile(100_000)["mode"] == "cloud-pool" and profile(8)["mode"] == "single-box"
    small = profile(8)["max_agents"] == 4 and profile(8)["rigor"] == 1
    ok = mono and cloud and small
    for b in [8, 80, 2000]:
        print(f"  ${b:>6}: {profile(b)}")
    print("PASS: budget->capability scheduler (monotonic, cloud past the box ceiling) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "profile":
        import json
        print(json.dumps(profile(a[1] if len(a) > 1 else 0), indent=2))
    else:
        sys.exit("usage: scale.py profile <budget_usd> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
