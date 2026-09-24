"""Full editable-state CAS; all persistence runs in temporary storage."""
import asyncio
from datetime import datetime, timezone

import pytest
from app import service
from test_storage_transactions import Session, make_doc, setup_storage


def revision(doc):
    from app import revisions
    return revisions.document_revision(doc)


@pytest.mark.parametrize('field,value', [
    ('title', 'new'), ('library', 'other'), ('md_type', 'howto'),
    ('project', 'new'), ('tags', ['new']), ('importance', 5),
    ('source', 'other'), ('links', [{'type': 'relates', 'target': '#2'}]),
    ('content', 'new body'),
])
def test_revision_covers_each_editable_field(field, value):
    doc = make_doc()
    before = revision(doc)
    setattr(doc, field, value)
    assert revision(doc) != before


def test_revision_ignores_access_and_timestamp_updates():
    doc = make_doc()
    before = revision(doc)
    doc.access_count = 12
    doc.last_accessed_at = doc.updated_at = datetime.now(timezone.utc)
    assert revision(doc) == before


def test_revision_canonicalizes_nested_mapping_order():
    doc = make_doc()
    doc.links = [{'type': 'relates', 'target': '#2'}]
    before = revision(doc)
    doc.links = [{'target': '#2', 'type': 'relates'}]
    assert revision(doc) == before


def test_revision_rejects_metadata_edit_discovered_under_lock(setup_storage):
    root, doc, user = setup_storage
    before = (root / doc.rel_path).read_bytes()
    token = revision(doc)
    session = Session()
    async def refresh(d):
        d.project = 'concurrent project'
    session.refresh = refresh
    with pytest.raises(service.ConflictError):
        asyncio.run(service.update_document(session, user, doc, {'project': 'stale'},
                                           expected_revision=token))
    assert (root / doc.rel_path).read_bytes() == before
    assert doc.project == 'concurrent project'
    assert not session.rolled_back


def test_matching_revision_allows_update_and_rotates_token(setup_storage):
    root, doc, user = setup_storage
    token = revision(doc)
    result = asyncio.run(service.update_document(Session(), user, doc,
                         {'project': 'edited'}, expected_revision=token))
    assert result.project == 'edited'
    assert revision(result) != token
    assert 'edited' in (root / doc.rel_path).read_text()


def test_deleted_after_read_is_never_recreated(setup_storage):
    root, doc, user = setup_storage
    (root / doc.rel_path).unlink()
    session = Session()
    async def refresh(d):
        d.deleted_at = datetime.now(timezone.utc)
    session.refresh = refresh
    with pytest.raises(service.ConflictError, match='删除'):
        asyncio.run(service.update_document(session, user, doc, {'content': 'resurrected'}))
    assert not (root / doc.rel_path).exists()


def test_legacy_hash_remains_body_only(setup_storage):
    root, doc, user = setup_storage
    old_hash = doc.content_hash
    result = asyncio.run(service.update_document(Session(), user, doc,
                         {'project': 'edited'}, expected_hash=old_hash))
    assert result.content_hash == old_hash
    assert result.project == 'edited'
