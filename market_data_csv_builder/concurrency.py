from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from types import TracebackType
from typing import Any, Generic, TypeVar

from .cancellation import CancellationRequested, CancellationToken

T = TypeVar("T")
R = TypeVar("R")


class BoundedExecutor(Generic[T, R]):
    """Keep at most ``max_workers`` submitted tasks and cancel cooperatively."""

    def __init__(
        self,
        items: Iterable[T],
        function: Callable[[T], R],
        *,
        max_workers: int,
        thread_name_prefix: str,
        cancellation_token: CancellationToken,
    ) -> None:
        self._items = iter(items)
        self._function = function
        self._max_workers = max_workers
        self._token = cancellation_token
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._pending: dict[Future[R], T] = {}
        self._exhausted = False

    def __enter__(self) -> BoundedExecutor[T, R]:
        self._fill_available_slots()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        cancelled = self._token.is_requested or (
            exc_type is not None and issubclass(exc_type, (CancellationRequested, KeyboardInterrupt))
        )
        if cancelled:
            for future in self._pending:
                future.cancel()
        self._executor.shutdown(wait=not cancelled, cancel_futures=cancelled)

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def next_completed(self, timeout: float = 0.5) -> list[tuple[T, Future[R]]]:
        self._token.raise_if_requested()
        if not self._pending:
            return []
        done, _pending = wait(
            set(self._pending), timeout=timeout, return_when=FIRST_COMPLETED
        )
        self._token.raise_if_requested()
        completed = [(self._pending.pop(future), future) for future in done]
        self._fill_available_slots()
        return completed

    def _fill_available_slots(self) -> None:
        while not self._exhausted and len(self._pending) < self._max_workers:
            self._token.raise_if_requested()
            try:
                item = next(self._items)
            except StopIteration:
                self._exhausted = True
                break
            future = self._executor.submit(self._function, item)
            self._pending[future] = item

