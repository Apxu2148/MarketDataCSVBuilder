from __future__ import annotations

import threading
from collections.abc import Callable


class CancellationRequested(RuntimeError):
    """Raised at cooperative cancellation checkpoints."""


class CancellationToken:
    """Thread-safe process-local cancellation signal."""

    def __init__(self, on_request: Callable[[], None] | None = None) -> None:
        self._event = threading.Event()
        self._on_request = on_request
        self._request_lock = threading.Lock()

    @property
    def is_requested(self) -> bool:
        return self._event.is_set()

    def request(self) -> bool:
        """Request cancellation once and return whether this call was first."""
        with self._request_lock:
            if self._event.is_set():
                return False
            self._event.set()
            if self._on_request is not None:
                self._on_request()
            return True

    def raise_if_requested(self) -> None:
        if self._event.is_set():
            raise CancellationRequested("Cancellation requested")

    def wait(self, timeout: float) -> bool:
        """Wait interruptibly; return true when cancellation was requested."""
        return self._event.wait(timeout)

