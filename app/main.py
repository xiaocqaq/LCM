"""FastAPI 主应用：REST API + MCP（streamable HTTP）+ Web UI。"""
import asyncio
import logging
import os
import stat
import subprocess
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from . import links as links_mod
from .mcp_server import mcp, public_persistence_status
from .config import settings
from .db import SessionLocal, get_session, engine
from .mdstore import user_root
from .mutations import finish_mutation, run_blocking, user_lock
from .models import ApiKey, Base, Document, User
from .security import AuthContext, authenticate_headers, hash_key, issue_local_jwt, new_api_key

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)
READY_TIMEOUT_SECONDS = 2.0


async def _startup_statement(conn, statement):
    """An optional migration failure must not poison the outer PG transaction."""
    try:
        async with conn.begin_nested():
            return await conn.execute(text(statement))
    except Exception:
        logger.warning("Optional startup statement failed: %s", statement, exc_info=True)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建表（幂等）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # DDL 必须设 lock_timeout。
        #
        # 这里踩过一次线上级别的坑：下面的 ALTER TABLE 需要 ACCESS EXCLUSIVE 锁，
        # 只要有任何一个连接还占着 chunks 的锁（比如一个 idle in transaction 的
        # 孤儿连接），ALTER 就会排队等待 —— 而 PG 的锁队列是 FIFO：
        # 一个**待授予**的 ACCESS EXCLUSIVE 会把它后面所有对该表的读写全部挡住。
        # 结果是整张 chunks 表连 SELECT 都做不了，检索、reindex、sync 全部挂死，
        # 表面症状是"请求超时"，完全看不出跟启动期 DDL 有关。
        #
        # 3 秒拿不到锁就放弃：这些 DDL 全是幂等的补列/建索引，
        # 这次没做成下次启动会再试，绝不值得拿整张表的可用性去换。
        await conn.execute(text("SET lock_timeout = '3s'"))

        # 索引与列调整（幂等）
        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin(tsv)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_trgm ON chunks USING gin(content gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_documents_user_project ON documents(user_id, project) WHERE deleted_at IS NULL",
            # 关系与热度字段。create_all 只建新表不改已有表，所以老库要靠这几句补列。
            # 用 IF NOT EXISTS 保持幂等，比引 alembic 轻 —— 这个项目的 schema
            # 变动是加列级别的，上迁移框架的维护成本大于收益。
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS links JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS superseded_by INTEGER",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS access_count INTEGER DEFAULT 0",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ",
            # 检索每次都带 superseded_by IS NULL，走部分索引比全表过滤便宜
            "CREATE INDEX IF NOT EXISTS idx_documents_not_superseded "
            "ON documents(user_id) WHERE deleted_at IS NULL AND superseded_by IS NULL",
        ]:
            await _startup_statement(conn, stmt)

        # Probe storage before ALTER to avoid unnecessary exclusive table locks.
        result = await _startup_statement(conn,
            "SELECT attstorage FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'")
        cur = result.scalar() if result is not None else None
        if isinstance(cur, (bytes, bytearray)):
            cur = cur.decode()
        if cur and cur != "e":
            await _startup_statement(conn, "ALTER TABLE chunks ALTER COLUMN embedding SET STORAGE EXTERNAL")

        dim = settings.embed_dim
        rows = await _startup_statement(conn,
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename='chunks' AND indexname LIKE 'idx_chunks_embedding%'")
        keep = f"idx_chunks_embedding_{dim}"
        # Each DROP/CREATE gets its own savepoint, including extension failures.
        if rows is not None:
            for (name,) in rows.fetchall():
                if name != keep:
                    quoted = name.replace('"', '""')
                    await _startup_statement(conn, f'DROP INDEX IF EXISTS "{quoted}"')
        await _startup_statement(conn,
            f"CREATE INDEX IF NOT EXISTS {keep} ON chunks "
            f"USING hnsw((embedding::vector({dim})) vector_cosine_ops)")
    # MCP streamable HTTP 需要在应用生命周期内跑起 task group
    from .mcp_app import session_manager
    async with session_manager.run():
        yield
    await engine.dispose()


# 不要用 root_path：它会让 Starlette 的 mount 匹配也带上前缀，
# 而 nginx 已经把 /mem 剥掉了，结果 /mcp 挂载点直接 404。
# docs 页面引用 openapi.json 的绝对路径问题，用下面的自定义 /api/docs 路由解决。
app = FastAPI(
    title="memorys",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    openapi_url="/api/openapi.json",
)


@app.get("/api/docs", include_in_schema=False)
async def swagger_docs():
    """自定义 docs：openapi.json 的 URL 要带上反代前缀，否则子路径部署下取不到。"""
    from fastapi.openapi.docs import get_swagger_ui_html

    return get_swagger_ui_html(
        openapi_url=f"{settings.root_path}/api/openapi.json",
        title="memorys API",
    )


# ---------- 认证 ----------
async def auth(request: Request, session: AsyncSession = Depends(get_session)) -> AuthContext:
    return await authenticate_headers(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
        session,
    )


@app.post("/api/v1/auth/ai-login")
async def ai_login(payload: dict, session: AsyncSession = Depends(get_session)):
    """复用 ai.xlingo.fun 账号密码登录。也支持 accessToken 模式。"""
    from .upstream import UpstreamAuthError, upstream_login, upstream_me

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    access_token = str(payload.get("accessToken") or "").strip()

    try:
        if access_token:
            user_view = await upstream_me(access_token)
        elif username and password:
            data = await upstream_login(username, password)
            user_view = data.get("user") or {}
        else:
            raise HTTPException(400, "需要 username/password 或 accessToken")
    except UpstreamAuthError as e:
        raise HTTPException(e.status, e.message)

    user = await service.upsert_user_from_xiaoai(session, user_view)
    token, exp = issue_local_jwt(user)
    return {
        "accessToken": token,
        "expiresAt": exp.isoformat(),
        "user": {"id": user.id, "username": user.username, "displayName": user.display_name, "role": user.role},
        "bound": True,
    }


@app.get("/api/v1/auth/me")
async def me(ctx: AuthContext = Depends(auth)):
    return {"id": ctx.user.id, "username": ctx.user.username, "displayName": ctx.user.display_name,
            "role": ctx.user.role, "via": ctx.via}


# ---------- API Key ----------
@app.get("/api/v1/keys")
async def list_keys(ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(ApiKey).where(ApiKey.user_id == ctx.user.id, ApiKey.revoked_at.is_(None)).order_by(ApiKey.id.desc())
    )).scalars().all()
    return [{"id": k.id, "name": k.name, "prefix": k.prefix,
             "createdAt": k.created_at.isoformat() if k.created_at else None,
             "lastUsedAt": k.last_used_at.isoformat() if k.last_used_at else None} for k in rows]


@app.post("/api/v1/keys")
async def create_key(payload: dict, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    raw, prefix, khash = new_api_key()
    k = ApiKey(user_id=ctx.user.id, name=str(payload.get("name") or "default")[:64], prefix=prefix, key_hash=khash)
    session.add(k)
    await session.commit()
    return {"id": k.id, "name": k.name, "prefix": prefix, "key": raw}  # key 只返回一次


@app.delete("/api/v1/keys/{key_id}")
async def revoke_key(key_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    from datetime import datetime, timezone
    k = (await session.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == ctx.user.id))).scalar_one_or_none()
    if not k:
        raise HTTPException(404, "key 不存在")
    k.revoked_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


# ---------- 文档 CRUD ----------
def _doc_json(d: Document, with_content: bool = True) -> dict:
    from .revisions import document_revision

    out = {"id": d.id, "title": d.title, "type": d.md_type, "library": d.library, "project": d.project,
           "tags": d.tags, "importance": d.importance, "source": d.source,
           "links": d.links or [],
           # 非空表示这篇已被别的文档取代，默认不参与检索和 bootstrap
           "supersededBy": d.superseded_by,
           "accessCount": d.access_count or 0,
           "lastAccessedAt": d.last_accessed_at.isoformat() if d.last_accessed_at else None,
           "createdAt": d.created_at.isoformat() if d.created_at else None,
           "updatedAt": d.updated_at.isoformat() if d.updated_at else None,
           "contentHash": d.content_hash,
           "revision": document_revision(d),
           "persistenceStatus": public_persistence_status(d)}
    if with_content:
        out["content"] = d.content
    # links 里有解析不出来的目标时必须让调用方看见：
    # 用户以为 supersedes 生效了，实际旧文档还在检索里，静默失败最难查
    report = getattr(d, "link_report", None)
    if report:
        out["linkReport"] = report
    return out


async def _get_doc(ctx: AuthContext, session: AsyncSession, doc_id: int, include_deleted=False) -> Document:
    d = (await session.execute(select(Document).where(Document.id == doc_id, Document.user_id == ctx.user.id))).scalar_one_or_none()
    if not d or (d.deleted_at and not include_deleted):
        raise HTTPException(404, "文档不存在")
    return d


@app.get("/api/v1/documents")
async def list_documents(ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session),
                         library: str | None = None, project: str | None = None, type: str | None = None,
                         q: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50,
                         offset: Annotated[int, Query(ge=0)] = 0, trash: bool = False, unassigned: bool = False):
    conds = [Document.user_id == ctx.user.id]
    if trash:
        conds.append(Document.deleted_at.is_not(None))
    else:
        conds.append(Document.deleted_at.is_(None))
    if library:
        conds.append(Document.library == library)
    if unassigned:
        conds.append(or_(Document.project == "", Document.project.is_(None)))
    elif project:
        conds.append(Document.project == project)
    if type:
        conds.append(Document.md_type == type)
    if q:
        conds.append(Document.title.ilike(f"%{q}%"))
    rows = (await session.execute(
        select(Document).where(*conds).order_by(Document.updated_at.desc(), Document.id.desc()).limit(limit).offset(offset)
    )).scalars().all()
    total = (await session.execute(select(func.count()).select_from(Document).where(*conds))).scalar_one()
    return {"total": total, "items": [_doc_json(d, with_content=False) for d in rows]}


@app.get("/api/v1/projects")
async def projects(q: str | None = None, type: str | None = None, library: str | None = None,
                   ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    """Aggregate all matching documents in SQL; never load their bodies."""
    conds = [Document.user_id == ctx.user.id, Document.deleted_at.is_(None)]
    if q:
        conds.append(Document.title.ilike(f"%{q}%"))
    if type:
        conds.append(Document.md_type == type)
    if library:
        conds.append(Document.library == library)
    project = func.coalesce(Document.project, "")
    ranked = select(
        project.label("project"), Document.title, Document.updated_at,
        func.count().over(partition_by=project).label("count"),
        func.row_number().over(partition_by=project,
                               order_by=(Document.updated_at.desc(), Document.id.desc())).label("position"),
    ).where(*conds).subquery()
    rows = (await session.execute(
        select(ranked.c.project, ranked.c.count, ranked.c.title, ranked.c.updated_at)
        .where(ranked.c.position == 1).order_by(ranked.c.count.desc(), ranked.c.project)
    )).all()
    libraries = (await session.execute(
        select(Document.library).where(*conds).distinct().order_by(Document.library)
    )).scalars().all()
    return {"totalDocuments": sum(row.count for row in rows),
            "projects": [{"project": row.project, "count": row.count, "latestTitle": row.title,
                          "updatedAt": row.updated_at.isoformat() if row.updated_at else None} for row in rows],
            "libraries": libraries}


@app.post("/api/v1/documents")
async def create_document(payload: dict, branch: str = "", ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    # branch 为空或等于当前分支 → 走正常路径（落盘 + 进 DB + 建索引）
    # 指定了别的分支 → 只往 git 对象库提交，不动工作区也不进 DB
    async with user_lock(ctx.user.id):
        if branch:
            try:
                return await service.write_to_branch(session, ctx.user, payload, branch)
            except ValueError as e:
                raise HTTPException(400, detail=str(e))
        try:
            doc = await service.create_document(session, ctx.user, payload)
        except service.DuplicateError as e:
            return JSONResponse(status_code=409, content={"detail": "已存在同标题文档", "documentId": e.doc.id, "title": e.doc.title})
        return _doc_json(doc)


@app.get("/api/v1/documents/{doc_id}")
async def get_document(doc_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    d = await _get_doc(ctx, session, doc_id)
    out = _doc_json(d)
    # 反向关系：谁指向了这篇。单篇详情才查（要扫 JSONB），列表页不查。
    # 有这个才能解释"为什么这篇文档搜不到了"—— 看到 supersededBy 指向谁。
    out["incomingLinks"] = await links_mod.incoming_links(session, ctx.user, d)
    return out


@app.get("/api/v1/documents/{doc_id}/related")
async def related_documents(doc_id: int, ctx: AuthContext = Depends(auth),
                            session: AsyncSession = Depends(get_session)):
    """这篇文档的关系全貌：出边（已解析）+ 入边。

    单独开一个端点而不是塞进详情：UI 的关系面板是按需展开的，
    详情页每次都算反向关系会让列表页和详情页的响应时间不一致。
    """
    d = await _get_doc(ctx, session, doc_id)
    outgoing = []
    for link in (d.links or []):
        t = await links_mod.resolve_target(session, ctx.user, link.get("target") or "")
        outgoing.append({
            **link,
            "resolved": bool(t),
            "targetId": t.id if t else None,
            "targetTitle": t.title if t else None,
            "targetType": t.md_type if t else None,
        })
    return {
        "id": d.id, "title": d.title,
        "outgoing": outgoing,
        "incoming": await links_mod.incoming_links(session, ctx.user, d),
        "supersededBy": d.superseded_by,
    }


@app.patch("/api/v1/documents/{doc_id}")
async def patch_document(doc_id: int, payload: dict, branch: str = "", ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    from .revisions import document_revision

    async with user_lock(ctx.user.id):
        d = await _get_doc(ctx, session, doc_id)
        # 指定别的分支 = "另存到该分支"：原文档在当前分支保持不动
        if branch:
            try:
                return await service.write_to_branch(session, ctx.user, payload, branch, doc=d)
            except ValueError as e:
                raise HTTPException(400, detail=str(e))
        try:
            d = await service.update_document(session, ctx.user, d, payload, expected_hash=payload.get("expectedHash"), expected_revision=payload.get("expectedRevision"))
        except service.ConflictError as e:
            return JSONResponse(status_code=409, content={"detail": str(e), "contentHash": d.content_hash, "revision": document_revision(d)})
        return _doc_json(d)


@app.delete("/api/v1/documents/{doc_id}")
async def delete_document(doc_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    async with user_lock(ctx.user.id):
        d = await _get_doc(ctx, session, doc_id)
        d = await service.soft_delete_document(session, ctx.user, d)
        return {"ok": True, "deletedAt": d.deleted_at.isoformat()}


@app.post("/api/v1/documents/{doc_id}/restore")
async def restore_document(doc_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    async with user_lock(ctx.user.id):
        d = await _get_doc(ctx, session, doc_id, include_deleted=True)
        if not d.deleted_at:
            return {"ok": True, "restored": False}
        d = await service.restore_document(session, ctx.user, d)
        return {"ok": True, "restored": True}


@app.delete("/api/v1/projects/{project:path}")
async def delete_project(project: str, library: str | None = None, unassigned: bool | None = None,
                         ctx: AuthContext = Depends(auth),
                         session: AsyncSession = Depends(get_session)):
    """删掉一个项目下的所有文档（软删除，进回收站）。

    路径用 {project:path} 而不是普通参数：项目名里可能有斜杠
    （实测库里就有 fsdp-portal-service 这类，将来也可能出现 a/b 形式），
    普通路径段遇到斜杠会 404 而且完全看不出为什么。

    "未归项目"（project 为空串）用 __none__ 这个哨兵表示 —— 空串没法放进
    URL 路径段，`DELETE /api/v1/projects/` 会被当成另一个路由。
    """
    async with user_lock(ctx.user.id):
        # Only the legacy path without an explicit flag reserves __none__.
        proj = "" if unassigned is True or (unassigned is None and project == "__none__") else project
        res = await service.soft_delete_project(session, ctx.user, proj, library)
        if not res["deleted"]:
            raise HTTPException(404, f"项目「{proj or '未归项目'}」下没有可删除的文档")
        return {"ok": True, **res}


@app.delete("/api/v1/projects")
async def delete_projects_query(project: str = "", unassigned: bool = False, library: str | None = None,
                                ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    """Collision-free query API: project names are literal, including __none__."""
    if not unassigned and not project:
        raise HTTPException(400, "需要 project 或 unassigned=true")
    return await delete_project(project=project, library=library, unassigned=unassigned, ctx=ctx, session=session)


async def _trash_snapshot(session: AsyncSession, user_id: int) -> dict:
    # Include deletion generation as well as identity: restore+delete of the same
    # document is a new deletion the user has not confirmed (ABA protection).
    rows = (await session.execute(select(Document.id, Document.deleted_at).where(
        Document.user_id == user_id, Document.deleted_at.is_not(None)
    ).order_by(Document.id))).all()
    members = [(doc_id, stamp.isoformat()) for doc_id, stamp in rows]
    encoded = json.dumps([user_id, members], separators=(',', ':')).encode()
    return {'total': len(members), 'revision': 'trash-v1:' + hashlib.sha256(encoded).hexdigest()}


@app.get("/api/v1/trash/snapshot")
async def trash_snapshot(ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    async with user_lock(ctx.user.id):
        return await _trash_snapshot(session, ctx.user.id)


@app.post("/api/v1/trash/empty")
async def empty_trash(ctx: AuthContext = Depends(auth),
                      session: AsyncSession = Depends(get_session),
                      expected_count: Annotated[int | None, Query(ge=0)] = None,
                      expected_revision: str | None = None):
    """清空回收站：DB 行和 .trash/ 下的 md 都真删，不可在界面上恢复。

    用 POST 而不是 DELETE /api/v1/trash：这不是"删除某个资源"，
    是一个有副作用的批量动作，而且 DELETE 在有些代理/客户端上会被
    当成幂等可重试的请求。
    """
    if not expected_revision:
        raise HTTPException(428, "请先获取回收站快照并确认后再清空")
    async with user_lock(ctx.user.id):
        snapshot = await _trash_snapshot(session, ctx.user.id)
        if snapshot['revision'] != expected_revision or (
                expected_count is not None and snapshot['total'] != expected_count):
            raise HTTPException(409, {"message": "回收站内容已变化，请刷新后重新确认", "actualCount": snapshot['total']})
        res = await service.empty_trash(session, ctx.user)
        return {"ok": True, **res}


@app.get("/api/v1/documents/{doc_id}/history")
async def doc_history(doc_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    d = await _get_doc(ctx, session, doc_id, include_deleted=True)
    from . import gitsvc
    root = user_root(settings.data_dir, ctx.user.id)
    return await run_blocking(gitsvc.history, root, d.rel_path)


# ---------- 检索 ----------
@app.get("/api/v1/search")
async def search(q: str = "", limit: int = 10, mode: str = "hybrid", library: str | None = None, project: str | None = None,
                 ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    if not q.strip():
        # 直接返回 200 空结果会掩盖"参数名拼错"这类调用错误（比如误传 query= 而非 q=），
        # 调用方看到的是"库里没有"，实际是请求本身没带上查询词。
        raise HTTPException(400, detail="缺少查询词：请用 ?q=... 传检索内容")
    # mode=hybrid（默认）走 search_chunks 的三路 RRF 融合。
    #
    # 这里曾经有个严重 bug：hybrid 时先单独调 semantic_search 并直接返回它的原始结果，
    # 只在它为空时才 fallback 到 search_chunks。后果是一旦配上 embedding，
    # 排序就完全由裸余弦相似度决定 —— 绕过了 RRF 融合、长度归一化、importance 加权
    # 和 PER_DOC_CAP，而且 library/project 过滤参数根本没传下去（过滤被静默绕过）。
    # 实测表现：长文相似度 0.671 > 对题短文 0.625，长文排第一，
    # 长度归一化的所有工作全部失效。
    # 那段代码是接三路融合之前的遗留，现已删除。
    if mode == "semantic":
        # 只有显式要求纯语义时才裸用向量路，用于调试对比
        results = await service.semantic_search(
            session, ctx.user, q, limit, library=library, project=project)
        if results is None:
            raise HTTPException(
                503, detail="向量检索不可用（未配置 embedding 或上游超时），可改用 mode=keyword")
        return {"results": results, "mode": "semantic", "query": q}
    if mode == "keyword":
        # 显式跳过向量路，用于对比测量向量带来的增益
        results = await service.search_chunks(
            session, ctx.user, q, limit, library, project, use_vector=False)
        return {"results": results, "mode": "keyword", "query": q}
    results = await service.search_chunks(session, ctx.user, q, limit, library, project)
    return {"results": results, "mode": "hybrid", "query": q}


# ---------- bootstrap ----------
@app.get("/api/v1/bootstrap")
async def bootstrap(project: str | None = None, token_budget: int = 4000,
                    ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    return await service.bootstrap_context(session, ctx.user, project, token_budget)


# ---------- 磁盘同步 / GitHub ----------
@app.post("/api/v1/sync")
@finish_mutation
async def sync(payload: dict | None = None, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    """磁盘→DB 重建索引；payload.pull=true 时先 git pull。"""
    async with user_lock(ctx.user.id):
        action = (payload or {}).get("action") or "reindex"
        root = user_root(settings.data_dir, ctx.user.id)
        pulled = None
        if action == "pull":
            # 走 gitsvc：远端和分支都是按用户算的，裸 git pull 不知道该拉哪个分支
            from . import gitsvc
            pulled = await run_blocking(gitsvc.pull_from_github, root, ctx.user.id)
        stats = await service.sync_from_disk(session, ctx.user)
        return {"pulled": pulled, **stats}


@app.post("/api/v1/sync/push")
async def sync_push(ctx: AuthContext = Depends(auth)):
    async with user_lock(ctx.user.id):
        from . import gitsvc
        root = user_root(settings.data_dir, ctx.user.id)
        return await run_blocking(gitsvc.sync_to_github, root, ctx.user.id)


@app.get("/api/v1/sync/schedule")
async def sync_schedule(ctx: AuthContext = Depends(auth)):
    """读 systemd timer 状态，让 UI 能显示下次推送时间和上次结果。

    只读 systemctl show 的几个字段，不接受任何用户输入 —— 单元名是写死的常量，
    没有注入面。timer 没装时返回 installed=false，UI 给出安装指引。
    """
    unit = "memorys-push.timer"
    try:
        p = await run_blocking(subprocess.run,
            ["systemctl", "show", unit, "--no-pager",
             "--property=LoadState,ActiveState,NextElapseUSecRealtime,LastTriggerUSec"],
            capture_output=True, text=True, timeout=10)
        kv = dict(l.split("=", 1) for l in p.stdout.strip().split("\n") if "=" in l)
        if kv.get("LoadState") != "loaded":
            return {"installed": False,
                    "hint": "定时推送未安装。在服务器执行："
                            "install -m644 /opt/memorys/deploy/memorys-push.{service,timer} "
                            "/etc/systemd/system/ && systemctl daemon-reload && "
                            "systemctl enable --now memorys-push.timer"}
        # 上次运行结果单独查 service（timer 只记触发时间，不记成败）
        s = await run_blocking(subprocess.run,
            ["systemctl", "show", "memorys-push.service", "--no-pager",
             "--property=ExecMainStatus,ExecMainExitTimestamp,Result"],
            capture_output=True, text=True, timeout=10)
        skv = dict(l.split("=", 1) for l in s.stdout.strip().split("\n") if "=" in l)
        return {
            "installed": True,
            "active": kv.get("ActiveState") == "active",
            "next": kv.get("NextElapseUSecRealtime") or "",
            "last_trigger": kv.get("LastTriggerUSec") or "",
            "last_result": skv.get("Result") or "",
            "last_exit": skv.get("ExecMainStatus") or "",
            "last_finished": skv.get("ExecMainExitTimestamp") or "",
            "schedule": "每天 01:00（随机延迟 0-3 分钟）",
        }
    except Exception as e:
        return {"installed": False, "error": str(e)[:200]}


# ---------- git 分支 ----------
# 用途：想大改记忆又不想动主线时，开分支改，满意再合。
# 每个用户只能操作自己的 repo（root 由 user_id 推出，前端传不了别人的路径）。
def _repo(ctx: AuthContext):
    from . import gitsvc
    return gitsvc, gitsvc.ensure_repo(settings.data_dir, ctx.user.id)


@app.get("/api/v1/branches")
async def branches(ctx: AuthContext = Depends(auth)):
    async with user_lock(ctx.user.id):
        gitsvc, root = await run_blocking(_repo, ctx)
        return await run_blocking(gitsvc.list_branches, root)


@app.post("/api/v1/branches")
@finish_mutation
async def branch_create(payload: dict, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    async with user_lock(ctx.user.id):
        gitsvc, root = await run_blocking(_repo, ctx)
        try:
            switch = bool(payload.get("switch", True))
            result = await run_blocking(gitsvc.create_branch, root, payload.get("name", ""), switch=switch)
            if result.get("ok") and switch:
                result["sync"] = await service.sync_from_disk(session, ctx.user)
            return result
        except ValueError as e:
            raise HTTPException(400, detail=str(e))


@app.post("/api/v1/branches/switch")
@finish_mutation
async def branch_switch(payload: dict, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    async with user_lock(ctx.user.id):
        gitsvc, root = await run_blocking(_repo, ctx)
        try:
            result = await run_blocking(gitsvc.switch_branch, root, payload.get("name", ""))
            if result.get("ok"):
                result["sync"] = await service.sync_from_disk(session, ctx.user)
            return result
        except ValueError as e:
            raise HTTPException(400, detail=str(e))


@app.post("/api/v1/branches/merge")
@finish_mutation
async def branch_merge(payload: dict, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    async with user_lock(ctx.user.id):
        gitsvc, root = await run_blocking(_repo, ctx)
        try:
            result = await run_blocking(gitsvc.merge_branch, root, payload.get("name", ""), payload.get("message", ""))
            if result.get("ok"):
                result["sync"] = await service.sync_from_disk(session, ctx.user)
            return result
        except ValueError as e:
            raise HTTPException(400, detail=str(e))


@app.delete("/api/v1/branches/{name:path}")
async def branch_delete(name: str, force: bool = False, ctx: AuthContext = Depends(auth)):
    async with user_lock(ctx.user.id):
        gitsvc, root = await run_blocking(_repo, ctx)
        try:
            return await run_blocking(gitsvc.delete_branch, root, name, force=force)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))


# ---------- 健康检查 ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "memorys"}


def _check_data_directory():
    # No mkdir/write probe: readiness must not change the source-of-truth tree.
    directory = Path(settings.data_dir)
    mode = directory.stat().st_mode
    if (not stat.S_ISDIR(mode) or not mode & 0o444 or not mode & 0o222 or not mode & 0o111
            or not os.access(directory, os.R_OK | os.W_OK | os.X_OK)):
        raise PermissionError("data directory unavailable")


async def _readiness_checks():
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    await run_blocking(_check_data_directory)


@app.get("/api/ready")
async def ready():
    request_id = uuid.uuid4().hex
    try:
        await asyncio.wait_for(_readiness_checks(), timeout=READY_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("Readiness check failed request_id=%s", request_id)
        return JSONResponse(status_code=503, content={"ok": False, "detail": "服务暂不可用", "requestId": request_id},
                            headers={"X-Request-ID": request_id})
    return {"ok": True, "service": "memorys"}


# ---------- MCP ----------
# MCP server 以独立 ASGI 子应用挂载（见 mcp_app.py），鉴权在子应用入口做
from .mcp_app import mcp_asgi_app  # noqa: E402

app.mount("/mcp", mcp_asgi_app)


class McpSlashMiddleware:
    """Starlette 的 Mount("/mcp") 只匹配 /mcp/...，裸 POST /mcp 会被 redirect_slashes
    回 307。MCP 客户端普遍不跟重定向（跟了也会丢 POST body），所以在进路由前
    把 /mcp 改写成 /mcp/，让客户端两种写法都能直连。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            raw = scope.get("raw_path")
            if raw is not None:
                scope["raw_path"] = raw + b"/"
        await self.app(scope, receive, send)


app.add_middleware(McpSlashMiddleware)

# 静态资源（Web UI）
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    f = STATIC_DIR / "index.html"
    if f.exists():
        return FileResponse(f)
    return {"service": "memorys", "docs": "/api/docs"}
