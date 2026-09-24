import asyncio
from unittest.mock import AsyncMock
import pytest
from app import service
from test_storage_transactions import Session, Rows, make_doc


class VectorSession(Session):
    def __init__(self):
        super().__init__()
        self.old = []
        self.writes = []
    async def execute(self, query, params=None):
        sql = str(query)
        if sql.startswith('SELECT'):
            # Both old and new implementations get the rows they actually selected.
            return Rows([(r[1], r[3]) for r in self.old] if sql.startswith('SELECT content,') else self.old)
        if 'SET embedding' in sql:
            self.writes.append(params['vec'])
        return Rows()


@pytest.mark.parametrize('change', ['title', 'heading', 'model', 'base', 'dim', 'preprocess'])
def test_vectors_recompute_when_full_input_or_configuration_changes(monkeypatch, change):
    doc = make_doc()
    doc.content = 'unchanged body'
    session = VectorSession()
    monkeypatch.setattr(service.settings, 'embed_dim', 2)
    monkeypatch.setattr(service, '_tsvector_literal', lambda x: x)
    embed = AsyncMock(return_value=[[1.0, 2.0]])
    monkeypatch.setattr(service, 'embed_texts', embed)
    pieces = [{'heading': 'old heading', 'content': doc.content}]
    monkeypatch.setattr(service, 'split_chunks', lambda _: pieces)
    asyncio.run(service._reindex_document(session, doc))
    session.old = [(0, doc.content, 'old heading', '[1,2]')]
    embed.reset_mock()
    if change == 'title': doc.title = 'new title'
    if change == 'heading': pieces[0]['heading'] = 'new heading'
    if change == 'model': monkeypatch.setattr(service.settings, 'embed_model', 'new-model')
    if change == 'base': monkeypatch.setattr(service.settings, 'embed_api_base', 'https://example.invalid/v2')
    if change == 'dim': monkeypatch.setattr(service.settings, 'embed_dim', 3)
    if change == 'preprocess': monkeypatch.setattr(service.settings, 'embed_max_chars', 77)
    asyncio.run(service._reindex_document(session, doc))
    embed.assert_awaited_once()


def test_vectors_reuse_only_matching_provenance_without_remote(monkeypatch):
    doc = make_doc()
    session = VectorSession()
    monkeypatch.setattr(service.settings, 'embed_dim', 2)
    monkeypatch.setattr(service, '_tsvector_literal', lambda x: x)
    embed = AsyncMock(return_value=[[1.0, 2.0]])
    monkeypatch.setattr(service, 'embed_texts', embed)
    asyncio.run(service._reindex_document(session, doc))
    session.old = [(0, doc.content, '', '[1,2]')]
    embed.reset_mock()
    session.writes = []
    asyncio.run(service._reindex_document(session, doc, do_embed=False))
    assert session.writes == ['[1,2]']
    assert '_memorys_vectors' in doc.meta
    assert '_memorys_vectors' not in service._doc_meta(doc)
    embed.assert_not_awaited()
