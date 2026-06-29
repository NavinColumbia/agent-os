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
# key=value style assignment of a secret: keep the key+separator, mask the value.
# NOTE: do NOT use \b around the keyword — underscore is a \w char, so an env-style key like
# ACCESS_TOKEN / GITHUB_API_KEY / DB_PASSWORD has NO word boundary before the keyword and would
# fail open (leak the value). Use character-class lookarounds that also break on underscore.
KV_PATTERN = re.compile(r"""(?i)(?<![A-Za-z0-9])(api[_-]?key|secret|token|password|passwd|aosnap_pass)(?![A-Za-z0-9])(\s*[:=]\s*)(['"]?)[^\s'"]{6,}""")
# DB url password: keep prefix + '@', mask the password between them.
DBURL_PATTERN = re.compile(r"(postgres(?:ql)?://[^:/\s]+:)([^@\s]+)(@)")
# Standalone secret shapes — masked wholesale.
TOKEN_PATTERNS = [
    re.compile(r"\baos_[A-Fa-f0-9]{16,}\b"),                      # our tenant tokens
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),                 # anthropic keys (sk-ant-api03-...)
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b"),               # openai project keys
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),                       # openai-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),               # github tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),             # slack tokens
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),                   # google api keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                          # aws access key id
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{10,}"),           # bearer tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),  # private keys
]
# Back-compat: expose a combined list for any external importer.
PATTERNS = [KV_PATTERN, DBURL_PATTERN] + TOKEN_PATTERNS


def scrub(text):
    if not text:
        return text
    out = text
    out = KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", out)
    out = DBURL_PATTERN.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
    for p in TOKEN_PATTERNS:
        out = p.sub(MASK, out)
    return out


def _main(a):
    if a and a[0] == "selftest":
        cases = [
            ("API_KEY=sk-abcdef0123456789ghijkl", "sk-abcdef"),
            ("ANTHROPIC_API_KEY=sk-ant-api03-AbCdEf0123456789GhIjKlMnOpQr", "sk-ant-api03"),
            ("key sk-ant-api03-XyZ01234567890abcdefghijKL in trace", "sk-ant-api03-XyZ"),
            ("here is aos_0123456789abcdef0123456789 token", "aos_0123"),
            ("Authorization: Bearer eyJhbGciOiJ.payload.sig", "Bearer eyJ"),
            ("DATABASE_URL=postgresql://agentos:supersecretpw@127.0.0.1:5433/agentos", "supersecretpw"),
            ("password: hunter2hunter2", "hunter2"),
            # underscore-prefixed env-style keys with opaque values (fail-open regression):
            ("access_token=abcdef1234567890deadbeef", "abcdef1234567890deadbeef"),
            ("refresh_token: zzzzz9999988887777", "zzzzz9999988887777"),
            ("GITHUB_API_KEY=plainsecret123456", "plainsecret123456"),
            ("MY_SECRET=plainsecret123456", "plainsecret123456"),
            ("db_password=plainsecret123456", "plainsecret123456"),
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
