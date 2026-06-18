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

    Implementation note — why not just ``asyncio.wait_for(lock.acquire(),
    timeout=0)``?
        It looks tempting, but it's a footgun. In Python 3.11+,
        ``wait_for`` checks the timeout *before* the wrapped coroutine
        gets a chance to run, so a free lock is *always* reported as a
        timeout. That makes timeout=0 useless for a fast-path "is it
        free?" probe. For non-zero timeouts ``wait_for`` is fine; for
        the zero case we take the explicit fast path below.

    The fast path for timeout=0 relies on single-threaded asyncio
    semantics: between ``lock.locked()`` and ``lock.acquire()`` there is
    no ``await`` (the unlocked branch of ``acquire`` is purely
    synchronous — it sets ``_locked = True`` and returns), so no other
    coroutine can interleave and "steal" the lock between our check and
    our take. This is exactly the property the original TOCTOU code
    lacked (it had an ``async with`` between check and take, which does
    yield).

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
    if timeout == 0:
        # Non-blocking fast path. lock.locked() and the unlocked branch
        # of lock.acquire() are both synchronous; no await, no yield,
        # no interleaving. See the docstring above for the rationale.
        if lock.locked():
            return False
        await lock.acquire()
        return True
    # Bounded wait. wait_for works correctly for non-zero timeouts:
    # the wrapped coroutine is allowed to run before the timeout fires.
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
