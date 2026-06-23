#!/usr/bin/env python3
"""security_scan.py — automated security posture check (runs in the regression).

Enforces the constitutional security invariants an acquirer's due-diligence would probe, by actually
scanning the tree rather than trusting a doc:
  * never bind 0.0.0.0 (localhost / tailnet only)
  * no hardcoded secrets in source (must come from env / vault)
  * secrets never git-tracked (.env*, keys/, *.aosnap)
  * untrusted generated code is run sandboxed (factory uses srt for tests)
  * .gitignore covers the secret surface

Exit non-zero (and FAIL) on any HIGH finding; MEDIUM/LOW are reported.

    security_scan.py            # scan + verdict
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / "projects" / "agent-os"
CODE_GLOBS = ["scripts/**/*.py", "scripts/*.sh", "platform/**/*.py", "platform/*.sh", "*.sh"]
SECRET_RE = re.compile(r"""(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['"][A-Za-z0-9_\-/+]{16,}['"]""")
BIND_RE = re.compile(r"""['"]0\.0\.0\.0['"]""")  # only an actual quoted bind address, not a comment
ALLOW_SECRET = ("CHANGE-ME", "os.environ", "getenv", "_cfg", ".env", "example", "AOSNAP_PASS=%s", "token_hex",
                "Bearer ", "f\"Bearer", "<", "your-", "xxx")

findings = []


def add(sev, where, issue):
    findings.append((sev, where, issue))


def _files():
    out = []
    for g in CODE_GLOBS:
        out += [p for p in ROOT.glob(g) if p.is_file()]
    return sorted(set(out))


def scan_binds_and_secrets():
    for p in _files():
        if p.name == "security_scan.py":   # the scanner legitimately contains the detection patterns
            continue
        rel = p.relative_to(ROOT)
        for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if BIND_RE.search(line):
                add("HIGH", f"{rel}:{i}", "binds a quoted 0.0.0.0 (must be localhost/tailnet only)")
            if SECRET_RE.search(line) and not any(a in line for a in ALLOW_SECRET):
                add("HIGH", f"{rel}:{i}", "possible hardcoded secret (must read from env/vault)")


def scan_git_tracked_secrets():
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
    tracked = r.stdout.splitlines()
    for t in tracked:
        if t == ".env.local" or t.startswith("keys/") or t.endswith(".aosnap") or t == "postgres/.env" or t == "ntfy/.env":
            add("HIGH", t, "secret file is GIT-TRACKED (must be gitignored)")


def scan_gitignore():
    gi = (ROOT / ".gitignore").read_text() if (ROOT / ".gitignore").exists() else ""
    for needed in [".env.local", "keys/", "backups/"]:
        if needed not in gi:
            add("MEDIUM", ".gitignore", f"does not cover {needed}")


def scan_sandbox():
    f = (ROOT / "scripts" / "factory.py").read_text(errors="ignore")
    if "srt" not in f or "allowWrite" not in f:
        add("HIGH", "scripts/factory.py", "untrusted generated code not run under the srt sandbox")
    # network must be denied for sandboxed test exec
    if '"allowedDomains": []' not in f:
        add("MEDIUM", "scripts/factory.py", "sandbox network policy not provably deny-all")


def main():
    for fn in (scan_binds_and_secrets, scan_git_tracked_secrets, scan_gitignore, scan_sandbox):
        fn()
    highs = [f for f in findings if f[0] == "HIGH"]
    for sev, where, issue in sorted(findings):
        print(f"  [{sev}] {where} — {issue}")
    print(f"\nsecurity scan: {len(findings)} findings ({len(highs)} HIGH)")
    if highs:
        print("FAIL: security invariants violated")
        sys.exit(1)
    print("PASS: security invariants hold (no 0.0.0.0, no hardcoded/tracked secrets, untrusted code sandboxed) ✅")
    sys.exit(0)


if __name__ == "__main__":
    main()
