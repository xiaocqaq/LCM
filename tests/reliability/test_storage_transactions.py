import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app import service, mdstore
from app.models import Document


class Rows:
    def __init__(self, values=()):
        self.values = list(values)
    def scalars(self): return self
    def all(self): return self.values
    def __iter__(self): return iter(self.values)
    def scalar_one_or_none(self): return self.values[0] if self.values else None


class Session:
    def __init__(self, doc=None, fail=False):
        self.doc, self.fail, self.rolled_back = doc, fail, False
        self.refresh = AsyncMock()
    async def execute(self, *a, **kw): return Rows()
    async def flush(self): pass
    async def commit(self):
        if self.fail: raise RuntimeError('DB unavailable')
    async def rollback(self): self.rolled_back = True
    def add(self, obj):
        obj.id = 1
        if isinstance(obj, Document):
            obj.created_at = obj.updated_at = datetime.now(timezone.utc)


def make_doc():
    return Document(id=1, user_id=1, title='old', slug='old', library='main',
        rel_path='main/old.md', content='original', content_hash=mdstore.compute_hash('original'),
        md_type='fact', project='', tags=[], importance=3, source='test', links=[], meta={},
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))


@pytest.fixture
def setup_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, 'data_dir', str(tmp_path))
    monkeypatch.setattr(service, '_reindex_document', AsyncMock())
    monkeypatch.setattr(service.links_mod, 'apply_links', AsyncMock(return_value={'unresolved': []}))
    monkeypatch.setattr(service.links_mod, 'clear_supersede_marks', AsyncMock())
    monkeypatch.setattr(service.gitsvc, 'try_commit_all', lambda *a: {'ok': True, 'status': 'committed', 'commit': 'abc'})
    root = mdstore.user_root(str(tmp_path), 1)
    (root / 'main').mkdir(parents=True)
    doc = make_doc()
    (root / doc.rel_path).write_text(mdstore.render_document({'id': 'stable', 'custom': {'flag': True}}, doc.content))
    return root, doc, SimpleNamespace(id=1)


def test_commit_refreshes_server_generated_attributes(setup_storage):
    root, doc, user = setup_storage
    session = Session()
    events = []
    async def commit(): events.append('commit')
    async def refresh(d): events.append('refresh')
    session.commit = commit
    session.refresh = refresh
    asyncio.run(service.create_document(session, user, {'title': 'new', 'content': 'body'}))
    assert events == ['commit', 'refresh']


def test_update_library_moves_file_and_reserves_deleted_paths(setup_storage):
    root, doc, user = setup_storage
    class TakenSession(Session):
        async def execute(self, *a, **kw): return Rows(['other/old.md'])
    result = asyncio.run(service.update_document(TakenSession(), user, doc, {'library': 'other'}))
    assert result.rel_path == 'other/old-2.md'
    assert (root / result.rel_path).exists()
    assert not (root / 'main/old.md').exists()


def test_update_normalizes_yaml_dates_for_jsonb(setup_storage):
    root, doc, user = setup_storage
    (root / doc.rel_path).write_text('---\ncustom_date: 2020-01-01\n---\nbody')
    result = asyncio.run(service.update_document(Session(), user, doc, {'content': 'saved'}))
    import json
    json.dumps(result.meta)  # JSONB must not receive date/datetime objects
    assert str(result.meta['custom_date']) == '2020-01-01'


def test_failed_update_restores_old_path_and_removes_new(setup_storage):
    root, doc, user = setup_storage
    before = (root / doc.rel_path).read_bytes()
    session = Session(fail=True)
    with pytest.raises(RuntimeError, match='DB unavailable'):
        asyncio.run(service.update_document(session, user, doc, {'title': 'new', 'content': 'changed'}))
    assert (root / 'main/old.md').exists()
    assert (root / 'main/old.md').read_bytes() == before
    assert not (root / 'main/new.md').exists()
    assert session.rolled_back


def test_failed_create_removes_uncommitted_file(setup_storage):
    root, doc, user = setup_storage
    with pytest.raises(RuntimeError, match='DB unavailable'):
        asyncio.run(service.create_document(Session(fail=True), user, {'title': 'new', 'content': 'created'}))
    assert not (root / 'main/new.md').exists()


def test_failed_delete_restores_file(setup_storage):
    root, doc, user = setup_storage
    with pytest.raises(RuntimeError, match='DB unavailable'):
        asyncio.run(service.soft_delete_document(Session(fail=True), user, doc))
    assert (root / 'main/old.md').exists()
    assert not list((root / '.trash').glob('*.md'))


def test_update_refreshes_before_conflict_check(setup_storage):
    root, doc, user = setup_storage
    old_hash = doc.content_hash
    session = Session()
    async def refresh(d): d.content_hash = 'newest'
    session.refresh = refresh
    with pytest.raises(service.ConflictError):
        asyncio.run(service.update_document(session, user, doc, {'content': 'overwrite'}, expected_hash=old_hash))
    assert not session.rolled_back  # conflict response still needs the refreshed hash


def test_append_merges_latest_body_and_preserves_extensions(setup_storage):
    root, doc, user = setup_storage
    session = Session()
    async def refresh(d):
        if d.content == 'original':
            d.content = 'latest'
    session.refresh = refresh
    result = asyncio.run(service.update_document(session, user, doc, {'_append_content': 'append'}))
    assert result.content == 'latest\n\nappend'
    meta, body = mdstore.parse_document((root / doc.rel_path).read_text())
    assert meta['id'] == 'stable'
    assert meta['custom'] == {'flag': True}


def test_git_failure_exposed_without_rolling_back_saved_doc(setup_storage, monkeypatch):
    root, doc, user = setup_storage
    monkeypatch.setattr(service.gitsvc, 'try_commit_all', lambda *a: {'ok': False, 'status': 'failed', 'error': 'full'})
    result = asyncio.run(service.update_document(Session(), user, doc, {'content': 'saved'}))
    assert getattr(result, 'persistence_status', {}).get('git', {}).get('status') == 'failed'
    assert 'saved' in (root / doc.rel_path).read_text()
