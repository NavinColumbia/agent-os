#!/usr/bin/env python3
"""appregistry.py — the lifecycle registry for every app the factory builds.

portfolio.py is the P&L *view* (cost/revenue); THIS is the source-of-truth registry: per app its kind,
version, status, dependencies (tracked), README presence, its private GitHub repo URL, and dev/prod URLs
once deployed. It self-initialises its table, scans the products directory to backfill, auto-registers on
LAUNCH (wired into factory), and can publish each app to its OWN private GitHub repo (git init + deps +
README + gh repo create --private --push).

    appregistry.py scan                 # detect+upsert every product (kind/version/deps/readme/git/status)
    appregistry.py list                 # human view   |   appregistry.py json   (machine view)
    appregistry.py publish <name>       # init git, track deps, create PRIVATE GitHub repo, push, record url
    appregistry.py publish-all <n1> <n2>...   # publish the named apps
    appregistry.py set-url <name> dev|prod <url>
Run with the agent-os venv python.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402
from dbpool import connection  # noqa: E402

PRODUCTS = Path.home() / "projects" / "products"
_SKIP = {"_inbox", "noupload"}

_DDL = """
CREATE TABLE IF NOT EXISTS app_registry (
  name         text PRIMARY KEY,
  kind         text NOT NULL,
  status       text NOT NULL DEFAULT 'built',
  version      text NOT NULL DEFAULT '0.1.0',
  repo_url     text,
  dev_url      text,
  prod_url     text,
  dependencies jsonb NOT NULL DEFAULT '[]',
  has_readme   boolean NOT NULL DEFAULT false,
  last_commit  text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
"""


def _conn():
    class _PooledConn:
        def __init__(self):
            self._cm = connection()
            self._conn = self._cm.__enter__()
            self._closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._closed = True
            return self._cm.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            if not self._closed:
                self._closed = True
                self._cm.__exit__(None, None, None)

    c = _PooledConn()
    with c.cursor() as cur:
        cur.execute(_DDL)
    return c


# ── detection (the repo on disk is the ground truth) ────────────────────────────
def detect_kind(repo: Path) -> str:
    if (repo / "manifest.json").exists():
        return "extension"
    if (repo / "docs" / "PLAN.json").exists():
        return "project"
    if (repo / "index.html").exists() or next(repo.glob("*/index.html"), None):
        return "web"
    for main in repo.glob("src/*/__main__.py"):
        return "service"
    if (repo / "src").exists():
        return "lib"
    return "unknown"


def detect_version(repo: Path, kind: str) -> str:
    if kind == "extension" and (repo / "manifest.json").exists():
        try:
            return json.loads((repo / "manifest.json").read_text()).get("version", "0.1.0")
        except Exception:
            pass
    vf = repo / "VERSION"
    return vf.read_text().strip() if vf.exists() else "0.1.0"


def detect_dependencies(repo: Path) -> list:
    """Tracked dependencies with versions. The factory builds stdlib/vanilla, so this is usually 'none' —
    but it's recorded explicitly (auditable) and parses a real requirements.txt/package.json if present."""
    req = repo / "requirements.txt"
    if req.exists():
        deps = [l.strip() for l in req.read_text().splitlines() if l.strip() and not l.startswith("#")]
        if deps:
            return deps
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            return [f"{k}@{v}" for k, v in (json.loads(pkg.read_text()).get("dependencies") or {}).items()]
        except Exception:
            pass
    if next(repo.rglob("*.py"), None):
        return ["python: stdlib only"]
    if next(repo.rglob("*.js"), None):
        return ["none (vanilla JS, no external deps)"]
    return []


def detect_status(name: str, conn) -> str:
    with conn.cursor() as cur:
        cur.execute("""SELECT decision FROM audit_log
                       WHERE action IN ('ProductComplete','ProjectComplete') AND resource=%s
                       ORDER BY id DESC LIMIT 1""", (name,))
        r = cur.fetchone()
    if not r:
        return "built"
    d = r[0] or ""
    if d in ("LAUNCHED", "INTEGRATED"):
        return "launched"
    if d.startswith("BLOCKED"):
        return "blocked"
    return "built"


def git_info(repo: Path) -> tuple:
    """(repo_url, last_commit) if this product dir is a git repo with a remote, else (None, None)."""
    if not (repo / ".git").exists():
        return None, None
    def g(*a):
        p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None
    url = g("remote", "get-url", "origin")
    if url and url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:"):].removesuffix(".git")
    return url, g("rev-parse", "--short", "HEAD")


# ── registry ────────────────────────────────────────────────────────────────────
def register(name, repo: Path = None, conn=None):
    repo = repo or (PRODUCTS / name)
    own = conn is None
    conn = conn or _conn()
    try:
        kind = detect_kind(repo)
        version = detect_version(repo, kind)
        deps = detect_dependencies(repo)
        status = detect_status(name, conn)
        repo_url, last_commit = git_info(repo)
        has_readme = (repo / "README.md").exists()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_registry (name,kind,status,version,repo_url,dependencies,has_readme,last_commit,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (name) DO UPDATE SET
                  kind=EXCLUDED.kind, status=EXCLUDED.status, version=EXCLUDED.version,
                  repo_url=COALESCE(EXCLUDED.repo_url, app_registry.repo_url),
                  dependencies=EXCLUDED.dependencies, has_readme=EXCLUDED.has_readme,
                  last_commit=COALESCE(EXCLUDED.last_commit, app_registry.last_commit), updated_at=now()
            """, (name, kind, status, version, repo_url, json.dumps(deps), has_readme, last_commit))
        conn.commit()
        return {"name": name, "kind": kind, "status": status, "version": version,
                "repo_url": repo_url, "dependencies": deps, "has_readme": has_readme}
    finally:
        if own:
            conn.close()


def scan(conn=None):
    own = conn is None
    conn = conn or _conn()
    try:
        out = []
        for d in sorted(PRODUCTS.iterdir()):
            if not d.is_dir() or d.name in _SKIP or d.name.startswith("."):
                continue
            if detect_kind(d) == "unknown" and not (d / "docs").exists():
                continue
            out.append(register(d.name, d, conn))
        return out
    finally:
        if own:
            conn.close()


def as_rows(conn=None):
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT name,kind,status,version,repo_url,dev_url,prod_url,dependencies,has_readme,last_commit
                           FROM app_registry ORDER BY updated_at DESC""")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        if own:
            conn.close()


def set_url(name, which, url):
    assert which in ("dev", "prod")
    with _conn() as c, c.cursor() as cur:
        cur.execute(f"UPDATE app_registry SET {which}_url=%s, updated_at=now() WHERE name=%s", (url, name))
        c.commit()
    audit.append(actor="appregistry", action="SetUrl", resource=name, decision="updated",
                 payload={"which": which, "url": url})


# ── publish: each app -> its own PRIVATE GitHub repo, deps + README tracked ──────
_GITIGNORE = ("*.db\n*.sqlite*\n__pycache__/\n*.pyc\n.pytest_cache/\nnode_modules/\n.DS_Store\n*.log\n"
              # SECRETS — a published product repo must NEVER carry credentials.
              ".env\n.env.*\n*.pem\n*.key\nkeys/\nsecrets/\n*credentials*\nid_rsa*\n.aws/\n.ssh/\n")


def _is_secret_path(p):
    """A path that must never be committed into a published product repo (credentials/keys). Used as a
    fail-closed backstop even beyond .gitignore, since a force-add or a pre-tracked file bypasses ignore."""
    pl = "/" + p.strip().lower().lstrip("/")
    base = pl.rsplit("/", 1)[-1]
    return (base == ".env" or base.startswith(".env.") or base.endswith(".pem") or base.endswith(".key")
            or "credentials" in base or base.startswith("id_rsa")
            or "/keys/" in pl or "/secrets/" in pl or pl.startswith("/.aws/") or pl.startswith("/.ssh/"))


def _ensure_repo_hygiene(repo: Path, kind: str):
    """Make the product a self-documenting repo: .gitignore, a tracked deps file, a VERSION, a README."""
    (repo / ".gitignore").write_text(_GITIGNORE)
    if not (repo / "VERSION").exists():
        (repo / "VERSION").write_text(detect_version(repo, kind) + "\n")
    is_py = next(repo.rglob("*.py"), None) is not None
    if is_py and not (repo / "requirements.txt").exists():
        (repo / "requirements.txt").write_text("# stdlib-only — no external runtime dependencies\n")
    if not (repo / "README.md").exists():
        (repo / "README.md").write_text(f"# {repo.name}\n\nAn {kind} built by the agent-os factory.\n")


def publish(name, private=True, role="controller", approval_id=None):
    """Init git (if needed), track deps/version/README, create a GitHub repo, push, record the URL.
    GATED action (creates a repo + pushes). A PUBLIC publish is high-blast-radius (it exposes the product
    to the world), so REBUILD-PLAN C4 makes the human-approval gate actually FIRE here: require_approval
    fail-closes a public publish unless a hash-pinned, single-use human approval exists (private publish is
    ungated). This is the first real call site of the approval gate that was defined but never invoked."""
    repo = PRODUCTS / name
    if not repo.exists():
        return {"name": name, "error": "no such product"}
    if not private:                                   # public publish -> require a verified human approval
        try:
            import governance
            governance.require_approval(role, "public_post", {"publish": name, "public": True},
                                        approval_id=approval_id)
        except PermissionError as e:
            return {"name": name, "error": f"public publish blocked — needs human approval: {e}", "blocked": True}
    kind = detect_kind(repo)
    _ensure_repo_hygiene(repo, kind)
    def git(*a, check=True):
        p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
        if check and p.returncode != 0 and "nothing to commit" not in (p.stdout + p.stderr):
            raise RuntimeError(f"git {' '.join(a)}: {(p.stderr or p.stdout)[:200]}")
        return p
    if not (repo / ".git").exists():
        git("init", "-q")
        git("symbolic-ref", "HEAD", "refs/heads/main", check=False)
    git("add", "-A")
    # FAIL-CLOSED secret guard: even with .gitignore, never publish a credential — unstage any secret-shaped
    # file that slipped into the index (force-add / pre-tracked from an earlier commit) before it's committed.
    staged = (git("diff", "--cached", "--name-only", check=False).stdout or "").splitlines()
    leaks = [f for f in staged if _is_secret_path(f)]
    for f in leaks:
        git("rm", "--cached", "-q", "--", f, check=False)
    if leaks:
        try:
            audit.append(actor="appregistry", action="PublishSecretsStripped", resource=name,
                         decision="stripped", payload={"files": leaks[:20]})
        except Exception:
            pass
    git("commit", "-q", "-m", f"{name} {detect_version(repo, kind)} — built by agent-os factory\n\n"
        f"Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>", check=False)
    has_remote = git("remote", "get-url", "origin", check=False).returncode == 0
    if not has_remote:
        vis = "--private" if private else "--public"
        p = subprocess.run(["gh", "repo", "create", name, vis, "--source", str(repo),
                            "--remote", "origin", "--push"], capture_output=True, text=True)
        if p.returncode != 0:
            return {"name": name, "error": f"gh repo create failed: {(p.stderr or p.stdout)[:200]}"}
    else:
        git("push", "-u", "origin", "HEAD", check=False)
    repo_url, last_commit = git_info(repo)
    # item 13: a PROVABLE effect record for the publish (a real external side-effect). The git commit SHA IS a
    # tamper-evident content identity, so it doubles as the idempotency key — a re-publish of the same tree is
    # recognisable. Best-effort; never blocks the publish.
    try:
        audit.append(actor=f"publish:{name}", action="Effect:git_publish",
                     resource=repo_url or str(repo),
                     payload={"commit": last_commit, "idempotency_key": last_commit,
                              "repo_url": repo_url, "private": bool(private)})
    except Exception:
        pass
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE app_registry SET repo_url=%s, last_commit=%s, status='published', updated_at=now()
                       WHERE name=%s""", (repo_url, last_commit, name))
        if cur.rowcount == 0:
            register(name, repo)
            cur.execute("UPDATE app_registry SET repo_url=%s, last_commit=%s, status='published' WHERE name=%s",
                        (repo_url, last_commit, name))
        c.commit()
    audit.append(actor="appregistry", action="PublishRepo", resource=name, decision="published",
                 payload={"repo_url": repo_url, "private": private})
    return {"name": name, "repo_url": repo_url, "last_commit": last_commit, "status": "published"}


def _print_table(rows):
    if not rows:
        print("(registry empty — run `appregistry.py scan`)"); return
    print(f"{'APP':22} {'KIND':10} {'STATUS':10} {'VER':8} {'README':7} {'REPO'}")
    for r in rows:
        print(f"{r['name'][:21]:22} {r['kind']:10} {r['status']:10} {r['version']:8} "
              f"{'yes' if r['has_readme'] else 'NO':7} {r['repo_url'] or '(local only)'}")
        deps = r["dependencies"]
        print(f"{'':22} deps: {', '.join(deps) if deps else 'none'}"
              + (f"  dev:{r['dev_url']}" if r.get('dev_url') else "")
              + (f"  prod:{r['prod_url']}" if r.get('prod_url') else ""))


def _main(a):
    if not a or a[0] == "list":
        _print_table(as_rows())
    elif a[0] == "scan":
        n = scan(); print(f"scanned {len(n)} apps"); _print_table(as_rows())
    elif a[0] == "json":
        print(json.dumps(as_rows(), default=str, indent=2))
    elif a[0] == "publish":
        print(publish(a[1]))
    elif a[0] == "publish-all":
        for name in a[1:]:
            print(publish(name))
    elif a[0] == "set-url":
        set_url(a[1], a[2], a[3]); print("ok")
    else:
        sys.exit(f"unknown command: {a[0]}")


if __name__ == "__main__":
    _main(sys.argv[1:])
