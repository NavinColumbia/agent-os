#!/usr/bin/env python3
"""context.py — keep agents under the context-window ceiling on large products/codebases.

An agent can't read an unbounded repo. This estimates a repo's token weight, builds a COMPACT structure
map (files + top-level symbols) so an agent can navigate without reading everything, and reports a
budget verdict. The factory uses it: when a repo is large, the agent gets the map + an instruction to
read only the relevant slice instead of the whole tree.

    context.py budget <repo>     # tokens vs model window + over/under
    context.py map <repo>        # compact structure map
Run with the agent-os venv python.
"""
import re
import sys
from pathlib import Path

CODE_EXT = (".py", ".js", ".ts", ".html", ".css", ".md", ".sql", ".json", ".yaml", ".yml")
MODEL_WINDOW = 200_000          # claude context window (tokens)
SAFE_FRACTION = 0.5             # leave headroom for the agent's own reasoning + output
SKIP = ("__pycache__", "/.git", "node_modules", "/.pytest_cache", "/launch/", "/intel/")


def _files(repo):
    return [p for p in Path(repo).rglob("*")
            if p.is_file() and p.suffix in CODE_EXT and not any(s in str(p).replace("\\", "/") for s in SKIP)]


def estimate_tokens(repo):
    chars = 0
    for f in _files(repo):
        try:
            chars += len(f.read_text(errors="ignore"))
        except Exception:
            pass
    return chars // 4              # ~4 chars/token


def repo_map(repo):
    """A compact map: each file with its top-level python def/class names (so an agent knows the shape)."""
    root = Path(repo)
    lines = []
    for f in sorted(_files(root)):
        rel = f.relative_to(root)
        syms = []
        if f.suffix == ".py":
            try:
                for m in re.finditer(r"^\s*(?:async\s+)?(def|class)\s+(\w+)", f.read_text(errors="ignore"), re.M):
                    syms.append(m.group(2))
            except Exception:
                pass
        lines.append(f"  {rel}" + (f"  :: {', '.join(syms[:12])}" if syms else ""))
    return "REPO MAP (files + top-level symbols):\n" + "\n".join(lines)


def budget(repo):
    toks = estimate_tokens(repo)
    cap = int(MODEL_WINDOW * SAFE_FRACTION)
    return {"tokens": toks, "window": MODEL_WINDOW, "safe_cap": cap, "over": toks > cap,
            "advice": ("repo too large to read whole — work on a slice using the map" if toks > cap
                       else "fits in one pass")}


def _main(a):
    import json
    if not a:
        sys.exit("usage: context.py budget <repo> | map <repo>")
    repo = a[1] if len(a) > 1 else "."
    if a[0] == "budget":
        print(json.dumps(budget(repo), indent=2))
    elif a[0] == "map":
        print(repo_map(repo))
    elif a[0] == "selftest":
        b = budget(str(Path.home() / "projects" / "agent-os" / "scripts"))   # a real, non-trivial dir
        ok = b["tokens"] > 0 and "over" in b and isinstance(repo_map(str(Path.home() / "projects" / "agent-os" / "scripts")), str)
        print(f"estimated {b['tokens']} tokens for scripts/ (cap {b['safe_cap']}, over={b['over']})")
        print("PASS: context budget + repo map ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
