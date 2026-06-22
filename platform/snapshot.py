#!/usr/bin/env python3
"""snapshot.py — one encrypted, portable file that IS your agent-os.

Captures everything that can't be re-derived from git: the full Postgres dump (durable exec, audit,
comms, memory, metrics, vault, blobs, skills — every embedded store), the signing keys, the local
config/secrets, the provenance, and the exact git commit of agent-os + control-plane. It all goes into
a single AES/Fernet-encrypted `.aosnap` file you can store offline or carry to a new laptop / cloud VM.

Encryption: passphrase -> PBKDF2-HMAC-SHA256(600k) -> Fernet key. The 16-byte salt is the file header.
Never put the passphrase on the command line — pass it via an env var.

    AOSNAP_PASS=... ./snapshot.py export                 # -> backups/agent-os-<ts>.aosnap
    AOSNAP_PASS=... ./snapshot.py import <file.aosnap>    # decrypt + stage + print restore steps
    AOSNAP_PASS=... ./snapshot.py import <file> --apply   # also pg_restore into the running DB
    ./snapshot.py selftest                                # crypto round-trip (no DB/docker)
Run with the agent-os venv python (needs cryptography).
"""
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path.home() / "projects" / "agent-os"
CP = Path.home() / "projects" / "control-plane"
ENV = ROOT / ".env.local"
ITER = 600_000
MAGIC = b"AOSNAP1\n"


def _key(passphrase: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER)
    return base64.urlsafe_b64encode(kdf.derive(passphrase))


def _passphrase() -> bytes:
    # 1) env var (interactive use); 2) AOSNAP_PASS= in gitignored .env.local (unattended/scheduled).
    p = os.environ.get("AOSNAP_PASS")
    if not p and ENV.exists():
        for l in ENV.read_text().splitlines():
            if l.strip().startswith("AOSNAP_PASS="):
                p = l.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not p:
        sys.exit("no passphrase — set AOSNAP_PASS env var or AOSNAP_PASS= in .env.local")
    return p.encode()


KEEP = 14  # retain the most recent N snapshots locally; older ones are pruned each export


def _prune(keep=KEEP):
    snaps = sorted((ROOT / "backups").glob("agent-os-*.aosnap"))
    for old in snaps[:-keep] if len(snaps) > keep else []:
        old.unlink(missing_ok=True)
        print(f"   pruned old snapshot {old.name}")


def _db_password() -> str:
    for l in ENV.read_text().splitlines():
        if l.startswith("DATABASE_URL=") and "@" in l:
            return l.split("//", 1)[1].split("@", 1)[0].split(":", 1)[1]
    raise RuntimeError("DATABASE_URL with a password not found in .env.local")


def _docker(args, **kw):
    direct = subprocess.run(["docker", "info"], capture_output=True)
    cmd = ["docker", *args] if direct.returncode == 0 else ["sg", "docker", "-c", "docker " + " ".join(args)]
    return subprocess.run(cmd, **kw)


def _git_sha(repo: Path) -> str:
    if not (repo / ".git").exists():
        return "(not a git repo)"
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or "(unknown)"


def export_snapshot():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stage = Path(tempfile.mkdtemp())
    # 1) full DB dump (custom format)
    pw = _db_password()
    dump = stage / "agentos.dump"
    with dump.open("wb") as f:
        p = _docker(["exec", "-e", f"PGPASSWORD={pw}", "agentos-postgres",
                     "pg_dump", "-U", "agentos", "-Fc", "agentos"], stdout=f, stderr=subprocess.PIPE)
    if p.returncode != 0:
        sys.exit(f"pg_dump failed: {p.stderr.decode()[:300]}")
    # 2) irreplaceable secrets/config + provenance
    for rel in [".env.local", "postgres/.env", "ntfy/.env", "PROVENANCE.json"]:
        src = ROOT / rel
        if src.exists():
            dst = stage / "files" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    if (ROOT / "keys").exists():
        shutil.copytree(ROOT / "keys", stage / "files" / "keys")
    # 3) manifest — what this is + how to rebuild the code around it
    manifest = {
        "schema": "aosnap/1",
        "created_utc": ts,
        "db_dump_bytes": dump.stat().st_size,
        "git": {"agent-os": _git_sha(ROOT), "control-plane": _git_sha(CP)},
        "contains": ["full Postgres dump (all embedded stores)", ".env.local", "postgres/.env",
                     "ntfy/.env", "keys/ (Ed25519 signing)", "PROVENANCE.json"],
        "restore": "snapshot.py import <file> --apply  (then platform/rebuild.sh on the new box)",
    }
    (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    # 4) tar -> encrypt -> .aosnap
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(stage, arcname=".")
    salt = os.urandom(16)
    token = Fernet(_key(_passphrase(), salt)).encrypt(buf.getvalue())
    out = ROOT / "backups" / f"agent-os-{ts}.aosnap"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(MAGIC + salt + token)
    shutil.rmtree(stage, ignore_errors=True)
    _prune()
    mb = out.stat().st_size / 1e6
    print(f"✅ snapshot -> {out} ({mb:.1f} MB, encrypted)")
    print(f"   git: agent-os@{manifest['git']['agent-os'][:10]} control-plane@{manifest['git']['control-plane'][:10]}")
    print("   ⚠ encrypted with AOSNAP_PASS — store the passphrase separately; without it this file is unrecoverable.")
    return out


def _decrypt(path: Path) -> bytes:
    raw = Path(path).read_bytes()
    if not raw.startswith(MAGIC):
        sys.exit("not an .aosnap file (bad magic)")
    salt, token = raw[len(MAGIC):len(MAGIC) + 16], raw[len(MAGIC) + 16:]
    try:
        return Fernet(_key(_passphrase(), salt)).decrypt(token)
    except Exception:
        sys.exit("decrypt failed — wrong AOSNAP_PASS or corrupted file")


def import_snapshot(path, apply=False):
    data = _decrypt(Path(path))
    stage = Path(tempfile.mkdtemp())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(stage)
    manifest = json.loads((stage / "MANIFEST.json").read_text())
    print("snapshot manifest:")
    print(json.dumps(manifest, indent=2))
    print(f"\nstaged at: {stage}")
    if not apply:
        print("\nTo restore on this box:")
        print(f"  1) copy {stage}/files/* into ~/projects/agent-os/ (keys, .env.local, postgres/.env, ntfy/.env)")
        print("  2) bash platform/rebuild.sh        # brings stack up with these secrets")
        print(f"  3) re-run with --apply to pg_restore {stage}/agentos.dump into the running DB")
        return
    # --apply: restore files + DB into the running container
    files = stage / "files"
    for rel in [".env.local", "postgres/.env", "ntfy/.env", "PROVENANCE.json"]:
        src = files / rel
        if src.exists():
            (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, ROOT / rel)
    if (files / "keys").exists():
        shutil.rmtree(ROOT / "keys", ignore_errors=True)
        shutil.copytree(files / "keys", ROOT / "keys")
    print("  ✓ restored secrets/config/keys")
    pw = _db_password()
    dump = stage / "agentos.dump"
    cp = _docker(["cp", str(dump), "agentos-postgres:/tmp/agentos.dump"])
    rp = _docker(["exec", "-e", f"PGPASSWORD={pw}", "agentos-postgres",
                  "pg_restore", "--clean", "--if-exists", "-U", "agentos", "-d", "agentos", "/tmp/agentos.dump"],
                 capture_output=True, text=True)
    # pg_restore returns non-zero on benign "already exists"/ownership notices; report tail either way
    print("  ✓ pg_restore complete" if rp.returncode == 0 else f"  ! pg_restore finished with notices: {rp.stderr[-200:]}")
    print("\nDone. Run scripts/selftest.sh to verify, scripts/provenance.py verify to check signatures.")


def selftest():
    # crypto round-trip only — proves the portable-encryption path without touching docker/DB
    os.environ.setdefault("AOSNAP_PASS", "selftest-passphrase")
    payload = os.urandom(4096)
    salt = os.urandom(16)
    f = Fernet(_key(_passphrase(), salt))
    blob = MAGIC + salt + f.encrypt(payload)
    salt2, token = blob[len(MAGIC):len(MAGIC) + 16], blob[len(MAGIC) + 16:]
    back = Fernet(_key(_passphrase(), salt2)).decrypt(token)
    ok = back == payload
    # wrong passphrase must fail
    try:
        Fernet(_key(b"wrong", salt2)).decrypt(token); wrong_ok = False
    except Exception:
        wrong_ok = True
    print(f"crypto round-trip {'ok' if ok else 'FAIL'}; wrong-pass-rejected {'ok' if wrong_ok else 'FAIL'}")
    print("PASS: portable encrypted snapshot ✅" if (ok and wrong_ok) else "FAIL")
    sys.exit(0 if (ok and wrong_ok) else 1)


def _main(a):
    if not a or a[0] == "export":
        export_snapshot()
    elif a[0] == "import":
        if len(a) < 2:
            sys.exit("usage: snapshot.py import <file.aosnap> [--apply]")
        import_snapshot(a[1], apply="--apply" in a)
    elif a[0] == "selftest":
        selftest()
    else:
        sys.exit("usage: snapshot.py export|import|selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
