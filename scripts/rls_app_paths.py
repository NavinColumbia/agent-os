#!/usr/bin/env python3
"""rls_app_paths.py - static inventory of app paths still risky for FORCE RLS.

RLS readiness is not only schema state. Before enabling FORCE RLS, tenant-facing code paths need to run as a
non-owner app role with transaction-local app.tenant_id. This scanner is deliberately conservative: it finds
Python files that both mention tenant-owned tables and still use direct psycopg.connect(DB/_DB). It is a
triage list, not a proof of vulnerability; each hit still needs code review because some paths are operator
or platform jobs.

    python scripts/rls_app_paths.py report
    python scripts/rls_app_paths.py selftest
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import rls_readiness  # noqa: E402

DIRECT_CONNECT_RE = re.compile(r"psycopg\.connect\((?:DB|_DB)\)")
TENANT_CONNECTION_RE = re.compile(r"\btenant_connection\(")
CONNECTION_RE = re.compile(r"\bconnection\(")


def _tenant_tables() -> set[str]:
    res = rls_readiness.evaluate(rls_readiness._catalog())
    return set(res["tenant_tables"])


def scan(paths: list[Path] | None = None, tenant_tables: set[str] | None = None) -> dict:
    tenant_tables = tenant_tables or _tenant_tables()
    paths = paths or sorted(SCRIPTS.rglob("*.py"))
    files = []
    by_table = {t: [] for t in sorted(tenant_tables)}
    for path in paths:
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        direct_count = len(DIRECT_CONNECT_RE.findall(text))
        tenant_conn_count = len(TENANT_CONNECTION_RE.findall(text))
        pooled_count = len(CONNECTION_RE.findall(text))
        mentioned = sorted(t for t in tenant_tables if re.search(rf"\b{re.escape(t)}\b", text))
        if not mentioned:
            continue
        rel = str(path.relative_to(ROOT))
        rec = {
            "file": rel,
            "tables": mentioned,
            "direct_connects": direct_count,
            "tenant_connections": tenant_conn_count,
            "pooled_connections": pooled_count,
            "needs_review": bool(direct_count),
        }
        files.append(rec)
        if direct_count:
            for table in mentioned:
                by_table[table].append(rel)
    reviewed = {k: sorted(v) for k, v in by_table.items() if v}
    return {
        "tenant_tables": sorted(tenant_tables),
        "files_mentioning_tenant_tables": files,
        "direct_connect_files": [f for f in files if f["direct_connects"]],
        "tables_with_direct_connect_refs": reviewed,
        "summary": {
            "tenant_table_count": len(tenant_tables),
            "files_with_table_refs": len(files),
            "files_with_direct_connect_refs": sum(1 for f in files if f["direct_connects"]),
            "tables_with_direct_connect_refs": len(reviewed),
            "direct_connect_refs": sum(f["direct_connects"] for f in files),
            "tenant_connection_refs": sum(f["tenant_connections"] for f in files),
            "pooled_connection_refs": sum(f["pooled_connections"] for f in files),
        },
    }


def report() -> int:
    res = scan()
    print(json.dumps(res, indent=2, sort_keys=True))
    if res["summary"]["files_with_direct_connect_refs"]:
        print("WARN: direct DB connection refs remain near tenant-owned tables; review before FORCE RLS")
        return 1
    print("PASS: no direct DB connection refs found near tenant-owned tables")
    return 0


def summary(limit=20) -> int:
    res = scan()
    print(json.dumps(res["summary"], indent=2, sort_keys=True))
    risky = sorted(res["direct_connect_files"], key=lambda r: (-r["direct_connects"], r["file"]))[:limit]
    print("\nTop files to review before FORCE RLS:")
    for rec in risky:
        print(f"- {rec['file']}: direct={rec['direct_connects']} tenant_connection={rec['tenant_connections']} "
              f"tables={', '.join(rec['tables'][:8])}{'…' if len(rec['tables']) > 8 else ''}")
    if res["summary"]["files_with_direct_connect_refs"]:
        print("WARN: direct DB connection refs remain near tenant-owned tables; review before FORCE RLS")
        return 1
    print("PASS: no direct DB connection refs found near tenant-owned tables")
    return 0


def _selftest() -> int:
    tmp = ROOT / "scratchpad" / "rls_app_paths_selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    good = tmp / "good.py"
    bad = tmp / "bad.py"
    neutral = tmp / "neutral.py"
    good.write_text("from dbpool import tenant_connection\nwith tenant_connection(tid):\n    'tenant_products'\n")
    bad.write_text("import psycopg\nwith psycopg.connect(DB):\n    'tenant_products'\n")
    neutral.write_text("import psycopg\nwith psycopg.connect(DB):\n    'schedules'\n")
    try:
        res = scan([good, bad, neutral], {"tenant_products"})
        checks = {
            "detects tenant table refs": len(res["files_mentioning_tenant_tables"]) == 2,
            "flags direct connect near tenant table": res["direct_connect_files"][0]["file"].endswith("bad.py"),
            "ignores direct connect without tenant table": all(not f["file"].endswith("neutral.py")
                                                               for f in res["files_mentioning_tenant_tables"]),
            "counts one risky table": res["summary"]["tables_with_direct_connect_refs"] == 1,
            "counts tenant_connection refs": res["summary"]["tenant_connection_refs"] == 1,
        }
        for label, ok in checks.items():
            print(("PASS" if ok else "FAIL") + f": {label}")
        ok = all(checks.values())
        print("PASS: RLS app-path scanner finds direct-connect tenant-table refs" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        for p in (good, bad, neutral):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "selftest":
        sys.exit(_selftest())
    if cmd == "report":
        sys.exit(report())
    if cmd == "summary":
        sys.exit(summary(int(sys.argv[2]) if len(sys.argv) > 2 else 20))
    sys.exit("usage: rls_app_paths.py report|summary [limit]|selftest")
