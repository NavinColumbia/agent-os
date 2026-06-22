#!/usr/bin/env python3
"""identity.py — Ed25519 signed-manifest agent identity (ADR 0004 K5 / ADR 0005).

did:wba's core idea, localized: an agent's identity is a SIGNED manifest. The Controller/hook
can verify *which role* is acting (not just what it's doing), and a tampered manifest fails
verification. Private keys live under ~/projects/agent-os/keys/ (gitignored, chmod 700); only the
public key + agent_id go in the (committable) manifest.

    identity.py keygen <agent_id>             # create keypair, print public key
    identity.py sign <agent_id> <manifest>    # write <manifest>.sig
    identity.py verify <agent_id> <manifest>  # verify manifest against its .sig (exit 0/1)
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

KEYS = Path.home() / "projects" / "agent-os" / "keys"


def _priv_path(agent_id):
    return KEYS / f"{agent_id}.ed25519"


def keygen(agent_id):
    KEYS.mkdir(mode=0o700, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    p = _priv_path(agent_id); p.write_bytes(raw); p.chmod(0o600)
    pub_hex = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw).hex()
    (KEYS / f"{agent_id}.pub").write_text(pub_hex)
    return pub_hex


def sign(agent_id, manifest_path):
    raw = _priv_path(agent_id).read_bytes()
    priv = Ed25519PrivateKey.from_private_bytes(raw)
    data = Path(manifest_path).read_bytes()
    sig = priv.sign(data).hex()
    Path(manifest_path + ".sig").write_text(sig)
    return sig


def verify(agent_id, manifest_path):
    pub_hex = (KEYS / f"{agent_id}.pub").read_text().strip()
    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
    data = Path(manifest_path).read_bytes()
    sig = bytes.fromhex(Path(manifest_path + ".sig").read_text().strip())
    try:
        pub.verify(sig, data)
        return True
    except InvalidSignature:
        return False


def _main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "keygen":
        print(f"public key for {argv[1]}: {keygen(argv[1])}")
    elif cmd == "sign":
        print(f"signed {argv[2]} -> {argv[2]}.sig ({sign(argv[1], argv[2])[:16]}…)")
    elif cmd == "verify":
        ok = verify(argv[1], argv[2])
        print("SIGNATURE VALID ✅" if ok else "SIGNATURE INVALID ❌ (manifest tampered or wrong key)")
        sys.exit(0 if ok else 1)
    else:
        sys.exit("usage: identity.py keygen|sign|verify <agent_id> [manifest]")


if __name__ == "__main__":
    _main(sys.argv[1:])
