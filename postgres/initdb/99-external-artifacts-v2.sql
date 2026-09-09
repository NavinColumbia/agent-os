-- Keep artifact identity/idempotency metadata under tenant RLS while allowing
-- immutable payload bytes to live in private object storage.

ALTER TABLE public.aos_v2_artifacts
    ADD COLUMN IF NOT EXISTS storage_backend text;
ALTER TABLE public.aos_v2_artifacts
    ADD COLUMN IF NOT EXISTS object_name text;

UPDATE public.aos_v2_artifacts
   SET storage_backend = 'inline'
 WHERE storage_backend IS NULL;

ALTER TABLE public.aos_v2_artifacts
    ALTER COLUMN storage_backend SET DEFAULT 'inline';
ALTER TABLE public.aos_v2_artifacts
    ALTER COLUMN storage_backend SET NOT NULL;
ALTER TABLE public.aos_v2_artifacts
    ALTER COLUMN content DROP NOT NULL;

-- Migration 90 predated named checks. Drop only its inline byte-length check,
-- regardless of the name PostgreSQL generated for it.
DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'public.aos_v2_artifacts'::regclass
           AND contype = 'c'
           AND pg_get_constraintdef(oid) LIKE '%octet_length(content)%'
    LOOP
        EXECUTE format(
            'ALTER TABLE public.aos_v2_artifacts DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END $$;

ALTER TABLE public.aos_v2_artifacts
    DROP CONSTRAINT IF EXISTS aos_v2_artifacts_storage_shape;
ALTER TABLE public.aos_v2_artifacts
    ADD CONSTRAINT aos_v2_artifacts_storage_shape CHECK (
        (
            storage_backend = 'inline'
            AND content IS NOT NULL
            AND object_name IS NULL
            AND octet_length(content) = byte_length
        )
        OR
        (
            storage_backend = 'gcs'
            AND content IS NULL
            AND object_name ~ '^tenants/[0-9a-f]{64}/artifacts/artifact-[0-9a-f]{64}$'
        )
    );
