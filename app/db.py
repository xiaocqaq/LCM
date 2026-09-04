"""数据库引擎与会话。

引擎参数按后端分开：SQLite 不吃连接池那套参数（它没有网络连接的概念），
硬传 pool_size 会直接报 TypeError。
"""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import DB_BACKEND, settings
from .dialect import PG, SQLITE, SQLITE_PRAGMAS

if DB_BACKEND == PG:
    engine = create_async_engine(
        settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
else:
    # SQLite：
    # - 不传 pool_size/max_overflow（aiosqlite 用 StaticPool 语义，传了报错）
    # - check_same_thread=False：aiosqlite 在线程池里跑，连接会跨线程
    engine = create_async_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )

    # 每条新连接都要重设 PRAGMA。
    #
    # 这一步容易漏：PRAGMA 是**连接级**设置，不是数据库级。
    # 在 lifespan 里执行一次只对那一条连接有效，后续从池里拿到的新连接
    # foreign_keys 又变回 OFF —— 于是删文档不级联删 chunk，留下孤儿行，
    # 而且只在"连接池扩容之后"才出现，极难复现。
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for p in SQLITE_PRAGMAS:
            try:
                cur.execute(p)
            except Exception:
                pass
        cur.close()

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session():
    async with SessionLocal() as session:
        yield session
