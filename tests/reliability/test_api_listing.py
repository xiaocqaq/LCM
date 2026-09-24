from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app import main


@pytest.fixture
def listing_session():
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE documents (id INTEGER PRIMARY KEY, user_id INTEGER, library TEXT, slug TEXT, title TEXT, md_type TEXT, project TEXT, tags JSON, importance INTEGER, source TEXT, content TEXT, links JSON, superseded_by INTEGER, access_count INTEGER, last_accessed_at DATETIME, meta JSON, rel_path TEXT, content_hash TEXT, created_at DATETIME, updated_at DATETIME, deleted_at DATETIME)'))
        for i in range(1, 207):
            conn.execute(text("INSERT INTO documents (id,user_id,library,title,md_type,project,tags,links,content,updated_at) VALUES (:id,:uid,'main',:title,'fact',:project,'[]','[]','BODY MUST NOT BE RETURNED','2026-01-01')"), {'id': i, 'uid': 1 if i <= 205 else 2, 'title': f'title-{i}', 'project': 'Alpha' if i <= 201 else (None if i == 202 else '')})
    with Session(engine) as session:
        async def execute(query):
            return session.execute(query)
        yield SimpleNamespace(execute=execute)
    engine.dispose()


@pytest.mark.asyncio
async def test_list_true_count_stable_order_unassigned(listing_session):
    ctx = SimpleNamespace(user=SimpleNamespace(id=1))
    result = await main.list_documents(ctx=ctx, session=listing_session, limit=2, offset=1)
    assert result['total'] == 205
    assert [d['id'] for d in result['items']] == [204, 203]
    result = await main.list_documents(ctx=ctx, session=listing_session, unassigned=True, limit=50, offset=0)
    assert result['total'] == 4
    assert {d['id'] for d in result['items']} == {202, 203, 204, 205}


@pytest.mark.asyncio
async def test_rest_document_includes_sanitized_persistence_status(listing_session):
    from sqlalchemy import select
    doc = (await listing_session.execute(select(main.Document).where(main.Document.id == 1))).scalar_one()
    doc.persistence_status = {'disk': 'saved', 'database': 'saved', 'ok': False, 'retryable': True,
                              'git': {'ok': False, 'status': 'failed', 'error': 'private-password'}}
    result = main._doc_json(doc)
    assert result['persistenceStatus']['git']['ok'] is False
    assert 'private-password' not in str(result)


@pytest.mark.asyncio
async def test_projects_full_database_aggregate_metadata_only(listing_session):
    ctx = SimpleNamespace(user=SimpleNamespace(id=1))
    assert hasattr(main, 'projects')
    result = await main.projects(ctx=ctx, session=listing_session)
    assert result['totalDocuments'] == 205
    assert result['libraries'] == ['main']
    assert [(p['project'], p['count'], p['latestTitle']) for p in result['projects']] == [('Alpha', 201, 'title-201'), ('', 4, 'title-205')]
    assert 'BODY' not in str(result)
    result = await main.projects(ctx=ctx, session=listing_session, q='title-205', type='fact', library='main')
    assert result['totalDocuments'] == 1
    assert result['projects'][0]['project'] == ''
    result = await main.projects(ctx=ctx, session=listing_session, q='nonexistent')
    assert result['totalDocuments'] == 0
    assert result['projects'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['limit=0', 'limit=201', 'offset=-1'])
async def test_invalid_paging_rejected(query, listing_session):
    main.app.dependency_overrides[main.auth] = lambda: SimpleNamespace(user=SimpleNamespace(id=1))
    main.app.dependency_overrides[main.get_session] = lambda: listing_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as client:
            response = await client.get('/api/v1/documents?' + query)
        assert response.status_code == 422
    finally:
        main.app.dependency_overrides.clear()
