"""Worker-owned ordering for the history Store's executor I/O only."""

from __future__ import annotations

import asyncio
import os
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any
from weakref import WeakValueDictionary

from homeassistant.core import HomeAssistant


class _StorageLane:
    """Keep submission order until the actual worker function has returned.

    The queue and worker belong to the path, independently of any event loop or
    HA executor. A cancelled waiter cannot cancel a queued operation or let a
    newer write pass an older snapshot. Lanes are shared across Store objects,
    HA instances, and event loops while the old worker is still alive.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._issued = 0
        self._finished = 0
        self._queue: deque[tuple[Callable[..., Any], tuple, Future]] = deque()
        self._worker: threading.Thread | None = None

    def submit(
        self, function: Callable[..., Any], args: tuple
    ) -> Future:
        result = Future()
        with self._condition:
            self._queue.append((function, args, result))
            self._issued += 1
            if self._worker is None:
                # No idle thread is retained. A daemon worker drains its queue
                # and exits immediately when empty; Core/process shutdown never
                # joins an indefinitely blocked disk thread. While it lives its
                # bound target keeps the weakly registered path lane alive.
                self._worker = threading.Thread(
                    target=self.run, name="csg-history-storage", daemon=True,
                )
                try:
                    self._worker.start()
                except BaseException:
                    self._queue.pop()
                    self._issued -= 1
                    self._worker = None
                    raise
        return result

    def run(self) -> None:
        while True:
            with self._condition:
                if not self._queue:
                    self._worker = None
                    return
                function, args, result = self._queue.popleft()
            value = error = None
            try:
                value = function(*args)
            except BaseException as err:
                error = err
            # Retire ownership only after the actual function (including its
            # finally clauses) has returned. Future callbacks never own it.
            with self._condition:
                self._finished += 1
            if error is None:
                result.set_result(value)
            else:
                result.set_exception(error)


_LANES: WeakValueDictionary[str, _StorageLane] = WeakValueDictionary()
_LANES_LOCK = threading.Lock()


class HistoryStorageHass:
    """Delegate HA facilities while isolating this Store's executor completion.

    HA 2024.12.5 and 2026.9.3 Store both use hass.async_add_executor_job for disk
    I/O. Their Core stop cancels background executor Futures directly; 2026.9.3
    executor shutdown also cancels queued work. A path-owned worker and queue
    therefore execute these operations with a separate asyncio completion:
    cancelling completion cannot cancel physical I/O or advance the queue.

    Store still owns its format, serializer, cache invalidation, final-write
    callback, and independent readback. No HA task sets or private Store methods
    are altered. The worker lane survives cancellation without blocking Core
    shutdown indefinitely or assuming a timeout means I/O has ended.
    """

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        self._hass = hass
        self._key = key
        self._lane: _StorageLane | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._hass, name)

    def async_add_executor_job(self, function: Callable[..., Any], *args: Any) -> asyncio.Future:
        if self._lane is None:
            path = os.path.normcase(os.path.realpath(
                self._hass.config.path(".storage", self._key)
            ))
            with _LANES_LOCK:
                self._lane = _LANES.setdefault(path, _StorageLane())
        lane = self._lane
        loop = self._hass.loop
        completion = loop.create_future()
        # Never expose the physical result Future to HA or the awaiting Store.
        # No default executor or closed event loop can discard its queued work.
        worker = lane.submit(function, args)

        def deliver(value: Any, error: BaseException | None) -> None:
            if completion.done():
                return
            if error is None:
                completion.set_result(value)
            else:
                completion.set_exception(error)

        def finished(result: Future) -> None:
            value = error = None
            try:
                value = result.result()
            except BaseException as err:
                error = err
            if loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(deliver, value, error)
            except RuntimeError:
                # Loop closed between the check and scheduling. Worker-owned
                # ordering is already retired correctly, independent of delivery.
                if not loop.is_closed():
                    raise

        worker.add_done_callback(finished)
        return completion
