#!/usr/bin/env python3
"""launch_kit.py — marketing-as-code. Turns a built product into a go-to-market package.

The founder's weak spot (marketing) becomes the OS's job: point this at any product the factory shipped
and a marketing-growth agent produces its launch kit — a polished landing page + launch copy (Show HN,
Product Hunt, tweet thread, SEO). It GENERATES everything but never PUBLISHES: posting to real channels
is a separate, human-approved step (your accounts, your call), consistent with the marketing role's
`public_post` approval gate. Output goes in products/<name>/launch/. Traced + redacted like any run.

    launch_kit.py make <product>     # generate the launch kit
    launch_kit.py selftest
Run with the agent-os venv python. Uses the headless `claude` CLI.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import factory  # noqa: E402


def make(product):
    repo = factory.PRODUCTS / product
    if not repo.exists():
        return {"error": f"no such product '{product}'"}
    (repo / "launch").mkdir(parents=True, exist_ok=True)
    factory._ctx.run = f"launch-{product}"        # so the kit run is debuggable + redacted like any run
    factory._ctx.product = product
    factory._ctx.stage = "LAUNCH-KIT"
    task = (
        f"Read docs/SPEC.md and README.md to understand the product '{product}'. You are producing its "
        f"GO-TO-MARKET kit. Write ONLY under launch/ — do NOT publish anything anywhere.\n\n"
        f"1) launch/landing.html — a polished, fully OFFLINE single file (inline CSS, no CDNs/network): "
        f"a hero with a one-line value proposition, 3 concrete feature highlights, a short 'how it works', "
        f"and a clear call-to-action. Modern, dark, responsive, accessible.\n"
        f"2) launch/COPY.md — launch copy, all grounded in what the product ACTUALLY does (no invented "
        f"metrics or claims): a one-line pitch; a Show HN title + ~120-word body; a Product Hunt tagline "
        f"(<=60 chars) + 2-sentence description; a 3-tweet launch thread; and an SEO <title> + meta "
        f"description + 5 keywords.\n\n"
        f"Be specific and honest. This is a real product; sell it on what it genuinely is.")
    r = factory.agent("marketing-growth", str(repo), task, timeout=320)
    audit.append(actor="launch-kit", action="GenerateKit", resource=product,
                 decision="generated", payload={"rc": r["rc"]})
    made = sorted(p.name for p in (repo / "launch").glob("*"))
    return {"product": product, "rc": r["rc"], "launch_dir": str(repo / "launch"), "files": made}


def _main(a):
    import json
    if not a:
        sys.exit("usage: launch_kit.py make <product> | selftest")
    if a[0] == "make" and len(a) > 1:
        print(json.dumps(make(a[1]), indent=2))
    elif a[0] == "selftest":
        # offline: the marketing role exists and never auto-publishes (its manifest gates public_post)
        rolefile = Path.home() / "projects" / "control-plane" / "roles" / "marketing-growth.yaml"
        ok = rolefile.exists() and "public_post" in rolefile.read_text()
        print(f"marketing-growth role present + public_post gated: {ok}")
        print("PASS: launch-kit wiring (generate, never auto-publish) ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    else:
        sys.exit("usage: launch_kit.py make <product> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
