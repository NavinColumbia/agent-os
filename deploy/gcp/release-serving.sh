#!/usr/bin/env bash
set -euo pipefail

for command_name in gcloud tofu rg date; do
    command -v "$command_name" >/dev/null || {
        echo "$command_name is required" >&2
        exit 2
    }
done

release_id=${1:-${TF_VAR_release_id:-}}
if [[ ! "$release_id" =~ ^[A-Za-z0-9._-]{1,128}$ ]]; then
    echo "a safe release ID argument is required" >&2
    exit 2
fi
if [[ -z "${GCP_PROJECT_ID:-}" ]]; then
    echo "GCP_PROJECT_ID is required" >&2
    exit 2
fi

tofu_root=${AOS_TOFU_ROOT:-deploy/gcp}
gcp_region=${GCP_REGION:-${TF_VAR_region:-us-central1}}
deployment_environment=${AOS_ENVIRONMENT:-${TF_VAR_environment:-production}}
python_bin=${AOS_PYTHON_BIN:-python}
stable_worker_pool="agentos-${deployment_environment}-worker"
rollout_worker_pool="agentos-${deployment_environment}-worker-rollout"
activation_job="agentos-${deployment_environment}-activate-release"

wait_for_worker_event() {
    local worker_pool_name=$1
    local expected_event=$2
    local not_before=$3
    local timeout_seconds=${AOS_V2_WORKER_ROLLOUT_TIMEOUT_SECONDS:-1200}
    local deadline=$((SECONDS + timeout_seconds))
    local filter
    filter="resource.type=\"cloud_run_workerpool\" AND resource.labels.workerpool_name=\"${worker_pool_name}\" AND jsonPayload.event=\"${expected_event}\" AND jsonPayload.application_version=\"${release_id}\" AND timestamp>=\"${not_before}\""
    while (( SECONDS < deadline )); do
        if gcloud logging read "$filter" \
            --project "$GCP_PROJECT_ID" --freshness=1d --limit=1 \
            --format='value(timestamp)' | rg -q .; then
            return 0
        fi
        sleep 10
    done
    echo "worker pool ${worker_pool_name} did not publish ${expected_event}" >&2
    return 1
}

run_edge_check() {
    local api_url apps_url edge_ip
    api_url=$(tofu -chdir="$tofu_root" output -raw api_url)
    apps_url=$(tofu -chdir="$tofu_root" output -raw static_apps_url)
    edge_ip=$(tofu -chdir="$tofu_root" output -raw public_edge_ipv4)
    "$python_bin" deploy/gcp/edge_check.py \
        --api-url "$api_url" --apps-url "$apps_url" --expected-ip "$edge_ip" \
        --timeout-seconds "${AOS_V2_EDGE_READY_TIMEOUT_SECONDS:-3600}" \
        --interval-seconds "${AOS_V2_EDGE_READY_INTERVAL_SECONDS:-15}"
}

if gcloud run worker-pools describe "$stable_worker_pool" \
    --project "$GCP_PROJECT_ID" --region "$gcp_region" >/dev/null 2>&1; then
    # Candidate processes can read the queue and publish health, but the
    # durable release fence prevents them from claiming any work yet.
    probe_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    tofu -chdir="$tofu_root" apply -auto-approve -input=false \
        -target='google_cloud_run_v2_job.activate_execution_release[0]' \
        -target='google_cloud_run_v2_worker_pool.rollout_worker[0]' \
        -var="activate_services=true" \
        -var="rollout_worker_enabled=true"
    wait_for_worker_event "$rollout_worker_pool" \
        "worker_release_probe_ready" "$probe_started_at"

    # This is the sole queue-claim cutover. Old workers finish in-flight work
    # but fail the fence before their next discovery/claim cycle.
    activation_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    gcloud run jobs execute "$activation_job" \
        --project "$GCP_PROJECT_ID" --region "$gcp_region" --wait
    wait_for_worker_event "$rollout_worker_pool" \
        "worker_release_ready" "$activation_started_at"

    tofu -chdir="$tofu_root" apply -auto-approve -input=false \
        -target='google_cloud_run_v2_service.api[0]' \
        -target='google_cloud_run_v2_service.static_router[0]' \
        -var="activate_services=true" \
        -var="rollout_worker_enabled=true"
    run_edge_check

    stable_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if [[ "${AOS_SERVING_ONLY:-0}" == "1" ]]; then
        tofu -chdir="$tofu_root" apply -auto-approve -input=false \
            -target='google_cloud_run_v2_worker_pool.worker[0]' \
            -var="activate_services=true" \
            -var="rollout_worker_enabled=true"
    else
        tofu -chdir="$tofu_root" apply -auto-approve -input=false \
            -var="activate_services=true" \
            -var="rollout_worker_enabled=true"
    fi
    wait_for_worker_event "$stable_worker_pool" \
        "worker_release_ready" "$stable_started_at"

    tofu -chdir="$tofu_root" apply -auto-approve -input=false \
        -target='google_cloud_run_v2_worker_pool.rollout_worker[0]' \
        -var="activate_services=true" \
        -var="rollout_worker_enabled=false"
else
    # A new cell has no traffic and no active execution release. Build the
    # serving plane, explicitly activate it, and only then require readiness.
    stable_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    tofu -chdir="$tofu_root" apply -auto-approve -input=false \
        -var="activate_services=true" \
        -var="rollout_worker_enabled=false"
    wait_for_worker_event "$stable_worker_pool" \
        "worker_release_probe_ready" "$stable_started_at"
    activation_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    gcloud run jobs execute "$activation_job" \
        --project "$GCP_PROJECT_ID" --region "$gcp_region" --wait
    wait_for_worker_event "$stable_worker_pool" \
        "worker_release_ready" "$activation_started_at"
    run_edge_check
fi

run_edge_check
