"""voice_guard.py — decide whether a (transcribed) instruction is a GATED/risky action.

Voice input must NEVER auto-trigger a gated action. This classifier is intentionally
broad and fails CLOSED: if a transcript looks anywhere near a risky action, it is gated
and must be confirmed by the human through the SAME approval path as a typed command.
"""
import re

# Risky/gated intents — spoken forms (loose, since STT is fuzzy) and CLI forms.
RISKY = [
    (r"\bdeploy(ing|ment)?\b", "deploy"),
    (r"\b(ship|release|roll ?out|go live|push to prod\w*)\b", "release/deploy"),
    (r"\bpush\b.*\b(main|master|prod\w*)\b", "push to protected branch"),
    (r"\bforce[- ]?push\b|\bpush\b.*\bforce\b", "force-push"),
    (r"\bmerge\b.*\b(main|master|pr|pull request)\b", "merge"),
    (r"\b(delete|drop|remove|wipe|destroy)\b", "destructive delete"),
    (r"\brm\s+-rf\b|\bdrop\s+table\b|\btruncate\b", "destructive command"),
    (r"\bsudo\b|\broot\b", "privilege escalation"),
    (r"\b(secret|api ?key|token|password|credential)s?\b", "secret handling"),
    (r"\b(pay|payment|charge|transfer|wire|refund|invoice)\b", "money movement"),
    (r"\b(email|tweet|post|publish|send)\b.*\b(everyone|public|customers|users)\b", "public/outbound comms"),
    (r"\bcurl\b.*\|\s*(sh|bash)\b|\bwget\b.*\|\s*(sh|bash)\b", "pipe-to-shell"),
    (r"\bshut ?down\b|\brestart\b.*\b(host|wsl|machine|server)\b", "host/WSL disruption"),
]


def classify(text):
    """Return (is_gated: bool, reason: str). Fails closed (empty/garbled -> gated)."""
    t = (text or "").strip().lower()
    if not t:
        return True, "empty/garbled transcript"
    for pat, label in RISKY:
        if re.search(pat, t):
            return True, label
    return False, ""


if __name__ == "__main__":
    import sys
    g, why = classify(" ".join(sys.argv[1:]))
    print(f"gated={g} reason={why!r}")
