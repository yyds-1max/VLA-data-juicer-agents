"""Validate and verify frozen navigation runtime manifests.

The manifest deliberately contains root aliases and root-relative paths only.
This module never executes a runtime entry and never writes to a verified root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
ENTRY_KINDS = frozenset(
    {"frozen_file", "generated_mutable", "external_runtime"}
)
COMMON_ENTRY_FIELDS = frozenset(
    {"root_alias", "relative_path", "kind", "role", "stage"}
)
FROZEN_FILE_FIELDS = COMMON_ENTRY_FIELDS | {
    "sha256",
    "size",
    "executable",
}
GENERATED_MUTABLE_FIELDS = COMMON_ENTRY_FIELDS | {
    "concurrency",
    "cleanup",
}
EXTERNAL_RUNTIME_FIELDS = COMMON_ENTRY_FIELDS | {
    "version",
    "sha256",
    "size",
    "executable",
}
TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "runtime_id", "root_aliases", "entries"}
)

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RUNTIME_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
_EMBEDDED_ABSOLUTE_PATTERN = re.compile(
    r"(?:^|[\s\"'=:(])(?:/[^\s]+|[A-Za-z]:[\\/][^\s]+|~[\\/][^\s]+)"
)
_USERNAME_DIRECTORY_PATTERN = re.compile(
    r"(?:^|[\\/])(?:Users|home)[\\/][^\\/\s]+"
)


class ManifestValidationError(ValueError):
    """The manifest does not satisfy schema v1."""


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(
                handle,
                object_pairs_hook=_object_without_duplicate_keys,
            )
    except _DuplicateKeyError as error:
        raise ManifestValidationError(str(error)) from error
    except json.JSONDecodeError as error:
        raise ManifestValidationError(
            f"invalid JSON at line {error.lineno}, column {error.colno}"
        ) from error
    except (OSError, UnicodeError) as error:
        raise RuntimeError(
            f"unable to read manifest ({type(error).__name__})"
        ) from error


def _reject_unknown_fields(
    value: dict[str, Any],
    *,
    allowed: frozenset[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ManifestValidationError(
            f"{location} contains unknown field(s): {', '.join(unknown)}"
        )


def _require_non_empty_string(value: Any, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{location} must be a non-empty string")
    _validate_safe_string(value, location=location)
    return value


def _validate_safe_string(value: str, *, location: str) -> None:
    if "\x00" in value:
        raise ManifestValidationError(f"{location} contains a NUL byte")
    if _EMBEDDED_ABSOLUTE_PATTERN.search(value):
        raise ManifestValidationError(f"{location} contains an absolute path")
    if _USERNAME_DIRECTORY_PATTERN.search(value):
        raise ManifestValidationError(
            f"{location} contains a username directory"
        )


def _validate_relative_path(value: Any, *, location: str) -> str:
    path = _require_non_empty_string(value, location=location)
    if "\\" in path:
        raise ManifestValidationError(
            f"{location} must use POSIX path separators"
        )
    if path.startswith(("/", "~")) or _WINDOWS_ABSOLUTE_PATTERN.match(path):
        raise ManifestValidationError(f"{location} must be relative")
    components = path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ManifestValidationError(
            f"{location} must be a normalized relative path"
        )
    if (
        len(components) >= 2
        and components[0] in {"Users", "home"}
    ):
        raise ManifestValidationError(
            f"{location} contains a username directory"
        )
    return path


def _validate_hash_metadata(
    entry: dict[str, Any],
    *,
    location: str,
    required: bool,
) -> None:
    fields = ("sha256", "size", "executable")
    present = tuple(field in entry for field in fields)
    if required and not all(present):
        missing = [field for field, exists in zip(fields, present) if not exists]
        raise ManifestValidationError(
            f"{location} is missing field(s): {', '.join(missing)}"
        )
    if any(present) and not all(present):
        raise ManifestValidationError(
            f"{location} must provide sha256, size, and executable together"
        )
    if not any(present):
        return

    sha256 = entry["sha256"]
    size = entry["size"]
    executable = entry["executable"]
    if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
        raise ManifestValidationError(
            f"{location}.sha256 must be 64 lowercase hexadecimal characters"
        )
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ManifestValidationError(
            f"{location}.size must be a non-negative integer"
        )
    if not isinstance(executable, bool):
        raise ManifestValidationError(
            f"{location}.executable must be a boolean"
        )


def validate_manifest(document: Any) -> dict[str, Any]:
    """Validate *document* as a runtime manifest schema v1."""

    if not isinstance(document, dict):
        raise ManifestValidationError("manifest must be a JSON object")
    _reject_unknown_fields(
        document,
        allowed=TOP_LEVEL_FIELDS,
        location="manifest",
    )
    missing = sorted(TOP_LEVEL_FIELDS - set(document))
    if missing:
        raise ManifestValidationError(
            f"manifest is missing field(s): {', '.join(missing)}"
        )
    if document["schema_version"] != SCHEMA_VERSION:
        raise ManifestValidationError(
            f"schema_version must be {SCHEMA_VERSION}"
        )

    runtime_id = _require_non_empty_string(
        document["runtime_id"],
        location="runtime_id",
    )
    if not _RUNTIME_ID_PATTERN.fullmatch(runtime_id):
        raise ManifestValidationError(
            "runtime_id must contain only letters, digits, dot, underscore, "
            "or hyphen"
        )

    root_aliases = document["root_aliases"]
    if not isinstance(root_aliases, list) or not root_aliases:
        raise ManifestValidationError(
            "root_aliases must be a non-empty array"
        )
    aliases: list[str] = []
    for index, alias_value in enumerate(root_aliases):
        alias = _require_non_empty_string(
            alias_value,
            location=f"root_aliases[{index}]",
        )
        if not _ALIAS_PATTERN.fullmatch(alias):
            raise ManifestValidationError(
                f"root_aliases[{index}] is not a valid root alias"
            )
        aliases.append(alias)
    if len(set(aliases)) != len(aliases):
        raise ManifestValidationError("root_aliases must not contain duplicates")

    entries = document["entries"]
    if not isinstance(entries, list):
        raise ManifestValidationError("entries must be an array")

    seen_paths: set[tuple[str, str]] = set()
    for index, entry_value in enumerate(entries):
        location = f"entries[{index}]"
        if not isinstance(entry_value, dict):
            raise ManifestValidationError(f"{location} must be an object")
        entry = entry_value
        kind = entry.get("kind")
        if kind not in ENTRY_KINDS:
            raise ManifestValidationError(
                f"{location}.kind must be one of: "
                + ", ".join(sorted(ENTRY_KINDS))
            )

        allowed_fields = {
            "frozen_file": FROZEN_FILE_FIELDS,
            "generated_mutable": GENERATED_MUTABLE_FIELDS,
            "external_runtime": EXTERNAL_RUNTIME_FIELDS,
        }[kind]
        _reject_unknown_fields(
            entry,
            allowed=allowed_fields,
            location=location,
        )
        missing_common = sorted(COMMON_ENTRY_FIELDS - set(entry))
        if missing_common:
            raise ManifestValidationError(
                f"{location} is missing field(s): "
                + ", ".join(missing_common)
            )

        alias = _require_non_empty_string(
            entry["root_alias"],
            location=f"{location}.root_alias",
        )
        if alias not in aliases:
            raise ManifestValidationError(
                f"{location}.root_alias is not declared"
            )
        relative_path = _validate_relative_path(
            entry["relative_path"],
            location=f"{location}.relative_path",
        )
        path_key = (alias, relative_path)
        if path_key in seen_paths:
            raise ManifestValidationError(
                f"{location} duplicates a root_alias/relative_path pair"
            )
        seen_paths.add(path_key)

        _require_non_empty_string(
            entry["role"],
            location=f"{location}.role",
        )
        _require_non_empty_string(
            entry["stage"],
            location=f"{location}.stage",
        )

        if kind == "frozen_file":
            _validate_hash_metadata(entry, location=location, required=True)
        elif kind == "generated_mutable":
            for optional_field in ("concurrency", "cleanup"):
                if optional_field in entry:
                    _require_non_empty_string(
                        entry[optional_field],
                        location=f"{location}.{optional_field}",
                    )
        else:
            if "version" in entry:
                _require_non_empty_string(
                    entry["version"],
                    location=f"{location}.version",
                )
            _validate_hash_metadata(entry, location=location, required=False)
            if "version" not in entry and "sha256" not in entry:
                raise ManifestValidationError(
                    f"{location} must provide version or hash metadata"
                )

    return document


def load_manifest(path: Path) -> dict[str, Any]:
    """Load and validate a manifest file."""

    return validate_manifest(_load_json(path))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _path_problem(root: Path, relative_path: str) -> str | None:
    current = root
    components = relative_path.split("/")
    for index, component in enumerate(components):
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError as error:
            raise RuntimeError(type(error).__name__) from error
        if stat.S_ISLNK(metadata.st_mode):
            return "symlink is not allowed"
        if index < len(components) - 1 and not stat.S_ISDIR(metadata.st_mode):
            return "parent component is not a directory"
    if not stat.S_ISREG(metadata.st_mode):
        return "not a regular file"
    return None


def verify_root(
    manifest: dict[str, Any],
    *,
    root_alias: str,
    root: Path,
) -> tuple[list[str], list[str]]:
    """Return ``(mismatches, runtime_errors)`` for one declared root alias."""

    if root_alias not in manifest["root_aliases"]:
        raise ManifestValidationError("requested root alias is not declared")
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise RuntimeError(
            f"root is unavailable ({type(error).__name__})"
        ) from error
    if stat.S_ISLNK(root_metadata.st_mode):
        raise RuntimeError("root must not be a symlink")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("root must be a directory")

    entries = sorted(
        (
            entry
            for entry in manifest["entries"]
            if entry["root_alias"] == root_alias
            and entry["kind"] == "frozen_file"
        ),
        key=lambda item: item["relative_path"],
    )
    mismatches: list[str] = []
    runtime_errors: list[str] = []
    for entry in entries:
        relative_path = entry["relative_path"]
        label = f"{root_alias}:{relative_path}"
        try:
            problem = _path_problem(root, relative_path)
            if problem is not None:
                mismatches.append(f"{label}: {problem}")
                continue
            candidate = root.joinpath(*relative_path.split("/"))
            before = candidate.stat(follow_symlinks=False)
            actual_sha256 = _sha256_file(candidate)
            after = candidate.stat(follow_symlinks=False)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                runtime_errors.append(f"{label}: file changed while reading")
                continue
            actual_executable = bool(after.st_mode & 0o111)
            if after.st_size != entry["size"]:
                mismatches.append(f"{label}: size mismatch")
            if actual_sha256 != entry["sha256"]:
                mismatches.append(f"{label}: sha256 mismatch")
            if actual_executable != entry["executable"]:
                mismatches.append(f"{label}: executable mismatch")
        except OSError as error:
            runtime_errors.append(
                f"{label}: unable to read file ({type(error).__name__})"
            )
        except RuntimeError as error:
            runtime_errors.append(
                f"{label}: unable to inspect file ({error})"
            )
    return mismatches, runtime_errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vla-nav-runtime-manifest",
        description="Validate or read-only verify navigation runtime manifests.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate-manifest",
        help="validate a commit-safe manifest",
    )
    validate_parser.add_argument("--manifest", required=True, type=Path)

    verify_parser = subparsers.add_parser(
        "verify-root",
        help="verify frozen files below one declared root",
    )
    verify_parser.add_argument("--manifest", required=True, type=Path)
    verify_parser.add_argument("--root-alias", required=True)
    verify_parser.add_argument("--root", required=True, type=Path)
    return parser


def _print_lines(lines: Iterable[str], *, stream: Any) -> None:
    for line in lines:
        print(line, file=stream)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "validate-manifest":
            print(
                "OK: runtime manifest schema v1 is valid "
                f"({len(manifest['entries'])} entries)"
            )
            return 0

        mismatches, runtime_errors = verify_root(
            manifest,
            root_alias=args.root_alias,
            root=args.root,
        )
        if runtime_errors:
            _print_lines(
                (f"ERROR: {message}" for message in runtime_errors),
                stream=sys.stderr,
            )
            return 2
        if mismatches:
            _print_lines(
                (f"MISMATCH: {message}" for message in mismatches),
                stream=sys.stdout,
            )
            return 1
        frozen_count = sum(
            entry["kind"] == "frozen_file"
            and entry["root_alias"] == args.root_alias
            for entry in manifest["entries"]
        )
        if frozen_count == 0:
            print(
                "ERROR: requested root alias declares no frozen files; "
                "generated and external entries were not verified",
                file=sys.stderr,
            )
            return 2
        print(
            f"OK: {args.root_alias} matches {frozen_count} frozen file(s)"
        )
        return 0
    except ManifestValidationError as error:
        print(f"ERROR: invalid manifest: {error}", file=sys.stderr)
        return 2
    except RuntimeError as error:
        print(f"ERROR: verification unavailable: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
