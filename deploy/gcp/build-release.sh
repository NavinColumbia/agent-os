#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 5 ]]; then
    echo "usage: build-release.sh PROJECT REPOSITORY RELEASE_COMMIT BUILD_SERVICE_ACCOUNT DOCKER_BUILDER_IMAGE" >&2
    exit 2
fi

project_id=$1
repository=$2
release_id=$3
build_service_account=$4
docker_builder_image=$5

[[ "$project_id" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || {
    echo "invalid GCP project ID" >&2
    exit 2
}
[[ "$repository" =~ ^[A-Za-z0-9._/-]+$ ]] || {
    echo "invalid Artifact Registry repository" >&2
    exit 2
}
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || {
    echo "release must be an exact 40-character Git commit" >&2
    exit 2
}
[[ "$build_service_account" =~ ^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/serviceAccounts/[A-Za-z0-9@._-]+$ ]] || {
    echo "invalid build service-account resource" >&2
    exit 2
}
[[ "$docker_builder_image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
    echo "Docker builder image must be pinned by sha256 digest" >&2
    exit 2
}
for command_name in gcloud git; do
    command -v "$command_name" >/dev/null || {
        echo "$command_name is required" >&2
        exit 2
    }
done
git cat-file -e "${release_id}^{commit}"

build_directory=$(mktemp -d /tmp/agentos-cloud-build.XXXXXX)
cleanup() {
    rm -rf -- "$build_directory"
}
trap cleanup EXIT
source_archive="${build_directory}/source.tar.gz"
build_config="${build_directory}/cloudbuild.yaml"

# Both source and build instructions come from the same immutable commit.
# Dirty and untracked workspace files therefore cannot enter a release.
git archive --format=tar.gz --output="$source_archive" "$release_id"
git show "${release_id}:deploy/gcp/cloudbuild.yaml" > "$build_config"

if [[ -n "$(git status --short)" ]]; then
    echo "notice: uncommitted workspace changes are excluded from release ${release_id}" >&2
fi
gcloud builds submit "$source_archive" \
    --project "$project_id" \
    --config "$build_config" \
    --substitutions="_REPOSITORY=${repository},_RELEASE_ID=${release_id},_BUILD_SERVICE_ACCOUNT=${build_service_account},_DOCKER_BUILDER_IMAGE=${docker_builder_image}"
