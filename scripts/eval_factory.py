#!/usr/bin/env python3
"""eval_factory.py — measure how well the autonomous factory actually works.

"Autonomous software factory" is only a claim until you have a number. This runs the factory across a
fixed benchmark of diverse specs and reports the metrics an acquirer's technical due-diligence asks for:
pass@1 (shipped with zero fix loops), pass@fix (shipped after the bounded re-flow), blocked rate, and
wall-clock per build. Results are written to docs/eval/ for the scorecard.

    eval_factory.py run [workers]      # build the benchmark, record metrics
    eval_factory.py report             # print the latest scorecard
Run with the agent-os venv python. Uses the headless `claude` CLI.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path.home() / "projects" / "agent-os"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import factory  # noqa: E402

EVAL_DIR = ROOT / "docs" / "eval"

# A diverse benchmark — libraries and web apps, varied difficulty.
BENCH = [
    {"product": "ev-tempconv", "kind": "lib",
     "charter": "Python lib 'tempconv': convert(value:float, frm:str, to:str)->float across 'C','F','K' "
                "(case-insensitive); raise ValueError on unknown unit or below absolute zero. Round-trip safe. Stdlib only."},
    {"product": "ev-csvstats", "kind": "lib",
     "charter": "Python lib 'csvstats': stats(csv_text:str)->dict mapping each numeric column name to "
                "{'mean','min','max','count'} using the stdlib csv module; ignore non-numeric columns; "
                "raise ValueError on empty input. Deterministic, stdlib only."},
    {"product": "ev-colorkit", "kind": "web",
     "charter": "Static web app 'colorkit': a color picker that shows the chosen color's HEX, RGB and HSL "
                "values and a copy-to-clipboard button for each. Offline, vanilla JS, dark theme, no console errors."},
    {"product": "ev-notes", "kind": "web",
     "charter": "Static web app 'notes': add, list and delete short notes persisted in localStorage, with a "
                "count and an empty-state. Offline, vanilla JS, accessible, dark theme, no console errors."},
]


def _one(spec):
    t0 = time.time()
    try:
        log = factory.build_product(spec["product"], spec["charter"], spec.get("kind", "lib"))
        qa = next((s["QA"] for s in log["stages"] if "QA" in s), {})
        return {"product": spec["product"], "kind": spec.get("kind", "lib"),
                "result": log.get("result"), "fix_attempts": qa.get("fix_attempts", None),
                "elapsed_s": round(time.time() - t0, 1)}
    except Exception as e:
        return {"product": spec["product"], "kind": spec.get("kind", "lib"),
                "result": f"ERROR: {e}", "fix_attempts": None, "elapsed_s": round(time.time() - t0, 1)}


def run(workers=2):
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    for s in BENCH:
        import shutil
        shutil.rmtree(factory.PRODUCTS / s["product"], ignore_errors=True)
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, s) for s in BENCH]
        for f in as_completed(futs):
            rows.append(f.result())
            print(f"[eval] {rows[-1]}", flush=True)
    n = len(rows)
    launched = [r for r in rows if r["result"] == "LAUNCHED"]
    pass1 = [r for r in launched if r["fix_attempts"] == 0]
    score = {
        "n": n,
        "pass_at_1": f"{len(pass1)}/{n}",
        "pass_with_fixloop": f"{len(launched)}/{n}",
        "blocked_or_error": f"{n - len(launched)}/{n}",
        "avg_elapsed_s": round(sum(r["elapsed_s"] for r in rows) / n, 1) if n else 0,
        "rows": rows,
    }
    (EVAL_DIR / "latest.json").write_text(json.dumps(score, indent=2))
    print("\n[eval] SCORECARD:", json.dumps({k: v for k, v in score.items() if k != "rows"}, indent=2), flush=True)
    return score


def report():
    p = EVAL_DIR / "latest.json"
    if not p.exists():
        print("no eval run yet — run: eval_factory.py run")
        return
    s = json.loads(p.read_text())
    print(f"Factory benchmark — {s['n']} specs")
    print(f"  pass@1 (zero fix loops):   {s['pass_at_1']}")
    print(f"  pass (after fix loop):     {s['pass_with_fixloop']}")
    print(f"  blocked/error:             {s['blocked_or_error']}")
    print(f"  avg wall-clock:            {s['avg_elapsed_s']}s")
    for r in s["rows"]:
        print(f"   - {r['product']:14} [{r['kind']}] {r['result']:12} fixes={r['fix_attempts']} {r['elapsed_s']}s")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "report":
        report()
    else:
        run(int(a[1]) if len(a) > 1 and a[0] == "run" else 2)
