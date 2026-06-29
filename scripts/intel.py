"""intel.py — market & competitor intelligence. A research agent studies a product's market: who the
competitors are, what features they have, where the gaps are, and what to build next. Advisory output
(it researches + recommends; it never ships or spends). Honest by instruction: it must not fabricate —
if it lacks live data it says so and uses known information.

    intel.py analyze <product>     # competitor + feature-gap analysis -> products/<p>/intel/MARKET.md
    intel.py selftest
Run with the agent-os venv python. Uses the headless `claude` CLI.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import factory  # noqa: E402


def analyze(product):
    repo = factory.PRODUCTS / product
    if not repo.exists():
        return {"error": f"no such product '{product}'"}
    (repo / "intel").mkdir(parents=True, exist_ok=True)
    factory._ctx.run = f"intel-{product}"
    factory._ctx.product = product
    factory._ctx.stage = "MARKET-INTEL"
    task = (
        f"Read docs/SPEC.md / README.md to understand product '{product}', then do MARKET INTELLIGENCE. "
        f"Write intel/MARKET.md with: (1) a one-paragraph market overview; (2) 3-6 real competitors with "
        f"their key features and pricing if known; (3) a FEATURE-GAP table — features competitors have "
        f"that this product does NOT, and genuine differentiators this product has; (4) 3 concrete, "
        f"prioritized product recommendations (what to build/improve next and why). "
        f"Use WebSearch/WebFetch to ground competitors, pricing, and market size in LIVE data, and cite "
        f"source URLs. Be honest: do NOT fabricate competitors, prices, or metrics — if a search yields "
        f"nothing, say so. Advisory only — do not ship, spend, or publish.")
    r = factory.agent("research-growth", str(repo), task, timeout=320)
    audit.append(actor="intel", action="MarketAnalysis", resource=product,
                 decision="generated", payload={"rc": r["rc"]})
    out = repo / "intel" / "MARKET.md"
    return {"product": product, "rc": r["rc"], "report": str(out) if out.exists() else None}


def _main(a):
    import json
    if not a:
        sys.exit("usage: intel.py analyze <product> | selftest")
    if a[0] == "analyze" and len(a) > 1:
        print(json.dumps(analyze(a[1]), indent=2))
    elif a[0] == "selftest":
        rolefile = Path.home() / "projects" / "control-plane" / "roles" / "research-growth.yaml"
        ok = rolefile.exists()
        print(f"research role present: {ok}")
        print("PASS: market-intel wiring (advisory, no fabrication) ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    else:
        sys.exit("usage: intel.py analyze <product> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
