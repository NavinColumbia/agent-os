#!/usr/bin/env python3
"""auditor_validate.py — VALIDATE the QA auditor before we trust its verdict.

Research ("Reliability without Validity", arXiv 2606.19544): high raw agreement with humans (80-85% exact-match)
collapses to only moderate chance-corrected agreement (Cohen's kappa ~0.48), and a judge can be highly
self-consistent (test-retest >= 0.95) WHILE being severely position-biased — "consistency is not correctness."
So a confident, stable auditor is *suspect until validated*. This runs the Minimum Viable Validation Protocol:

  * COHEN'S KAPPA (not raw agreement) between the auditor's accept/reject and a HUMAN-labeled set.
  * POSITION-BIAS probe: re-run on order-swapped evidence; a verdict that FLIPS on reordering is biased.
  * TEST-RETEST: run the same case >=3x; measure how often the verdict is stable.
  * The killer flag: HIGH test-retest stability + HIGH position bias => FAIL (confidently biased), surfaced on
    the pulse plane so a stable-but-wrong auditor can't masquerade as trustworthy.

    validate(cases, ensemble=1, runs=3) -> report dict         cases=[{"dir": <evidence_dir>, "label": "accept"|"reject"}]
    python auditor_validate.py <manifest.json>                  manifest = the cases list (or {"cases":[...]})
    python auditor_validate.py --selftest
"""
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in (str(_HERE), str(_HERE.parent)):            # qa/ and scripts/
    if p not in sys.path:
        sys.path.insert(0, p)

# thresholds for the "confidently biased" FAIL (from the validation-protocol finding)
STABLE_HI = float(os.environ.get("AOS_AUDITOR_STABLE_HI", "0.9"))    # test-retest at/above this = "stable"
BIAS_HI = float(os.environ.get("AOS_AUDITOR_BIAS_HI", "0.1"))        # position-flip rate at/above this = "biased"


def _label_bool(x):
    return str(x).strip().lower() in ("accept", "pass", "passed", "true", "1", "yes")


def split_holdout(cases, holdout_frac=0.3):
    """Item 4: partition labeled cases into (optimizer_set, holdout_set) DETERMINISTICALLY by hashing each
    case's dir, so if we ever tune/select agents against the auditor's score the hold-out is one the optimizer
    NEVER sees (reward-hacking a judge is only prevented by a judge the optimizer can't observe). Stable across
    runs (hash-based, no RNG) so the same case always lands in the same partition."""
    import hashlib
    cut = max(0, min(100, int(holdout_frac * 100)))
    opt, hold = [], []
    for c in cases:
        h = int(hashlib.sha256(str(c.get("dir", "")).encode()).hexdigest(), 16) % 100
        (hold if h < cut else opt).append(c)
    return opt, hold


def _accepted(verdict) -> bool:
    """A verdict counts as ACCEPT only on a clean pass — a reject OR a jury-split close call is not-accept
    (matches the launch-gate semantics: a close call escalates, it does not clear)."""
    return verdict.get("passed_audit") is True and not verdict.get("close_call")


def cohen_kappa(a, b) -> float:
    """Cohen's kappa for two equal-length boolean sequences (auditor vs human)."""
    n = len(a)
    if n == 0:
        return 0.0
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa = sum(1 for x in a if x) / n
    pb = sum(1 for x in b if x) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if po >= 1.0:
        return 1.0
    return round((po - pe) / (1 - pe), 3) if pe < 1.0 else 0.0


def _swap_order(md: str) -> str:
    """Reorder the evidence: reverse the order of the top-level '## ' sections (keep the preamble). A verdict
    that changes under this reordering is exhibiting position bias, not judging the content."""
    lines = md.split("\n")
    pre, sections, cur = [], [], None
    for ln in lines:
        if ln.startswith("## "):
            if cur is not None:
                sections.append(cur)
            cur = [ln]
        elif cur is None:
            pre.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        sections.append(cur)
    return "\n".join(pre + [l for sec in reversed(sections) for l in sec])


def _juror(evidence_dir, doss_md, lens="skeptic"):
    """One deterministic-config auditor pass over a given dossier md (single juror, to isolate stability/bias)."""
    import review
    return review._one_review(evidence_dir, doss_md, None, lens)


def validate(cases, ensemble=1, runs=3, lens="skeptic"):
    """Run the validation protocol over labeled cases. Returns a report dict + writes nothing (caller decides)."""
    import review
    cases = [c for c in cases if Path(str(c.get("dir", ""))).is_dir() and "label" in c]
    auditor_bools, human_bools = [], []
    flips = retests = stable = 0
    per_case = []
    for c in cases:
        d = Path(c["dir"])
        doss_md = review.dossier(d)["md"]
        base = _juror(d, doss_md, lens)
        a = _accepted(base)
        h = _label_bool(c["label"])
        auditor_bools.append(a)
        human_bools.append(h)

        swapped = _juror(d, _swap_order(doss_md), lens)
        flipped = _accepted(swapped) != a
        flips += 1 if flipped else 0

        reruns = [_accepted(_juror(d, doss_md, lens)) for _ in range(max(0, runs - 1))]
        all_runs = [a] + reruns
        is_stable = len(set(all_runs)) == 1
        stable += 1 if is_stable else 0
        retests += 1
        per_case.append({"dir": str(d), "label": c["label"], "auditor_accept": a,
                         "agrees_with_human": a == h, "position_flipped": flipped, "stable": is_stable})

    n = len(cases)
    kappa = cohen_kappa(auditor_bools, human_bools) if n else 0.0
    raw_agree = round(sum(1 for x, y in zip(auditor_bools, human_bools) if x == y) / n, 3) if n else 0.0
    bias_rate = round(flips / n, 3) if n else 0.0
    stability = round(stable / retests, 3) if retests else 0.0
    biased_and_stable = stability >= STABLE_HI and bias_rate >= BIAS_HI
    report = {
        "n": n, "ensemble": ensemble, "runs": runs,
        "cohen_kappa": kappa, "raw_agreement": raw_agree,
        "position_bias_rate": bias_rate, "test_retest_stability": stability,
        "biased_and_stable_FAIL": biased_and_stable,
        "verdict": ("FAIL — confidently biased (stable but flips on reordering); do NOT trust this auditor"
                    if biased_and_stable else
                    "WEAK — kappa below 0.4 (near-chance agreement); validate before trusting" if kappa < 0.4 and n else
                    "OK — kappa acceptable, not confidently biased"),
        "cases": per_case,
    }
    _flag_pulse(report)
    return report


def _flag_pulse(report):
    """Surface a validation failure on the observability plane so a stable-but-biased auditor is visible."""
    if not (report.get("biased_and_stable_FAIL") or report.get("cohen_kappa", 1) < 0.4):
        return
    try:
        import pulse
        pulse.beat("auditor:validation", stage="validate",
                   progress=report["verdict"], meta={"kappa": report["cohen_kappa"],
                   "position_bias_rate": report["position_bias_rate"],
                   "test_retest_stability": report["test_retest_stability"]})
    except Exception:
        pass


def _selftest():
    import tempfile, types, shutil, re

    def _mk(stories):
        d = Path(tempfile.mkdtemp(prefix="auditval-"))
        (d / "run-input.json").write_text(json.dumps({"vision": "v"}))
        (d / "run-final.json").write_text(json.dumps({"stories": [
            {"id": f"s{i}", "title": t, "status": "done",
             "steps": [{"reasoning": "r", "action": "a", "expected": "e", "actual": "x",
                        "verdict": "pass", "covers": ["c"]}]} for i, t in enumerate(stories)]}))
        return d

    # Order-SENSITIVE stub auditor: accepts iff the FIRST step-by-step story title starts with "OK".
    # Deterministic (=> test-retest stable) AND order-dependent (=> position bias) => must trip the FAIL flag.
    _real = sys.modules.get("factory")
    fake = types.ModuleType("factory")
    def _agent(role, repo, task, **k):
        m = re.search(r"Step-by-step for:\s*([^\n(|]+)", task)
        ok = bool(m) and m.group(1).strip().startswith("OK")
        return {"rc": 0, "out_full": json.dumps({"passed_audit": ok, "score": 8 if ok else 3,
                "skipped_flows": [], "unbacked_claims": [], "vague_reporting": [], "evidence_gaps": [],
                "summary": "s", "recommendation": "accept" if ok else "reject"})}
    fake.agent = _agent
    sys.modules["factory"] = fake

    caseA = _mk(["OK login"])            # auditor accepts; label accept  (agree, no flip)
    caseB = _mk(["BAD checkout"])        # auditor rejects; label reject  (agree, no flip)
    caseC = _mk(["OK a", "BAD b"])       # normal: first=OK -> accept; swapped: first=BAD -> reject (FLIP)
    try:
        rep = validate([{"dir": caseA, "label": "accept"},
                        {"dir": caseB, "label": "reject"},
                        {"dir": caseC, "label": "accept"}], runs=3)
        assert rep["n"] == 3, rep
        assert rep["cohen_kappa"] == 1.0, f"perfect agreement -> kappa 1.0, got {rep['cohen_kappa']}"
        assert rep["position_bias_rate"] > 0, f"caseC must flip on reorder: {rep}"
        assert rep["test_retest_stability"] == 1.0, f"deterministic stub -> fully stable: {rep}"
        assert rep["biased_and_stable_FAIL"] is True, f"stable + biased must FAIL: {rep}"
        # kappa is computed independently of raw agreement (the whole point of the finding)
        assert cohen_kappa([True, True, True, True], [True, False, True, False]) < 1.0
        # item 4: hold-out partition is deterministic + disjoint (the optimizer never sees the hold-out)
        many = [{"dir": f"/x/case-{i}", "label": "accept"} for i in range(200)]
        opt, hold = split_holdout(many, 0.3)
        assert len(opt) + len(hold) == 200 and 40 <= len(hold) <= 80, (len(opt), len(hold))
        assert split_holdout(many, 0.3)[1] == hold, "partition must be stable across calls"
        assert not ({c["dir"] for c in opt} & {c["dir"] for c in hold}), "partitions disjoint"
        print("auditor_validate selftest: PASS (kappa computed vs labels; position-flip detected; "
              "deterministic auditor is stable; stable+biased trips the FAIL flag; hold-out split stable+disjoint)")
        return 0
    finally:
        if _real is not None:
            sys.modules["factory"] = _real
        else:
            sys.modules.pop("factory", None)
        for d in (caseA, caseB, caseC):
            shutil.rmtree(d, ignore_errors=True)


def _main(argv):
    if not argv or argv[0] == "--selftest":
        return _selftest()
    manifest = json.loads(Path(argv[0]).read_text())
    cases = manifest.get("cases") if isinstance(manifest, dict) else manifest
    rep = validate(cases, ensemble=int(os.environ.get("AOS_AUDITOR_ENSEMBLE", "1")))
    print(json.dumps(rep, indent=2))
    return 0 if not rep["biased_and_stable_FAIL"] else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
