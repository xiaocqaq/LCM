"""Count equality is insufficient for destructive trash confirmation."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import main


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['replace', 'redeleted', 'none'])
async def test_confirmed_membership_must_match_under_lock(monkeypatch, change):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    members = [(10, stamp)]
    held = False
    @asynccontextmanager
    async def lock(uid):
        nonlocal held
        assert uid == 7
        held = True
        try: yield
        finally: held = False
    async def execute(query):
        assert held, 'snapshot and purge checks must share mutation locking'
        assert 'user_id' in str(query)
        if 'count(' in str(query).lower():
            return SimpleNamespace(scalar_one=lambda:len(members))
        return SimpleNamespace(all=lambda:list(members))
    async def purge(*args):
        assert held
        return {'purged':len(members),'files':len(members)}
    monkeypatch.setattr(main, 'user_lock', lock)
    mutation = AsyncMock(side_effect=purge)
    monkeypatch.setattr(main.service, 'empty_trash', mutation)
    main.app.dependency_overrides[main.auth] = lambda: SimpleNamespace(user=SimpleNamespace(id=7))
    main.app.dependency_overrides[main.get_session] = lambda: SimpleNamespace(execute=execute)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://testserver') as c:
            response = await c.get('/api/v1/trash/snapshot')
            assert response.status_code == 200
            snapshot = response.json()
            assert snapshot['total'] == 1
            if change == 'replace': members[:] = [(11, stamp)]
            if change == 'redeleted': members[:] = [(10, stamp + timedelta(seconds=1))]
            response = await c.post('/api/v1/trash/empty',params={
                'expected_count':1,'expected_revision':snapshot['revision']})
            expected = 200 if change == 'none' else 409
            assert response.status_code == expected, response.text
            assert mutation.await_count == (1 if expected == 200 else 0)
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_purge_without_snapshot_is_rejected(monkeypatch):
    mutation = AsyncMock(return_value={'purged':1})
    monkeypatch.setattr(main.service,'empty_trash',mutation)
    main.app.dependency_overrides[main.auth] = lambda: SimpleNamespace(user=SimpleNamespace(id=7))
    main.app.dependency_overrides[main.get_session] = lambda: object()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://testserver') as c:
            r=await c.post('/api/v1/trash/empty')
            assert r.status_code == 428
            assert mutation.await_count == 0
    finally:
        main.app.dependency_overrides.clear()
