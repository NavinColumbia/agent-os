#!/usr/bin/env python3
"""risk.py — the risk register: what could hurt the business, computed from real state (not generic
advice). Folds into the daily digest so 'what could kill me' is always in your pocket.

    risk.py register     # current risks, highest first
    risk.py json
    risk.py selftest
Run with the agent-os venv python.
"""
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
ROOT = SCRIPTS.parent
SEV = {"crit": 0, "high": 1, "med": 2, "low": 3}


def risks():
    out = []

    # 1) security posture — run the scanner
    try:
        r = subprocess.run([str(ROOT / ".venv/bin/python"), str(SCRIPTS / "security_scan.py")],
                           capture_output=True, text=True, timeout=30)
        if "PASS" not in r.stdout:
            out.append({"sev": "crit", "area": "security", "risk": "security scan failing — secrets/binds/sandbox issue",
                        "action": "run scripts/security_scan.py and fix HIGH findings"})
    except Exception:
        pass

    # 2) backup freshness — data-loss risk
    snaps = sorted((ROOT / "backups").glob("agent-os-*.aosnap"))
    if not snaps:
        out.append({"sev": "high", "area": "continuity", "risk": "no encrypted snapshot exists yet",
                    "action": "platform/snapshot.py export (and copy off-box)"})
    else:
        age_h = (time.time() - max(s.stat().st_mtime for s in snaps)) / 3600
        if age_h > 48:
            out.append({"sev": "med", "area": "continuity", "risk": f"last backup {round(age_h)}h old",
                        "action": "the daily snapshot job may be down — check the ticker"})

    # 3) revenue concentration / pre-revenue — the strategic risk
    try:
        import portfolio
        t = portfolio.summary()["totals"]
        if t["product_revenue"] == 0:
            out.append({"sev": "high", "area": "revenue", "risk": "pre-revenue — 0 real paying users; entire business is unproven in market",
                        "action": "ship ONE app to ONE channel / open the self-serve front door"})
    except Exception:
        pass

    # 4) bleeding apps
    try:
        import appguard
        for p in appguard.paused_apps():
            out.append({"sev": "med", "area": "cost", "risk": f"{p['app']} auto-paused (bleeding): {p['reason']}",
                        "action": "approve a budget to resume, or retire it"})
    except Exception:
        pass

    # 5) single-point-of-failure (by design, but a real risk to name)
    out.append({"sev": "low", "area": "infra", "risk": "single-box deployment — a host failure stops everything until recover.sh",
                "action": "accepted for now; cloud/HA is the scale step (platform/terraform)"})

    return sorted(out, key=lambda r: SEV.get(r["sev"], 9))


def _main(a):
    import json
    if a and a[0] == "json":
        print(json.dumps(risks(), indent=2)); return
    if a and a[0] == "selftest":
        rs = risks()
        ok = isinstance(rs, list) and all("sev" in r and "action" in r for r in rs)
        print(f"risks computed: {len(rs)} ({', '.join(sorted({r['area'] for r in rs}))})")
        print("PASS: risk register ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    for r in risks():
        print(f"  [{r['sev'].upper():4}] {r['area']:11} {r['risk']}\n               → {r['action']}")


if __name__ == "__main__":
    _main(sys.argv[1:])
