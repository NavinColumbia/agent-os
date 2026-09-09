#!/usr/bin/env bash
set -euo pipefail

for command_name in gcloud tofu curl date rg; do
    command -v "$command_name" >/dev/null || {
        echo "$command_name is required" >&2
        exit 2
    }
done

required_variables=(
    GCP_PROJECT_ID GCP_SANDBOX_PROJECT_ID GCP_APP_PROJECT_ID
    AOS_V2_PUBLIC_BASE_URL AOS_V2_APPS_BASE_URL
    AOS_V2_OIDC_ISSUER AOS_V2_OIDC_AUDIENCE AOS_V2_OIDC_JWKS_URL
    AOS_V2_OIDC_AUTHORIZATION_URL AOS_V2_OIDC_TOKEN_URL AOS_V2_OIDC_CLIENT_ID
    AOS_V2_STRIPE_STARTER_PRICE_ID AOS_V2_STRIPE_GROWTH_PRICE_ID AOS_V2_MODEL
    AOS_V2_DATABASE_RUNTIME_ROLE
)
for variable_name in "${required_variables[@]}"; do
    if [[ -z "${!variable_name:-}" ]]; then
        echo "$variable_name is required" >&2
        exit 2
    fi
done
if [[ -z "${AOS_ROLLBACK_APPLICATION_IMAGE:-}" && -z "${AOS_ROLLBACK_RELEASE_ID:-}" ]]; then
    echo "AOS_ROLLBACK_APPLICATION_IMAGE or AOS_ROLLBACK_RELEASE_ID is required" >&2
    exit 2
fi

gcp_region=${GCP_REGION:-us-central1}
deployment_environment=${AOS_ENVIRONMENT:-production}
state_bucket=${GCP_STATE_BUCKET:-${GCP_PROJECT_ID}-agentos-tofu-state}
state_prefix="agent-os/${deployment_environment}"
github_repository_id=${GITHUB_REPOSITORY_ID:-1276674620}
tofu_root=deploy/gcp

export TF_VAR_project_id="$GCP_PROJECT_ID"
export TF_VAR_sandbox_project_id="$GCP_SANDBOX_PROJECT_ID"
export TF_VAR_app_project_id="$GCP_APP_PROJECT_ID"
export TF_VAR_region="$gcp_region"
export TF_VAR_environment="$deployment_environment"
export TF_VAR_public_base_url="$AOS_V2_PUBLIC_BASE_URL"
export TF_VAR_apps_base_url="$AOS_V2_APPS_BASE_URL"
export TF_VAR_state_bucket_name="$state_bucket"
export TF_VAR_github_repository_id="$github_repository_id"
export TF_VAR_oidc_issuer="$AOS_V2_OIDC_ISSUER"
export TF_VAR_oidc_audience="$AOS_V2_OIDC_AUDIENCE"
export TF_VAR_oidc_jwks_url="$AOS_V2_OIDC_JWKS_URL"
export TF_VAR_oidc_authorization_url="$AOS_V2_OIDC_AUTHORIZATION_URL"
export TF_VAR_oidc_token_url="$AOS_V2_OIDC_TOKEN_URL"
export TF_VAR_oidc_client_id="$AOS_V2_OIDC_CLIENT_ID"
export TF_VAR_oidc_scope="${AOS_V2_OIDC_SCOPE:-openid profile email}"
export TF_VAR_oidc_authorization_audience_parameter="${AOS_V2_OIDC_AUTHORIZATION_AUDIENCE_PARAMETER:-}"
export TF_VAR_stripe_starter_price_id="$AOS_V2_STRIPE_STARTER_PRICE_ID"
export TF_VAR_stripe_growth_price_id="$AOS_V2_STRIPE_GROWTH_PRICE_ID"
export TF_VAR_model="$AOS_V2_MODEL"
export TF_VAR_database_runtime_role="$AOS_V2_DATABASE_RUNTIME_ROLE"
export TF_VAR_model_provider_secret_environment="${AOS_V2_MODEL_PROVIDER_SECRET_ENVIRONMENT:-OPENAI_API_KEY}"
export TF_VAR_activate_services=true

gcloud projects describe "$GCP_PROJECT_ID" >/dev/null
tofu -chdir="$tofu_root" init -reconfigure -input=false \
    -backend-config="bucket=${state_bucket}" \
    -backend-config="prefix=${state_prefix}"

current_migration_image=$(tofu -chdir="$tofu_root" output -raw migration_image)
current_sandbox_image=$(tofu -chdir="$tofu_root" output -raw sandbox_image)
current_app_builder_image=$(tofu -chdir="$tofu_root" output -raw app_builder_image)
if [[ ! "$current_migration_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "current migration image in state is not digest-pinned; refusing rollback" >&2
    exit 2
fi
if [[ ! "$current_sandbox_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "current sandbox image in state is not digest-pinned; refusing rollback" >&2
    exit 2
fi
if [[ ! "$current_app_builder_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "current generated-app builder image in state is not digest-pinned; refusing rollback" >&2
    exit 2
fi

rollback_image=${AOS_ROLLBACK_APPLICATION_IMAGE:-}
rollback_release_id=${AOS_ROLLBACK_RELEASE_ID:-}
if [[ -n "$rollback_release_id" && ! "$rollback_release_id" =~ ^[A-Za-z0-9._-]{1,128}$ ]]; then
    echo "AOS_ROLLBACK_RELEASE_ID is invalid" >&2
    exit 2
fi
if [[ -z "$rollback_image" ]]; then
    runtime_repository=$(tofu -chdir="$tofu_root" output -raw artifact_registry_repository)
    rollback_tag="${runtime_repository}/agent-os:${rollback_release_id}"
    rollback_digest=$(gcloud artifacts docker images describe "$rollback_tag" \
        --project "$GCP_PROJECT_ID" --format='value(image_summary.digest)')
    rollback_image="${runtime_repository}/agent-os@${rollback_digest}"
fi
if [[ ! "$rollback_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "rollback application image must be pinned by sha256 digest" >&2
    exit 2
fi
if [[ -z "$rollback_release_id" ]]; then
    rollback_release_id="rollback-$(date -u +%Y%m%dT%H%M%SZ)"
fi

export TF_VAR_application_image="$rollback_image"
export TF_VAR_migration_image="$current_migration_image"
export TF_VAR_sandbox_image="$current_sandbox_image"
export TF_VAR_app_builder_image="$current_app_builder_image"
export TF_VAR_release_id="$rollback_release_id"

# Roll back serving revisions only. The migration job is not updated or run;
# production migrations are required to be backward-compatible expand/contract changes.
tofu -chdir="$tofu_root" apply -auto-approve -input=false \
    -target='google_cloud_run_v2_service.api[0]' \
    -target='google_cloud_run_v2_service.static_router[0]' \
    -target='google_cloud_run_v2_worker_pool.worker[0]'

api_url=$(tofu -chdir="$tofu_root" output -raw api_url)
apps_url=$(tofu -chdir="$tofu_root" output -raw static_apps_url)
curl --fail --silent --show-error --retry 8 --retry-all-errors \
    --retry-delay 3 "${api_url}/ready"
curl --fail --silent --show-error --retry 8 --retry-all-errors \
    --retry-delay 3 "${apps_url}/health"
echo
echo "Agent OS serving plane rolled back to ${rollback_image}; API and app router are healthy"
