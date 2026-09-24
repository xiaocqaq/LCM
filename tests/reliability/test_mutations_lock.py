import asyncio
import importlib.util
import multiprocessing
import os
import time


def test_user_lock_serializes_tasks_and_is_reentrant(tmp_path, monkeypatch):
    assert importlib.util.find_spec('app.mutations') is not None
    from app.mutations import user_lock
    from app.config import settings
    monkeypatch.setattr(settings, 'data_dir', str(tmp_path))
    async def run():
        events = []
        async def first():
            async with user_lock(1):
                async with user_lock(1):
                    events.append('start')
                    await asyncio.sleep(.03)
                    events.append('end')
        async def second():
            await asyncio.sleep(.005)
            async with user_lock(1):
                events.append('second')
        await asyncio.wait_for(asyncio.gather(first(), second()), 2)
        assert events == ['start', 'end', 'second']
    asyncio.run(run())
    assert not (tmp_path / 'data' / 'users').exists()


def _lock_child(data_dir, queue):
    from app.config import settings
    settings.data_dir = data_dir
    from app.mutations import user_lock
    async def run():
        async with user_lock(2):
            queue.put('acquired')
            await asyncio.sleep(.25)
    asyncio.run(run())


def test_user_lock_serializes_processes_without_blocking_loop(tmp_path, monkeypatch):
    assert importlib.util.find_spec('app.mutations') is not None
    from app.config import settings
    from app.mutations import user_lock
    monkeypatch.setattr(settings, 'data_dir', str(tmp_path))
    ctx = multiprocessing.get_context('fork')
    queue = ctx.Queue()
    child = ctx.Process(target=_lock_child, args=(str(tmp_path), queue))
    child.start()
    assert queue.get(timeout=3) == 'acquired'
    async def run():
        ticks = []
        async def ticker():
            for _ in range(5):
                await asyncio.sleep(.015)
                ticks.append(1)
        async def contender():
            async with user_lock(2):
                assert len(ticks) == 5
        await asyncio.wait_for(asyncio.gather(ticker(), contender()), 3)
    try:
        asyncio.run(run())
    finally:
        child.join(3)
        if child.is_alive():
            child.terminate()
    assert child.exitcode == 0
