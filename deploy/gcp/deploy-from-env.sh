#!/usr/bin/env bash
set -euo pipefail

reuse_existing_secrets=0
if [[ "${1:-}" == "--reuse-existing-secrets" ]]; then
    reuse_existing_secrets=1
    shift
fi
if [[ "$#" -ne 0 ]]; then
    echo "usage: deploy-from-env.sh [--reuse-existing-secrets]" >&2
    exit 2
fi

environment_file=${AOS_GCP_LAUNCH_ENV_FILE:-.runtime/gcp-launch.env}
if [[ ! -f "$environment_file" || -L "$environment_file" ]]; then
    echo "$environment_file must be a regular non-symlink file" >&2
    exit 2
fi
file_mode=$(stat -c '%a' "$environment_file")
file_owner=$(stat -c '%u' "$environment_file")
if [[ "$file_mode" != "600" || "$file_owner" != "$(id -u)" ]]; then
    echo "$environment_file must be owned by the current user with mode 600" >&2
    exit 2
fi

preflight=(.venv/bin/python deploy/gcp/launch_preflight.py --env-file "$environment_file")
if [[ "$reuse_existing_secrets" != "1" ]]; then
    preflight+=(--require-bootstrap-secrets)
fi
"${preflight[@]}"

set -a
# shellcheck disable=SC1090 -- exact owner-only file selected above
source "$environment_file"
set +a
exec deploy/gcp/deploy.sh
