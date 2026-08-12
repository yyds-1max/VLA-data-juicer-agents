from __future__ import annotations

from io import BytesIO
import zipfile

from vla_data_juicer_agents.training.worker_artifact import (
    build_training_worker_release,
)


def test_worker_zipapp_is_reproducible_self_contained_and_digest_verified() -> None:
    first = build_training_worker_release()
    second = build_training_worker_release()

    assert first.sha256 == second.sha256
    assert first.artifact == second.artifact
    with zipfile.ZipFile(BytesIO(first.artifact)) as archive:
        names = set(archive.namelist())
    assert "__main__.py" in names
    assert "vla_data_juicer_agents/__init__.py" in names
    assert "vla_data_juicer_agents/training_worker/cli.py" in names
    assert "vla_data_juicer_agents/training_worker/client.py" in names
    assert not any(name.endswith(".pyc") or "__pycache__" in name for name in names)
