"""Real PostgreSQL + HTTP regression. Only an explicitly named test DB is allowed."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('MEM_TEST_DATABASE_URL'),
                                 reason='set MEM_TEST_DATABASE_URL to a disposable PostgreSQL database')]


@asynccontextmanager
async def api_client(tmp_path, monkeypatch):
    from app.main import app
    from app.config import settings
    from app.db import engine, SessionLocal
    from app.models import User
    from app.security import issue_local_jwt
    url = os.environ['MEM_TEST_DATABASE_URL']
    assert 'test' in urlparse(url).path and str(settings.database_url) == url
    monkeypatch.setattr(settings, 'data_dir', str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    ids = []
    async with app.router.lifespan_context(app):
        async with SessionLocal() as s:
            tokens = []
            for _ in range(2):
                suffix = uuid.uuid4().hex[:10]
                u = User(username='regression-' + suffix, xiaoai_user_id=int(suffix, 16),
                         display_name='隔离验收', email='', role='user')
                s.add(u)
                await s.commit()
                await s.refresh(u)
                ids.append(u.id)
                from app import gitsvc
                await asyncio.to_thread(gitsvc.ensure_repo, settings.data_dir, u.id)
                tokens.append(issue_local_jwt(u)[0])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://testserver',
                                     headers={'Authorization': 'Bearer ' + tokens[0]}) as client:
            try:
                yield client, ids, tokens
            finally:
                async with SessionLocal() as s:
                    await s.execute(delete(User).where(User.id.in_(ids)))
                    await s.commit()


async def verify_auth_isolation_and_real_pagination(api_client):
    c, ids, tokens = api_client
    from app.db import SessionLocal
    from app.models import Document
    async with SessionLocal() as s:
        for i in range(205):
            s.add(Document(user_id=ids[0], title=f'分页验收-{i:03}', library='main',
                           slug=f'page-{i}', rel_path=f'main/page-{i}.md', content='验收正文',
                           project='项目甲' if i < 204 else '', md_type='fact', tags=[]))
        await s.commit()
    r = await c.get('/api/v1/documents', params={'limit': 50, 'offset': 200})
    assert r.status_code == 200, r.text
    assert r.json()['total'] == 205
    assert len(r.json()['items']) == 5
    r = await c.get('/api/v1/documents', params={'unassigned': 'true'})
    assert r.json()['total'] == 1
    groups = (await c.get('/api/v1/projects')).json()
    assert groups['totalDocuments'] == 205
    assert {p['project']:p['count'] for p in groups['projects']} == {'项目甲':204, '':1}
    other = {'Authorization': 'Bearer ' + tokens[1]}
    assert (await c.get('/api/v1/documents', headers=other)).json()['total'] == 0
    assert (await c.get('/api/v1/projects', headers=other)).json()['totalDocuments'] == 0
    doc_id = r.json()['items'][0]['id']
    assert (await c.get(f'/api/v1/documents/{doc_id}', headers=other)).status_code == 404


async def verify_markdown_metadata_sync_and_conflict(api_client):
    c, ids, _ = api_client
    from app.config import settings
    from app.db import SessionLocal
    from app.models import Document
    from app.mdstore import user_root, parse_document, render_document
    created = await c.post('/api/v1/documents', json={'title':'原始标题','content':'正文保持一致',
                                                    'project':'旧项目','tags':['旧标签']})
    assert created.status_code == 200, created.text
    data = created.json(); doc_id = data['id']
    async with SessionLocal() as s:
        d = await s.get(Document, doc_id)
        path = user_root(settings.data_dir, ids[0]) / d.rel_path
    meta, body = parse_document(path.read_text())
    meta.update(title='修改后的标题', project='新项目', tags=['新标签'], importance=5, type='decision')
    path.write_text(render_document(meta, body))
    synced = await c.post('/api/v1/sync', json={'action':'reindex'})
    assert synced.status_code == 200, synced.text
    assert synced.json()['updated'] == 1
    actual = (await c.get(f'/api/v1/documents/{doc_id}')).json()
    assert actual['title'] == '修改后的标题'
    assert actual['project'] == '新项目'
    assert actual['type'] == 'decision'
    assert actual['importance'] == 5
    old_hash = actual['contentHash']
    first = await c.patch(f'/api/v1/documents/{doc_id}', json={'content':'第一位修改','expectedHash':old_hash})
    assert first.status_code == 200, first.text
    stale = await c.patch(f'/api/v1/documents/{doc_id}', json={'content':'过期修改','expectedHash':old_hash})
    assert stale.status_code == 409, stale.text
    assert (await c.get(f'/api/v1/documents/{doc_id}')).json()['content'] == '第一位修改'
    current = (await c.get(f'/api/v1/documents/{doc_id}')).json()
    edits = await asyncio.gather(*[
        c.patch(f'/api/v1/documents/{doc_id}', json={'content':text,'expectedHash':current['contentHash']})
        for text in ['并发修改甲', '并发修改乙']])
    assert sorted(r.status_code for r in edits) == [200,409], [r.text for r in edits]
    stored = (await c.get(f'/api/v1/documents/{doc_id}')).json()
    _, disk_body = parse_document(path.read_text())
    assert disk_body.strip() == stored['content'].strip()
    history = await c.get(f'/api/v1/documents/{doc_id}/history')
    assert history.status_code == 200, history.text


async def verify_ready_and_mcp_readonly_tool(api_client):
    c, _, _ = api_client
    assert (await c.get('/api/ready')).status_code == 200
    headers = {'Accept': 'application/json, text/event-stream'}
    r = await c.post('/mcp', headers=headers, json={'jsonrpc':'2.0','id':1,'method':'initialize',
        'params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'isolated-test','version':'1'}}})
    assert r.status_code == 200, r.text
    r = await c.post('/mcp', headers=headers, json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
    assert r.status_code == 200, r.text
    assert 'expected_hash' in r.text
    r = await c.post('/mcp', headers=headers, json={'jsonrpc':'2.0','id':3,'method':'tools/call',
        'params':{'name':'memory_list_docs','arguments':{}}})
    assert r.status_code == 200, r.text
    assert 'isError":true' not in r.text


async def verify_trash_snapshot_membership(api_client):
    c, ids, _ = api_client
    from app.db import SessionLocal
    from app.models import Document
    created = []
    for title in ['确认时的删除对象', '确认后进入回收站']:
        r = await c.post('/api/v1/documents', json={'title': title, 'content': '隔离删除验收'})
        assert r.status_code == 200, r.text
        created.append(r.json()['id'])
    first, second = created
    assert (await c.delete(f'/api/v1/documents/{first}')).status_code == 200
    snapshot = (await c.get('/api/v1/trash/snapshot')).json()
    assert (await c.post(f'/api/v1/documents/{first}/restore')).status_code == 200
    assert (await c.delete(f'/api/v1/documents/{second}')).status_code == 200
    newer = (await c.get('/api/v1/trash/snapshot')).json()
    assert snapshot['total'] == newer['total']
    assert snapshot['revision'] != newer['revision']
    stale = await c.post('/api/v1/trash/empty', params={
        'expected_count': snapshot['total'], 'expected_revision': snapshot['revision']})
    assert stale.status_code == 409, stale.text
    async with SessionLocal() as s:
        doc = await s.get(Document, second)
        assert doc is not None and doc.deleted_at is not None
    assert (await c.post('/api/v1/trash/empty')).status_code == 428
    confirmed = await c.post('/api/v1/trash/empty', params={
        'expected_count': newer['total'], 'expected_revision': newer['revision']})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['purged'] == newer['total']
    async with SessionLocal() as s:
        assert await s.get(Document, second) is None
        assert await s.get(Document, first) is not None


@pytest.mark.asyncio
async def test_postgres_http_and_mcp_end_to_end(tmp_path, monkeypatch):
    # MCP task group must enter/exit in one task and may run only once per instance.
    async with api_client(tmp_path, monkeypatch) as fixture:
        await verify_auth_isolation_and_real_pagination(fixture)
        await verify_markdown_metadata_sync_and_conflict(fixture)
        await verify_ready_and_mcp_readonly_tool(fixture)
        await verify_trash_snapshot_membership(fixture)
