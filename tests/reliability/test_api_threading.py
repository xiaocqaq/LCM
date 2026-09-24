import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import main, gitsvc


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['branches', 'branch_create', 'branch_switch', 'branch_merge', 'branch_delete', 'sync', 'sync_push', 'doc_history', 'sync_schedule'])
async def test_git_offloaded_and_mutations_locked(monkeypatch, tmp_path, endpoint):
    event_loop_thread = threading.get_ident()
    events = []
    held = False
    @asynccontextmanager
    async def lock(uid):
        nonlocal held
        assert uid == 1
        held = True
        events.append('lock')
        try:
            yield
        finally:
            held = False
            events.append('unlock')
    monkeypatch.setattr(main, 'user_lock', lock, raising=False)
    mutation = endpoint not in {'doc_history', 'sync_schedule'}
    def git_call(*args, **kwargs):
        assert threading.get_ident() != event_loop_thread, 'blocking git on event loop'
        if mutation:
            assert held, 'git mutation without user lock'
        events.append('git')
        return {'ok': True}
    def repo(*args):
        git_call()
        return tmp_path
    monkeypatch.setattr(gitsvc, 'ensure_repo', repo)
    for name in ['list_branches', 'create_branch', 'switch_branch', 'merge_branch', 'delete_branch', 'pull_from_github', 'sync_to_github', 'history']:
        monkeypatch.setattr(gitsvc, name, git_call)
    def run(*args, **kwargs):
        git_call()
        return SimpleNamespace(stdout='LoadState=loaded\n')
    monkeypatch.setattr(main.subprocess, 'run', run)
    async def reindex(*args):
        assert held
        events.append('reindex')
        return {'indexed': 1}
    monkeypatch.setattr(main.service, 'sync_from_disk', reindex)
    monkeypatch.setattr(main, '_get_doc', AsyncMock(return_value=SimpleNamespace(rel_path='main/doc.md')))
    ctx = SimpleNamespace(user=SimpleNamespace(id=1))
    kwargs = {'ctx': ctx}
    if endpoint in {'branch_create', 'branch_switch', 'branch_merge', 'sync'}:
        kwargs['payload'] = {'name': 'draft', 'action': 'pull', 'switch': True}
        kwargs['session'] = object()
    if endpoint == 'branch_delete':
        kwargs['name'] = 'draft'
    if endpoint == 'doc_history':
        kwargs.update(doc_id=1, session=object())
    result = await getattr(main, endpoint)(**kwargs)
    assert 'git' in events
    if endpoint in {'branch_create', 'branch_switch', 'branch_merge', 'sync'}:
        assert events[-2:] == ['reindex', 'unlock']


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['create_document', 'patch_document', 'delete_document', 'restore_document', 'delete_project', 'empty_trash'])
async def test_document_mutations_hold_user_lock(monkeypatch, endpoint):
    held = False
    @asynccontextmanager
    async def lock(uid):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False
    monkeypatch.setattr(main, 'user_lock', lock, raising=False)
    doc = SimpleNamespace(id=8, deleted_at=SimpleNamespace(isoformat=lambda: 'now'))
    async def mutation(*args, **kwargs):
        assert held
        return {'deleted': 1} if endpoint in {'delete_project', 'empty_trash'} else doc
    service_name = {'patch_document': 'update_document', 'delete_document': 'soft_delete_document', 'delete_project': 'soft_delete_project'}.get(endpoint, endpoint)
    monkeypatch.setattr(main.service, service_name, mutation)
    monkeypatch.setattr(main, '_get_doc', AsyncMock(return_value=doc))
    monkeypatch.setattr(main, '_doc_json', lambda d: {'id': d.id})
    kwargs = {'ctx': SimpleNamespace(user=SimpleNamespace(id=1)), 'session': object()}
    if endpoint in {'create_document', 'patch_document'}:
        kwargs['payload'] = {}
    if endpoint in {'patch_document', 'delete_document', 'restore_document'}:
        kwargs['doc_id'] = 8
    if endpoint == 'delete_project':
        kwargs['project'] = 'project'
    if endpoint == 'empty_trash':
        kwargs['expected_revision'] = 'trash-fixture'
        monkeypatch.setattr(main, '_trash_snapshot', AsyncMock(return_value={'total':1,'revision':'trash-fixture'}))
    await getattr(main, endpoint)(**kwargs)
