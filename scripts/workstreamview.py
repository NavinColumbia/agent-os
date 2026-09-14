#!/usr/bin/env python3
"""Tenant-scoped controller workstream visibility shared by CEO-facing views.

Products appear only after the build has a concrete product row. The controller can still be doing
real work before that point, so cockpit/projects/live views must read controller_state directly or they
can render a false idle state.
"""
from dbpool import tenant_connection


def active_workstreams(tid, org_id=0):
    """Return non-terminal controller workstreams for this tenant/org without creating new threads."""
    q = """SELECT thread_id, org_id, phase, product, awaiting, job_kind, job_status,
                  EXTRACT(EPOCH FROM (now()-updated_at))::int
           FROM controller_state
           WHERE tenant_id=%s AND NOT (phase='DELIVER' AND awaiting IS NULL)"""
    args = [tid]
    if org_id:
        q += " AND org_id=%s"
        args.append(int(org_id))
    q += " ORDER BY updated_at DESC, thread_id DESC"
    try:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute(q, args)
            rows = cur.fetchall()
    except Exception:
        return []
    out = []
    for thread_id, oid, phase, product, awaiting, job_kind, job_status, age_s in rows:
        running = awaiting == "fleet"
        out.append({
            "thread_id": thread_id,
            "org_id": oid,
            "phase": phase,
            "product": product,
            "label": product or f"workstream-{thread_id}",
            "awaiting": awaiting,
            "running": running,
            "job": job_kind,
            "status": job_status,
            "age_s": int(age_s or 0),
            "can_cancel": running,
            "cancel_action": "stop" if running else None,
        })
    return out


def cancel_workstream(tid, org_id, thread_id, reason="stopped by user from workstream view", who="user"):
    """Tenant-scoped Stop for a visible controller workstream.

    The Assistant Stop button cancels the default company thread. Cockpit/Projects can show multiple active
    workstreams, including pre-product provisional work, so they need a thread-specific and ownership-checked
    cancel path.
    """
    try:
        thread_id = int(thread_id)
    except Exception:
        return {"error": "invalid workstream"}
    rows = active_workstreams(tid, org_id)
    mine = next((w for w in rows if int(w.get("thread_id") or 0) == thread_id), None)
    if not mine:
        return {"error": "not your active workstream"}
    if not mine.get("running"):
        return {"error": "workstream is not running", "thread_id": thread_id}
    import loopcontroller
    return loopcontroller.cancel(tid, thread_id, reason=reason, who=who)
