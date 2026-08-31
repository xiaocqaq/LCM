"""FastAPI 主应用：REST API + MCP（streamable HTTP）+ Web UI。"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .mcp_server import mcp
from .config import settings
from .db import SessionLocal, get_session, engine
from .mdstore import user_root
from .models import ApiKey, Base, Document, User
from .security import AuthContext, authenticate_headers, hash_key, issue_local_jwt, new_api_key

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建表（幂等）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 索引与列调整（幂等）
        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin(tsv)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_trgm ON chunks USING gin(content gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_documents_user_project ON documents(user_id, project) WHERE deleted_at IS NULL",
            "ALTER TABLE chunks ALTER COLUMN embedding SET STORAGE EXTERNAL",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass
        try:
            await conn.execute(text("SELECT 1 FROM chunks LIMIT 1"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING hnsw((embedding::vector(1536)) vector_cosine_ops)"))
        except Exception:
            pass
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
    out = {"id": d.id, "title": d.title, "type": d.md_type, "library": d.library, "project": d.project,
           "tags": d.tags, "importance": d.importance, "source": d.source,
           "createdAt": d.created_at.isoformat() if d.created_at else None,
           "updatedAt": d.updated_at.isoformat() if d.updated_at else None,
           "contentHash": d.content_hash}
    if with_content:
        out["content"] = d.content
    return out


async def _get_doc(ctx: AuthContext, session: AsyncSession, doc_id: int, include_deleted=False) -> Document:
    d = (await session.execute(select(Document).where(Document.id == doc_id, Document.user_id == ctx.user.id))).scalar_one_or_none()
    if not d or (d.deleted_at and not include_deleted):
        raise HTTPException(404, "文档不存在")
    return d


@app.get("/api/v1/documents")
async def list_documents(ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session),
                         library: str | None = None, project: str | None = None, type: str | None = None,
                         q: str | None = None, limit: int = 50, offset: int = 0, trash: bool = False):
    conds = [Document.user_id == ctx.user.id]
    if trash:
        conds.append(Document.deleted_at.is_not(None))
    else:
        conds.append(Document.deleted_at.is_(None))
    if library:
        conds.append(Document.library == library)
    if project:
        conds.append(Document.project == project)
    if type:
        conds.append(Document.md_type == type)
    if q:
        conds.append(Document.title.ilike(f"%{q}%"))
    rows = (await session.execute(
        select(Document).where(*conds).order_by(Document.updated_at.desc()).limit(min(limit, 200)).offset(offset)
    )).scalars().all()
    total = len(rows)
    return {"total": total, "items": [_doc_json(d, with_content=False) for d in rows]}


@app.post("/api/v1/documents")
async def create_document(payload: dict, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    try:
        doc = await service.create_document(session, ctx.user, payload)
    except service.DuplicateError as e:
        return JSONResponse(status_code=409, content={"detail": "已存在同标题文档", "documentId": e.doc.id, "title": e.doc.title})
    return _doc_json(doc)


@app.get("/api/v1/documents/{doc_id}")
async def get_document(doc_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    d = await _get_doc(ctx, session, doc_id)
    return _doc_json(d)


@app.patch("/api/v1/documents/{doc_id}")
async def patch_document(doc_id: int, payload: dict, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    d = await _get_doc(ctx, session, doc_id)
    try:
        d = await service.update_document(session, ctx.user, d, payload, expected_hash=payload.get("expectedHash"))
    except service.ConflictError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "contentHash": d.content_hash})
    return _doc_json(d)


@app.delete("/api/v1/documents/{doc_id}")
async def delete_document(doc_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    d = await _get_doc(ctx, session, doc_id)
    d = await service.soft_delete_document(session, ctx.user, d)
    return {"ok": True, "deletedAt": d.deleted_at.isoformat()}


@app.post("/api/v1/documents/{doc_id}/restore")
async def restore_document(doc_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    d = await _get_doc(ctx, session, doc_id, include_deleted=True)
    if not d.deleted_at:
        return {"ok": True, "restored": False}
    d = await service.restore_document(session, ctx.user, d)
    return {"ok": True, "restored": True}


@app.get("/api/v1/documents/{doc_id}/history")
async def doc_history(doc_id: int, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    d = await _get_doc(ctx, session, doc_id, include_deleted=True)
    from . import gitsvc
    root = user_root(settings.data_dir, ctx.user.id)
    return gitsvc.history(root, d.rel_path)


# ---------- 检索 ----------
@app.get("/api/v1/search")
async def search(q: str = "", limit: int = 10, mode: str = "hybrid", library: str | None = None, project: str | None = None,
                 ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    if not q.strip():
        return {"results": [], "mode": mode, "query": q}
    results = None
    if mode in ("hybrid", "semantic"):
        results = await service.semantic_search(session, ctx.user, q, limit)
    if not results:
        results = await service.search_chunks(session, ctx.user, q, limit, library, project)
        mode_used = "keyword"
    else:
        mode_used = "semantic"
    return {"results": results, "mode": mode_used, "query": q}


# ---------- bootstrap ----------
@app.get("/api/v1/bootstrap")
async def bootstrap(project: str | None = None, token_budget: int = 4000,
                    ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    return await service.bootstrap_context(session, ctx.user, project, token_budget)


# ---------- 磁盘同步 / GitHub ----------
@app.post("/api/v1/sync")
async def sync(payload: dict | None = None, ctx: AuthContext = Depends(auth), session: AsyncSession = Depends(get_session)):
    """磁盘→DB 重建索引；payload.pull=true 时先 git pull。"""
    action = (payload or {}).get("action") or "reindex"
    root = user_root(settings.data_dir, ctx.user.id)
    pulled = None
    if action == "pull":
        import subprocess
        p = subprocess.run(["git", "-C", str(root), "pull", "--ff-only"], capture_output=True, text=True, timeout=60)
        pulled = {"ok": p.returncode == 0, "output": (p.stdout + p.stderr).strip()[:300]}
    stats = await service.sync_from_disk(session, ctx.user)
    return {"pulled": pulled, **stats}


@app.post("/api/v1/sync/push")
async def sync_push(ctx: AuthContext = Depends(auth)):
    from . import gitsvc
    root = user_root(settings.data_dir, ctx.user.id)
    return gitsvc.sync_to_github(root)


# ---------- 健康检查 ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "memorys"}


# ---------- MCP ----------
# MCP server 以独立 ASGI 子应用挂载（见 mcp_app.py），鉴权在子应用入口做
from .mcp_app import mcp_asgi_app  # noqa: E402

app.mount("/mcp", mcp_asgi_app)

# 静态资源（Web UI）
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    f = STATIC_DIR / "index.html"
    if f.exists():
        return FileResponse(f)
    return {"service": "memorys", "docs": "/api/docs"}
