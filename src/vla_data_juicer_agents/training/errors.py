from __future__ import annotations

from typing import Any


class TrainingError(RuntimeError):
    """A safe, API-facing training domain error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        current: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.current = current


class TrainingValidationError(TrainingError):
    def __init__(self, code: str, message: str, *, current: Any | None = None) -> None:
        super().__init__(code, message, status_code=400, current=current)


class TrainingForbiddenError(TrainingError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=403)


class TrainingNotFoundError(TrainingError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=404)


class TrainingConflictError(TrainingError):
    def __init__(self, code: str, message: str, *, current: Any | None = None) -> None:
        super().__init__(code, message, status_code=409, current=current)


class TrainingUnavailableError(TrainingError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=503)
