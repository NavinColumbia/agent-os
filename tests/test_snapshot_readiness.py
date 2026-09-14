import importlib.util
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aos_snapshot", ROOT / "platform" / "snapshot.py")
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


def test_snapshot_is_location_independent_and_includes_product_source_policy():
    assert snapshot.ROOT == ROOT
    keep = tarfile.TarInfo("products/customer-app/src/app.py")
    dependency = tarfile.TarInfo("products/customer-app/node_modules/pkg/index.js")
    virtualenv = tarfile.TarInfo("products/customer-app/venv/bin/python")
    assert snapshot._product_tar_filter(keep) is keep
    assert snapshot._product_tar_filter(dependency) is None
    assert snapshot._product_tar_filter(virtualenv) is None


def test_legacy_virtualenv_links_are_skipped_but_source_links_stay_fail_closed(tmp_path):
    generated = tarfile.TarInfo("./products/customer-app/venv/bin/python3")
    generated.type = tarfile.SYMTYPE
    generated.linkname = "/usr/bin/python3"
    assert snapshot._snapshot_extract_filter(generated, str(tmp_path)) is None

    source = tarfile.TarInfo("./products/customer-app/src/runtime-link")
    source.type = tarfile.SYMTYPE
    source.linkname = "/etc/passwd"
    try:
        snapshot._snapshot_extract_filter(source, str(tmp_path))
    except tarfile.AbsoluteLinkError:
        pass
    else:
        raise AssertionError("an absolute source symlink must remain a hard restore failure")


def test_configured_offsite_copy_is_atomic_and_retained(monkeypatch, tmp_path):
    local = tmp_path / "local"; local.mkdir()
    offsite = tmp_path / "offsite"; offsite.mkdir()
    artifact = local / "agent-os-20260824-000000.aosnap"
    artifact.write_bytes(b"encrypted-backup")

    monkeypatch.delenv("AOSNAP_SKIP_OFFSITE", raising=False)
    monkeypatch.setattr(snapshot, "_cfg", lambda key: str(offsite) if key == "AOSNAP_OFFSITE_DIR" else None)
    snapshot._offsite(artifact, keep=2)

    assert (offsite / artifact.name).read_bytes() == b"encrypted-backup"
    assert not list(offsite.glob(".*.tmp"))


def test_daily_snapshot_has_a_long_but_finite_scheduler_budget():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import scheduler

    schedules = {name: (command, interval) for name, command, interval in scheduler.DEFAULT_SCHEDULES}
    command, interval = schedules["encrypted-snapshot"]
    assert "platform/snapshot.py" in command and interval == 86400
    assert scheduler.JOB_TIMEOUT < scheduler._job_timeout("encrypted-snapshot") <= 7200
