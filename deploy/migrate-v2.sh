#!/bin/sh
set -eu

: "${AOS_V2_MIGRATION_DATABASE_URL:?AOS_V2_MIGRATION_DATABASE_URL is required}"
: "${AOS_V2_DATABASE_RUNTIME_ROLE:?AOS_V2_DATABASE_RUNTIME_ROLE is required}"

case "$AOS_V2_DATABASE_RUNTIME_ROLE" in
    *[!a-zA-Z0-9_-]*|'')
        echo "AOS_V2_DATABASE_RUNTIME_ROLE is invalid" >&2
        exit 2
        ;;
esac

# Order first by the numeric revision and then by the length of the intentional
# compatibility suffix (99, 99z, 99zz, ...). Version sorting reverses those suffixes,
# while plain lexical order would place revision 100 before 86.
find /migrations -maxdepth 1 -type f -name '*.sql' -print |
awk '
{
    count = split($0, parts, "/")
    base = parts[count]
    match(base, /^[0-9]+/)
    revision = substr(base, RSTART, RLENGTH) + 0
    stem = base
    sub(/-.*/, "", stem)
    suffix_length = length(stem) - RLENGTH
    printf "%012d\t%04d\t%s\n", revision, suffix_length, $0
}' |
sort -t '	' -k1,1n -k2,2n -k3,3 |
cut -f3- |
while IFS= read -r migration; do
    psql "$AOS_V2_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f "$migration"
done

# The application login must be distinct from the table owner and must never
# bypass RLS. psql identifier interpolation safely quotes the configured role.
psql "$AOS_V2_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 \
    -v runtime_role="$AOS_V2_DATABASE_RUNTIME_ROLE" <<'SQL'
SELECT count(*) = 1 AS runtime_role_is_safe
  FROM pg_roles
 WHERE rolname = :'runtime_role'
   AND rolcanlogin
   AND NOT rolsuper
   AND NOT rolbypassrls
\gset
\if :runtime_role_is_safe
ALTER ROLE :"runtime_role" NOINHERIT;
GRANT agentos_app, agentos_worker TO :"runtime_role";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
GRANT USAGE ON SCHEMA public TO :"runtime_role";
SELECT format(
    'CREATE SCHEMA IF NOT EXISTS dbos AUTHORIZATION %I',
    :'runtime_role'
)
\gexec
SELECT format('ALTER SCHEMA dbos OWNER TO %I', :'runtime_role')
\gexec
\else
\echo 'runtime database role is missing, cannot login, or bypasses RLS'
\quit 3
\endif
SQL
