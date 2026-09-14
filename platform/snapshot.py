#!/usr/bin/env python3
"""snapshot.py — one encrypted, portable file that IS your agent-os.

Captures everything that can't be re-derived from git: the full Postgres dump (durable exec, audit,
comms, memory, metrics, vault, blobs, skills — every embedded store), the signing keys, the local
config/secrets, provenance, generated product source repositories, and the exact git commit of agent-os +
control-plane. Reproducible dependency/build caches are excluded. It all goes into a single AES/Fernet-
encrypted `.aosnap` file you can store offline or carry to a new laptop / cloud VM.

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
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(os.environ.get("AOS_ROOT") or Path(__file__).resolve().parents[1]).expanduser().resolve()
CP = Path(os.environ.get("AOS_CONTROL_PLANE_ROOT") or ROOT.parent / "control-plane").expanduser().resolve()
PRODUCTS = Path(os.environ.get("AOS_PRODUCTS_DIR") or ROOT.parent / "products").expanduser().resolve()
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


def _prune(directory=None, keep=KEEP):
    snaps = sorted(Path(directory or ROOT / "backups").glob("agent-os-*.aosnap"))
    for old in snaps[:-keep] if len(snaps) > keep else []:
        old.unlink(missing_ok=True)
        print(f"   pruned old snapshot {old.name}")


def _cfg(key):
    """Read a key from env, else from gitignored .env.local."""
    v = os.environ.get(key)
    if not v and ENV.exists():
        for l in ENV.read_text().splitlines():
            if l.strip().startswith(f"{key}="):
                return l.split("=", 1)[1].strip().strip('"').strip("'")
    return v


def _offsite(path, keep=KEEP):
    """Copy to a configured off-host mount, then optionally to the developer Mac bridge.

    A configured generic destination is strict: silently retaining only the same-host copy would report a
    false disaster-recovery success. The legacy Mac bridge remains best-effort because laptops sleep.
    """
    if os.environ.get("AOSNAP_SKIP_OFFSITE", "").strip().lower() in ("1", "true", "yes"):
        return
    offsite_dir = _cfg("AOSNAP_OFFSITE_DIR")
    if offsite_dir and "CHANGE-ME" not in offsite_dir:
        target_dir = Path(offsite_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / Path(path).name
        temp_target = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.copy2(path, temp_target)
        temp_target.replace(target)
        _prune(target_dir, keep)
        print(f"   off-site copy -> {target_dir} (kept newest {keep})")
    mac_dir = _cfg("AOSNAP_MAC_DIR")
    if not mac_dir:
        return
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import mac_runner
        r = mac_runner.push(str(path), mac_dir)
        if r["rc"] == 0:
            # keep newest `keep`, delete the rest on the Mac
            mac_runner.run(f"ls -t {mac_dir}/agent-os-*.aosnap 2>/dev/null | tail -n +{keep + 1} | xargs -r rm -f")
            print(f"   off-site copy -> Mac:{mac_dir} (kept newest {keep})")
        else:
            print(f"   ! off-site copy skipped (Mac scp rc={r['rc']}: {r['err'][:80]})")
    except Exception as e:
        print(f"   ! off-site copy skipped (Mac unreachable): {str(e)[:100]}")


def _docker(args, **kw):
    direct = subprocess.run(["docker", "info"], capture_output=True)
    cmd = ["docker", *args] if direct.returncode == 0 else ["sg", "docker", "-c", "docker " + " ".join(args)]
    return subprocess.run(cmd, **kw)


def _git_sha(repo: Path) -> str:
    if not (repo / ".git").exists():
        return "(not a git repo)"
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or "(unknown)"


_PRODUCT_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "dist", "build", "coverage",
                      ".next", "playwright-report", "test-results"}


def _product_tar_filter(info):
    """Keep source, tests, config, and git history; omit reproducible dependency/build caches."""
    if any(part in _PRODUCT_SKIP_DIRS for part in Path(info.name).parts):
        return None
    return info


def _snapshot_extract_filter(info, destination):
    """Apply Python's hardened data filter while tolerating only legacy dependency-cache entries.

    Snapshots created before ``venv`` joined ``_PRODUCT_SKIP_DIRS`` can contain host-specific absolute
    interpreter symlinks. Those environments are reproducible and must never be restored. Skip the whole
    known cache namespace before ``tarfile.data_filter`` examines links; unsafe links anywhere in retained
    source/config still raise and fail the restore rather than silently weakening extraction safety.
    """
    if any(part in _PRODUCT_SKIP_DIRS for part in Path(info.name).parts):
        return None
    return tarfile.data_filter(info, destination)


def export_snapshot(output_dir=None):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stage = Path(tempfile.mkdtemp())
    # 1) full DB dump (custom format)
    dump = stage / "agentos.dump"
    with dump.open("wb") as f:
        # Container-local socket authentication needs no password. Never expose the DB password in the host
        # argv via `docker exec -e PGPASSWORD=...`, where another same-host user could read it with ps.
        p = _docker(["exec", "agentos-postgres", "pg_dump", "-U", "agentos", "-Fc", "agentos"],
                    stdout=f, stderr=subprocess.PIPE)
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
    product_names = sorted(path.name for path in PRODUCTS.iterdir()) if PRODUCTS.is_dir() else []
    manifest = {
        "schema": "aosnap/1",
        "created_utc": ts,
        "db_dump_bytes": dump.stat().st_size,
        "git": {"agent-os": _git_sha(ROOT), "control-plane": _git_sha(CP)},
        "contains": ["full Postgres dump (all embedded stores)", ".env.local", "postgres/.env",
                     "ntfy/.env", "keys/ (Ed25519 signing)", "PROVENANCE.json",
                     "product source repositories (dependency/build caches excluded)"],
        "products_root": str(PRODUCTS),
        "products": product_names,
        "restore": "snapshot.py import <file> --apply  (then platform/rebuild.sh on the new box)",
    }
    (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    # 4) tar -> encrypt -> .aosnap
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(stage, arcname=".")
        if PRODUCTS.is_dir():
            tar.add(PRODUCTS, arcname="./products", filter=_product_tar_filter)
    salt = os.urandom(16)
    token = Fernet(_key(_passphrase(), salt)).encrypt(buf.getvalue())
    out_dir = Path(output_dir).expanduser().resolve() if output_dir else ROOT / "backups"
    out = out_dir / f"agent-os-{ts}.aosnap"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(MAGIC + salt + token)
    shutil.rmtree(stage, ignore_errors=True)
    _prune(out_dir)
    _offsite(out)
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
        tar.extractall(stage, filter=_snapshot_extract_filter)
    manifest = json.loads((stage / "MANIFEST.json").read_text())
    print("snapshot manifest:")
    print(json.dumps(manifest, indent=2))
    print(f"\nstaged at: {stage}")
    if not apply:
        print("\nTo restore on this box:")
        print(f"  1) copy {stage}/files/* into {ROOT}/ (keys, .env.local, postgres/.env, ntfy/.env)")
        print(f"     and {stage}/products/* into {PRODUCTS}/")
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
    restored_products = stage / "products"
    if restored_products.is_dir():
        PRODUCTS.mkdir(parents=True, exist_ok=True)
        for item in restored_products.iterdir():
            target = PRODUCTS / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        print(f"  ✓ restored product source repositories -> {PRODUCTS}")
    dump = stage / "agentos.dump"
    cp = _docker(["cp", str(dump), "agentos-postgres:/tmp/agentos.dump"])
    rp = _docker(["exec", "agentos-postgres", "pg_restore", "--clean", "--if-exists",
                  "-U", "agentos", "-d", "agentos", "/tmp/agentos.dump"],
                 capture_output=True, text=True)
    # pg_restore returns non-zero on benign "already exists"/ownership notices; report tail either way
    print("  ✓ pg_restore complete" if rp.returncode == 0 else f"  ! pg_restore finished with notices: {rp.stderr[-200:]}")
    print("\nDone. Run scripts/selftest.sh to verify, scripts/provenance.py verify to check signatures.")


def verify_snapshot(path, restore_drill=False):
    """Decrypt and validate an actual snapshot; optionally restore it into an isolated throwaway database.

    A successful export is not a backup proof until pg_restore can read it. The drill never touches `agentos`:
    it creates an exact uniquely-named database, checks the restored schema, then force-drops only that DB.
    """
    data = _decrypt(Path(path))
    stage = Path(tempfile.mkdtemp(prefix="aosnap-verify-"))
    dbname = f"agentos_verify_{os.getpid()}_{int(time.time())}"
    container_dump = f"/tmp/{dbname}.dump"
    created = False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(stage, filter=_snapshot_extract_filter)
        manifest_path = stage / "MANIFEST.json"
        dump = stage / "agentos.dump"
        if not manifest_path.exists() or not dump.exists() or dump.stat().st_size <= 0:
            raise RuntimeError("snapshot is missing MANIFEST.json or a non-empty agentos.dump")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema") != "aosnap/1":
            raise RuntimeError(f"unsupported snapshot schema: {manifest.get('schema')!r}")
        cp = _docker(["cp", str(dump), f"agentos-postgres:{container_dump}"], capture_output=True, text=True)
        if cp.returncode != 0:
            raise RuntimeError(f"could not stage dump in Postgres container: {cp.stderr[:200]}")
        listing = _docker(["exec", "agentos-postgres", "pg_restore", "--list", container_dump],
                          capture_output=True, text=True)
        if listing.returncode != 0 or "TABLE" not in listing.stdout:
            raise RuntimeError(f"pg_restore could not read archive: {listing.stderr[:200]}")
        tables = None
        if restore_drill:
            mk = _docker(["exec", "agentos-postgres", "createdb", "-U", "agentos", dbname],
                         capture_output=True, text=True)
            if mk.returncode != 0:
                raise RuntimeError(f"could not create restore-drill DB: {mk.stderr[:200]}")
            created = True
            restored = _docker(["exec", "agentos-postgres", "pg_restore", "--no-owner", "-U", "agentos",
                                "-d", dbname, container_dump], capture_output=True, text=True)
            if restored.returncode != 0:
                raise RuntimeError(f"restore drill failed: {restored.stderr[-300:]}")
            probe = _docker(["exec", "agentos-postgres", "psql", "-U", "agentos", "-d", dbname,
                             "-Atc", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"],
                            capture_output=True, text=True)
            if probe.returncode != 0 or int((probe.stdout or "0").strip() or 0) <= 0:
                raise RuntimeError(f"restored database schema probe failed: {probe.stderr[:200]}")
            tables = int(probe.stdout.strip())
        print(json.dumps({"ok": True, "snapshot": str(Path(path).resolve()),
                          "created_utc": manifest.get("created_utc"), "dump_bytes": dump.stat().st_size,
                          "archive_entries": len(listing.stdout.splitlines()),
                          "restore_drill": bool(restore_drill), "restored_public_tables": tables}, indent=2))
        return True
    finally:
        if created:
            _docker(["exec", "agentos-postgres", "dropdb", "--if-exists", "--force", "-U", "agentos", dbname],
                    capture_output=True)
        _docker(["exec", "agentos-postgres", "rm", "-f", container_dump], capture_output=True)
        shutil.rmtree(stage, ignore_errors=True)


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
        export_snapshot(a[1] if len(a) > 1 else None)
    elif a[0] == "import":
        if len(a) < 2:
            sys.exit("usage: snapshot.py import <file.aosnap> [--apply]")
        import_snapshot(a[1], apply="--apply" in a)
    elif a[0] == "verify":
        if len(a) < 2:
            sys.exit("usage: snapshot.py verify <file.aosnap> [--restore-drill]")
        sys.exit(0 if verify_snapshot(a[1], restore_drill="--restore-drill" in a) else 1)
    elif a[0] == "selftest":
        selftest()
    else:
        sys.exit("usage: snapshot.py export|import|verify|selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
