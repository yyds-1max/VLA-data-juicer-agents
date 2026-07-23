from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from .comparison import compare_roots
from .models import GoldenComparison, GoldenSnapshot
from .snapshot import GoldenError, capture_snapshot, find_case, load_case_bundle


_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?!/)[^\s\"'<>|,;:)}\]]+"
    r"(?:/[^\s\"'<>|,;:)}\]]+)*",
    flags=re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z]:\\[^\s\"'<>|,;)}\]]+",
)
_SECRET = re.compile(
    r"(?i)(?:\bbearer\s+[a-z0-9._~+/=-]{8,}|"
    r"(?<![a-z0-9])(?:sk-[a-z0-9_-]{12,}|dashscope[-_a-z0-9]{12,}))",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vla-nav-golden",
        description="Capture and compare safe navigation Golden fingerprints.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate a Golden case bundle.")
    validate.add_argument("--cases", type=Path, required=True)

    capture = subparsers.add_parser("capture", help="Capture a safe artifact fingerprint.")
    capture.add_argument("--cases", type=Path, required=True)
    capture.add_argument("--case", dest="case_id", required=True)
    capture.add_argument("--root", type=Path, required=True)
    capture.add_argument("--source-root", type=Path)
    capture.add_argument("--role", choices=("legacy", "candidate"), required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--runtime-manifest-sha256", default=None)
    capture.add_argument("--command-step", action="append", default=[])

    compare = subparsers.add_parser("compare", help="Compare legacy and candidate roots.")
    compare.add_argument("--cases", type=Path, required=True)
    compare.add_argument("--case", dest="case_id", required=True)
    compare.add_argument("--baseline-root", type=Path, required=True)
    compare.add_argument("--candidate-root", type=Path, required=True)
    compare.add_argument("--source-root", type=Path)
    compare.add_argument("--output-dir", type=Path, required=True)
    compare.add_argument("--runtime-manifest-sha256", default=None)
    compare.add_argument("--baseline-command-step", action="append", default=[])
    compare.add_argument("--candidate-command-step", action="append", default=[])
    return parser


def _safe_text(value: object) -> str:
    text = str(value)
    text = _SECRET.sub("[REDACTED]", text)
    text = _POSIX_ABSOLUTE_PATH.sub("[PATH]", text)
    return _WINDOWS_ABSOLUTE_PATH.sub("[PATH]", text)


def _assert_output_outside(output: Path, inputs: list[Path]) -> None:
    resolved_output = output.resolve(strict=False)
    for source in inputs:
        try:
            resolved_source = source.resolve(strict=True)
        except OSError:
            continue
        if resolved_output == resolved_source or resolved_output.is_relative_to(resolved_source):
            raise GoldenError("refusing to write Golden reports inside an input data root")


def _serialized_json(value: GoldenSnapshot | GoldenComparison) -> str:
    payload = value.model_dump(mode="json", exclude_none=True)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    if (
        _POSIX_ABSOLUTE_PATH.search(serialized)
        or _WINDOWS_ABSOLUTE_PATH.search(serialized)
        or _SECRET.search(serialized)
    ):
        raise GoldenError("refusing to persist a Golden artifact that failed the safety scan")
    return serialized


def _write_json(path: Path, value: GoldenSnapshot | GoldenComparison) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialized_json(value), encoding="utf-8")


def _write_markdown(path: Path, comparison: GoldenComparison) -> None:
    lines = [
        f"# Navigation Golden comparison: {comparison.case_id}",
        "",
        f"- Verdict: `{comparison.verdict}`",
        f"- Business equivalence: `{str(comparison.business_equivalence).lower()}`",
        f"- Byte identity: `{str(comparison.byte_identity).lower()}`",
        f"- Differences: `{comparison.difference_count}`",
        f"- Warnings: `{comparison.warning_count}`",
        "",
    ]
    if comparison.differences:
        lines.extend(["## Differences", ""])
        for difference in comparison.differences:
            path_label = difference.relative_path or "(run)"
            lines.append(f"- `{difference.code}` — `{path_label}`")
        lines.append("")
    if comparison.warnings:
        lines.extend(["## Warnings", ""])
        for warning in comparison.warnings:
            path_label = warning.relative_path or "(run)"
            lines.append(f"- `{warning.code}` — `{path_label}`")
        lines.append("")
    serialized = "\n".join(lines)
    if (
        _POSIX_ABSOLUTE_PATH.search(serialized)
        or _WINDOWS_ABSOLUTE_PATH.search(serialized)
        or _SECRET.search(serialized)
    ):
        raise GoldenError("refusing to persist a Golden report that failed the safety scan")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = load_case_bundle(args.cases)
        if args.command == "validate":
            print(f"Validated {len(bundle.cases)} Golden case(s).")
            return 0

        case = find_case(bundle, args.case_id)
        if args.command == "capture":
            capture_inputs = [args.root]
            if args.source_root is not None:
                capture_inputs.append(args.source_root)
            _assert_output_outside(args.output, capture_inputs)
            snapshot = capture_snapshot(
                root=args.root,
                bundle=bundle,
                case=case,
                role=args.role,
                runtime_manifest_sha256=args.runtime_manifest_sha256,
                command_steps=args.command_step,
                source_root=args.source_root,
            )
            _write_json(args.output, snapshot)
            print("Golden snapshot written.")
            return 0

        comparison_inputs = [args.baseline_root, args.candidate_root]
        if args.source_root is not None:
            comparison_inputs.append(args.source_root)
        _assert_output_outside(args.output_dir, comparison_inputs)
        comparison = compare_roots(
            baseline_root=args.baseline_root,
            candidate_root=args.candidate_root,
            bundle=bundle,
            case=case,
            baseline_command_steps=args.baseline_command_step,
            candidate_command_steps=args.candidate_command_step,
            runtime_manifest_sha256=args.runtime_manifest_sha256,
            source_root=args.source_root,
        )
        _write_json(args.output_dir / "comparison.json", comparison)
        _write_markdown(args.output_dir / "comparison.md", comparison)
        print(
            f"{comparison.verdict}: {comparison.difference_count} difference(s), "
            f"{comparison.warning_count} warning(s).",
        )
        return 0 if comparison.business_equivalence else 1
    except (GoldenError, OSError, TypeError, ValueError) as exc:
        print(
            f"Golden infrastructure failed: {type(exc).__name__}: {_safe_text(exc)}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
