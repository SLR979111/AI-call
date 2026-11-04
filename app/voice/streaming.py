"""Bounded bridge for native inference. A cancelled consumer never waits for a kernel.

The producer must retain its model lock until native generation actually returns.
Backpressure and cancellation are checked between callbacks / codec chunks.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from app.cancel import CancelToken


async def cancellable(awaitable, cancel: CancelToken, timeout: float | None = None):
    work = asyncio.ensure_future(awaitable)
    stopped = asyncio.create_task(cancel.wait())
    try:
        cancel.check()
        done, _ = await asyncio.wait((work, stopped), timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        cancel.check()
        if work not in done:
            raise TimeoutError("voice operation timed out")
        return work.result()
    finally:
        for task in (work, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, stopped, return_exceptions=True)


async def thread_stream(produce, cancel: CancelToken):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=8)
    stop = threading.Event()
    end = object()

    def emit(value):
        if stop.is_set():
            return False
        future = asyncio.run_coroutine_threadsafe(queue.put(value), loop)
        while not stop.is_set():
            try:
                future.result(timeout=0.05)
                return True
            except concurrent.futures.TimeoutError:
                continue
        future.cancel()
        return False

    def worker():
        try:
            produce(emit, stop)
        except Exception as exc:
            emit(exc)
        finally:
            emit(end)

    thread = threading.Thread(target=worker, daemon=True, name="voice-inference")
    thread.start()
    try:
        while True:
            item = await cancellable(queue.get(), cancel, timeout=120)
            if item is end:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop.set()
