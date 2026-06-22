#!/usr/bin/env python3
"""provenance.py — cryptographic proof of authorship + tamper/theft detection.

Builds a manifest of every design/code artifact (path -> sha256), computes a root hash,
and SIGNS it with the org Ed25519 key (scripts/identity.py key 'agent-os-org'). Writes
PROVENANCE.json (committed). Because only the holder of the private key can produce the
signature, and git+GitHub timestamp the commit, this is portable evidence that THIS design
existed in THIS exact form, authored by the key holder, at this time.

Theft detection: any third-party system whose files hash to entries in this manifest — or
which carries the build fingerprint in .watermark / NOTICE — is provably derived from this Work.

    provenance.py stamp     # (re)generate + sign PROVENANCE.json
    provenance.py verify     # verify signature + detect any drift vs the signed manifest
Run with the agent-os venv python.
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

ROOT = Path.home() / "projects"
KEYS = ROOT / "agent-os" / "keys"
KEY_ID = "agent-os-org"
OUT = ROOT / "agent-os" / "PROVENANCE.json"
# the IP we are protecting: design docs (control-plane ADRs/schemas/protocols) + agent-os code
INCLUDE = [
    ("agent-os", ["scripts/**/*.py", "scripts/*.sh", "postgres/initdb/*.sql", "*.md", "*/docker-compose.yml", "cerbos/policies/*.yaml"]),
    ("control-plane", ["docs/adr/*.md", "schemas/*.json", "protocols/*.md", "policies/*.md", "roles/*.yaml", "hooks/*.py", "constitution/*.md", "templates/*.md"]),
]


def _files():
    out = {}
    for base, globs in INCLUDE:
        b = ROOT / base
        for g in globs:
            for p in sorted(b.glob(g)):
                if p.is_file():
                    out[f"{base}/{p.relative_to(b)}"] = hashlib.sha256(p.read_bytes()).hexdigest()
    return dict(sorted(out.items()))


def _root_hash(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _git_commit():
    try:
        return subprocess.check_output(["git", "-C", str(ROOT / "agent-os"), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return None


def stamp():
    files = _files()
    root = _root_hash(files)
    priv = Ed25519PrivateKey.from_private_bytes((KEYS / f"{KEY_ID}.ed25519").read_bytes())
    sig = priv.sign(root.encode()).hex()
    pub = (KEYS / f"{KEY_ID}.pub").read_text().strip()
    wm = (ROOT / "agent-os" / ".watermark").read_text().strip()
    doc = {
        "work": "agent-os (privacy-first single-box governed agent operating system)",
        "author_key_id": KEY_ID,
        "public_key": pub,
        "build_fingerprint": wm,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "file_count": len(files),
        "root_hash": root,
        "signature": sig,
        "files": files,
        "notice": "Proprietary. Signature over root_hash proves authorship by the holder of author_key_id's private key.",
    }
    OUT.write_text(json.dumps(doc, indent=2))
    print(f"stamped {len(files)} files; root={root[:16]}…; sig={sig[:16]}…  -> {OUT.name}")
    return doc


def verify():
    doc = json.loads(OUT.read_text())
    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(doc["public_key"]))
    try:
        pub.verify(bytes.fromhex(doc["signature"]), doc["root_hash"].encode())
    except InvalidSignature:
        print("PROVENANCE SIGNATURE INVALID ❌"); return False
    # detect drift vs current files
    cur = _files()
    if _root_hash(cur) != doc["root_hash"]:
        changed = [f for f in set(cur) | set(doc["files"]) if cur.get(f) != doc["files"].get(f)]
        print(f"signature valid, but {len(changed)} file(s) drifted since stamp (re-stamp): {changed[:5]}")
        return True
    print(f"PROVENANCE VALID ✅ — signature verifies, {doc['file_count']} files match the signed manifest (root {doc['root_hash'][:16]}…)")
    return True


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "stamp"
    if mode == "stamp":
        stamp()
    elif mode == "verify":
        sys.exit(0 if verify() else 1)
