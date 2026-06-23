#!/usr/bin/env python3
"""redact.py — scrub secrets from text before it is persisted or shown.

Used on debug traces (and reusable anywhere). Stored traces capture real agent I/O, which could contain
tokens, keys, DB URLs, etc. — shipping those to a UI or a customer is a security non-starter. scrub()
masks the common secret shapes deterministically so the trace stays useful for debugging but leaks nothing.

    redact.py "<text>"     # print scrubbed text
    redact.py selftest
"""
import re
import sys

MASK = "‹REDACTED›"
PATTERNS = [
    re.compile(r"""(?i)\b(api[_-]?key|secret|token|password|passwd|aosnap_pass)\b(\s*[:=]\s*)(['"]?)[^\s'"]{6,}"""),
    re.compile(r"\baos_[A-Fa-f0-9]{16,}\b"),                      # our tenant tokens
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),                       # openai-style
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                          # aws access key id
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{10,}"),           # bearer tokens
    re.compile(r"(postgres(?:ql)?://[^:/\s]+:)([^@\s]+)(@)"),     # DB url password
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),  # private keys
]


def scrub(text):
    if not text:
        return text
    out = text
    out = PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", out)
    out = PATTERNS[5].sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
    for p in (PATTERNS[1], PATTERNS[2], PATTERNS[3], PATTERNS[4], PATTERNS[6]):
        out = p.sub(MASK, out)
    return out


def _main(a):
    if a and a[0] == "selftest":
        cases = [
            ("API_KEY=sk-abcdef0123456789ghijkl", "sk-abcdef"),
            ("here is aos_0123456789abcdef0123456789 token", "aos_0123"),
            ("Authorization: Bearer eyJhbGciOiJ.payload.sig", "Bearer eyJ"),
            ("DATABASE_URL=postgresql://agentos:supersecretpw@127.0.0.1:5433/agentos", "supersecretpw"),
            ("password: hunter2hunter2", "hunter2"),
        ]
        leaked = [raw for raw, secret in cases if secret in scrub(raw)]
        kept = scrub("the build passed 163 tests in 0.3s") == "the build passed 163 tests in 0.3s"
        ok = not leaked and kept
        for raw, _ in cases:
            print("  ", scrub(raw))
        print(f"leaked={leaked}; benign-text-preserved={kept}")
        print("PASS: secret redaction ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    elif a:
        print(scrub(a[0]))


if __name__ == "__main__":
    _main(sys.argv[1:])
