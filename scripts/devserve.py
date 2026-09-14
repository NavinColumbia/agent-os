#!/usr/bin/env python3
"""devserve.py — run the factory's RUNNABLE apps locally so the owner can reach them in a browser.

Web apps are served as static sites; service apps boot their stdlib server. Each gets a STABLE localhost
port (persisted as the app's dev_url in the registry), binds 127.0.0.1 ONLY (never 0.0.0.0), runs detached
with a pidfile + single-instance guard, and logs to /tmp. `up-all` is idempotent — a tick / recover.sh can
re-run it to keep everything up. Libs and extensions have no URL (not browser-reachable), so they're skipped.

    devserve.py up-all            # bring up every runnable app, record dev_urls
    devserve.py up <name>         # one app
    devserve.py status            # what's up + the URLs
    devserve.py down <name> | down-all
Run with the agent-os venv python.
"""
import os
import socket
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import appregistry as ar  # noqa: E402

from aoscfg import VENV_PY
PRODUCTS = ar.PRODUCTS
BASE_PORT = 8810


def _pidfile(name):
    return Path(f"/tmp/devserve-{name}.pid")


def _alive(pid):
    try:
        import os
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _running(name):
    pf = _pidfile(name)
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
            if _alive(pid):
                return pid
        except Exception:
            pass
    return None


def _port_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _assign_port(name, kind, conn=None):
    """Stable per-app port: reuse the one already stored in dev_url, else pick the next free from BASE."""
    rows = {r["name"]: r for r in ar.as_rows()}
    cur = rows.get(name, {}).get("dev_url")
    if cur and ":" in cur:
        try:
            return int(cur.rsplit(":", 1)[1].rstrip("/"))
        except Exception:
            pass
    used = set()
    for r in rows.values():
        if r.get("dev_url") and ":" in r["dev_url"]:
            try:
                used.add(int(r["dev_url"].rsplit(":", 1)[1].rstrip("/")))
            except Exception:
                pass
    p = BASE_PORT
    while p in used or _port_listening(p):
        p += 1
    return p


def _web_root(repo: Path) -> str:
    if (repo / "index.html").exists():
        return str(repo)
    idx = next(repo.rglob("index.html"), None)
    return str(idx.parent) if idx else str(repo)


def up(name):
    repo = PRODUCTS / name
    if not repo.exists():
        return {"name": name, "error": "no such product"}
    kind = ar.detect_kind(repo)
    # A run.sh IS a declared deployable run-target (serves the connected app at one origin) — it's browser-
    # reachable regardless of the registry 'kind' (a hierarchical web app is kind='project' but still a real,
    # runnable, connected app). Honor it. Only genuinely non-servable kinds with no run-target are skipped.
    has_runsh = (repo / "run.sh").exists()
    if kind not in ("web", "service") and not has_runsh:
        return {"name": name, "skipped": f"{kind} is not browser-reachable"}
    pid = _running(name)
    port = _assign_port(name, kind if kind in ("web", "service") else "web")
    url = f"http://127.0.0.1:{port}"
    if pid and _port_listening(port):
        ar.set_url(name, "dev", url)
        return {"name": name, "already_up": True, "url": url, "pid": pid}
    log = open(f"/tmp/devserve-{name}.log", "a")
    import os
    # GRAND-SCHEME / END-TO-END (the deployable-unit rule): a web product that needs a backend must ship a
    # RUN-TARGET (run.sh) that serves the app CONNECTED to its backend at ONE origin — so visiting the URL
    # shows the working app exactly as a user would see it deployed. QA must run THAT, not raw static files
    # (static-serving a same-origin SPA 404s every /api/* call → the app can't load data → every story blocks).
    # Prefer run.sh over everything else; it is the honest "as-deployed" surface.
    runsh = repo / "run.sh"
    if runsh.exists():
        env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}
        proc = subprocess.Popen(["bash", str(runsh)], cwd=str(repo), env=env, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        _pidfile(name).write_text(str(proc.pid))
        import time
        ok = any(_port_listening(port) or time.sleep(0.5) for _ in range(60))
        if ok:
            ar.set_url(name, "dev", url)
        return {"name": name, "kind": "run.sh", "url": url if ok else None, "pid": proc.pid,
                "up": ok, **({} if ok else {"error": "run.sh did not bind PORT — see /tmp/devserve-%s.log" % name})}
    # NODE/JS app with its OWN server (F11): a package.json `start` script means the app runs a real backend
    # (API + SPA), so RUN it — a Python static file server would 404 every /api/* call. This is the serving
    # counterpart to the stack-aware VERIFIER (F10): the QA harness must bring an app up in its own language.
    if (repo / "package.json").exists():
        try:
            import json as _json
            pkg = _json.loads((repo / "package.json").read_text())
        except Exception:
            pkg = {}
        if (pkg.get("scripts") or {}).get("start"):
            env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}
            cmd = ["npm", "start", "--silent"]
            cwd = str(repo)
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            _pidfile(name).write_text(str(proc.pid))
            import time
            ok = any(_port_listening(port) or time.sleep(0.5) for _ in range(60))   # node start can take longer
            if ok:
                ar.set_url(name, "dev", url)
            return {"name": name, "kind": "node", "url": url if ok else None, "pid": proc.pid,
                    "up": ok, **({} if ok else {"error": "node app did not start — see /tmp/devserve-%s.log" % name})}
    if kind == "web":
        cmd = [VENV_PY, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", _web_root(repo)]
        cwd = str(repo)
        env = None
    else:  # service: boot its stdlib server on PORT, persistent SQLite under the repo
        pkg = name.replace("-", "_")
        import os
        env = {**os.environ, "PORT": str(port), "PYTHONPATH": str(repo),
               "SQLITE_PATH": str(repo / f"{pkg}.dev.db")}
        cmd = [VENV_PY, "-m", f"src.{pkg}"]
        cwd = str(repo)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    _pidfile(name).write_text(str(proc.pid))
    # confirm it came up
    import time
    ok = False
    for _ in range(20):
        if _port_listening(port):
            ok = True
            break
        time.sleep(0.3)
    if ok:
        ar.set_url(name, "dev", url)
    return {"name": name, "kind": kind, "url": url if ok else None, "pid": proc.pid,
            "up": ok, **({} if ok else {"error": "did not start — see /tmp/devserve-%s.log" % name})}


def up_all():
    """Optionally restore a bounded recent working set, never every historical generated product.

    Automatic recovery defaults to zero. Products start on demand through `up <name>`/QA. A dedicated host
    may opt into restoring N recent apps with AOS_DEVSERVE_MAX_ACTIVE=N.
    """
    out = []
    limit = max(0, int(os.environ.get("AOS_DEVSERVE_MAX_ACTIVE", "0")))
    if limit == 0:
        return out
    for r in ar.as_rows():
        if r["kind"] in ("web", "service") and not r["name"].startswith(("ev-", "ut-", "dbg-", "st-")):
            if len(out) >= limit:
                break
            out.append(up(r["name"]))
    return out


def down(name):
    pid = _running(name)
    if pid:
        import os
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    _pidfile(name).unlink(missing_ok=True)
    return {"name": name, "down": True}


def status():
    rows = []
    for r in ar.as_rows():
        if r["kind"] not in ("web", "service"):
            continue
        pid = _running(r["name"])
        port = None
        if r.get("dev_url") and ":" in r["dev_url"]:
            try:
                port = int(r["dev_url"].rsplit(":", 1)[1].rstrip("/"))
            except Exception:
                pass
        up_now = bool(pid and port and _port_listening(port))
        rows.append({"name": r["name"], "kind": r["kind"], "url": r.get("dev_url"),
                     "up": up_now, "pid": pid})
    return rows


def _main(a):
    if not a or a[0] == "status":
        for r in status():
            print(f"  {'UP  ' if r['up'] else 'down'} {r['name'][:20]:20} {r['kind']:8} {r['url'] or '(no url)'}")
    elif a[0] == "up-all":
        for r in up_all():
            print(r)
    elif a[0] == "up":
        print(up(a[1]))
    elif a[0] == "down":
        print(down(a[1]))
    elif a[0] == "down-all":
        for r in status():
            print(down(r["name"]))
    else:
        sys.exit(f"unknown command: {a[0]}")


if __name__ == "__main__":
    _main(sys.argv[1:])
