from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

import yaml

from .image_headers import ImageHeaderError, image_dimensions
from .models import (
    DocumentFingerprint,
    GoldenCase,
    GoldenCaseBundle,
    GoldenEntry,
    GoldenSnapshot,
    ImageFingerprint,
    InputExpectation,
    NumericFingerprint,
)


class GoldenError(ValueError):
    """Safe, user-facing Golden configuration or artifact error."""


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _strict_mapping(loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False):
    pairs = loader.construct_pairs(node, deep=deep)
    result: dict[Any, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GoldenError("YAML contains a duplicate mapping key")
        result[key] = value
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _strict_mapping,
)


def _reject_json_constant(value: str) -> None:
    raise GoldenError(f"JSON contains non-finite number token {value}")


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GoldenError("JSON contains a duplicate object key")
        result[key] = value
    return result


def load_json_document(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except GoldenError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GoldenError(f"cannot parse JSON artifact {path.name}: {type(exc).__name__}") from exc


def load_yaml_document(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictSafeLoader)
    except GoldenError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise GoldenError(f"cannot parse YAML artifact {path.name}: {type(exc).__name__}") from exc


def load_case_bundle(path: Path) -> GoldenCaseBundle:
    payload = load_yaml_document(path)
    try:
        return GoldenCaseBundle.model_validate(payload)
    except Exception as exc:
        raise GoldenError(f"invalid Golden case bundle: {type(exc).__name__}: {exc}") from exc


def find_case(bundle: GoldenCaseBundle, case_id: str) -> GoldenCase:
    for case in bundle.cases:
        if case.id == case_id:
            return case
    raise GoldenError(f"unknown Golden case ID {case_id!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise GoldenError(f"cannot hash artifact {path.name}: {type(exc).__name__}") from exc
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise GoldenError(f"unsupported document scalar type {type(value).__name__}")


def _update_token(digest: Any, token: str) -> None:
    encoded = token.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, byteorder="big"))
    digest.update(encoded)


def document_fingerprint(value: Any) -> tuple[DocumentFingerprint, NumericFingerprint]:
    shape_digest = hashlib.sha256()
    numeric_digest = hashlib.sha256()
    numeric_count = 0

    def visit(item: Any) -> None:
        nonlocal numeric_count
        kind = _type_name(item)
        _update_token(shape_digest, kind)
        if kind == "float":
            if not math.isfinite(item):
                raise GoldenError("document contains a non-finite number")
            numeric_count += 1
            _update_token(numeric_digest, "float")
            _update_token(numeric_digest, float(item).hex())
            return
        if kind == "int":
            numeric_count += 1
            _update_token(numeric_digest, "int")
            _update_token(numeric_digest, str(item))
            return
        if kind in {"null", "bool", "string"}:
            return
        if kind == "array":
            _update_token(shape_digest, str(len(item)))
            for child in item:
                visit(child)
            return

        assert isinstance(item, dict)
        if any(not isinstance(key, str) for key in item):
            raise GoldenError("document mapping keys must be strings")
        _update_token(shape_digest, str(len(item)))
        for key in sorted(item):
            _update_token(shape_digest, key)
            visit(item[key])

    visit(value)
    try:
        canonical_hash = _sha256_json(value)
    except (TypeError, ValueError) as exc:
        raise GoldenError(f"document cannot be canonicalized: {type(exc).__name__}") from exc
    document = DocumentFingerprint(
        root_type=_type_name(value),
        shape_sha256=shape_digest.hexdigest(),
        canonical_sha256=canonical_hash,
        numeric_leaf_count=numeric_count,
    )
    numeric = NumericFingerprint(
        count=numeric_count,
        sha256=numeric_digest.hexdigest(),
    )
    return document, numeric


def _matches(relative_path: str, patterns: list[str]) -> bool:
    candidate = PurePosixPath(relative_path)
    return any(
        candidate.match(pattern)
        or (pattern.startswith("**/") and candidate.match(pattern[3:]))
        for pattern in patterns
    )


def semantic_type(relative_path: str, case: GoldenCase) -> str:
    suffix = PurePosixPath(relative_path).suffix.lower()
    if _matches(relative_path, case.patterns.gridmaps):
        return "gridmap"
    if _matches(relative_path, case.patterns.trajectories):
        return "trajectory"
    if _matches(relative_path, case.patterns.images):
        return "image"
    if suffix == ".json":
        return "json"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    return "binary"


def _is_explicitly_ignored(relative_path: str, case: GoldenCase) -> bool:
    return _matches(relative_path, case.ignored_artifact_patterns)


def load_document_for_type(path: Path, kind: str) -> Any:
    return load_yaml_document(path) if kind == "yaml" else load_json_document(path)


def _safe_root(root: Path, *, label: str) -> Path:
    if root.is_symlink():
        raise GoldenError(f"Golden {label} root cannot be a symlink")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise GoldenError(
            f"Golden {label} root is unavailable: {type(exc).__name__}",
        ) from exc
    if not resolved_root.is_dir():
        raise GoldenError(f"Golden {label} root must be a directory")
    return resolved_root


def _safe_scope(root: Path, case: GoldenCase) -> Path:
    resolved_root = _safe_root(root, label="artifact")

    scope = resolved_root
    scope_parts = PurePosixPath(case.artifact_scope).parts
    for component in scope_parts:
        if component == ".":
            continue
        scope = scope / component
        try:
            metadata = scope.lstat()
        except OSError as exc:
            raise GoldenError(
                f"Golden artifact scope is unavailable: {type(exc).__name__}",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise GoldenError("Golden artifact scope cannot contain a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise GoldenError("Golden artifact scope is not a directory")
    return scope


def _sample_for_case(
    bundle: GoldenCaseBundle,
    case: GoldenCase,
):
    if case.sample_id is None:
        return None
    return next(sample for sample in bundle.samples if sample.id == case.sample_id)


def _validate_expectations(
    *,
    scope: Path,
    expectations: list[InputExpectation],
) -> None:
    for expectation in expectations:
        try:
            matches = sorted(scope.glob(expectation.relative_pattern))
        except OSError as exc:
            raise GoldenError(
                f"cannot inspect sample expectation {expectation.modality}: "
                f"{type(exc).__name__}",
            ) from exc
        regular_file_count = 0
        directory_count = 0
        for match in matches:
            current = scope
            for component in match.relative_to(scope).parts:
                current = current / component
                try:
                    metadata = current.lstat()
                except OSError as exc:
                    raise GoldenError(
                        f"cannot inspect sample expectation "
                        f"{expectation.modality}: {type(exc).__name__}",
                    ) from exc
                if stat.S_ISLNK(metadata.st_mode):
                    raise GoldenError(
                        f"sample expectation {expectation.modality} "
                        "matched a symlink",
                    )
            try:
                resolved_match = match.resolve(strict=True)
            except OSError as exc:
                raise GoldenError(
                    f"cannot inspect sample expectation {expectation.modality}: "
                    f"{type(exc).__name__}",
                ) from exc
            if not resolved_match.is_relative_to(scope):
                raise GoldenError(
                    f"sample expectation {expectation.modality} escaped the root",
                )
            if match.is_file():
                regular_file_count += 1
            elif match.is_dir():
                directory_count += 1
            else:
                raise GoldenError(
                    f"sample expectation {expectation.modality} "
                    "matched an unsupported artifact",
                )

        if not expectation.present:
            if matches:
                raise GoldenError(
                    f"sample expectation mismatch for {expectation.modality}",
                )
            continue
        if expectation.expected_kind == "file":
            is_present = regular_file_count > 0
        elif expectation.expected_kind == "directory":
            is_present = directory_count > 0
        else:
            is_present = bool(matches)
        if not is_present:
            raise GoldenError(
                f"sample expectation mismatch for {expectation.modality}",
            )
        if (
            expectation.file_count is not None
            and regular_file_count != expectation.file_count
        ):
            raise GoldenError(
                f"sample file count mismatch for {expectation.modality}",
            )


def _validate_case_root(
    *,
    scope: Path,
    bundle: GoldenCaseBundle,
    case: GoldenCase,
) -> None:
    sample = _sample_for_case(bundle, case)
    if (
        sample is not None
        and sample.internal_segment is not None
        and scope.name != sample.internal_segment
    ):
        raise GoldenError("Golden root name does not match the registered sample")

    _validate_expectations(scope=scope, expectations=case.root_expectations)


def _validate_source_root(
    *,
    source_root: Path | None,
    bundle: GoldenCaseBundle,
    case: GoldenCase,
) -> None:
    sample = _sample_for_case(bundle, case)
    if sample is None or not sample.source_expectations:
        return
    if source_root is None:
        raise GoldenError("this Golden sample requires a source root")
    resolved_source = _safe_root(source_root, label="source")
    if (
        sample.internal_segment is not None
        and resolved_source.name != sample.internal_segment
    ):
        raise GoldenError(
            "Golden source root name does not match the registered sample",
        )
    _validate_expectations(
        scope=resolved_source,
        expectations=sample.source_expectations,
    )


def command_sequence_sha256(steps: list[str]) -> str | None:
    return _sha256_json(steps) if steps else None


def capture_snapshot(
    *,
    root: Path,
    bundle: GoldenCaseBundle,
    case: GoldenCase,
    role: str,
    runtime_manifest_sha256: str | None = None,
    command_steps: list[str] | None = None,
    source_root: Path | None = None,
) -> GoldenSnapshot:
    scope = _safe_scope(root, case)
    _validate_case_root(scope=scope, bundle=bundle, case=case)
    _validate_source_root(
        source_root=source_root,
        bundle=bundle,
        case=case,
    )
    supplied_steps = list(command_steps or [])
    if case.expected_command_steps and not supplied_steps:
        raise GoldenError("this Golden case requires a normalized command sequence")
    if supplied_steps and any(step not in case.expected_command_steps for step in supplied_steps):
        raise GoldenError("command sequence contains an unrecognized step ID")

    entries: list[GoldenEntry] = []
    try:
        candidates = sorted(
            scope.rglob("*"),
            key=lambda item: item.relative_to(scope).as_posix(),
        )
    except OSError as exc:
        raise GoldenError(f"cannot enumerate Golden artifacts: {type(exc).__name__}") from exc

    for path in candidates:
        relative_path = path.relative_to(scope).as_posix()
        if path.is_symlink():
            raise GoldenError(f"Golden artifacts cannot contain symlinks: {relative_path}")
        if _is_explicitly_ignored(relative_path, case):
            continue
        if path.is_dir():
            entries.append(
                GoldenEntry(
                    relative_path=relative_path,
                    kind="directory",
                    semantic_type="directory",
                ),
            )
            continue
        if not path.is_file():
            raise GoldenError(f"unsupported artifact type: {relative_path}")

        try:
            before = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise GoldenError(
                f"cannot stat artifact {relative_path}: {type(exc).__name__}",
            ) from exc
        kind = semantic_type(relative_path, case)
        image = None
        document = None
        numeric = None
        if kind == "image":
            try:
                image_format, width, height = image_dimensions(path)
            except (OSError, ImageHeaderError) as exc:
                raise GoldenError(
                    f"cannot read image header for {relative_path}: {type(exc).__name__}",
                ) from exc
            image = ImageFingerprint(format=image_format, width=width, height=height)
        elif kind in {"json", "yaml", "gridmap", "trajectory"}:
            value = load_document_for_type(path, kind)
            document, numeric = document_fingerprint(value)

        content_sha256 = _sha256_file(path)
        try:
            after = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise GoldenError(f"cannot stat artifact {relative_path}: {type(exc).__name__}") from exc
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
        ):
            raise GoldenError(f"artifact changed while reading: {relative_path}")
        entries.append(
            GoldenEntry(
                relative_path=relative_path,
                kind="file",
                size=after.st_size,
                sha256=content_sha256,
                semantic_type=kind,
                image=image,
                document=document,
                numeric=numeric if kind in {"gridmap", "trajectory"} else None,
            ),
        )

    tree_payload = [
        entry.model_dump(mode="json", exclude_none=True)
        for entry in entries
    ]
    return GoldenSnapshot(
        case_id=case.id,
        role=role,
        runtime_id=bundle.runtime_id,
        runtime_manifest_sha256=runtime_manifest_sha256,
        command_sequence_sha256=command_sequence_sha256(supplied_steps),
        tree_sha256=_sha256_json(tree_payload),
        entries=entries,
    )
