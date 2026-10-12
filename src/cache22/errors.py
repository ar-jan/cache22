"""Expected failures carry domain meaning; scheduling policy belongs to Scheduler."""

from typing import ClassVar, Literal

Category = Literal["transport", "unavailable", "busy", "structural"]


class DomainError(RuntimeError):
    category: ClassVar[Category]


class TransportError(DomainError):
    category = "transport"


class StorageUnavailableError(DomainError):
    category = "unavailable"


class RepositoryBusyError(DomainError):
    category = "busy"


class StructuralError(DomainError, ValueError):
    """Invalid repository data, configuration, or a violated storage invariant."""

    category = "structural"


def storage_error(exc: OSError, summary: str = "Storage operation failed") -> DomainError:
    """Translate filesystem failures only at a boundary with storage context."""
    error = (
        StorageUnavailableError
        if isinstance(exc, (FileNotFoundError, PermissionError))
        else StructuralError
    )
    return error(f"{summary}: {exc}")
