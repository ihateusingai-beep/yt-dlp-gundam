"""
Concurrency helpers for yt-dlp-gundam.

Why this module exists
----------------------
The original /api/download endpoint had two concurrency bugs:

  1. **TOCTOU race** between ``lock.locked()`` and ``async with lock:``:
     two concurrent requests could both pass the ``locked()`` check and
     then both reach the ``async with`` block, where the second one would
     block forever (waiting on a lock held by the first).

  2. **Lock held past client disconnect**: the lock was scoped to the
     full ``async with download_lock:`` block inside the SSE generator,
     so even after the browser tab was closed, the lock stayed held
     until the underlying yt-dlp download finished — which could be
     many minutes. The user could not start a new download during that
     window.

This module's ``try_acquire_lock`` solves Bug 1 by collapsing the
check-and-acquire into a single atomic step. Bug 2 is fixed in main.py
by moving the lock acquisition above the SSE generator and pairing it
with a ``finally`` block that releases it as soon as the consumer exits
(including via client disconnect).

Design notes
------------
``asyncio.Lock.acquire()`` does not accept a ``timeout`` argument; the
canonical way to bound the wait is ``asyncio.wait_for``. We pass
``timeout=0.0`` (the default) to get a non-blocking try-acquire — the
exact semantics needed for a "fail fast with 409" guard. A larger
timeout would also work (e.g. for a bounded queue), but the current
caller always wants immediate failure.
"""
from __future__ import annotations

import asyncio


async def try_acquire_lock(lock: asyncio.Lock, timeout: float = 0.0) -> bool:
    """Try to acquire ``lock`` within ``timeout`` seconds.

    Returns ``True`` if the lock was acquired, ``False`` if it could not
    be obtained within the timeout. The caller is responsible for
    releasing the lock (via ``lock.release()`` or ``async with``) when
    done; this function does NOT release on the failure path.

    Why ``asyncio.wait_for(lock.acquire(), timeout=...)``:
        ``asyncio.Lock.acquire()`` is a coroutine with no native timeout
        parameter. ``wait_for`` is the documented way to bound the wait.
        On timeout we cancel the pending acquire, which the asyncio
        implementation handles cleanly (no leaked state).

    Args:
        lock:    The asyncio.Lock to acquire.
        timeout: Maximum seconds to wait. ``0.0`` (default) means
                 non-blocking — return immediately if the lock is held.
                 Negative values are treated as 0.

    Returns:
        True if the lock is now held by the caller; False otherwise.
    """
    if timeout < 0:
        timeout = 0.0
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
