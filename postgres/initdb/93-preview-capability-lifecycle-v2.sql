-- Make every public preview capability short-lived and owner-revocable.

ALTER TABLE public.aos_v2_preview_deployments
    ADD COLUMN IF NOT EXISTS expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS revoked_at timestamptz,
    ADD COLUMN IF NOT EXISTS revocation_key text;

UPDATE public.aos_v2_preview_deployments
SET expires_at = created_at + interval '7 days'
WHERE expires_at IS NULL;

UPDATE public.aos_v2_preview_deployments
SET record = record || jsonb_build_object(
    'expires_at', expires_at,
    'revoked_at', revoked_at,
    'active', active
);

ALTER TABLE public.aos_v2_preview_deployments
    ALTER COLUMN expires_at SET NOT NULL;

ALTER TABLE public.aos_v2_preview_deployments
    DROP CONSTRAINT IF EXISTS aos_v2_preview_deployments_expiry_check;
ALTER TABLE public.aos_v2_preview_deployments
    ADD CONSTRAINT aos_v2_preview_deployments_expiry_check
    CHECK (expires_at > created_at);

ALTER TABLE public.aos_v2_preview_deployments
    DROP CONSTRAINT IF EXISTS aos_v2_preview_deployments_revocation_check;
ALTER TABLE public.aos_v2_preview_deployments
    ADD CONSTRAINT aos_v2_preview_deployments_revocation_check
    CHECK (
        (active AND revoked_at IS NULL AND revocation_key IS NULL)
        OR
        (NOT active AND revoked_at IS NOT NULL AND revocation_key IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS aos_v2_preview_deployments_public_active_idx
    ON public.aos_v2_preview_deployments (tenant_id, public_id, expires_at)
    WHERE active;
