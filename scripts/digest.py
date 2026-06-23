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
        "Next steps (prioritized):",
    ]
    for i, (title, why) in enumerate(recommendations(s), 1):
        lines.append(f"{i}. {title} — {why}")
    return "\n".join(lines)


def _main(a):
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
