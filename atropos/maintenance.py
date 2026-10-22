"""Backend-independent contract for execution maintenance.

Admission capacity is separate. Implementations must fence new step claims,
retain accepted work and never resume implicitly after an administrator crash.
"""
from typing import Protocol

PAUSE_SUFFIX = ":execution-paused"


class MaintenanceError(ValueError):
    """Fixed safe error from a maintenance control operation."""


class ExecutionMaintenance(Protocol):
    def pause(self) -> dict: ...
    def resume(self, *, reconciled: bool = False) -> dict: ...
    def status(self) -> dict: ...
    def begin_backup(self) -> str: ...
    def end_backup(self, operation: str) -> None: ...

