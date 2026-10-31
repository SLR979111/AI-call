from __future__ import annotations


class TurnCancelled(Exception):
    """Raised inside a turn when the orchestrator cancels it (barge-in, timeout...)."""

    def __init__(self, reason: str = "CANCELLED") -> None:
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._reason: str | None = None

    def cancel(self, reason: str = "CANCELLED") -> None:
        if not self._cancelled:
            self._reason = reason
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    def reason(self) -> str | None:
        return self._reason

    def check(self) -> None:
        if self._cancelled:
            raise TurnCancelled(self._reason or "CANCELLED")
