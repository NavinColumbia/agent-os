-- Cross-tenant scheduler fairness is global control-plane state, not customer
-- data. Keep the cursor owner-only so no tenant app role can steer which tenant
-- gets the next forecast pass.

CREATE TABLE IF NOT EXISTS forecast_sweep_state (
  name TEXT PRIMARY KEY,
  cursor_tenant_id TEXT NOT NULL DEFAULT ''
);
INSERT INTO forecast_sweep_state(name,cursor_tenant_id)
VALUES ('forecast-sweep','') ON CONFLICT (name) DO NOTHING;

REVOKE ALL ON TABLE forecast_sweep_state FROM agentos_app;
