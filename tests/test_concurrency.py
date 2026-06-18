"""
Tests for concurrency.py — atomic try-acquire semantics.

Covers:
  C1 — try_acquire_lock returns True on a free lock
  C2 — try_acquire_lock returns False when the lock is already held
  C3 — try_acquire_lock does NOT release the lock on the failure path
       (caller must release explicitly)
  C4 — try_acquire_lock with a non-zero timeout actually waits
  C5 — try_acquire_lock with timeout < 0 is treated as 0
  C6 — Two concurrent "check then take" patterns: the second request must
       fail fast (Bug 1 regression test — the original TOCTOU window)

Run with:  python3 -m unittest tests.test_concurrency -v
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

# Make the project root importable so `import concurrency` works regardless of CWD.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from concurrency import try_acquire_lock  # noqa: E402


class TestTryAcquireLock(unittest.TestCase):
    """Atomic try-acquire semantics for asyncio.Lock."""

    def test_free_lock_returns_true(self):
        async def scenario():
            lock = asyncio.Lock()
            result = await try_acquire_lock(lock)
            self.assertTrue(result)
            self.assertTrue(lock.locked())

        asyncio.run(scenario())

    def test_held_lock_returns_false(self):
        async def scenario():
            lock = asyncio.Lock()
            await lock.acquire()
            try:
                result = await try_acquire_lock(lock)
                self.assertFalse(result)
            finally:
                lock.release()

        asyncio.run(scenario())

    def test_failure_does_not_release(self):
        """A failed try-acquire must NOT have side-effects on the lock."""
        async def scenario():
            lock = asyncio.Lock()
            await lock.acquire()
            try:
                await try_acquire_lock(lock)
                # Lock must still be held — the failing caller didn't grab it,
                # and a successful prior acquire still owns it.
                self.assertTrue(lock.locked())
            finally:
                lock.release()
            # And after release, the lock is free again.
            self.assertFalse(lock.locked())

        asyncio.run(scenario())

    def test_nonzero_timeout_waits(self):
        async def scenario():
            lock = asyncio.Lock()
            await lock.acquire()

            async def release_after_delay():
                await asyncio.sleep(0.05)
                lock.release()

            asyncio.create_task(release_after_delay())
            # Wait up to 500 ms — should succeed when release fires at 50 ms.
            result = await try_acquire_lock(lock, timeout=0.5)
            self.assertTrue(result)
            lock.release()

        asyncio.run(scenario())

    def test_negative_timeout_treated_as_zero(self):
        async def scenario():
            lock = asyncio.Lock()
            await lock.acquire()
            try:
                # Negative timeout must not block; should return False immediately.
                start = time.monotonic()
                result = await try_acquire_lock(lock, timeout=-1.0)
                elapsed = time.monotonic() - start
                self.assertFalse(result)
                self.assertLess(
                    elapsed, 0.05,
                    f"negative timeout blocked for {elapsed:.3f}s",
                )
            finally:
                lock.release()

        asyncio.run(scenario())

    def test_concurrent_check_then_take_no_double_lock(self):
        """Bug 1 regression: two concurrent requests must not both win.

        Old code:
            if lock.locked(): raise 409
            async with lock: ...    # race window between check and take
        New code (try_acquire_lock): check + take is atomic.
        """
        async def scenario():
            lock = asyncio.Lock()
            results = await asyncio.gather(
                try_acquire_lock(lock),
                try_acquire_lock(lock),
                try_acquire_lock(lock),
            )
            # Exactly one should have won.
            self.assertEqual(results.count(True), 1)
            self.assertEqual(results.count(False), 2)
            self.assertTrue(lock.locked())
            lock.release()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
