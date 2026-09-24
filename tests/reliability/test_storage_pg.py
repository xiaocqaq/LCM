"""Optional real-PG tests; use only the explicitly provisioned isolated database."""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app import service, mdstore
from app.models import User


@pytest.mark.skipif(not os.environ.get('MEM_TEST_DATABASE_URL'), reason='isolated PostgreSQL not requested')
def test_real_pg_sync_keeps_markdown_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, 'data_dir', str(tmp_path))
    monkeypatch.setattr(service, 'embed_texts', AsyncMock(return_value=None))
    async def run():
        engine = create_async_engine(os.environ['MEM_TEST_DATABASE_URL'])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        uid = None
        try:
            async with sessions() as session:
                nonce = uuid.uuid4().hex[:12]
                user = User(xiaoai_user_id=int(nonce, 16), username='storage-test-' + nonce)
                session.add(user)
                await session.commit()
                uid = user.id
                await asyncio.to_thread(service.gitsvc.ensure_repo, str(tmp_path), uid)
                doc = await service.create_document(session, user, {'title': 'real fixture', 'content': 'body'})
                path = mdstore.user_root(str(tmp_path), uid) / doc.rel_path
                meta, body = mdstore.parse_document(path.read_text())
                meta.update(title='changed on disk', updated_at='2021-02-03T00:00:00Z')
                mdstore.atomic_write(path, mdstore.render_document(meta, body))
                await service.sync_from_disk(session, user)
                await session.refresh(doc)
                assert doc.updated_at == datetime(2021, 2, 3, tzinfo=timezone.utc)
        finally:
            if uid is not None:
                async with sessions() as session:
                    await session.execute(delete(User).where(User.id == uid))
                    await session.commit()
            await engine.dispose()
    asyncio.run(run())
