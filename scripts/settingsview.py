#!/usr/bin/env python3
"""settingsview.py — tenant Settings (Area 13): one read for the whole settings screen.

A tenant's Settings screen needs four things at once: their profile (plan/suspended/name), the
state of their AI-consent gate (the named-provider disclosure they must accept before AI builds),
whether they've brought their own LLM key (BYO key — reported as a boolean only; the secret itself
NEVER leaves the vault), and their per-category notification preferences. This module is the
data/logic layer behind that screen — no web server. It composes consent.py, notifications'
notification_prefs table, billing's plan/quota, and vault key-presence into one settings(tid)
view, plus the two write paths a settings screen actually uses (toggle a notification pref,
accept/revoke AI consent).

    settingsview.py json <tenant_id>     # the full settings view as JSON
    settingsview.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit          # noqa: E402
import consent        # noqa: E402
import billing        # noqa: E402
import vault          # noqa: E402
import notifications  # noqa: E402  (reuse its notification_prefs table + _ensure)
from dbpool import connection, tenant_connection  # noqa: E402

# the notification categories surfaced on the settings screen (mirrors notifications.py taxonomy)
PREF_CATEGORIES = ("build", "billing", "changelog", "incident", "security")
PREF_DEFAULT = {"in_app": True, "email": True, "push": False}   # default when no row exists yet
BYO_KEY_NAME = "byo_llm_key"                                     # vault secret name for a tenant's BYO LLM key


def _profile(tid):
    """tenant_id + plan + suspended + name (name omitted if the tenants row has none)."""
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT plan, suspended, name FROM tenants WHERE tenant_id=%s", (tid,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"no such tenant {tid}")
    prof = {"tenant_id": tid, "plan": row[0], "suspended": row[1]}
    if row[2] is not None:
        prof["name"] = row[2]
    return prof


def _byo_key_set(tid):
    """True iff the tenant has a 'byo_llm_key' secret in the vault. NEVER returns the secret itself."""
    try:
        # BYO keys are stored under product='tenant:<tid>' with allowed_roles=['builder','factory']
        # (console.py /api/byok, frontdoor.py /api/byok). Read with the same scope: tenant-namespaced
        # product, an allowed role, and the tenant bind — otherwise get_secret fail-closes and we'd
        # always report 'no key'. Presence only; the secret itself never leaves the vault.
        vault.get_secret(BYO_KEY_NAME, f"tenant:{tid}", "prod", "builder", tenant_id=tid)
        return True
    except Exception:
        return False


def _notification_prefs(tid):
    """Per-category prefs for PREF_CATEGORIES, falling back to PREF_DEFAULT for any with no row."""
    notifications._ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT category, in_app, email, push FROM notification_prefs WHERE tenant_id=%s", (tid,))
        rows = {r[0]: {"in_app": r[1], "email": r[2], "push": r[3]} for r in cur.fetchall()}
    out = []
    for cat in PREF_CATEGORIES:
        p = rows.get(cat, PREF_DEFAULT)
        out.append({"category": cat, "in_app": p["in_app"], "email": p["email"], "push": p["push"]})
    return out


def settings(tid):
    """The whole settings screen in one read: profile + AI-consent + BYO-key presence + notification prefs."""
    return {
        "profile": _profile(tid),
        "ai_consent": consent.state(tid),
        "byo_key_set": _byo_key_set(tid),
        "notification_prefs": _notification_prefs(tid),
    }


def set_pref(tid, category, in_app, email, push):
    """Upsert a single category's notification preference. Audited."""
    notifications._ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO notification_prefs (tenant_id, category, in_app, email, push)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, category)
                       DO UPDATE SET in_app=EXCLUDED.in_app, email=EXCLUDED.email, push=EXCLUDED.push""",
                    (tid, category, bool(in_app), bool(email), bool(push)))
    audit.append(actor="settings", action="SetNotificationPref", resource=tid, decision="updated",
                 payload={"category": category, "in_app": bool(in_app), "email": bool(email), "push": bool(push)},
                 tenant_id=tid)
    return {"category": category, "in_app": bool(in_app), "email": bool(email), "push": bool(push)}


def set_consent(tid, accept):
    """Accept (record) or revoke the tenant's AI consent for the current disclosure. Audited by consent.py."""
    if accept:
        consent.record(tid)
    else:
        consent.revoke(tid)
    return consent.state(tid)


def _selftest():
    tid = billing.signup("settings-selftest", "free")["tenant_id"]
    try:
        s = settings(tid)
        sections = all(k in s for k in ("profile", "ai_consent", "byo_key_set", "notification_prefs"))
        cats = [p["category"] for p in s["notification_prefs"]]
        cats_ok = cats == list(PREF_CATEGORIES)
        fresh_no_key = s["byo_key_set"] is False                  # fresh tenant: no BYO key

        # positive case: a saved BYO key (same scope console/frontdoor use) must read back as set
        vault.put_secret(BYO_KEY_NAME, f"tenant:{tid}", "prod", ["builder", "factory"], "sk-selftest")
        byo_key_on = settings(tid)["byo_key_set"] is True

        set_consent(tid, True)
        consent_on = settings(tid)["ai_consent"]["accepted"] is True   # accept -> ai_consent.accepted True

        set_pref(tid, "billing", True, False, False)
        billing_pref = next(p for p in settings(tid)["notification_prefs"] if p["category"] == "billing")
        pref_ok = billing_pref == {"category": "billing", "in_app": True, "email": False, "push": False}

        ok = sections and cats_ok and fresh_no_key and byo_key_on and consent_on and pref_ok
        print(f"sections={sections} categories={cats_ok} fresh-no-key={fresh_no_key} "
              f"byo-key-on={byo_key_on} consent-on={consent_on} pref-reflected={pref_ok}")
        print("PASS: settings view composes profile/consent/byo-key/prefs + writes reflect ✅" if ok else "FAIL")
    finally:
        vault.delete_secrets_for_products([f"tenant:{tid}"])      # purge the selftest BYO key
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM notification_prefs WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(settings(a[1]), indent=2))
    else:
        sys.exit("usage: settingsview.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
