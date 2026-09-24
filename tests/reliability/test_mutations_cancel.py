"""Cancellation must not release a repo lock while its Git thread is still running."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from app import mutations


@pytest.mark.asyncio
async def test_cancellation_waits_for_thread_before_unlock(tmp_path, monkeypatch):
    monkeypatch.setattr(mutations, 'settings', SimpleNamespace(data_dir=str(tmp_path)))
    started = threading.Event()
    finish = threading.Event()
    second_entered = asyncio.Event()

    def work():
        started.set()
        assert finish.wait(timeout=3)

    async def first():
        async with mutations.user_lock(701):
            await mutations.run_blocking(work)

    async def second():
        async with mutations.user_lock(701):
            second_entered.set()

    task = asyncio.create_task(first())
    follower = None
    try:
        for _ in range(100):
            if started.is_set() or task.done():
                break
            await asyncio.sleep(.005)
        if task.done():
            await task
        assert started.is_set()
        task.cancel()
        follower = asyncio.create_task(second())
        await asyncio.sleep(.05)
        assert not second_entered.is_set(), 'lock released while Git thread still mutates repo'
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(follower, 1)
        assert second_entered.is_set()
    finally:
        finish.set()
        await asyncio.gather(task, *([follower] if follower else []), return_exceptions=True)
