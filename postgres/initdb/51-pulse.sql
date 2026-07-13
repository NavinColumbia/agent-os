-- agent_pulse — the LIVE PULSE of all in-flight agentic work (unified observability plane).
-- North Star: heartbeats on everything INCLUDING agentic work in progress; silence = a failure signal.
-- Complements (does not duplicate) heartbeats (daemons), orchestra_actors (fleet), traces (replay):
-- this is the one row-per-work-item registry that answers "what is every agent doing right now, stuck?".
-- scripts/pulse.py also creates this on demand (_ensure), so an existing DB needs no re-init.
CREATE TABLE IF NOT EXISTS agent_pulse (
    work_id            TEXT PRIMARY KEY,               -- unique handle: qa:<run>, build:<product>, run:<id>
    kind               TEXT NOT NULL,                  -- qa-run | factory-build | orchestra-run | dev-fix | ...
    label              TEXT NOT NULL DEFAULT '',
    tenant_id          TEXT,
    status             TEXT NOT NULL DEFAULT 'active', -- active | done | failed | incomplete | stalled
    stage              TEXT,                           -- current phase (explore | report | filing | ...)
    progress           TEXT,                           -- free-form live status ("step 18 · 9/11 covered")
    expected_cadence_s INT  NOT NULL DEFAULT 90,       -- how often it promises to beat; silence*MULT = stalled
    meta               JSONB NOT NULL DEFAULT '{}',
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_beat          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    result             JSONB
);
CREATE INDEX IF NOT EXISTS agent_pulse_status_idx ON agent_pulse (status, last_beat DESC);
