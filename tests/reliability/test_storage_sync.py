import asyncio
from unittest.mock import AsyncMock
import pytest
from app import service, mdstore
from test_storage_transactions import Session, Rows, setup_storage


@pytest.mark.parametrize('deleted', [False, True])
def test_sync_treats_all_frontmatter_and_body_as_authority(setup_storage, monkeypatch, deleted):
    root, doc, user = setup_storage
    doc.deleted_at = service._now() if deleted else None
    body = 'restored disk content' if deleted else doc.content
    meta = {'id': 'stable', 'title': 'disk title', 'type': 'decision', 'project': 'disk project',
            'tags': ['disk'], 'importance': 5, 'source': 'manual', 'custom': {'kept': True},
            'created_at': '2020-01-02T00:00:00Z', 'updated_at': '2021-02-03T00:00:00Z',
            'links': [{'type': 'relates', 'target': 'other'}]}
    # No renderer whitespace change to accidentally turn metadata-only test into body edit.
    (root / doc.rel_path).write_text(mdstore.build_frontmatter(meta) + body)
    class SyncSession(Session):
        async def execute(self, *a, **kw): return Rows([doc])
    result = asyncio.run(service.sync_from_disk(SyncSession(), user))
    assert result['updated'] == 1
    assert doc.deleted_at is None
    assert doc.content == body
    assert doc.content_hash == mdstore.compute_hash(body)
    assert (doc.title, doc.md_type, doc.project, doc.tags, doc.importance, doc.source) == (
        'disk title', 'decision', 'disk project', ['disk'], 5, 'manual')
    assert doc.created_at.year == 2020
    assert doc.updated_at.year == 2021
    assert doc.meta['custom'] == {'kept': True}
    service._reindex_document.assert_awaited_once()
