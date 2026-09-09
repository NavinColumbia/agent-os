from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "deploy" / "gcp" / "build-release.sh"


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE,
    ).stdout.strip()


def test_release_builder_submits_only_the_exact_commit_and_its_build_config(tmp_path: Path):
    repository = tmp_path / "source"
    repository.mkdir()
    run("git", "init", "-q", cwd=repository)
    run("git", "config", "user.name", "Agent OS test", cwd=repository)
    run("git", "config", "user.email", "test@example.invalid", cwd=repository)
    config = repository / "deploy" / "gcp" / "cloudbuild.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("steps: pinned\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
    run("git", "add", ".", cwd=repository)
    run("git", "commit", "-q", "-m", "fixture", cwd=repository)
    release_id = run("git", "rev-parse", "HEAD", cwd=repository)

    config.write_text("steps: dirty-mutation\n", encoding="utf-8")
    (repository / "untracked-secret.txt").write_text("must-not-upload\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "test \"$1\" = builds\n"
        "test \"$2\" = submit\n"
        "tar -tzf \"$3\" > \"$CAPTURE_ARCHIVE\"\n"
        "test \"$6\" = --config\n"
        "cp \"$7\" \"$CAPTURE_CONFIG\"\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    archive_listing = tmp_path / "archive.txt"
    captured_config = tmp_path / "config.yaml"
    captured_args = tmp_path / "args.txt"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE_ARCHIVE": str(archive_listing),
        "CAPTURE_CONFIG": str(captured_config),
        "CAPTURE_ARGS": str(captured_args),
    }

    completed = subprocess.run(
        [
            str(BUILD_SCRIPT),
            "abcde1",
            "us-central1-docker.pkg.dev/abcde1/runtime",
            release_id,
            "projects/abcde1/serviceAccounts/builder@abcde1.iam.gserviceaccount.com",
            "gcr.io/cloud-builders/docker@sha256:" + "a" * 64,
        ],
        cwd=repository,
        env=environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    listing = archive_listing.read_text(encoding="utf-8")
    assert "tracked.txt" in listing
    assert "untracked-secret.txt" not in listing
    assert captured_config.read_text(encoding="utf-8") == "steps: pinned\n"
    assert "notice: uncommitted workspace changes are excluded" in completed.stderr
    assert "_DOCKER_BUILDER_IMAGE=gcr.io/cloud-builders/docker@sha256:" in (
        captured_args.read_text(encoding="utf-8")
    )


def test_release_builder_rejects_non_digest_builder_before_gcloud(tmp_path: Path):
    completed = subprocess.run(
        [
            str(BUILD_SCRIPT), "abcde1", "us-central1-docker.pkg.dev/abcde1/runtime",
            "a" * 40,
            "projects/abcde1/serviceAccounts/builder@abcde1.iam.gserviceaccount.com",
            "gcr.io/cloud-builders/docker:latest",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert completed.returncode == 2
    assert "pinned by sha256" in completed.stderr
