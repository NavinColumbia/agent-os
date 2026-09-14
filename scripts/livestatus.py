#!/usr/bin/env python3
"""livestatus.py — "is it actually LIVE, and what's the link?" for every product a tenant owns.

The cockpit shows LAUNCHED/building/failed — the lifecycle state — but never answers the CEO's first
question about a finished product: *is it running right now, and where do I click?* This is that view.
For each of a tenant's products it pulls the registered kind + dev_url/prod_url + lifecycle status from
appregistry, picks the best clickable URL (prod first, else dev), and does a SHORT, SAFE reachability
probe (2s HTTP HEAD/GET, localhost/tailnet/registered urls only — never crawls arbitrary hosts).

  • live_status(tid)        — every product: kind, result, dev_url, prod_url, running, reachable, url
  • one(tid, product)       — ownership-checked single product (or {"error":"not your product"})

    livestatus.py json <tenant_id>      # the live view on the CLI
    livestatus.py selftest
Run with the agent-os venv python. NO web server — read-only surface other panes/CLI consume.
"""
import ipaddress
import json
import socket
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import appregistry  # noqa: E402
import audit        # noqa: E402
import workstreamview  # noqa: E402

from dbpool import connection, tenant_connection

TIMEOUT = 2.0
# only these hosts are probed — never crawl arbitrary external hosts
_SAFE_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _safe_host(url):
    """A url we're allowed to probe: localhost/loopback, a tailnet host (*.ts.net / 100.x), or a
    private LAN address. Anything else (public internet) we record but do NOT hit."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in _SAFE_HOSTS:
        return True
    if host.endswith(".ts.net"):           # tailscale MagicDNS
        return True
    if host.startswith("100."):            # tailscale CGNAT range is ONLY 100.64.0.0/10
        try:                               # — NOT the whole 100.0.0.0/8 (rest is public)
            if ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10"):
                return True
        except ValueError:
            pass
    if host.startswith(("10.", "192.168.")):
        return True
    if host.startswith("172."):            # 172.16.0.0/12 private range
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


def _reachable(url):
    """SHORT, SAFE probe: HEAD (fallback GET) with a 2s timeout. Any 2xx/3xx/401/403 == reachable.
    Only hits safe hosts; wrapped so it can NEVER block long or crash the caller."""
    if not url or not _safe_host(url):
        return False
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return 200 <= resp.status < 400
        except urllib.error.HTTPError as e:
            # the host answered — auth/forbidden still means it's LIVE
            if e.code in (401, 403):
                return True
            if 200 <= e.code < 400:
                return True
            continue
        except (socket.timeout, TimeoutError):
            return False
        except Exception:
            # HEAD not allowed / connection refused / bad url — try GET, then give up
            continue
    return False


def _products(cur, tid):
    cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s ORDER BY created_at DESC", (tid,))
    return [r[0] for r in cur.fetchall()]


def _live_row(product, reg):
    """Build the live-status row for one product from its registry row (or None if unregistered)."""
    reg = reg or {}
    kind = reg.get("kind")
    dev_url = reg.get("dev_url")
    prod_url = reg.get("prod_url")
    result = reg.get("status")            # appregistry lifecycle: built / launched / published / blocked
    url = prod_url or dev_url             # best clickable url: prefer prod, else dev
    reachable = _reachable(url)
    return {
        "product": product,
        "kind": kind,
        "result": result,
        "dev_url": dev_url,
        "prod_url": prod_url,
        # "running": there IS a url to hit (it's been deployed/served) and it answered;
        # "reachable": the probe succeeded. We keep both keys; for a single safe probe they align.
        "running": bool(url) and reachable,
        "reachable": reachable,
        "url": url,
    }


def live_status(tid):
    """Live status + best clickable url for each of the tenant's products."""
    with tenant_connection(tid) as c, c.cursor() as cur:
        prods = _products(cur, tid)
    rows = []
    reg = {r["name"]: r for r in appregistry.as_rows()}
    rows.extend(_live_row(p, reg.get(p)) for p in prods)
    seen = set(prods)
    for w in workstreamview.active_workstreams(tid):
        product = w.get("product")
        if product and product in seen:
            continue
        rows.append({
            "product": product or w["label"],
            "kind": w.get("job"),
            "result": w.get("phase"),
            "dev_url": None,
            "prod_url": None,
            "running": bool(w.get("running")),
            "reachable": False,
            "url": None,
            "provisional": True,
            "thread_id": w.get("thread_id"),
        })
    return rows


def one(tid, product):
    """Ownership-checked single-product live status."""
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM tenant_products WHERE tenant_id=%s AND product=%s", (tid, product))
        if not cur.fetchone():
            return {"error": "not your product"}
    reg = {r["name"]: r for r in appregistry.as_rows()}
    return _live_row(product, reg.get(product))


def _selftest():
    """Real tenant + tenant_products row + an app_registry row; prove live_status surfaces the product
    with its url + a boolean 'reachable' (server may be down — only assert the key exists, no crash)."""
    import billing  # noqa: E402  (signup makes a REAL tenant; tenant_products FKs to tenants)
    reg = billing.signup("livestatus-selftest", "free")
    tid = reg["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-live"
    dev_url = "http://127.0.0.1:5000"
    inserted_reg = False
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (prod, tid))
            # no register()/upsert public fn takes a raw dev_url, so INSERT into its table directly
            # (appregistry self-inits the table via _conn()); ensure the table exists first
            appregistry._conn().close()
            cur.execute("""INSERT INTO app_registry (name, kind, status, dev_url)
                           VALUES (%s,'web','launched',%s)
                           ON CONFLICT (name) DO UPDATE SET kind='web', status='launched', dev_url=EXCLUDED.dev_url""",
                        (prod, dev_url))
            inserted_reg = True

        rows = live_status(tid)
        mine = next((r for r in rows if r["product"] == prod), None)
        single = one(tid, prod)
        notmine = one(tid, "someone-else-product")

        ok = (isinstance(rows, list) and mine is not None
              and mine["url"] == dev_url and mine["dev_url"] == dev_url
              and mine["kind"] == "web" and mine["result"] == "launched"
              and isinstance(mine["reachable"], bool) and isinstance(mine["running"], bool)
              and single.get("product") == prod and isinstance(single.get("reachable"), bool)
              and notmine == {"error": "not your product"})

        print(f"products={len(rows)} url={mine['url'] if mine else None} kind={mine['kind'] if mine else None} "
              f"result={mine['result'] if mine else None} reachable={mine['reachable'] if mine else None} "
              f"ownership-guard={notmine.get('error')}")
        print("PASS: livestatus surfaces per-product LIVE status + clickable url, ownership-checked ✅"
              if ok else "FAIL")
    finally:
        with connection() as c, c.cursor() as cur:
            if inserted_reg:
                cur.execute("DELETE FROM app_registry WHERE name=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(live_status(a[1]), indent=2))
    else:
        sys.exit("usage: livestatus.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
