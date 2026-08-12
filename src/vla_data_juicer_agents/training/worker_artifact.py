"""Build the dependency-free Training Worker zipapp used by node deployment."""

from __future__ import annotations

from io import BytesIO
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import hashlib
import zipfile

from .worker_deployment import WorkerRelease
from vla_data_juicer_agents import training_worker


def build_training_worker_release() -> WorkerRelease:
    package_directory = Path(training_worker.__file__).resolve().parent
    project_package_directory = package_directory.parent
    stream = BytesIO()
    with zipfile.ZipFile(
        stream,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        _write_source(
            archive,
            project_package_directory / "__init__.py",
            "vla_data_juicer_agents/__init__.py",
        )
        for source in sorted(package_directory.glob("*.py")):
            _write_source(
                archive,
                source,
                f"vla_data_juicer_agents/training_worker/{source.name}",
            )
        _write_bytes(
            archive,
            "__main__.py",
            b"from vla_data_juicer_agents.training_worker.cli import main\n"
            b"raise SystemExit(main())\n",
        )
    artifact = stream.getvalue()
    release = WorkerRelease(
        version=_package_version(),
        sha256=hashlib.sha256(artifact).hexdigest(),
        artifact=artifact,
    )
    release.validate()
    return release


def _write_source(archive: zipfile.ZipFile, source: Path, destination: str) -> None:
    _write_bytes(archive, destination, source.read_bytes())


def _write_bytes(archive: zipfile.ZipFile, destination: str, content: bytes) -> None:
    metadata = zipfile.ZipInfo(destination, date_time=(2026, 1, 1, 0, 0, 0))
    metadata.compress_type = zipfile.ZIP_DEFLATED
    metadata.external_attr = 0o644 << 16
    archive.writestr(metadata, content)


def _package_version() -> str:
    try:
        return version("vla-data-juicer-agents")
    except PackageNotFoundError:
        return "0.1.0"


__all__ = ["build_training_worker_release"]
