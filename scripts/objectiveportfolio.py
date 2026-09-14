#!/usr/bin/env python3
"""Durable, evidence-led portfolio planning for an agent organization.

The module sits above :mod:`workcontracts`: a strategic objective references the
accepted work contract that owns execution.  It deliberately does not schedule,
delegate, or infer that elapsed time means failure.  It preserves the facts an
agent manager needs to make those judgments and the history needed to audit them.
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

_STATES = {"proposed", "active", "at_risk", "blocked", "achieved", "abandoned"}
_TERMINAL = {"achieved", "abandoned"}
_TRANSITIONS = {
    "proposed": {"active", "abandoned"},
    "active": {"at_risk", "blocked", "achieved", "abandoned"},
    "at_risk": {"active", "blocked", "achieved", "abandoned"},
    "blocked": {"active", "at_risk", "achieved", "abandoned"},
    "achieved": set(),
    "abandoned": set(),
}
_ensured = False
_ensure_lock = threading.Lock()


def ensure():
    """Install this additive module on upgraded as well as fresh deployments."""
    global _ensured
    if _ensured:
        return True
    with _ensure_lock:
        if not _ensured:
            migration = SCRIPTS.parent / "postgres" / "initdb" / "62-objective-portfolio.sql"
            with connection() as c, c.cursor() as cur:
                cur.execute(migration.read_text())
            _ensured = True
    return True


def _required(value, label):
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} is required")
    return value


def normalize_key_result(title, metric_name, unit, direction, baseline, target,
                         *, current_value=None, weight=1, owner, due_on=None):
    """Validate that a KR is quantified and has an unambiguous success direction."""
    direction = str(direction or "").lower()
    if direction not in {"increase", "decrease", "binary"}:
        raise ValueError("direction must be increase, decrease, or binary")
    baseline, target = Decimal(str(baseline)), Decimal(str(target))
    current = baseline if current_value is None else Decimal(str(current_value))
    weight = Decimal(str(weight))
    if weight <= 0:
        raise ValueError("key result weight must be positive")
    if direction == "increase" and target <= baseline:
        raise ValueError("increase target must exceed baseline")
    if direction == "decrease" and target >= baseline:
        raise ValueError("decrease target must be below baseline")
    if direction == "binary" and (baseline != 0 or target != 1 or current not in {0, 1}):
        raise ValueError("binary KR must use baseline=0, target=1, and current 0 or 1")
    return {
        "title": _required(title, "key result title"),
        "metric_name": _required(metric_name, "metric_name"),
        "unit": _required(unit, "unit"), "direction": direction,
        "baseline": baseline, "target": target, "current_value": current,
        "weight": weight, "owner": _required(owner, "key result owner"),
        "due_on": due_on,
    }


def key_result_progress(key_result):
    """Return bounded 0..1 progress; overachievement is retained in raw measurements."""
    baseline = Decimal(str(key_result["baseline"]))
    target = Decimal(str(key_result["target"]))
    current = Decimal(str(key_result["current_value"]))
    direction = key_result["direction"]
    if direction == "binary":
        result = current
    elif direction == "increase":
        result = (current - baseline) / (target - baseline)
    elif direction == "decrease":
        result = (baseline - current) / (baseline - target)
    else:
        raise ValueError(f"unknown KR direction: {direction}")
    return float(max(Decimal(0), min(Decimal(1), result)))


def validate_transition(from_state, to_state, rationale, evidence):
    """Pure lifecycle guard: management state claims always carry evidence."""
    if from_state not in _STATES or to_state not in _STATES:
        raise ValueError("unknown objective state")
    if to_state not in _TRANSITIONS[from_state]:
        raise ValueError(f"invalid objective transition: {from_state} -> {to_state}")
    _required(rationale, "state-change rationale")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("state-change evidence is required")
    return {"from_state": from_state, "to_state": to_state,
            "rationale": str(rationale).strip(), "evidence": dict(evidence)}


def validate_tradeoff(chosen_option, forgone_options, rationale, revisit_trigger):
    forgone = [_required(x, "forgone option") for x in (forgone_options or [])]
    if not forgone:
        raise ValueError("at least one forgone option is required")
    return {"chosen_option": _required(chosen_option, "chosen option"),
            "forgone_options": forgone, "rationale": _required(rationale, "tradeoff rationale"),
            "revisit_trigger": _required(revisit_trigger, "revisit trigger")}


def rollup(objectives, key_results):
    """Compute an explainable weighted tree rollup from plain dict records.

    An objective's own KRs and its immediate child objectives are weighted peers.
    Sibling objective weights express strategic allocation. Empty leaves report no
    progress rather than manufacturing a zero. Cycles and orphan parents fail closed.
    """
    records = {str(o["objective_id"]): dict(o) for o in objectives}
    if len(records) != len(objectives):
        raise ValueError("duplicate objective_id")
    children = defaultdict(list)
    for oid, obj in records.items():
        parent = obj.get("parent_objective_id")
        if parent is not None:
            parent = str(parent)
            if parent not in records:
                raise ValueError(f"orphan objective parent: {parent}")
            children[parent].append(oid)
    krs = defaultdict(list)
    for kr in key_results:
        oid = str(kr["objective_id"])
        if oid not in records:
            raise ValueError(f"KR references unknown objective: {oid}")
        krs[oid].append(kr)

    visiting, done = set(), {}
    def visit(oid):
        if oid in visiting:
            raise ValueError("objective hierarchy contains a cycle")
        if oid in done:
            return done[oid]
        visiting.add(oid)
        components = []
        for kr in krs[oid]:
            components.append((Decimal(str(kr.get("weight", 1))), key_result_progress(kr)))
        child_rows = []
        for child_id in children[oid]:
            child = visit(child_id)
            child_rows.append(child)
            if child["progress"] is not None:
                components.append((Decimal(str(records[child_id].get("weight", 1))), child["progress"]))
        total_weight = sum((w for w, _ in components), Decimal(0))
        progress = (sum(float(w) * p for w, p in components) / float(total_weight)
                    if total_weight else None)
        visiting.remove(oid)
        done[oid] = {"objective_id": oid, "title": records[oid].get("title", oid),
                     "state": records[oid].get("state"), "progress": progress,
                     "children": child_rows,
                     "key_results": [{"key_result_id": kr.get("key_result_id"),
                                      "progress": key_result_progress(kr)} for kr in krs[oid]]}
        return done[oid]

    roots = [oid for oid, o in records.items() if o.get("parent_objective_id") is None]
    trees = [visit(oid) for oid in roots]
    if len(done) != len(records):
        # A rootless component can only be a cycle.
        for oid in records:
            if oid not in done:
                visit(oid)
    weighted_roots = [(Decimal(str(records[t["objective_id"]].get("weight", 1))), t["progress"])
                      for t in trees if t["progress"] is not None]
    denom = sum((w for w, _ in weighted_roots), Decimal(0))
    overall = (sum(float(w) * p for w, p in weighted_roots) / float(denom) if denom else None)
    return {"progress": overall, "roots": trees}


def create_portfolio(tenant_id, title, purpose, accountable_owner, manager_owner, *,
                     created_by, org_id=None, horizon_start=None, horizon_end=None,
                     status="active", portfolio_id=None):
    ensure()
    if status not in {"draft", "active"}:
        raise ValueError("a new portfolio must be draft or active")
    if horizon_start and horizon_end and date.fromisoformat(str(horizon_end)) < date.fromisoformat(str(horizon_start)):
        raise ValueError("horizon_end cannot precede horizon_start")
    pid = str(portfolio_id or f"op-{uuid.uuid4().hex}")
    values = (_required(title, "portfolio title"), _required(purpose, "portfolio purpose"),
              _required(accountable_owner, "accountable_owner"),
              _required(manager_owner, "manager_owner"))
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO objective_portfolios
            (portfolio_id,tenant_id,org_id,title,purpose,horizon_start,horizon_end,
             accountable_owner,manager_owner,status,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (pid, str(tenant_id), org_id, values[0], values[1], horizon_start, horizon_end,
             values[2], values[3], status, created_by))
        snapshot = {"portfolio_id": pid, "title": values[0], "purpose": values[1], "objectives": []}
        cur.execute("""INSERT INTO objective_plan_revisions
            (tenant_id,portfolio_id,revision,change_kind,rationale,changed_by,
             previous_snapshot,resulting_snapshot)
            VALUES (%s,%s,1,'portfolio_created','initial plan',%s,'{}',%s)""",
            (str(tenant_id), pid, created_by, json.dumps(snapshot)))
    audit.append(actor=created_by, action="ObjectivePortfolioCreated", resource=pid,
                 decision=status, payload={"owner": values[2], "manager": values[3]},
                 tenant_id=str(tenant_id))
    return {"portfolio_id": pid, "planning_revision": 1, "status": status}


def _portfolio_snapshot(cur, tenant_id, portfolio_id):
    cur.execute("""SELECT portfolio_id,title,purpose,status,planning_revision
                   FROM objective_portfolios WHERE tenant_id=%s AND portfolio_id=%s""",
                (tenant_id, portfolio_id))
    portfolio = cur.fetchone()
    if not portfolio:
        raise KeyError(portfolio_id)
    cur.execute("""SELECT objective_id,parent_objective_id,work_contract_id,title,outcome,
                          accountable_owner,manager_owner,state,priority,weight,confidence,due_on,version
                   FROM strategic_objectives WHERE tenant_id=%s AND portfolio_id=%s
                   ORDER BY created_at,objective_id""", (tenant_id, portfolio_id))
    columns = ("objective_id", "parent_objective_id", "work_contract_id", "title", "outcome",
               "accountable_owner", "manager_owner", "state", "priority", "weight", "confidence",
               "due_on", "version")
    objectives = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.execute("""SELECT k.key_result_id,k.objective_id,k.title,k.metric_name,k.unit,k.direction,
                          k.baseline,k.target,k.current_value,k.weight,k.owner,k.due_on,k.status,k.version
                   FROM objective_key_results k JOIN strategic_objectives o
                     ON o.objective_id=k.objective_id
                   WHERE o.tenant_id=%s AND o.portfolio_id=%s
                   ORDER BY k.created_at,k.key_result_id""", (tenant_id, portfolio_id))
    kr_columns = ("key_result_id", "objective_id", "title", "metric_name", "unit", "direction",
                  "baseline", "target", "current_value", "weight", "owner", "due_on", "status", "version")
    key_results = [dict(zip(kr_columns, row)) for row in cur.fetchall()]
    cur.execute("""SELECT d.dependency_id,d.objective_id,d.depends_on_objective_id,
                          d.required_condition,d.dependency_owner,d.criticality,d.status
                   FROM objective_dependencies d JOIN strategic_objectives o
                     ON o.objective_id=d.objective_id
                   WHERE o.tenant_id=%s AND o.portfolio_id=%s
                   ORDER BY d.created_at,d.dependency_id""", (tenant_id, portfolio_id))
    dep_columns = ("dependency_id", "objective_id", "depends_on_objective_id", "required_condition",
                   "dependency_owner", "criticality", "status")
    dependencies = [dict(zip(dep_columns, row)) for row in cur.fetchall()]
    cur.execute("""SELECT tradeoff_id,objective_id,chosen_option,forgone_options,rationale,
                          assumptions,decided_by,status,revisit_trigger,decided_at
                   FROM objective_tradeoffs WHERE tenant_id=%s AND portfolio_id=%s
                   ORDER BY decided_at,tradeoff_id""", (tenant_id, portfolio_id))
    trade_columns = ("tradeoff_id", "objective_id", "chosen_option", "forgone_options", "rationale",
                     "assumptions", "decided_by", "status", "revisit_trigger", "decided_at")
    tradeoffs = [dict(zip(trade_columns, row)) for row in cur.fetchall()]
    # json.dumps(default=str) at the storage boundary preserves dates/Decimals without losing DB types here.
    return {"portfolio_id": portfolio[0], "title": portfolio[1], "purpose": portfolio[2],
            "status": portfolio[3], "planning_revision": portfolio[4], "objectives": objectives,
            "key_results": key_results, "dependencies": dependencies, "tradeoffs": tradeoffs}


def _record_plan_revision(cur, tenant_id, portfolio_id, before, change_kind, rationale,
                          changed_by, evidence=None):
    """Advance one optimistic portfolio revision inside the caller's transaction."""
    expected = int(before["planning_revision"])
    revision = expected + 1
    cur.execute("""UPDATE objective_portfolios SET planning_revision=%s,updated_at=now()
                   WHERE tenant_id=%s AND portfolio_id=%s AND planning_revision=%s""",
                (revision, tenant_id, portfolio_id, expected))
    if cur.rowcount != 1:
        raise ValueError("stale portfolio planning revision")
    after = _portfolio_snapshot(cur, tenant_id, portfolio_id)
    cur.execute("""INSERT INTO objective_plan_revisions
        (tenant_id,portfolio_id,revision,change_kind,rationale,changed_by,
         previous_snapshot,resulting_snapshot,evidence)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (tenant_id, portfolio_id, revision, change_kind, _required(rationale, "plan rationale"),
         changed_by, json.dumps(before, default=str), json.dumps(after, default=str),
         json.dumps(evidence or {})))
    return revision


def add_objective(tenant_id, portfolio_id, work_contract_id, title, outcome, *, created_by,
                  parent_objective_id=None, priority=3, weight=1, confidence=0.5,
                  starts_on=None, due_on=None, objective_id=None, key_results=None):
    """Add a strategic objective only when its operational owner accepted a work contract."""
    ensure()
    oid = str(objective_id or f"so-{uuid.uuid4().hex}")
    priority, weight, confidence = int(priority), Decimal(str(weight)), Decimal(str(confidence))
    if not 0 <= priority <= 5 or weight <= 0 or not 0 <= confidence <= 1:
        raise ValueError("invalid priority, weight, or confidence")
    normalized_krs = [normalize_key_result(**kr) for kr in (key_results or [])]
    tid, pid, wid = str(tenant_id), str(portfolio_id), _required(work_contract_id, "work_contract_id")
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT accountable_owner,manager_owner,status FROM work_contracts
                       WHERE tenant_id=%s AND contract_id=%s""", (tid, wid))
        contract = cur.fetchone()
        if not contract:
            raise KeyError(f"work contract not found in tenant: {wid}")
        if contract[2] in {"completed", "cancelled"}:
            raise ValueError(f"cannot attach a {contract[2]} work contract")
        before = _portfolio_snapshot(cur, tid, pid)
        cur.execute("""INSERT INTO strategic_objectives
            (objective_id,tenant_id,portfolio_id,parent_objective_id,work_contract_id,title,outcome,
             accountable_owner,manager_owner,state,priority,weight,confidence,starts_on,due_on,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'proposed',%s,%s,%s,%s,%s,%s)""",
            (oid, tid, pid, parent_objective_id, wid, _required(title, "objective title"),
             _required(outcome, "objective outcome"), contract[0], contract[1], priority,
             weight, confidence, starts_on, due_on, created_by))
        for kr in normalized_krs:
            kid = f"kr-{uuid.uuid4().hex}"
            cur.execute("""INSERT INTO objective_key_results
                (key_result_id,tenant_id,objective_id,title,metric_name,unit,direction,baseline,
                 target,current_value,weight,owner,due_on)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (kid, tid, oid, kr["title"], kr["metric_name"], kr["unit"], kr["direction"],
                 kr["baseline"], kr["target"], kr["current_value"], kr["weight"], kr["owner"], kr["due_on"]))
        revision = _record_plan_revision(cur, tid, pid, before, "objective_added",
                                         f"Added objective: {title}", created_by,
                                         {"work_contract_id": wid})
    audit.append(actor=created_by, action="StrategicObjectiveAdded", resource=oid,
                 decision="proposed", payload={"portfolio_id": pid, "work_contract_id": wid,
                 "planning_revision": revision}, tenant_id=tid)
    return {"objective_id": oid, "state": "proposed", "accountable_owner": contract[0],
            "manager_owner": contract[1], "planning_revision": revision}


def revise_objective(tenant_id, objective_id, changes, rationale, *, changed_by,
                     expected_version, expected_plan_revision, evidence=None):
    """Revise strategic terms with optimistic locks and immutable before/after history.

    Owner/manager/work-contract and lifecycle state are intentionally excluded: ownership
    changes through workcontracts, while state changes through :func:`change_state`.
    """
    ensure()
    allowed = {"title", "outcome", "parent_objective_id", "priority", "weight", "confidence",
               "starts_on", "due_on"}
    changes = dict(changes or {})
    unknown = set(changes) - allowed
    if not changes:
        raise ValueError("objective changes are required")
    if unknown:
        raise ValueError(f"unsupported objective changes: {sorted(unknown)}")
    if "title" in changes:
        changes["title"] = _required(changes["title"], "objective title")
    if "outcome" in changes:
        changes["outcome"] = _required(changes["outcome"], "objective outcome")
    if "priority" in changes and not 0 <= int(changes["priority"]) <= 5:
        raise ValueError("priority must be between 0 and 5")
    if "weight" in changes and Decimal(str(changes["weight"])) <= 0:
        raise ValueError("weight must be positive")
    if "confidence" in changes and not 0 <= Decimal(str(changes["confidence"])) <= 1:
        raise ValueError("confidence must be between 0 and 1")
    tid, oid = str(tenant_id), str(objective_id)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT portfolio_id,version,state FROM strategic_objectives
                       WHERE tenant_id=%s AND objective_id=%s FOR UPDATE""", (tid, oid))
        row = cur.fetchone()
        if not row:
            raise KeyError(oid)
        if row[2] in _TERMINAL:
            raise ValueError(f"cannot revise a {row[2]} objective")
        if row[1] != int(expected_version):
            raise ValueError("stale objective version")
        before = _portfolio_snapshot(cur, tid, row[0])
        if before["planning_revision"] != int(expected_plan_revision):
            raise ValueError("stale portfolio planning revision")
        if changes.get("parent_objective_id"):
            cur.execute("""SELECT 1 FROM strategic_objectives
                           WHERE tenant_id=%s AND portfolio_id=%s AND objective_id=%s""",
                        (tid, row[0], changes["parent_objective_id"]))
            if not cur.fetchone():
                raise ValueError("parent objective must belong to the same portfolio")
        assignments = ",".join(f"{column}=%s" for column in changes)
        cur.execute(f"""UPDATE strategic_objectives SET {assignments},version=version+1,updated_at=now()
                        WHERE tenant_id=%s AND objective_id=%s AND version=%s""",
                    (*changes.values(), tid, oid, int(expected_version)))
        if cur.rowcount != 1:
            raise ValueError("stale objective version")
        revision = _record_plan_revision(cur, tid, row[0], before, "objective_revised", rationale,
                                         changed_by, {"objective_id": oid, **(evidence or {})})
    audit.append(actor=changed_by, action="StrategicObjectiveRevised", resource=oid,
                 decision="revised", payload={"changes": json.loads(json.dumps(changes, default=str)),
                 "planning_revision": revision},
                 tenant_id=tid)
    return {"objective_id": oid, "version": int(expected_version) + 1,
            "planning_revision": revision}


def add_dependency(tenant_id, objective_id, depends_on_objective_id, required_condition, *,
                   dependency_owner, added_by, criticality="blocking", dependency_id=None):
    """Record an owned inter-objective dependency and include it in plan history."""
    ensure()
    if criticality not in {"informational", "important", "blocking"}:
        raise ValueError("unknown dependency criticality")
    tid, oid, upstream = str(tenant_id), str(objective_id), str(depends_on_objective_id)
    if oid == upstream:
        raise ValueError("an objective cannot depend on itself")
    did = str(dependency_id or f"od-{uuid.uuid4().hex}")
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT portfolio_id FROM strategic_objectives
                       WHERE tenant_id=%s AND objective_id=%s""", (tid, oid))
        row = cur.fetchone()
        if not row:
            raise KeyError(oid)
        cur.execute("""SELECT 1 FROM strategic_objectives
                       WHERE tenant_id=%s AND objective_id=%s""", (tid, upstream))
        if not cur.fetchone():
            raise KeyError(upstream)
        before = _portfolio_snapshot(cur, tid, row[0])
        cur.execute("""INSERT INTO objective_dependencies
            (dependency_id,tenant_id,objective_id,depends_on_objective_id,required_condition,
             dependency_owner,criticality) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (did, tid, oid, upstream, _required(required_condition, "required condition"),
             _required(dependency_owner, "dependency owner"), criticality))
        revision = _record_plan_revision(cur, tid, row[0], before, "dependency_added",
                                         f"Added dependency for {oid}", added_by,
                                         {"dependency_id": did, "depends_on": upstream})
    return {"dependency_id": did, "status": "pending", "planning_revision": revision}


def change_dependency(tenant_id, dependency_id, status, evidence, *, changed_by,
                      expected_status="pending"):
    """Resolve/reopen a dependency with evidence and preserve the decision in plan history."""
    allowed = {
        "pending": {"satisfied", "failed", "waived"},
        "failed": {"pending", "satisfied", "waived"},
        "satisfied": {"pending"},
        "waived": {"pending"},
    }
    if status not in allowed.get(expected_status, set()):
        raise ValueError(f"invalid dependency transition: {expected_status} -> {status}")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("dependency transition evidence is required")
    ensure()
    tid, did = str(tenant_id), str(dependency_id)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT d.status,o.portfolio_id,d.objective_id
                       FROM objective_dependencies d JOIN strategic_objectives o
                         ON o.objective_id=d.objective_id
                       WHERE d.tenant_id=%s AND d.dependency_id=%s FOR UPDATE OF d""", (tid, did))
        row = cur.fetchone()
        if not row:
            raise KeyError(did)
        if row[0] != expected_status:
            raise ValueError("stale dependency status")
        before = _portfolio_snapshot(cur, tid, row[1])
        cur.execute("""UPDATE objective_dependencies SET status=%s,evidence=%s,updated_at=now()
                       WHERE tenant_id=%s AND dependency_id=%s AND status=%s""",
                    (status, json.dumps(evidence), tid, did, expected_status))
        revision = _record_plan_revision(cur, tid, row[1], before, "dependency_state_changed",
                                         f"Dependency {did}: {expected_status} -> {status}", changed_by,
                                         {"dependency_id": did, "evidence": evidence})
    return {"dependency_id": did, "from_status": expected_status, "status": status,
            "planning_revision": revision}


def record_measurement(tenant_id, key_result_id, measured_value, evidence, *, measured_by,
                       expected_version=None):
    """Append source evidence and atomically update the current KR projection."""
    ensure()
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("measurement evidence is required")
    tid, kid, value = str(tenant_id), str(key_result_id), Decimal(str(measured_value))
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT current_value,direction,baseline,target,version FROM objective_key_results
                       WHERE tenant_id=%s AND key_result_id=%s FOR UPDATE""", (tid, kid))
        row = cur.fetchone()
        if not row:
            raise KeyError(kid)
        if expected_version is not None and int(expected_version) != row[4]:
            raise ValueError("stale key-result version")
        if row[1] == "binary" and value not in {0, 1}:
            raise ValueError("binary measurement must be 0 or 1")
        cur.execute("""INSERT INTO objective_measurements
            (tenant_id,key_result_id,previous_value,measured_value,evidence,measured_by)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING measurement_id""",
            (tid, kid, row[0], value, json.dumps(evidence), measured_by))
        mid = cur.fetchone()[0]
        cur.execute("""UPDATE objective_key_results SET current_value=%s,last_evidence=%s,
                         version=version+1,updated_at=now() WHERE tenant_id=%s AND key_result_id=%s""",
                    (value, json.dumps(evidence), tid, kid))
    progress = key_result_progress({"current_value": value, "direction": row[1],
                                    "baseline": row[2], "target": row[3]})
    return {"measurement_id": mid, "key_result_id": kid, "version": row[4] + 1,
            "progress": progress}


def change_state(tenant_id, objective_id, to_state, rationale, evidence, *, changed_by,
                 expected_version=None):
    """Make an evidence-backed, optimistic-locked management state transition."""
    ensure()
    tid, oid = str(tenant_id), str(objective_id)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT state,version,portfolio_id FROM strategic_objectives
                       WHERE tenant_id=%s AND objective_id=%s FOR UPDATE""", (tid, oid))
        row = cur.fetchone()
        if not row:
            raise KeyError(oid)
        transition = validate_transition(row[0], to_state, rationale, evidence)
        if expected_version is not None and int(expected_version) != row[1]:
            raise ValueError("stale objective version")
        cur.execute("""INSERT INTO objective_state_changes
            (tenant_id,objective_id,from_state,to_state,rationale,evidence,changed_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING state_change_id""",
            (tid, oid, row[0], to_state, transition["rationale"], json.dumps(evidence), changed_by))
        sid = cur.fetchone()[0]
        cur.execute("""UPDATE strategic_objectives SET state=%s,version=version+1,updated_at=now()
                       WHERE tenant_id=%s AND objective_id=%s""", (to_state, tid, oid))
    audit.append(actor=changed_by, action="ObjectiveStateChanged", resource=oid,
                 decision=to_state, payload={"from_state": row[0], "state_change_id": sid,
                 "evidence": evidence}, tenant_id=tid)
    return {"state_change_id": sid, "from_state": row[0], "to_state": to_state,
            "version": row[1] + 1}


def record_tradeoff(tenant_id, portfolio_id, chosen_option, forgone_options, rationale, *,
                    decided_by, revisit_trigger, objective_id=None, assumptions=None,
                    tradeoff_id=None, supersedes_tradeoff_id=None):
    ensure()
    item = validate_tradeoff(chosen_option, forgone_options, rationale, revisit_trigger)
    tid, pid = str(tenant_id), str(portfolio_id)
    xid = str(tradeoff_id or f"ot-{uuid.uuid4().hex}")
    with tenant_connection(tid) as c, c.cursor() as cur:
        before = _portfolio_snapshot(cur, tid, pid)
        if supersedes_tradeoff_id:
            cur.execute("""UPDATE objective_tradeoffs SET status='superseded'
                           WHERE tenant_id=%s AND portfolio_id=%s AND tradeoff_id=%s
                             AND status IN ('decided','reopened')""",
                        (tid, pid, str(supersedes_tradeoff_id)))
            if cur.rowcount != 1:
                raise ValueError("tradeoff to supersede is missing, stale, or in another portfolio")
        cur.execute("""INSERT INTO objective_tradeoffs
            (tradeoff_id,tenant_id,portfolio_id,objective_id,chosen_option,forgone_options,
             rationale,assumptions,decided_by,revisit_trigger,supersedes_tradeoff_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (xid, tid, pid, objective_id, item["chosen_option"],
             json.dumps(item["forgone_options"]), item["rationale"],
             json.dumps(assumptions or []), decided_by, item["revisit_trigger"],
             supersedes_tradeoff_id))
        revision = _record_plan_revision(cur, tid, pid, before, "tradeoff_decided",
                                         item["rationale"], decided_by, {"tradeoff_id": xid})
    audit.append(actor=decided_by, action="ObjectiveTradeoffRecorded", resource=xid,
                 decision="decided", payload={"portfolio_id": portfolio_id,
                 "chosen": item["chosen_option"], "forgone": item["forgone_options"]}, tenant_id=tid)
    return {"tradeoff_id": xid, "status": "decided", "planning_revision": revision, **item}


def management_evidence(tenant_id, portfolio_id=None):
    """Return decision-ready facts, never automatic conclusions based on elapsed time."""
    ensure()
    tid = str(tenant_id)
    portfolio_clause = "AND o.portfolio_id=%s" if portfolio_id else ""
    params = (tid, str(portfolio_id)) if portfolio_id else (tid,)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute(f"""SELECT o.objective_id,o.title,o.state,o.accountable_owner,o.manager_owner,
                               o.confidence,o.due_on,o.version,w.status,
                               count(d.dependency_id) FILTER (WHERE d.status IN ('pending','failed')
                                   AND d.criticality='blocking')
                        FROM strategic_objectives o
                        LEFT JOIN work_contracts w ON w.contract_id=o.work_contract_id
                        JOIN tenants t ON t.tenant_id=o.tenant_id
                        LEFT JOIN objective_dependencies d ON d.objective_id=o.objective_id
                        WHERE o.tenant_id=%s {portfolio_clause}
                          AND COALESCE(w.constraints->>'execution_scope','production')='production'
                          AND o.state NOT IN ('achieved','abandoned')
                        GROUP BY o.objective_id,w.status ORDER BY o.priority,o.due_on NULLS LAST""", params)
        objectives = cur.fetchall()
        cur.execute(f"""SELECT k.key_result_id,k.objective_id,k.title,k.direction,k.baseline,k.target,
                               k.current_value,k.last_evidence,k.updated_at
                        FROM objective_key_results k JOIN strategic_objectives o
                          ON o.objective_id=k.objective_id
                        LEFT JOIN work_contracts w ON w.contract_id=o.work_contract_id
                        JOIN tenants t ON t.tenant_id=o.tenant_id
                        WHERE o.tenant_id=%s {portfolio_clause} AND k.status='active'
                          AND COALESCE(w.constraints->>'execution_scope','production')='production'
                        ORDER BY k.updated_at""", params)
        key_results = cur.fetchall()
    kr_by_objective = defaultdict(list)
    for row in key_results:
        kr_by_objective[row[1]].append({"key_result_id": row[0], "title": row[2],
            "progress": key_result_progress({"direction": row[3], "baseline": row[4],
                                             "target": row[5], "current_value": row[6]}),
            "last_evidence": row[7], "measured_at": str(row[8])})
    return [{"objective_id": r[0], "title": r[1], "state": r[2],
             "accountable_owner": r[3], "manager_owner": r[4], "confidence": float(r[5]),
             "due_on": str(r[6]) if r[6] else None, "version": r[7],
             "work_contract_status": r[8], "blocking_dependencies": r[9],
             "key_results": kr_by_objective[r[0]]} for r in objectives]
