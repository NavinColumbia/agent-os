#!/usr/bin/env python3
"""test_governance_wired.py — STANDING GUARD against the "declared-but-not-wired" bug class.

A governance control that is DECLARED in a role manifest but enforced by NOTHING in code is
worse than no control at all: it reads as governed on paper while granting unchecked behavior
in practice. That is exactly how the `factory.role_brief` bug shipped (a field declared in YAML
that no code consumed) and how every can_*/denied_* flag below can rot silently.

This test fails the build if any declared capability/constraint key in
  ~/projects/control-plane/roles/*.yaml
has no REAL consumer in
  ~/projects/agent-os/scripts/*.py

"Real consumer" = the key is actually READ by code — a `.get("key")`, a subscript `m["key"]`,
an attribute `.key`, or a comparison `"key" == ...` — and NOT merely mentioned inside a comment,
a docstring, or some unrelated string literal. Detection is token-based (via the `tokenize`
module) so comments and docstrings are structurally excluded rather than regex-guessed.

It ALSO asserts that `factory.role_brief` actually embeds a sample role's responsibilities AND
communicates_via content into the brief — a direct regression guard for the original bug.

Run:  python scripts/test_governance_wired.py   (with the agent-os venv)
Exit: 0 = PASS (every declared control is wired), non-zero = FAIL (offending keys listed).
"""
import ast
import io
import sys
import tokenize
from pathlib import Path

ROLES = Path.home() / "projects" / "control-plane" / "roles"
SCRIPTS = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()

# Constraint keys we treat as governance controls (beyond the auto-discovered can_* capability
# flags). These are the manifest fields that are supposed to BIND agent behavior. Descriptive
# metadata (display_name, summary, layer, ownership, ...) is intentionally excluded — this guard
# is about controls that must be enforced, not prose that is merely rendered.
CONSTRAINT_KEYS = {
    "approval_required_for",
    "allowed_paths",
    "denied_paths",
    "denied_tools",
    "must_never",
    "communicates_via",
}

_SKIP_TOK = {
    tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
    tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
}
_SUBSCRIPTABLE = {")", "]"}


def discover_governance_keys():
    """Parse every role manifest and return the set of capability/constraint keys actually used:
    any top-level `can_*` flag plus any of the named constraint keys that appears in a manifest."""
    import yaml
    keys = set()
    files = sorted(ROLES.glob("*.yaml"))
    for f in files:
        m = yaml.safe_load(f.read_text()) or {}
        if not isinstance(m, dict):
            continue
        for k in m:
            if not isinstance(k, str):
                continue
            if k.startswith("can_") or k in CONSTRAINT_KEYS:
                keys.add(k)
    return keys, files


def _significant_tokens(src):
    """Tokenize source, dropping comments / layout tokens. Returns a list of tokens. Comments are a
    distinct token type, so any key mentioned in a comment is gone here; a key mentioned inside a
    docstring survives only as part of one big STRING token whose value != the key, so it is never
    matched as a read below."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    return [t for t in toks if t.type not in _SKIP_TOK]


def _is_real_read(sig, j, key):
    """Is sig[j] a real READ of `key`?  Recognises:
       m.get("key")        -> STRING preceded by  get (
       m["key"] / d["key"] -> STRING preceded by [ that follows a subscriptable token
       "key" == x / == key -> STRING adjacent to ==
       obj.key             -> NAME preceded by .
    Pure string-literal mentions (list elements, docstrings, args to other calls) are NOT reads."""
    t = sig[j]
    prev = sig[j - 1].string if j - 1 >= 0 else ""
    prev2 = sig[j - 2].string if j - 2 >= 0 else ""
    nxt = sig[j + 1].string if j + 1 < len(sig) else ""

    # attribute access: .key
    if t.type == tokenize.NAME and t.string == key and prev == ".":
        return True

    if t.type == tokenize.STRING:
        try:
            val = ast.literal_eval(t.string)
        except Exception:
            return False
        if val != key:
            return False
        # .get("key")
        if prev == "(" and prev2 == "get":
            return True
        # subscript obj["key"]  (distinguish from list literal ["key", ...])
        if prev == "[" and j - 2 >= 0:
            before = sig[j - 2]
            if before.string in _SUBSCRIPTABLE or (
                before.type == tokenize.NAME and before.string not in ("and", "or", "not", "in",
                                                                        "return", "if", "elif",
                                                                        "while", "assert", "yield")
            ):
                return True
        # comparison "key" == ...  or  ... == "key"
        if nxt == "==" or prev == "==":
            return True
    return False


def has_consumer(key, py_files):
    """True if any python source has a real read of `key` (returns (True, 'file:line') or (False, ''))."""
    for path in py_files:
        try:
            src = path.read_text()
        except Exception:
            continue
        sig = _significant_tokens(src)
        for j in range(len(sig)):
            if _is_real_read(sig, j, key):
                return True, f"{path.name}:{sig[j].start[0]}"
    return False, ""


def check_role_brief_regression():
    """Regression guard for the original `factory.role_brief` bug: the generated brief MUST embed
    the role's actual responsibilities AND its communicates_via channels, not drop them on the floor."""
    import yaml
    sys.path.insert(0, str(SCRIPTS))
    import factory  # noqa: E402

    sample = "controller"
    manifest = yaml.safe_load((ROLES / f"{sample}.yaml").read_text())
    brief = factory.role_brief(sample)

    problems = []
    resp = manifest.get("responsibilities") or []
    comms = manifest.get("communicates_via") or []

    if not resp:
        problems.append(f"sample role '{sample}' declares no responsibilities to test against")
    else:
        # a distinctive slice of the first responsibility must appear verbatim in the brief
        probe = resp[0][:48]
        if probe not in brief:
            problems.append(f"role_brief() dropped responsibilities (missing: {probe!r})")

    if not comms:
        problems.append(f"sample role '{sample}' declares no communicates_via to test against")
    else:
        if not any(c in brief for c in comms):
            problems.append(f"role_brief() dropped communicates_via (none of {comms} present)")

    return problems


def main():
    if not ROLES.is_dir():
        print(f"FAIL: roles dir not found: {ROLES}")
        return 2

    gov_keys, manifest_files = discover_governance_keys()
    py_files = [p for p in sorted(SCRIPTS.glob("*.py")) if p.resolve() != SELF]

    print(f"Scanned {len(manifest_files)} manifests, {len(py_files)} python sources.")
    print(f"Declared governance keys ({len(gov_keys)}): {', '.join(sorted(gov_keys))}\n")

    wired, unwired = [], []
    for key in sorted(gov_keys):
        ok, where = has_consumer(key, py_files)
        if ok:
            wired.append((key, where))
            print(f"  [WIRED ]  {key:<28} consumed at {where}")
        else:
            unwired.append(key)
            print(f"  [UNWIRED] {key:<28} NO enforcing consumer in scripts/*.py")

    print("\nRegression guard: factory.role_brief embeds responsibilities + communicates_via")
    try:
        brief_problems = check_role_brief_regression()
    except Exception as e:
        brief_problems = [f"role_brief regression check raised: {e!r}"]
    if brief_problems:
        for p in brief_problems:
            print(f"  [BROKEN]  {p}")
    else:
        print("  [OK    ]  responsibilities + communicates_via present in sample brief")

    print()
    failed = bool(unwired) or bool(brief_problems)
    if failed:
        if unwired:
            print("Declared-but-NOT-wired governance keys (no enforcing consumer):")
            for k in unwired:
                print(f"    - {k}")
        if brief_problems:
            print("role_brief regression:")
            for p in brief_problems:
                print(f"    - {p}")
        print(f"\nFAIL: {len(unwired)} declared governance control(s) have no enforcing consumer"
              + ("; role_brief regression detected." if brief_problems else "."))
        return 1

    print(f"PASS: all {len(gov_keys)} declared governance controls have a real enforcing consumer, "
          "and role_brief embeds responsibilities + communicates_via.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
