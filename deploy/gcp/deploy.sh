#!/usr/bin/env bash
set -euo pipefail

for command_name in gcloud tofu git curl rg openssl; do
    command -v "$command_name" >/dev/null || {
        echo "$command_name is required" >&2
        exit 2
    }
done
if [[ ! -x .venv/bin/python ]]; then
    echo ".venv/bin/python is required; run this script from the repository root" >&2
    exit 2
fi

required_variables=(
    GCP_PROJECT_ID GCP_SANDBOX_PROJECT_ID GCP_APP_PROJECT_ID
    AOS_V2_PUBLIC_BASE_URL AOS_V2_APPS_BASE_URL AOS_V2_APP_BUILDER_IMAGE
    AOS_V2_OIDC_ISSUER AOS_V2_OIDC_AUDIENCE AOS_V2_OIDC_JWKS_URL
    AOS_V2_OIDC_AUTHORIZATION_URL AOS_V2_OIDC_TOKEN_URL AOS_V2_OIDC_CLIENT_ID
    AOS_V2_STRIPE_STARTER_PRICE_ID AOS_V2_STRIPE_GROWTH_PRICE_ID AOS_V2_MODEL
    AOS_V2_MIGRATION_DATABASE_URL AOS_V2_DATABASE_RUNTIME_ROLE
)
for variable_name in "${required_variables[@]}"; do
    if [[ -z "${!variable_name:-}" ]]; then
        echo "$variable_name is required" >&2
        exit 2
    fi
done

gcp_region=${GCP_REGION:-us-central1}
deployment_environment=${AOS_ENVIRONMENT:-production}
state_bucket=${GCP_STATE_BUCKET:-${GCP_PROJECT_ID}-agentos-tofu-state}
state_prefix="agent-os/${deployment_environment}"
github_repository_id=${GITHUB_REPOSITORY_ID:-1276674620}
release_id=${AOS_RELEASE_ID:-$(git rev-parse --verify HEAD)}
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
export TF_VAR_release_id="$release_id"
export TF_VAR_app_builder_image="$AOS_V2_APP_BUILDER_IMAGE"

gcloud projects describe "$GCP_PROJECT_ID" >/dev/null
gcloud projects describe "$GCP_SANDBOX_PROJECT_ID" >/dev/null
gcloud projects describe "$GCP_APP_PROJECT_ID" >/dev/null
if ! gcloud storage buckets describe "gs://${state_bucket}" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${state_bucket}" \
        --project "$GCP_PROJECT_ID" \
        --location "$gcp_region" \
        --uniform-bucket-level-access
    gcloud storage buckets update "gs://${state_bucket}" --versioning
fi

tofu -chdir="$tofu_root" init -reconfigure \
    -backend-config="bucket=${state_bucket}" \
    -backend-config="prefix=${state_prefix}"

# Only a brand-new cell needs the foundation-only apply. Re-running an
# inactive plan against an existing cell would plan deletion of the serving
# resources, so the bootstrap is deliberately skipped once the API is in
# state. Later changes are applied migration-first below.
if ! tofu -chdir="$tofu_root" state list | rg -q '^google_cloud_run_v2_service\.api\[0\]$'; then
    tofu -chdir="$tofu_root" apply -auto-approve -var="activate_services=false"
fi

put_secret_version() {
    local logical_name=$1
    local value_variable=$2
    local allow_generated=${3:-0}
    local allow_rotation=${4:-1}
    local secret_id
    local secret_value
    secret_id=$(tofu -chdir="$tofu_root" output -json runtime_secret_ids | \
        .venv/bin/python -c "import json,sys; print(json.load(sys.stdin)['${logical_name}'])")
    if gcloud secrets versions list "$secret_id" --project "$GCP_PROJECT_ID" \
        --filter='state=ENABLED' --limit=1 --format='value(name)' | rg -q .; then
        if [[ "${AOS_ROTATE_SECRETS:-0}" != "1" || "$allow_rotation" != "1" ]]; then
            return
        fi
    fi
    secret_value=${!value_variable:-}
    if [[ -z "$secret_value" && "$allow_generated" == "1" ]]; then
        secret_value=$(openssl rand -hex 32)
    fi
    if [[ -z "$secret_value" ]]; then
        echo "$value_variable is required because $secret_id has no enabled version" >&2
        exit 2
    fi
    printf '%s' "$secret_value" | gcloud secrets versions add "$secret_id" \
        --project "$GCP_PROJECT_ID" --data-file=- >/dev/null
}

put_secret_version migration_database_url AOS_V2_MIGRATION_DATABASE_URL
put_secret_version system_database_url AOS_V2_SYSTEM_DATABASE_URL
put_secret_version application_database_url AOS_V2_APPLICATION_DATABASE_URL
put_secret_version capability_secret AOS_V2_CAPABILITY_SECRET 1
put_secret_version tenant_derivation_secret AOS_V2_TENANT_DERIVATION_SECRET 1 0
put_secret_version stripe_secret_key AOS_V2_STRIPE_SECRET_KEY
put_secret_version stripe_webhook_secret AOS_V2_STRIPE_WEBHOOK_SECRET
put_secret_version model_provider_key AOS_V2_MODEL_PROVIDER_KEY

runtime_repository=$(tofu -chdir="$tofu_root" output -raw artifact_registry_repository)
build_service_account=$(tofu -chdir="$tofu_root" output -raw build_service_account)
gcloud builds submit . --project "$GCP_PROJECT_ID" --config deploy/gcp/cloudbuild.yaml \
    --substitutions="_REPOSITORY=${runtime_repository},_RELEASE_ID=${release_id},_BUILD_SERVICE_ACCOUNT=${build_service_account}"

application_tag="${runtime_repository}/agent-os:${release_id}"
migration_tag="${runtime_repository}/migrations:${release_id}"
sandbox_tag="${runtime_repository}/sandbox:${release_id}"
application_digest=$(gcloud artifacts docker images describe "$application_tag" \
    --project "$GCP_PROJECT_ID" --format='value(image_summary.digest)')
migration_digest=$(gcloud artifacts docker images describe "$migration_tag" \
    --project "$GCP_PROJECT_ID" --format='value(image_summary.digest)')
sandbox_digest=$(gcloud artifacts docker images describe "$sandbox_tag" \
    --project "$GCP_PROJECT_ID" --format='value(image_summary.digest)')
application_image="${runtime_repository}/agent-os@${application_digest}"
migration_image="${runtime_repository}/migrations@${migration_digest}"
sandbox_image="${runtime_repository}/sandbox@${sandbox_digest}"

# Create or update only the migration job first. Targeting prevents a repeat
# release from rolling (or removing) the currently serving API/worker before
# the schema is ready. The full desired-state apply follows a successful job.
tofu -chdir="$tofu_root" apply -auto-approve \
    -target='google_cloud_run_v2_job.migrate[0]' \
    -var="activate_services=true" \
    -var="application_image=${application_image}" \
    -var="migration_image=${migration_image}" \
    -var="sandbox_image=${sandbox_image}"
gcloud run jobs execute "agentos-${deployment_environment}-migrate" \
    --project "$GCP_PROJECT_ID" --region "$gcp_region" --wait
tofu -chdir="$tofu_root" apply -auto-approve \
    -var="activate_services=true" \
    -var="application_image=${application_image}" \
    -var="migration_image=${migration_image}" \
    -var="sandbox_image=${sandbox_image}"

api_url=$(tofu -chdir="$tofu_root" output -raw api_url)
apps_url=$(tofu -chdir="$tofu_root" output -raw static_apps_url)
curl --fail --silent --show-error --retry 8 --retry-all-errors \
    --retry-delay 3 "${api_url}/ready"
curl --fail --silent --show-error --retry 8 --retry-all-errors \
    --retry-delay 3 "${apps_url}/health"
echo
echo "Agent OS ${release_id} is healthy at ${api_url}; app router: ${apps_url}"
echo "Configure DNS/OIDC/Stripe for ${AOS_V2_PUBLIC_BASE_URL} and app DNS for ${AOS_V2_APPS_BASE_URL} before customer traffic."
