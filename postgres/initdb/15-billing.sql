-- SaaS billing: a plan per tenant. Usage is DERIVED from real activity (builds shipped + tokens
-- spent on the tenant's products), so metering can't drift from what actually happened.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT false;
