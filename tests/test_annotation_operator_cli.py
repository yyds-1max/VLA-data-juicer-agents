from __future__ import annotations

import json
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vla_data_juicer_agents.annotation.store as annotation_store_module
from vla_data_juicer_agents.annotation import operator_cli
from vla_data_juicer_agents.annotation.maintenance import (
    AnnotationServiceOnlineError,
    acquire_annotation_maintenance,
    annotation_maintenance_lock_path,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.writer_lock import (
    ensure_navigation_writer_quarantine,
    navigation_writer_quarantine_present,
)
from vla_data_juicer_agents.web.app import create_app


GLOBAL_CONFIRMATION = "all_navigation_annotation_writer_process_groups_absent"
JOB_CONFIRMATION = "old_process_group_absent"


def _hold_maintenance_lease_in_child(
    database: str,
    connection: Connection,
) -> None:
    try:
        lease = acquire_annotation_maintenance(
            Path(database),
            create_lock_file=True,
        )
    except BaseException as exc:
        connection.send(("error", type(exc).__name__))
        connection.close()
        return
    try:
        connection.send(("locked", None))
        connection.recv()
    finally:
        lease.close()
        connection.close()


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _recovery_fixture(
    tmp_path: Path,
    *,
    suffix: str = "a",
) -> tuple[Path, Path, dict[str, object]]:
    state = _private_directory(tmp_path / f"state-{suffix}")
    database = state / "annotation.sqlite"
    locks = _private_directory(tmp_path / f"locks-{suffix}")
    writer_lock = locks / "navigation-annotation-writer.lock"
    store = AnnotationStore(database)
    job = store.create_job(
        job_ref="job_" + suffix * 32,
        dataset_date="20270605",
        source_clips=[f"20270605_16090{suffix}"],
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "processing calibration",
            "content_sha256": "a" * 64,
        },
        snapshot_dir=state / "private-calibration-snapshot",
        snapshot_files=[],
        reserved_bytes=1,
        idempotency_key=f"create-{suffix}",
    )
    claimed = store.claim_next_run(worker_id=f"crashed-worker-{suffix}")
    assert claimed is not None
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM runtime_leases")
    assert store.recover_interrupted_runs(writer_lock_path=writer_lock) == 1
    failed = store.get_job(str(job["job_ref"]))
    assert failed["failure"]["code"] == "recovery_required"
    return database, writer_lock, failed


def _scope_args(database: Path, writer_lock: Path) -> list[str]:
    return [
        "--annotation-db",
        str(database),
        "--writer-lock",
        str(writer_lock),
    ]


def _configure_production_scope(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    writer_lock: Path,
) -> None:
    monkeypatch.setenv(
        "VLA_DATA_AGENT_WEB_WORKING_DIR",
        str(database.parent),
    )
    monkeypatch.setenv(
        "VLA_NAVIGATION_WRITER_LOCK_PATH",
        str(writer_lock),
    )
    with acquire_annotation_maintenance(
        database,
        create_lock_file=True,
    ):
        pass


def _clear_args(
    database: Path,
    writer_lock: Path,
    *,
    operator_reference: str = "OPS-GLOBAL-001",
    idempotency_key: str = "clear-global-001",
    confirmation: str = GLOBAL_CONFIRMATION,
) -> list[str]:
    return [
        *_scope_args(database, writer_lock),
        "clear-global",
        "--confirmation",
        confirmation,
        "--operator-reference",
        operator_reference,
        "--idempotency-key",
        idempotency_key,
    ]


def _confirm_args(
    database: Path,
    writer_lock: Path,
    failed: dict[str, object],
    action_ref: str,
    *,
    disposition: str,
    idempotency_key: str,
) -> list[str]:
    return [
        *_scope_args(database, writer_lock),
        "confirm-job",
        disposition,
        "--job-ref",
        str(failed["job_ref"]),
        "--expected-job-revision",
        str(failed["state_revision"]),
        "--global-action-ref",
        action_ref,
        "--confirmation",
        JOB_CONFIRMATION,
        "--operator-reference",
        f"OPS-JOB-{disposition.upper()}",
        "--idempotency-key",
        idempotency_key,
    ]


def _json_output(text: str) -> dict[str, object]:
    return json.loads(text)


def test_list_recovery_is_read_only_and_projects_only_safe_public_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, failed = _recovery_fixture(tmp_path)
    _configure_production_scope(monkeypatch, database, writer_lock)
    private_path = str(tmp_path / "never-publish-this-path")
    private_command = f"rm -rf {private_path}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE annotation_jobs
            SET failure_message = ?, private_failure_detail = ?
            WHERE job_ref = ?
            """,
            (
                f"unsafe sk-abcdefghijklmnop {private_path}",
                private_command,
                failed["job_ref"],
            ),
        )
    durable_database_files = [
        path
        for path in (database, Path(f"{database}-wal"))
        if path.is_file()
    ]
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in durable_database_files
    }
    maintenance_lock = annotation_maintenance_lock_path(database)
    maintenance_identity = (
        maintenance_lock.stat().st_dev,
        maintenance_lock.stat().st_ino,
        maintenance_lock.stat().st_mtime_ns,
        maintenance_lock.stat().st_ctime_ns,
        maintenance_lock.stat().st_mode,
        maintenance_lock.read_bytes(),
    )

    assert operator_cli.main(
        [*_scope_args(database, writer_lock), "list-recovery"],
    ) == 0

    captured = capsys.readouterr()
    payload = _json_output(captured.out)
    assert captured.err == ""
    assert payload["ok"] is True
    result = payload["result"]
    assert result["global_writer"] == {
        "active_marker_present": False,
        "marker_count": 1,
        "quarantine_marker_present": True,
        "recovery_marker_present": True,
    }
    assert result["jobs"] == [
        {
            "cancel_requested": False,
            "dataset_date": "20270605",
            "error_ref": failed["failure"]["error_ref"],
            "job_ref": failed["job_ref"],
            "state_revision": failed["state_revision"],
            "status": "failed",
        }
    ]
    assert str(tmp_path) not in captured.out
    assert "sk-abcdefghijklmnop" not in captured.out
    assert "rm -rf" not in captured.out
    after = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in durable_database_files
    }
    # SQLite may update ephemeral read-lock slots in the shared-memory sidecar
    # even for a query-only connection. The durable database and WAL must not
    # change.
    assert after == before
    assert (
        maintenance_lock.stat().st_dev,
        maintenance_lock.stat().st_ino,
        maintenance_lock.stat().st_mtime_ns,
        maintenance_lock.stat().st_ctime_ns,
        maintenance_lock.stat().st_mode,
        maintenance_lock.read_bytes(),
    ) == maintenance_identity


@pytest.mark.parametrize("disposition", ["retry", "abandon"])
def test_cli_reuses_global_and_job_store_recovery_with_idempotent_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    disposition: str,
) -> None:
    database, writer_lock, failed = _recovery_fixture(
        tmp_path,
        suffix="b" if disposition == "retry" else "c",
    )
    _configure_production_scope(monkeypatch, database, writer_lock)
    assert operator_cli.main(_clear_args(database, writer_lock)) == 0
    cleared = _json_output(capsys.readouterr().out)
    action_ref = cleared["result"]["action_ref"]
    assert action_ref.startswith("writer_quarantine_action_")
    assert str(tmp_path) not in json.dumps(cleared)

    args = _confirm_args(
        database,
        writer_lock,
        failed,
        action_ref,
        disposition=disposition,
        idempotency_key=f"confirm-{disposition}-001",
    )
    assert operator_cli.main(args) == 0
    first = capsys.readouterr()
    assert operator_cli.main(args) == 0
    replay = capsys.readouterr()
    assert first.out == replay.out
    assert first.err == replay.err == ""
    payload = _json_output(first.out)
    assert payload["result"]["job_ref"] == failed["job_ref"]
    if disposition == "retry":
        assert payload["result"]["status"] == "preparing"
    else:
        assert payload["result"] == {
            "completion_outcome": "abandoned_after_recovery_confirmation",
            "job_ref": failed["job_ref"],
            "state_revision": int(failed["state_revision"]) + 1,
            "status": "cancelled",
        }
    serialized = json.dumps(payload)
    assert str(tmp_path) not in serialized
    assert "operator_reference" not in serialized
    assert "idempotency" not in serialized
    assert "job_id" not in serialized


def test_clear_global_preserves_store_idempotency_conflict_and_safe_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="d")
    _configure_production_scope(monkeypatch, database, writer_lock)
    assert operator_cli.main(
        _clear_args(
            database,
            writer_lock,
            idempotency_key="same-global-key",
        ),
    ) == 0
    capsys.readouterr()

    assert operator_cli.main(
        _clear_args(
            database,
            writer_lock,
            operator_reference=f"DIFFERENT {tmp_path}",
            idempotency_key="same-global-key",
        ),
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "idempotency_key_reused"},
        "ok": False,
    }
    assert str(tmp_path) not in captured.err


def test_confirm_job_requires_the_exact_store_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, failed = _recovery_fixture(tmp_path, suffix="e")
    _configure_production_scope(monkeypatch, database, writer_lock)
    assert operator_cli.main(_clear_args(database, writer_lock)) == 0
    action_ref = _json_output(capsys.readouterr().out)["result"]["action_ref"]
    args = _confirm_args(
        database,
        writer_lock,
        failed,
        action_ref,
        disposition="retry",
        idempotency_key="wrong-job-confirmation",
    )
    args[args.index(JOB_CONFIRMATION)] = f"wrong-confirmation {tmp_path}"

    assert operator_cli.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "invalid_recovery_confirmation"},
        "ok": False,
    }
    assert str(tmp_path) not in captured.err


@pytest.mark.parametrize(
    ("command_tail", "expected_code"),
    [
        (
            [
                "clear-global",
                "--confirmation",
                "not-the-required-confirmation",
                "--operator-reference",
                "OPS-INVALID-CONFIRMATION",
                "--idempotency-key",
                "invalid-confirmation",
            ],
            "invalid_global_recovery_confirmation",
        ),
        (
            ["confirm-job", "not-a-disposition"],
            "invalid_arguments",
        ),
    ],
)
def test_cli_requires_exact_control_arguments_without_echoing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command_tail: list[str],
    expected_code: str,
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="a")
    _configure_production_scope(monkeypatch, database, writer_lock)
    assert operator_cli.main(
        [*_scope_args(database, writer_lock), *command_tail],
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": expected_code},
        "ok": False,
    }
    assert str(tmp_path) not in captured.err
    assert "not-a-disposition" not in captured.err


def test_cli_requires_explicit_absolute_existing_database_and_lock_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert operator_cli.main(
        [
            "--annotation-db",
            "relative.sqlite",
            "--writer-lock",
            "relative.lock",
            "list-recovery",
        ],
    ) == 2
    assert not (tmp_path / "relative.sqlite").exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "invalid_arguments"},
        "ok": False,
    }
    assert str(tmp_path) not in captured.err


def test_list_recovery_rejects_database_symlink_without_leaking_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="f")
    _configure_production_scope(monkeypatch, database, writer_lock)
    linked = database.parent / "linked-annotation.sqlite"
    linked.symlink_to(database)

    assert operator_cli.main(
        [*_scope_args(linked, writer_lock), "list-recovery"],
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "operator_scope_mismatch"},
        "ok": False,
    }
    assert str(tmp_path) not in captured.err


def test_cli_rejects_writer_lock_that_differs_from_explicit_production_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="a")
    _configure_production_scope(monkeypatch, database, writer_lock)
    wrong_parent = _private_directory(tmp_path / "wrong-lock")
    wrong_lock = wrong_parent / "navigation-annotation-writer.lock"

    assert operator_cli.main(
        [*_scope_args(database, wrong_lock), "list-recovery"],
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "operator_scope_mismatch"},
        "ok": False,
    }
    assert not wrong_lock.exists()
    assert str(tmp_path) not in captured.err


def test_cli_never_creates_a_missing_maintenance_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="a")
    monkeypatch.setenv(
        "VLA_DATA_AGENT_WEB_WORKING_DIR",
        str(database.parent),
    )
    monkeypatch.setenv(
        "VLA_NAVIGATION_WRITER_LOCK_PATH",
        str(writer_lock),
    )
    maintenance_lock = annotation_maintenance_lock_path(database)
    assert not maintenance_lock.exists()

    assert operator_cli.main(
        [*_scope_args(database, writer_lock), "list-recovery"],
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "annotation_maintenance_unavailable"},
        "ok": False,
    }
    assert not maintenance_lock.exists()
    assert str(tmp_path) not in captured.err


def test_cli_rejects_working_directory_with_symlink_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="b")
    alias = tmp_path / "state-alias"
    alias.symlink_to(database.parent, target_is_directory=True)
    linked_database = alias / "annotation.sqlite"
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(alias))
    monkeypatch.setenv("VLA_NAVIGATION_WRITER_LOCK_PATH", str(writer_lock))

    assert operator_cli.main(
        [*_scope_args(linked_database, writer_lock), "list-recovery"],
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "operator_scope_mismatch"},
        "ok": False,
    }
    assert str(tmp_path) not in captured.err


def test_online_web_service_blocks_cli_until_lifespan_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="c")
    _configure_production_scope(monkeypatch, database, writer_lock)
    runtime = SimpleNamespace(
        app=FastAPI(),
        config=SimpleNamespace(agentscope_mount_path="/api/agentscope"),
    )
    app = create_app(
        working_dir=str(database.parent),
        db_path=database.parent / "sessions.sqlite",
        agentscope_runtime=runtime,
        annotation_db_path=database,
        annotation_runtime=object(),
    )
    args = [*_scope_args(database, writer_lock), "list-recovery"]

    with TestClient(app):
        assert operator_cli.main(args) == 2
        online = capsys.readouterr()
        assert online.out == ""
        assert _json_output(online.err) == {
            "error": {"code": "annotation_service_online"},
            "ok": False,
        }
        assert str(tmp_path) not in online.err

    assert app.state.annotation_maintenance_lease.closed is True
    assert operator_cli.main(args) == 0
    offline = capsys.readouterr()
    assert offline.err == ""
    assert _json_output(offline.out)["ok"] is True


def test_rotated_maintenance_lock_cannot_bypass_live_process_lease(
    tmp_path: Path,
) -> None:
    state = _private_directory(tmp_path / "rotated-maintenance-state")
    database = state / "annotation.sqlite"
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    process = context.Process(
        target=_hold_maintenance_lease_in_child,
        args=(str(database), child_connection),
    )
    process.start()
    child_connection.close()
    released = False
    try:
        assert parent_connection.poll(10)
        assert parent_connection.recv() == ("locked", None)
        maintenance_lock = annotation_maintenance_lock_path(database)
        replacement = state / "replacement-maintenance.lock"
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        os.replace(replacement, maintenance_lock)

        with pytest.raises(AnnotationServiceOnlineError):
            acquire_annotation_maintenance(database)

        parent_connection.send("release")
        released = True
        process.join(timeout=10)
        assert process.exitcode == 0
        with acquire_annotation_maintenance(database):
            pass
    finally:
        if not released and process.is_alive():
            parent_connection.send("release")
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        parent_connection.close()


def test_rotated_working_directory_cannot_bypass_live_process_lease(
    tmp_path: Path,
) -> None:
    state = _private_directory(tmp_path / "rotated-maintenance-parent")
    database = state / "annotation.sqlite"
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    process = context.Process(
        target=_hold_maintenance_lease_in_child,
        args=(str(database), child_connection),
    )
    process.start()
    child_connection.close()
    released = False
    try:
        assert parent_connection.poll(10)
        assert parent_connection.recv() == ("locked", None)
        state.rename(tmp_path / "original-maintenance-parent")
        _private_directory(tmp_path / "rotated-maintenance-parent")
        replacement_lock = annotation_maintenance_lock_path(database)
        replacement_lock.write_bytes(b"")
        replacement_lock.chmod(0o600)

        with pytest.raises(AnnotationServiceOnlineError):
            acquire_annotation_maintenance(database)

        parent_connection.send("release")
        released = True
        process.join(timeout=10)
        assert process.exitcode == 0
        with acquire_annotation_maintenance(database):
            pass
    finally:
        if not released and process.is_alive():
            parent_connection.send("release")
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        parent_connection.close()


def test_existing_mutable_store_never_creates_a_missing_database(
    tmp_path: Path,
) -> None:
    database = _private_directory(tmp_path / "missing-state") / "annotation.sqlite"

    with pytest.raises(RuntimeError, match="already exist"):
        AnnotationStore.open_existing_mutable(database)

    assert not database.exists()


@pytest.mark.parametrize("change", ["delete", "rotate"])
def test_existing_mutable_store_fails_closed_after_database_identity_changes(
    tmp_path: Path,
    change: str,
) -> None:
    state = _private_directory(tmp_path / "mutable-state")
    database = state / "annotation.sqlite"
    AnnotationStore(database)
    mutable = AnnotationStore.open_existing_mutable(database)
    original_identity = database.stat().st_ino

    if change == "delete":
        database.unlink()
    else:
        replacement_state = _private_directory(tmp_path / "replacement-state")
        replacement = replacement_state / "annotation.sqlite"
        AnnotationStore(replacement)
        os.replace(replacement, database)
        assert database.stat().st_ino != original_identity

    with pytest.raises(RuntimeError, match="identity changed"):
        mutable.list_jobs()
    if change == "delete":
        assert not database.exists()


def test_existing_mutable_store_rejects_stale_schema_without_migrating(
    tmp_path: Path,
) -> None:
    state = _private_directory(tmp_path / "stale-state")
    database = state / "annotation.sqlite"
    AnnotationStore(database)
    with sqlite3.connect(database) as connection:
        latest = connection.execute(
            "SELECT MAX(version) FROM annotation_schema_migrations",
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM annotation_schema_migrations WHERE version = ?",
            (latest,),
        )

    with pytest.raises(RuntimeError, match="migration ledger is not current"):
        AnnotationStore.open_existing_mutable(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM annotation_schema_migrations",
        ).fetchone()[0] == latest - 1


def test_existing_mutable_store_rechecks_schema_after_rw_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "mutable-open-schema-race")
    database = state / "annotation.sqlite"
    AnnotationStore(database)
    real_identity = annotation_store_module._private_mutable_sqlite_identity

    def bind_then_downgrade(path: Path):
        result = real_identity(path)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "DELETE FROM annotation_schema_migrations "
                "WHERE version = (SELECT MAX(version) "
                "FROM annotation_schema_migrations)",
            )
        return result

    monkeypatch.setattr(
        annotation_store_module,
        "_private_mutable_sqlite_identity",
        bind_then_downgrade,
    )

    with pytest.raises(RuntimeError, match="migration ledger is not current"):
        AnnotationStore.open_existing_mutable(database)


def test_existing_mutable_store_rechecks_schema_inside_each_write(
    tmp_path: Path,
) -> None:
    state = _private_directory(tmp_path / "mutable-write-schema-race")
    database = state / "annotation.sqlite"
    AnnotationStore(database)
    mutable = AnnotationStore.open_existing_mutable(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM annotation_schema_migrations "
            "WHERE version = (SELECT MAX(version) "
            "FROM annotation_schema_migrations)",
        )

    with pytest.raises(RuntimeError, match="migration ledger is not current"):
        with mutable._write():
            pass


def test_completed_global_clear_replay_reports_new_marker_as_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="d")
    _configure_production_scope(monkeypatch, database, writer_lock)
    args = _clear_args(
        database,
        writer_lock,
        idempotency_key="clear-before-new-marker",
    )
    assert operator_cli.main(args) == 0
    first = _json_output(capsys.readouterr().out)
    assert first["result"]["status"] == "global_quarantine_clear_confirmed"
    ensure_navigation_writer_quarantine(
        writer_lock,
        recovery_ref="newer_writer_incident",
    )

    assert operator_cli.main(args) == 2
    replay = capsys.readouterr()
    assert replay.out == ""
    assert _json_output(replay.err) == {
        "error": {"code": "global_writer_quarantine_active"},
        "ok": False,
    }
    assert "global_quarantine_clear_confirmed" not in replay.err
    assert navigation_writer_quarantine_present(writer_lock)
    assert str(tmp_path) not in replay.err


def test_completed_global_clear_rechecks_empty_observation_under_writer_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, _failed = _recovery_fixture(tmp_path, suffix="e")
    _configure_production_scope(monkeypatch, database, writer_lock)
    args = _clear_args(
        database,
        writer_lock,
        idempotency_key="clear-before-observation-race",
    )
    assert operator_cli.main(args) == 0
    capsys.readouterr()
    real_marker_state = annotation_store_module.navigation_writer_marker_state

    def observe_empty_then_add_marker(path: Path):
        observed = real_marker_state(path)
        assert observed.marker_entry_sha256s == ()
        ensure_navigation_writer_quarantine(
            path,
            recovery_ref="marker_after_empty_observation",
        )
        return observed

    monkeypatch.setattr(
        annotation_store_module,
        "navigation_writer_marker_state",
        observe_empty_then_add_marker,
    )

    assert operator_cli.main(args) == 2
    replay = capsys.readouterr()
    assert replay.out == ""
    assert _json_output(replay.err) == {
        "error": {"code": "writer_recovery_state_changed"},
        "ok": False,
    }
    assert "global_quarantine_clear_confirmed" not in replay.err
    assert navigation_writer_quarantine_present(writer_lock)
    assert str(tmp_path) not in replay.err


def test_completed_job_confirm_rechecks_empty_observation_under_writer_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, failed = _recovery_fixture(tmp_path, suffix="a")
    _configure_production_scope(monkeypatch, database, writer_lock)
    assert operator_cli.main(
        _clear_args(
            database,
            writer_lock,
            idempotency_key="job-replay-global-clear",
        ),
    ) == 0
    global_action = _json_output(capsys.readouterr().out)
    confirm_args = _confirm_args(
        database,
        writer_lock,
        failed,
        str(global_action["result"]["action_ref"]),
        disposition="retry",
        idempotency_key="completed-job-replay",
    )
    assert operator_cli.main(confirm_args) == 0
    capsys.readouterr()
    real_marker_state = annotation_store_module.navigation_writer_marker_state

    def observe_empty_then_add_marker(path: Path):
        observed = real_marker_state(path)
        assert observed.marker_entry_sha256s == ()
        ensure_navigation_writer_quarantine(
            path,
            recovery_ref="job_marker_after_empty_observation",
        )
        return observed

    monkeypatch.setattr(
        annotation_store_module,
        "navigation_writer_marker_state",
        observe_empty_then_add_marker,
    )

    assert operator_cli.main(confirm_args) == 2
    replay = capsys.readouterr()
    assert replay.out == ""
    assert _json_output(replay.err) == {
        "error": {"code": "writer_recovery_state_changed"},
        "ok": False,
    }
    assert navigation_writer_quarantine_present(writer_lock)
    assert str(tmp_path) not in replay.err


def test_initial_job_confirm_keeps_job_unchanged_when_marker_wins_lock_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, writer_lock, failed = _recovery_fixture(tmp_path, suffix="b")
    _configure_production_scope(monkeypatch, database, writer_lock)
    assert operator_cli.main(
        _clear_args(
            database,
            writer_lock,
            idempotency_key="initial-race-global-clear",
        ),
    ) == 0
    global_action = _json_output(capsys.readouterr().out)
    confirm_args = _confirm_args(
        database,
        writer_lock,
        failed,
        str(global_action["result"]["action_ref"]),
        disposition="retry",
        idempotency_key="initial-job-marker-race",
    )
    with sqlite3.connect(database) as connection:
        before = (
            connection.execute(
                "SELECT status, state_revision, failure_code "
                "FROM annotation_jobs WHERE job_ref = ?",
                (failed["job_ref"],),
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*) FROM runtime_runs WHERE status = 'queued'",
            ).fetchone()[0],
            connection.execute(
                "SELECT COUNT(*) FROM annotation_operator_actions",
            ).fetchone()[0],
        )
    real_marker_state = annotation_store_module.navigation_writer_marker_state

    def observe_empty_then_add_marker(path: Path):
        observed = real_marker_state(path)
        assert observed.marker_entry_sha256s == ()
        ensure_navigation_writer_quarantine(
            path,
            recovery_ref="initial_job_marker_race",
        )
        return observed

    monkeypatch.setattr(
        annotation_store_module,
        "navigation_writer_marker_state",
        observe_empty_then_add_marker,
    )

    assert operator_cli.main(confirm_args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "writer_recovery_state_changed"},
        "ok": False,
    }
    with sqlite3.connect(database) as connection:
        after = (
            connection.execute(
                "SELECT status, state_revision, failure_code "
                "FROM annotation_jobs WHERE job_ref = ?",
                (failed["job_ref"],),
            ).fetchone(),
            connection.execute(
                "SELECT COUNT(*) FROM runtime_runs WHERE status = 'queued'",
            ).fetchone()[0],
            connection.execute(
                "SELECT COUNT(*) FROM annotation_operator_actions",
            ).fetchone()[0],
        )
    assert after == before
    assert navigation_writer_quarantine_present(writer_lock)
    assert str(tmp_path) not in captured.err


def test_unexpected_failures_never_echo_paths_commands_or_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_detail = f"{tmp_path} rm -rf sk-abcdefghijklmnop"

    def fail_safely(_args: object) -> dict[str, object]:
        raise RuntimeError(private_detail)

    monkeypatch.setattr(operator_cli, "_execute", fail_safely)
    assert operator_cli.main(
        [
            "--annotation-db",
            str(tmp_path / "annotation.sqlite"),
            "--writer-lock",
            str(tmp_path / "writer.lock"),
            "list-recovery",
        ],
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json_output(captured.err) == {
        "error": {"code": "operator_infrastructure_error"},
        "ok": False,
    }
    assert str(tmp_path) not in captured.err
    assert "rm -rf" not in captured.err
    assert "sk-abcdefghijklmnop" not in captured.err
