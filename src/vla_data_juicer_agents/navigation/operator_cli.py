"""Stopped-service operator entry point for Navigation schema migration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from vla_data_juicer_agents.navigation.migrations import (
    migrate_navigation_store_offline,
)
from vla_data_juicer_agents.navigation.writer_lock import (
    configured_writer_lock_path,
)


class _UsageError(ValueError):
    pass


class _ScopeError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _UsageError("invalid navigation operator arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="vla-navigation-operator",
        description="Migrate Navigation state while the DataPilot service is stopped.",
    )
    parser.add_argument("--navigation-db", type=Path, required=True)
    parser.add_argument("--writer-lock", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    migrate = subparsers.add_parser("migrate-schema")
    migrate.add_argument("--backup-root", type=Path, required=True)
    return parser


def _bind_scope(
    navigation_db: Path,
    writer_lock: Path,
    backup_root: Path,
) -> tuple[Path, Path]:
    configured = os.getenv("VLA_DATA_AGENT_WEB_WORKING_DIR")
    if not configured:
        raise _ScopeError("the DataPilot working directory must be configured")
    working_dir = Path(configured)
    if not working_dir.is_absolute():
        raise _ScopeError("the DataPilot working directory must be absolute")
    try:
        metadata = working_dir.lstat()
        canonical = working_dir.resolve(strict=True)
    except OSError as exc:
        raise _ScopeError("the DataPilot working directory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or canonical != working_dir
    ):
        raise _ScopeError("the DataPilot working directory is unsafe")
    if navigation_db != canonical / "navigation-tasks.sqlite":
        raise _ScopeError("the Navigation database does not match production scope")
    if writer_lock != configured_writer_lock_path():
        raise _ScopeError("the writer lock does not match production scope")
    if (
        not backup_root.is_absolute()
        or backup_root.parent != canonical
        or re.fullmatch(
            r"navigation-migration-backup-[A-Za-z0-9._-]{1,100}",
            backup_root.name,
        )
        is None
    ):
        raise _ScopeError("the Navigation backup is outside production scope")
    return navigation_db, backup_root


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    navigation_db = Path(args.navigation_db)
    writer_lock = Path(args.writer_lock)
    backup_root = Path(args.backup_root)
    if not navigation_db.is_absolute() or not writer_lock.is_absolute():
        raise _UsageError("database and writer lock paths must be absolute")
    navigation_db, backup_root = _bind_scope(
        navigation_db,
        writer_lock,
        backup_root,
    )
    result = migrate_navigation_store_offline(
        navigation_db,
        backup_root=backup_root,
    )
    return {
        "status": result["status"],
        "from_generation": result["from_generation"],
        "to_generation": result["to_generation"],
        "to_version": result["to_version"],
        "backup_manifest_sha256": result["backup_manifest_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        payload = _execute(args)
    except (RuntimeError, ValueError, OSError) as exc:
        payload = {
            "status": "error",
            "code": {
                _UsageError: "invalid_arguments",
                _ScopeError: "invalid_scope",
            }.get(type(exc), "migration_failed"),
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
