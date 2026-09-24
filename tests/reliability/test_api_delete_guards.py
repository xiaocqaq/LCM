from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import main


@pytest.mark.asyncio
@pytest.mark.parametrize('actual,expected,status', [(3, 2, 409), (3, 3, 200)])
async def test_empty_trash_count_guard_inside_lock(monkeypatch, actual, expected, status):
    held = False
    @asynccontextmanager
    async def lock(uid):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False
    async def execute(query):
        assert held
        assert 'count(' in str(query).lower()
        return SimpleNamespace(scalar_one=lambda: actual)
    async def purge(*args):
        assert held
        return {'purged': actual}
    monkeypatch.setattr(main, 'user_lock', lock)
    purge_mock = AsyncMock(side_effect=purge)
    monkeypatch.setattr(main.service, 'empty_trash', purge_mock)
    main.app.dependency_overrides[main.auth] = lambda: SimpleNamespace(user=SimpleNamespace(id=1))
    main.app.dependency_overrides[main.get_session] = lambda: SimpleNamespace(execute=execute)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as client:
            result = await client.post(f'/api/v1/trash/empty?expected_count={expected}')
        assert result.status_code == status
        assert purge_mock.await_count == (1 if status == 200 else 0)
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('query,project', [('project=__none__', '__none__'), ('unassigned=true&project=', '')])
async def test_delete_projects_query_has_no_sentinel_collision(monkeypatch, query, project):
    mutation = AsyncMock(return_value={'deleted': 1, 'titles': []})
    monkeypatch.setattr(main.service, 'soft_delete_project', mutation)
    main.app.dependency_overrides[main.auth] = lambda: SimpleNamespace(user=SimpleNamespace(id=1))
    main.app.dependency_overrides[main.get_session] = lambda: object()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as client:
            result = await client.delete('/api/v1/projects?' + query)
        assert result.status_code == 200
        assert mutation.call_args.args[2] == project
    finally:
        main.app.dependency_overrides.clear()
