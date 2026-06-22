"""secret_filter.py — refuse anything that looks like a secret/API key.

Used by the reply listener so a credential typed on the phone never lands in a
.cmd file (or in logs). Conservative: when in doubt, treat as a secret.
"""
import re

# Named, high-signal credential shapes.
_PATTERNS = [
    (r"sk-[A-Za-z0-9]{16,}", "openai-style key"),
    (r"sk-ant-[A-Za-z0-9_-]{16,}", "anthropic key"),
    (r"AKIA[0-9A-Z]{16}", "aws access key id"),
    (r"ASIA[0-9A-Z]{16}", "aws temp key id"),
    (r"ghp_[A-Za-z0-9]{20,}", "github token"),
    (r"gho_[A-Za-z0-9]{20,}", "github oauth token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "github fine-grained pat"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "slack token"),
    (r"AIza[0-9A-Za-z_-]{30,}", "google api key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block"),
    (r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}", "jwt"),
    (r"(?i)\b(api[_-]?key|apikey|secret|passwd|password|token|bearer|access[_-]?key)\b\s*[:=]\s*\S{6,}",
     "key=value secret"),
]

# Generic high-entropy token: a long unbroken run of base64/hex-ish chars.
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b")


def looks_like_secret(text):
    """Return (is_secret: bool, reason: str)."""
    if not text:
        return False, ""
    for pat, label in _PATTERNS:
        if re.search(pat, text):
            return True, label
    for m in _HIGH_ENTROPY.finditer(text):
        tok = m.group(0)
        # require some mixing so normal long words / ids don't trip it
        if (any(c.isdigit() for c in tok)
                and any(c.isalpha() for c in tok)
                and len(tok) >= 32):
            return True, "high-entropy token"
    return False, ""


if __name__ == "__main__":
    import sys
    s = " ".join(sys.argv[1:])
    sec, why = looks_like_secret(s)
    print(f"secret={sec} reason={why!r}")
