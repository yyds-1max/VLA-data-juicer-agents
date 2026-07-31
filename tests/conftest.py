from __future__ import annotations

from collections.abc import Iterator

import pytest

import vla_data_juicer_agents.web.app as web_app_module
from vla_data_juicer_agents.annotation.maintenance import (
    AnnotationMaintenanceLease,
)


@pytest.fixture(autouse=True)
def close_created_web_app_maintenance_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Explicitly release every Web app lease created during a test."""

    acquire = web_app_module.acquire_annotation_maintenance
    leases: list[AnnotationMaintenanceLease] = []

    def tracked_acquire(*args, **kwargs) -> AnnotationMaintenanceLease:
        lease = acquire(*args, **kwargs)
        leases.append(lease)
        return lease

    monkeypatch.setattr(
        web_app_module,
        "acquire_annotation_maintenance",
        tracked_acquire,
    )
    try:
        yield
    finally:
        for lease in reversed(leases):
            lease.close()
