"""Interpret inventory history before asking storage for a local observation."""

from collections.abc import Mapping
from typing import Any

from .storage import Repository, absent_fields


def observe_local(record: Mapping[str, Any], repo: Repository | None) -> dict[str, Any]:
    """Keep missing previously ready storage distinct from a never-fetched archive.

    Reconciliation governs trust in observations, not the last-known local state.
    Storage receives only the prior readiness and selected format, never a record
    or scheduling policy. The caller owns fenced publication of the observation.
    """
    previously_ready = record["local_state"] in {"ready", "missing"}
    if repo is None:
        return absent_fields(previously_ready)
    return repo.observe_local(
        record["source_path"],
        previously_ready=previously_ready,
        expected_format=record["storage_format"],
    )
