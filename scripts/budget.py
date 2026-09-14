#!/usr/bin/env python3
"""budget.py — runtime cost governor (ADR 0002 governor). Caps token/cost spend per product so an
agent loop can't burn unbounded budget. The Controller calls allow_spend() before dispatching;
over budget => denied (or warned) + audited.

    budget.py set <product> <token_budget>
    budget.py status <product>
    from budget import allow_spend, status
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

from dbpool import connection, tenant_connection


def _conn(tenant_id=None):
    return tenant_connection(tenant_id) if tenant_id else connection()


def _tenant_for(product):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenant_products WHERE product=%s ORDER BY created_at DESC LIMIT 1",
                        (product,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def set_budget(product, token_budget, hard_stop=True):
    tenant_id = _tenant_for(product)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("INSERT INTO budgets (product, token_budget, hard_stop) VALUES (%s,%s,%s) "
                    "ON CONFLICT (product) DO UPDATE SET token_budget=EXCLUDED.token_budget, hard_stop=EXCLUDED.hard_stop",
                    (product, token_budget, hard_stop))


def status(product):
    tenant_id = _tenant_for(product)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT token_budget, hard_stop FROM budgets WHERE product=%s", (product,))
        r = cur.fetchone()
        budget, hard = (r[0], r[1]) if r else (None, False)
        cur.execute("SELECT coalesce(sum(tokens_in+tokens_out),0) FROM org_metrics WHERE product=%s", (product,))
        spent = cur.fetchone()[0]
    return {"product": product, "budget": budget, "spent": spent,
            "remaining": (budget - spent) if budget is not None else None, "hard_stop": hard}


def allow_spend(product, est_tokens):
    """True if (spent + est) within budget, else deny (when hard_stop) — audited either way."""
    s = status(product)
    if s["budget"] is None:
        return True
    over = (s["spent"] + est_tokens) > s["budget"]
    decision = "deny" if (over and s["hard_stop"]) else "allow"
    audit.append(actor="governor", action="BudgetCheck", resource=product, decision=decision,
                 payload={"spent": int(s["spent"]), "budget": int(s["budget"]), "est": est_tokens},
                 tenant_id=_tenant_for(product))
    return not (over and s["hard_stop"])


def _test():
    sys.path.insert(0, str(SCRIPTS)); import metrics
    p = "budget-test"
    set_budget(p, 1000)
    metrics.record("state_change", product=p, to_state="build", tokens_in=400, tokens_out=300)  # 700 spent
    allowed = allow_spend(p, 200)     # 700+200=900 <= 1000 -> allow
    denied = allow_spend(p, 500)      # 700+500=1200 > 1000 -> deny
    with _conn(_tenant_for(p)) as c, c.cursor() as cur:
        cur.execute("DELETE FROM budgets WHERE product=%s", (p,))
        cur.execute("DELETE FROM org_metrics WHERE product=%s", (p,))
    ok = allowed and not denied
    print(f"under budget allowed={allowed}, over budget denied={not denied}")
    print("PASS: budget governor enforces token caps ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if a and a[0] == "set":
        set_budget(a[1], int(a[2])); print(f"budget set: {a[1]} = {a[2]} tokens")
    elif a and a[0] == "status":
        print(status(a[1]))
    elif a and a[0] == "test":
        _test()
    else:
        sys.exit("usage: budget.py set|status|test ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
