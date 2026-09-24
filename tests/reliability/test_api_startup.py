from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import main, mcp_app


@pytest.mark.asyncio
async def test_startup_ddl_failure_rolled_back_before_next_statement(monkeypatch):
    aborted = False
    depth = 0
    seen = []
    @asynccontextmanager
    async def nested():
        nonlocal depth, aborted
        depth += 1
        try:
            yield
        except Exception:
            aborted = False
            raise
        finally:
            depth -= 1
    async def execute(statement):
        nonlocal aborted
        sql = str(statement)
        if aborted:
            raise RuntimeError('transaction aborted')
        seen.append(sql)
        if 'idx_chunks_tsv' in sql:
            aborted = True
            raise RuntimeError('missing extension')
        if sql.startswith(('CREATE ', 'ALTER ', 'DROP ')):
            assert depth > 0, 'each optional DDL needs a savepoint'
        return SimpleNamespace(scalar=lambda: b'e', fetchall=lambda: [('old-index',)])
    conn = SimpleNamespace(execute=execute, begin_nested=nested, run_sync=AsyncMock())
    @asynccontextmanager
    async def begin():
        yield conn
        assert not aborted
    @asynccontextmanager
    async def run():
        yield
    monkeypatch.setattr(main, 'engine', SimpleNamespace(begin=begin, dispose=AsyncMock()))
    monkeypatch.setattr(mcp_app, 'session_manager', SimpleNamespace(run=run))
    async with main.lifespan(main.app):
        pass
    assert any('idx_documents_user_project' in sql for sql in seen)
    assert any('USING hnsw' in sql for sql in seen)
