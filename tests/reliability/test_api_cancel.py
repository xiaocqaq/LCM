"""A cancelled checkout must still reindex before releasing its repository lock."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import main, mutations


@pytest.mark.asyncio
async def test_cancelled_switch_completes_sync_under_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(mutations, 'settings', SimpleNamespace(data_dir=str(tmp_path)))
    started = threading.Event(); finish = threading.Event(); synced = []
    def switch(*args):
        started.set(); assert finish.wait(3)
        return {'ok': True}
    fake_git = SimpleNamespace(switch_branch=switch)
    monkeypatch.setattr(main, '_repo', lambda ctx:(fake_git,tmp_path))
    async def sync(session,user):
        synced.append(user.id)
        return {'updated':1}
    monkeypatch.setattr(main.service,'sync_from_disk',sync)
    job=asyncio.create_task(main.branch_switch({'name':'next'},SimpleNamespace(user=SimpleNamespace(id=887)),object()))
    try:
        for _ in range(100):
            if started.is_set(): break
            if job.done(): await job
            await asyncio.sleep(.005)
        assert started.is_set()
        job.cancel()
        await asyncio.sleep(.03)
        finish.set()
        with pytest.raises(asyncio.CancelledError): await job
        assert synced == [887], 'checkout completed, but cancellation skipped rebuilding the index'
    finally:
        finish.set();await asyncio.gather(job,return_exceptions=True)
