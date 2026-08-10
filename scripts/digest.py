#!/usr/bin/env python3
"""digest.py — the founder's digest: one message, through one channel (ntfy → your phone), telling you
where the portfolio stands and the prioritized NEXT STEPS. Schedule it weekly so you get a standing
brief without asking. Recommendations are derived from the real portfolio state (not generic advice).

    digest.py show       # print the digest
    digest.py send       # push it to your phone (ntfy)
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import notify     # noqa: E402
import portfolio  # noqa: E402


def recommendations(s):
    t, recs = s["totals"], []
    try:
        import appguard
        paused = appguard.paused_apps()
    except Exception:
        paused = []
    if paused:
        recs.append(("Review auto-paused apps", f"{len(paused)} app(s) auto-paused for bleeding money "
                     f"({', '.join(p['app'] for p in paused[:4])}) — approve a budget to resume, or retire them"))
    no_kit = [r["product"] for r in s["products"] if r["shipped"] and not r["has_launch_kit"]]
    if no_kit:
        recs.append(("Generate launch kits", f"{len(no_kit)} shipped app(s) have no marketing kit "
                     f"({', '.join(no_kit[:4])}) — `launch_kit.py make <product>`"))
    building = [r["product"] for r in s["products"] if not r["shipped"]]
    if building:
        recs.append(("Finish in-flight", f"{len(building)} in progress: {', '.join(building[:4])}"))
    if t["product_revenue"] == 0 and t["with_launch_kit"] > 0:
        recs.append(("Pick ONE distribution channel", "apps are built + marketed but $0 product revenue — "
                     "choose a single channel (Product Hunt / an SEO page / app store) and ship it"))
    if t["product_revenue"] == 0:
        recs.append(("Stand up the self-serve front door", "no real paying users yet — the "
                     "signup→paste-key→build→pay loop is the unlock that needs no pitching from you"))
    recs.append(("Run market intel", "`intel.py analyze <top-app>` for competitor + feature-gap analysis"))
    return recs[:5]


def compose():
    s = portfolio.summary()
    t = s["totals"]
    try:
        import appguard
        npaused = len(appguard.paused_apps())
    except Exception:
        npaused = 0
    lines = [
        "agent-os digest",
        f"Products: {t['products']} built · {t['shipped']} shipped · {t['with_launch_kit']} with launch kit",
        f"Build cost: ${t['total_build_cost']} · Platform MRR: ${t['platform_mrr']} · Product revenue: ${t['product_revenue']}",
        f"Money guard: {npaused} app(s) auto-paused for losses" if npaused else "Money guard: all apps within budget",
        "",
    ]
    # Open findings, worst first. The board was write-only — dogfood/QA filed bugs nobody ever read back
    # out (27 open, none triaged, 3 critical). Surfacing it in the digest is what makes the loop close.
    try:
        import findings_sweep
        fs = findings_sweep.summary()
        if fs:
            lines += [fs, ""]
    except Exception:
        pass
    lines.append("Next steps (prioritized):")
    for i, (title, why) in enumerate(recommendations(s), 1):
        lines.append(f"{i}. {title} — {why}")
    return "\n".join(lines)


def daily():
    """A comprehensive daily founder brief: profitability, users, market, and what to build —
    grounded in real data, honest about what's not yet measurable."""
    import appguard
    import osq
    s = portfolio.summary()
    t = s["totals"]
    apps = osq.apps()
    spend = round(sum(osq.app(a)["build_cost_usd"] for a in apps), 2)
    paused = appguard.paused_apps()
    top = sorted(((a, osq.app(a)["build_cost_usd"]) for a in apps), key=lambda x: -x[1])
    top = [(a, c) for a, c in top if c > 0][:5]
    market = [a for a in apps if (portfolio.PRODUCTS / a / "intel" / "MARKET.md").exists()]
    L = ["=== agent-os daily digest ===", ""]
    L += ["PROFITABILITY",
          f"  apps: {t['products']} built · {t['shipped']} shipped · {t['with_launch_kit']} marketed",
          f"  build spend (real $): ${spend}   product revenue: ${t['product_revenue']}   net: ${round(t['product_revenue']-spend,2)}",
          f"  platform MRR: ${t['platform_mrr']} (demo tenants {s['tenants_by_plan']})   auto-paused apps: {len(paused)}",
          ("  top build cost: " + ", ".join(f"{a} ${c}" for a, c in top)) if top else "  (no real-cost builds recorded yet)", ""]
    L += ["USERS",
          "  real paying users: 0 — no product is deployed/sold yet (so churn/CAC/LTV are N/A)",
          "  usage telemetry: none until an app is live behind the self-serve front door",
          f"  billing tenants on record: {sum(s['tenants_by_plan'].values())} (demo/test data, not real customers)", ""]
    L += ["MARKET",
          (f"  competitor analysis done for: {', '.join(market)} (products/<app>/intel/MARKET.md)"
           if market else "  none yet — run intel.py analyze <app> on your top product"),
          "  read on pomodoro: a tier-a commodity (the feature is free everywhere); the only defensible",
          "  angles are trust, speed, privacy, offline — not feature breadth (per the intel report)", ""]
    # awaiting your decision
    try:
        import osq
        dq = osq.decisions()
    except Exception:
        dq = []
    L += ["AWAITING YOUR DECISION"]
    L += [f"  • {d['what']} ({d['why']})" for d in dq] if dq else ["  • nothing — you're clear"]
    L += [""]
    # YOUR ORG (LIVE): human-pattern status reporting — what the agents are doing right now, and anything
    # gone silent/stuck that needs the CEO. Briefed, not bothered: only the working summary + the flags.
    try:
        import pulse
        live = pulse.live()
        active = [w for w in live if w.get("status") == "active" and not w.get("stalled")]
        stuck = [w for w in live if w.get("stalled")]
        L += ["YOUR ORG (LIVE)"]
        L += ["  " + (f"{len(active)} agent(s) working: " + ", ".join(
                  f"{w['kind']}·{w.get('stage') or '?'}" for w in active[:6]) if active
              else "idle — no agentic work in flight right now")]
        if stuck:
            L += ["  ⚠ NEEDS YOU — silent/stuck: " + ", ".join(
                  f"{w['kind']} '{w.get('label') or w['work_id']}'" for w in stuck[:4])]
        L += [""]
    except Exception:
        pass
    # risks
    try:
        import risk
        rks = risk.risks()
    except Exception:
        rks = []
    L += ["RISKS (what could hurt the business)"]
    L += [f"  [{r['sev'].upper()}] {r['risk']} → {r['action']}" for r in rks[:5]]
    L += [""]
    L += ["WHAT TO BUILD / NEXT STEPS"]
    for i, (title, why) in enumerate(recommendations(s), 1):
        L.append(f"  {i}. {title} — {why}")
    return "\n".join(L)


def _main(a):
    if a and a[0] == "daily":
        text = daily()
        print(text)
        if "send" in a:
            notify.send(text[:1500], title="agent-os daily digest", tags="sunrise")
        return
    text = compose()
    if a and a[0] == "send":
        ok = notify.send(text, title="agent-os digest", tags="bar_chart")
        print("digest sent ✅" if ok else "send failed (check NTFY_TOPIC)")
    elif a and a[0] == "selftest":
        ok = "Next steps" in text and "Products:" in text
        print(text[:200])
        print("PASS: digest composes from portfolio + recommendations ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    else:
        print(text)


if __name__ == "__main__":
    _main(sys.argv[1:])
