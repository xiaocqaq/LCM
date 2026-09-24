import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import mcp_server as m


@pytest.fixture
def mcp_fixture(monkeypatch):
    doc = SimpleNamespace(id=8, title='title', content='old stale body', content_hash='abc', library='main', slug='slug', deleted_at=True, persistence_status={'disk': 'saved', 'database': 'saved', 'git': {'ok': False, 'status': 'failed'}, 'ok': False, 'retryable': True})
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: doc)))
    monkeypatch.setattr(m, 'SessionLocal', session)
    token = m.current_auth.set(SimpleNamespace(user=SimpleNamespace(id=1)))
    for name in ['update_document', 'create_document', 'soft_delete_document', 'restore_document']:
        monkeypatch.setattr(m.service, name, AsyncMock(return_value=doc))
    yield doc
    m.current_auth.reset(token)


@pytest.mark.asyncio
async def test_update_append_passes_cas_and_append_intent(mcp_fixture):
    result = json.loads(await m.memory_update(8, content='new', mode='append', project='', expected_hash='abc'))
    args = m.service.update_document.call_args
    assert args.args[3] == {'_append_content': 'new', 'project': ''}
    assert args.kwargs['expected_hash'] == 'abc'
    assert result['content_hash'] == 'abc'
    assert result['persistence_status'] == mcp_fixture.persistence_status
    assert '已 git 提交' not in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['write', 'delete', 'restore', 'upsert'])
async def test_crud_exposes_actual_persistence_status(mcp_fixture, operation):
    if operation == 'write':
        result = json.loads(await m.memory_write('title', 'body'))
    elif operation == 'upsert':
        m.service.create_document.side_effect = m.service.DuplicateError(mcp_fixture)
        result = json.loads(await m.memory_write('title', 'body', mode='upsert'))
    elif operation == 'delete':
        result = json.loads(await m.memory_delete(8))
    else:
        result = json.loads(await m.memory_restore(8))
    assert result['persistence_status'] == mcp_fixture.persistence_status
    assert '已 git 提交' not in result['message']


@pytest.mark.asyncio
async def test_write_rejects_unknown_mode(mcp_fixture):
    result = json.loads(await m.memory_write('title', 'body', mode='wrong'))
    assert result['ok'] is False
    m.service.create_document.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('git_ok', [False, True])
async def test_persistence_messages_use_actual_git_result(mcp_fixture, git_ok):
    mcp_fixture.persistence_status['git'] = {'ok': git_ok, 'status': 'committed' if git_ok else 'failed', 'commit': 'abc123' if git_ok else None, 'error': 'password=secret-private-remote'}
    result = json.loads(await m.memory_write('title', 'body'))
    assert 'secret-private-remote' not in str(result)
    assert result['persistence_status']['git']['ok'] is git_ok
    assert ('已 git 提交' in result['message']) is git_ok


@pytest.mark.asyncio
async def test_update_cas_conflict_is_structured(mcp_fixture):
    m.service.update_document.side_effect = m.service.ConflictError('stale hash')
    result = json.loads(await m.memory_update(8, content='new', expected_hash='stale'))
    assert result['ok'] is False
    assert result['error'] == 'conflict'
    assert result['content_hash'] == 'abc'


@pytest.mark.asyncio
async def test_update_rejects_unknown_mode(mcp_fixture):
    result = json.loads(await m.memory_update(8, content='new', mode='oops'))
    assert result['ok'] is False
    assert result['error'] == 'invalid'
    m.service.update_document.assert_not_awaited()
