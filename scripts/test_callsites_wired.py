#!/usr/bin/env python3
"""
STANDING GUARD — callsite signature wiring.

Kills a recurring agent-os bug-class: a cross-module LEAF function's signature
changes (e.g. a new required `tenant_id` param) but a CALLER in another module
is not updated, so production bricks while the leaf's OWN selftest (which seeds /
monkeypatches the leaf) stays green.

This has bitten us 3x:
  - designview.gallery        -> design_fleet / console
  - research.run_state/select -> loopcontroller

How it works:
  1. A curated list of INTEGRATION-SEAM targets (cross-module leaf APIs that
     callers invoke positionally). Each target's REAL signature is discovered at
     runtime via `inspect.signature` (no hardcoded arity).
  2. Every scripts/*.py is parsed with `ast`. We resolve import aliases
     (`import X`, `import X as Y`, `from X import f [as g]` — at ANY scope) and
     find Call nodes that invoke a target via its resolved module.
  3. A callsite FAILS if it passes FEWER positional args than the target's
     required-positional count, or MORE than its positional capacity (when the
     target has no *args). Keyword args that fill required params are honored so
     `decide(t, o, artifact_id=a, status=s)` is NOT a false positive.

To avoid false positives we only flag when the callee module alias confidently
resolves to the real leaf module, and we skip calls that splat *args / **kwargs
(unverifiable). Monkeypatch lines (`_r.run_state = lambda ...`) are assignments,
not Call nodes, so they are naturally ignored.

Run: .venv/bin/python scripts/test_callsites_wired.py
Exit 0 + "PASS:" when all verifiable callsites are correctly wired.
Exit 1 + "FAIL:" (with file:line, expected-vs-actual) on any mismatch.
"""
import ast
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------------------------------------------------------------------------
# 1. Curated INTEGRATION-SEAM targets: module -> {leaf function names}
#    These are the cross-module APIs callers invoke positionally. Includes the
#    modules that have bitten us plus their siblings on the same seams.
# ---------------------------------------------------------------------------
TARGETS = {
    "research":     {"start", "run_state", "select"},
    "designview":   {"gallery", "decide"},
    "design_fleet": {"prototype"},
    "qualityloop":  {"run"},
    "verify":       {"verify"},
    "billing":      {"signup"},
    "vault":        {"get_secret"},
    "orgs":         {"get"},
    "cockpit":      {"cockpit", "control", "health", "company_summary", "comms_graph"},
}


def discover_signature(func):
    """Return (param_list, has_varargs) where param_list is a list of
    (name, required) for positional-capable params in declaration order."""
    sig = inspect.signature(func)
    params = []
    has_varargs = False
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            params.append((p.name, p.default is inspect.Parameter.empty))
        elif p.kind == p.VAR_POSITIONAL:
            has_varargs = True
    return params, has_varargs


def load_targets():
    """Import each target module and resolve real signatures.
    Returns spec[(module, func)] = (params, has_varargs, req, cap)."""
    spec = {}
    for mod_name, fns in TARGETS.items():
        mod = __import__(mod_name)
        for fn in fns:
            f = getattr(mod, fn, None)
            if not callable(f):
                print(f"FAIL: target {mod_name}.{fn} is missing or not callable "
                      f"(curated list is stale)")
                sys.exit(1)
            params, has_varargs = discover_signature(f)
            req = sum(1 for _, required in params if required)
            cap = len(params)
            spec[(mod_name, fn)] = (params, has_varargs, req, cap)
    return spec


# ---------------------------------------------------------------------------
# 2. Per-file alias resolution.
# ---------------------------------------------------------------------------
def build_aliases(tree):
    """Walk ALL import statements (any scope) and return:
       module_alias: alias_name -> real_module_name   (import X [as Y])
       func_alias:   alias_name -> (module, func)      (from X import f [as g])
    Only entries that point at a TARGET module are kept."""
    module_alias = {}
    func_alias = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in TARGETS:
                    module_alias[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom):
            if node.module in TARGETS and node.level == 0:
                for a in node.names:
                    if a.name in TARGETS[node.module]:
                        func_alias[a.asname or a.name] = (node.module, a.name)
    return module_alias, func_alias


def resolve_call(node, module_alias, func_alias):
    """Return (module, func) if this Call targets a known leaf, else None."""
    fn = node.func
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        mod = module_alias.get(fn.value.id)
        if mod and fn.attr in TARGETS.get(mod, ()):
            return (mod, fn.attr)
    elif isinstance(fn, ast.Name):
        hit = func_alias.get(fn.id)
        if hit:
            return hit
    return None


def analyze_call(node, params, has_varargs, req, cap):
    """Classify a resolved callsite. Returns None if OK or unverifiable,
    else an error string describing expected-vs-actual."""
    # Splatted *args makes positional count unknowable; **kwargs may supply
    # required params by name we cannot see — treat both as unverifiable.
    if any(isinstance(a, ast.Starred) for a in node.args):
        return ("SKIP", "splatted *args (unverifiable)")
    if any(k.arg is None for k in node.keywords):
        return ("SKIP", "splatted **kwargs (unverifiable)")

    n_pos = len(node.args)
    kw_names = {k.arg for k in node.keywords}

    # Too many positional args and no *args to absorb them.
    if not has_varargs and n_pos > cap:
        return ("FAIL", f"passes {n_pos} positional args but capacity is {cap} "
                        f"(no *args) — expected {req}..{cap}")

    # A required param is missing if it is past the positional fill AND not
    # supplied by keyword name.
    missing = [name for i, (name, required) in enumerate(params)
               if required and i >= n_pos and name not in kw_names]
    if missing:
        return ("FAIL", f"passes {n_pos} positional args; required params "
                        f"unfilled: {', '.join(missing)} — expected >= {req} "
                        f"(got {n_pos})")
    return None


# ---------------------------------------------------------------------------
# 3. Scan.
# ---------------------------------------------------------------------------
def main():
    spec = load_targets()
    self_file = os.path.basename(__file__)
    files = sorted(f for f in os.listdir(HERE)
                   if f.endswith(".py") and f != self_file)

    verified = 0
    skipped = 0
    failures = []  # (file, line, target, message)

    for fname in files:
        path = os.path.join(HERE, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=fname)
        except (SyntaxError, UnicodeDecodeError):
            continue

        module_alias, func_alias = build_aliases(tree)
        if not module_alias and not func_alias:
            continue  # this file imports none of the targets

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = resolve_call(node, module_alias, func_alias)
            if target is None:
                continue
            params, has_varargs, req, cap = spec[target]
            result = analyze_call(node, params, has_varargs, req, cap)
            tgt = f"{target[0]}.{target[1]}"
            if result is None:
                verified += 1
            elif result[0] == "SKIP":
                skipped += 1
            else:
                failures.append((fname, node.lineno, tgt, result[1]))

    print(f"callsites verified: {verified}  (unverifiable/skipped: {skipped})")
    print(f"targets: {sum(len(v) for v in TARGETS.values())} leaf APIs across "
          f"{len(TARGETS)} modules")

    if failures:
        print(f"FAIL: {len(failures)} mis-wired callsite(s) "
              f"(leaf signature drifted from caller):")
        for fname, line, tgt, msg in failures:
            print(f"  {fname}:{line}  {tgt}  ->  {msg}")
        return 1

    print(f"PASS: all {verified} cross-module callsites match their leaf "
          f"signatures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
