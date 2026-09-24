import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import main


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['ok', 'db_error', 'db_timeout', 'directory_missing', 'directory_readonly'])
async def test_ready_bounded_readonly_checks_and_liveness(monkeypatch, tmp_path, caplog, state):
    async def execute(query):
        assert str(query) == 'SELECT 1'
        if state == 'db_error':
            raise RuntimeError('database secret')
        if state == 'db_timeout':
            await asyncio.sleep(60)
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=execute)
    monkeypatch.setattr(main, 'SessionLocal', session)
    monkeypatch.setattr(main, 'READY_TIMEOUT_SECONDS', 0.02, raising=False)
    directory = tmp_path / 'data'
    if state != 'directory_missing':
        directory.mkdir()
        if state == 'directory_readonly':
            directory.chmod(0o500)
    monkeypatch.setattr(main.settings, 'data_dir', str(directory))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as client:
        response = await asyncio.wait_for(client.get('/api/ready'), timeout=1)
        health = await client.get('/api/health')
    assert health.json() == {'ok': True, 'service': 'memorys'}
    assert response.status_code == (200 if state == 'ok' else 503)
    assert list(directory.iterdir()) == [] if directory.exists() else not directory.exists()
    if state != 'ok':
        assert 'secret' not in response.text
        assert response.headers['x-request-id'] in caplog.text
