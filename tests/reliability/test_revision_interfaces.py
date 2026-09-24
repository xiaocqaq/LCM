import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import main, mcp_server as m
from app.revisions import document_revision
from test_storage_transactions import Session, Rows, setup_storage


@pytest.mark.asyncio
async def test_rest_revision_roundtrip_and_metadata_conflict(setup_storage, monkeypatch):
    root, doc, user = setup_storage
    session = Session()
    session.execute = AsyncMock(return_value=Rows([doc]))
    monkeypatch.setattr(main.links_mod, 'incoming_links', AsyncMock(return_value=[]))
    main.app.dependency_overrides[main.auth] = lambda: SimpleNamespace(user=user)
    main.app.dependency_overrides[main.get_session] = lambda: session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as c:
            original = (await c.get('/api/v1/documents/1')).json()
            assert original['revision'] == document_revision(doc)
            saved = await c.patch('/api/v1/documents/1', json={'project': 'winner', 'expectedRevision': original['revision']})
            assert saved.status_code == 200
            assert saved.json()['revision'] != original['revision']
            assert saved.json()['contentHash'] == original['contentHash']
            stale = await c.patch('/api/v1/documents/1', json={'project': 'loser', 'expectedRevision': original['revision']})
            assert stale.status_code == 409
            assert stale.json()['revision'] == saved.json()['revision']
            assert doc.project == 'winner'
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_mcp_revision_roundtrip_and_metadata_conflict(setup_storage, monkeypatch):
    root, doc, user = setup_storage
    session = Session()
    session.execute = AsyncMock(return_value=Rows([doc]))
    @asynccontextmanager
    async def sessions():
        yield session
    monkeypatch.setattr(m, 'SessionLocal', sessions)
    monkeypatch.setattr(m.links_mod, 'incoming_links', AsyncMock(return_value=[]))
    auth = m.current_auth.set(SimpleNamespace(user=user))
    try:
        original = json.loads(await m.memory_get(1))
        assert original['revision'] == document_revision(doc)
        saved = json.loads(await m.memory_update(1, project='winner', expected_revision=original['revision']))
        assert saved['ok'] is True
        assert saved['revision'] != original['revision']
        assert saved['content_hash'] == original['content_hash']
        stale = json.loads(await m.memory_update(1, project='loser', expected_revision=original['revision']))
        assert stale['error'] == 'conflict'
        assert stale['revision'] == saved['revision']
        assert doc.project == 'winner'
        schema = next(t for t in await m.mcp.list_tools() if t.name == 'memory_update').inputSchema
        assert 'expected_revision' in schema['properties']
        assert 'expected_revision' not in schema.get('required', [])
    finally:
        m.current_auth.reset(auth)


def test_ui_save_sends_loaded_revision_and_keeps_it_on_conflict():
    # Reuse the existing real-script VM harness without registering its tests.
    root = Path(__file__).resolve().parents[2]
    script = r'''
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const source=fs.readFileSync('tests/frontend/navigation.test.cjs','utf8');
const prefix=source.slice(source.indexOf('const html ='),source.indexOf("test('"));
const scope={require,console,URLSearchParams,vm,readFileSync:fs.readFileSync,__dirname:process.cwd()+'/tests/frontend'};
vm.createContext(scope);vm.runInContext(prefix+';this.h=harness;',scope);
(async()=>{
 const {ctx,get}=scope.h();ctx.nav=()=>{};ctx.loadEditBranches=()=>{};ctx.loadLibs=()=>{};
 let calls=[];let conflict=false;
 ctx.api=async(url,opts)=>{
   if(!opts)return {id:1,title:'doc',content:'body',revision:'v1:loaded',library:'main',type:'fact',tags:[]};
   calls.push(JSON.parse(opts.body));if(conflict)throw Error('conflict');
   return {id:1,revision:'v1:saved'};
 };
 await ctx.openDoc(1);await ctx.saveDoc();
 assert.equal(calls[0].expectedRevision,'v1:loaded');
 assert.equal(ctx.state.doc.revision,'v1:saved');
 conflict=true;await ctx.saveDoc();await ctx.saveDoc();
 assert.equal(calls[1].expectedRevision,'v1:saved');
 assert.equal(calls[2].expectedRevision,'v1:saved');
 assert.equal(ctx.state.doc.revision,'v1:saved');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
