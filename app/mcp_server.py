"""MCP server：把知识库暴露成 agent 可直接调用的工具。

鉴权：请求头 Authorization: Bearer <本地 JWT 或 xiaoai access token> 或 X-Api-Key: hk_xxx。
由 mcp_app.py 的中间件把认证结果放进 contextvar，工具函数从中取当前用户。
"""
from __future__ import annotations

import contextvars
import json

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from . import gitsvc, service
from .config import settings
from .db import SessionLocal
from .mdstore import user_root
from .models import Document

# 当前请求的认证上下文（由 mcp_app 中间件设置）
current_auth: contextvars.ContextVar = contextvars.ContextVar("current_auth", default=None)

mcp = FastMCP("memorys")


class NotAuthenticated(Exception):
    pass


def _user():
    ctx = current_auth.get()
    if ctx is None or getattr(ctx, "user", None) is None:
        raise NotAuthenticated("未认证：请在 MCP 客户端配置 Authorization: Bearer <token> 或 X-Api-Key")
    return ctx.user


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


@mcp.tool()
async def memory_search(query: str, limit: int = 8, library: str = "", project: str = "") -> str:
    """在当前用户的知识库中检索记忆。中文友好（jieba 分词 + BM25 + 模糊兜底 + 可选向量混合）。

    返回命中的 chunk 列表，含所属文档 id/title/type，需要全文时再调 memory_get。
    """
    user = _user()
    async with SessionLocal() as s:
        hits = await service.semantic_search(s, user, query, limit)
        mode = "semantic"
        if not hits:
            hits = await service.search_chunks(s, user, query, limit, library or None, project or None)
            mode = "keyword"
    if not hits:
        return f"没有命中「{query}」。可以先用 memory_list_docs 看看库里有什么。"
    lines = [f"检索模式: {mode}，命中 {len(hits)} 条\n"]
    for h in hits:
        d = h["doc"]
        lines.append(
            f"[score={h['score']}] doc={d['id']} type={d['type']} "
            f"project={d['project'] or '-'} tags={','.join(d['tags']) or '-'}\n"
            f"标题: {d['title']}  章节: {h['heading'] or '-'}\n{h['content'][:600]}\n"
        )
    return "\n".join(lines)


@mcp.tool()
async def memory_get(doc_id: int) -> str:
    """按 id 读取一篇记忆的完整内容（含元数据）。"""
    user = _user()
    async with SessionLocal() as s:
        d = (await s.execute(
            select(Document).where(Document.id == doc_id, Document.user_id == user.id,
                                   Document.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not d:
            return f"文档 {doc_id} 不存在或无权访问。"
        return _j({
            "id": d.id, "title": d.title, "type": d.md_type, "library": d.library,
            "project": d.project, "tags": d.tags, "importance": d.importance,
            "source": d.source, "content": d.content,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            "content_hash": d.content_hash,
        })


@mcp.tool()
async def memory_write(
    title: str,
    content: str,
    type: str = "fact",
    project: str = "",
    tags: list[str] | None = None,
    importance: int = 3,
    library: str = "main",
    source: str = "agent",
    mode: str = "create",
) -> str:
    """写入一条记忆（md 格式落盘 + git 提交 + 建索引）。

    type: project_summary | decision | preference | howto | glossary | fact
    importance: 1-5，影响 memory_bootstrap 的优先级。
    mode: create（默认，撞同名标题会报冲突并给出已存在的 id）| upsert（同名则覆盖内容）。
    """
    user = _user()
    payload = {
        "title": title, "content": content, "type": type, "project": project,
        "tags": tags or [], "importance": importance, "library": library, "source": source,
    }
    async with SessionLocal() as s:
        try:
            doc = await service.create_document(s, user, payload)
        except service.DuplicateError as e:
            if mode != "upsert":
                return (f"⚠️ 已存在同标题记忆 doc={e.doc.id}「{e.doc.title}」。\n"
                        f"要更新就调 memory_update(doc_id={e.doc.id}, ...)，"
                        f"或用 mode='upsert' 重新调用本工具直接覆盖。")
            doc = await service.update_document(s, user, e.doc, payload)
            return f"已覆盖 doc={doc.id}「{doc.title}」（{doc.library}/{doc.slug}.md）。"
        except ValueError as e:
            return f"写入失败：{e}"
        return f"已写入 doc={doc.id}「{doc.title}」（{doc.library}/{doc.slug}.md），已 git 提交。"


@mcp.tool()
async def memory_update(
    doc_id: int,
    title: str = "",
    content: str = "",
    type: str = "",
    project: str = "",
    tags: list[str] | None = None,
    importance: int = 0,
    mode: str = "replace",
) -> str:
    """更新一条已有记忆。只传需要改的字段。

    mode: replace（默认，content 整体替换）| append（content 追加到正文末尾）。
    """
    user = _user()
    async with SessionLocal() as s:
        d = (await s.execute(
            select(Document).where(Document.id == doc_id, Document.user_id == user.id,
                                   Document.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not d:
            return f"文档 {doc_id} 不存在或无权访问。"
        payload: dict = {}
        if title:
            payload["title"] = title
        if content:
            payload["content"] = (d.content.rstrip() + "\n\n" + content) if mode == "append" else content
        if type:
            payload["type"] = type
        if project:
            payload["project"] = project
        if tags is not None:
            payload["tags"] = tags
        if importance:
            payload["importance"] = importance
        if not payload:
            return "没有要更新的字段。"
        d = await service.update_document(s, user, d, payload)
        return f"已更新 doc={d.id}「{d.title}」，已 git 提交。"


@mcp.tool()
async def memory_delete(doc_id: int) -> str:
    """软删除一条记忆（进回收站，md 文件移入 .trash/，可在 Web UI 或 memory_restore 恢复）。"""
    user = _user()
    async with SessionLocal() as s:
        d = (await s.execute(
            select(Document).where(Document.id == doc_id, Document.user_id == user.id,
                                   Document.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not d:
            return f"文档 {doc_id} 不存在或已删除。"
        title = d.title
        await service.soft_delete_document(s, user, d)
        return f"已删除 doc={doc_id}「{title}」（软删除，可恢复）。"


@mcp.tool()
async def memory_restore(doc_id: int) -> str:
    """从回收站恢复一条记忆。"""
    user = _user()
    async with SessionLocal() as s:
        d = (await s.execute(
            select(Document).where(Document.id == doc_id, Document.user_id == user.id)
        )).scalar_one_or_none()
        if not d:
            return f"文档 {doc_id} 不存在。"
        if not d.deleted_at:
            return f"doc={doc_id} 未被删除，无需恢复。"
        d = await service.restore_document(s, user, d)
        return f"已恢复 doc={d.id}「{d.title}」。"


@mcp.tool()
async def memory_bootstrap(project: str = "", token_budget: int = 4000) -> str:
    """换模型/换 agent 后的开场引导：一次性取回某项目的压缩上下文包。

    按「项目总结 → 决策/偏好 → howto → 术语 → 事实」优先级和 importance 排序，
    在 token_budget 内截断。新会话开场调它就能快速恢复工作上下文。
    """
    user = _user()
    async with SessionLocal() as s:
        pack = await service.bootstrap_context(s, user, project or None, token_budget)
    if not pack["documents"]:
        return f"项目「{pack['project']}」暂无记忆。先用 memory_write 写入一些项目总结/决策。"
    lines = [
        f"# 上下文引导包：{pack['project']}",
        f"（{pack['document_count']}/{pack['total_documents']} 篇，约 {pack['estimated_tokens']} tokens）\n",
    ]
    for e in pack["documents"]:
        lines.append(f"## [{e['type']}] {e['title']}"
                     + (f" · 项目: {e['project']}" if e["project"] else "")
                     + (f" · 标签: {','.join(e['tags'])}" if e["tags"] else "")
                     + f" · 重要度 {e['importance']}")
        lines.append(e["content"] + ("\n…（已截断，用 memory_get 看全文）" if e["truncated"] else ""))
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def memory_list_libraries() -> str:
    """列出当前用户的所有知识库（library）及各自文档数。"""
    user = _user()
    from sqlalchemy import func as sfunc
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Document.library, sfunc.count(Document.id))
            .where(Document.user_id == user.id, Document.deleted_at.is_(None))
            .group_by(Document.library).order_by(Document.library)
        )).all()
    if not rows:
        return "还没有任何记忆。用 memory_write 写第一条。"
    return "\n".join(f"- {lib}: {cnt} 篇" for lib, cnt in rows)


@mcp.tool()
async def memory_list_docs(library: str = "", project: str = "", type: str = "", limit: int = 30) -> str:
    """按条件列出记忆标题（不含正文），用于快速浏览库里有什么。"""
    user = _user()
    conds = [Document.user_id == user.id, Document.deleted_at.is_(None)]
    if library:
        conds.append(Document.library == library)
    if project:
        conds.append(Document.project == project)
    if type:
        conds.append(Document.md_type == type)
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Document).where(*conds).order_by(Document.updated_at.desc()).limit(min(limit, 100))
        )).scalars().all()
    if not rows:
        return "没有符合条件的记忆。"
    lines = [f"共 {len(rows)} 篇："]
    for d in rows:
        lines.append(f"- doc={d.id} [{d.md_type}] {d.title}"
                     + (f" · 项目 {d.project}" if d.project else "")
                     + f" · 重要度 {d.importance}"
                     + (f" · {d.updated_at.strftime('%Y-%m-%d')}" if d.updated_at else ""))
    return "\n".join(lines)


@mcp.tool()
async def memory_history(doc_id: int, limit: int = 15) -> str:
    """查看一篇记忆的 git 变更历史（谁改了、什么时候、提交信息）。"""
    user = _user()
    async with SessionLocal() as s:
        d = (await s.execute(
            select(Document).where(Document.id == doc_id, Document.user_id == user.id)
        )).scalar_one_or_none()
        if not d:
            return f"文档 {doc_id} 不存在。"
    root = user_root(settings.data_dir, user.id)
    hist = gitsvc.history(root, d.rel_path, limit)
    if not hist:
        return f"doc={doc_id} 暂无 git 历史。"
    return "\n".join(f"- {h['date']} {h['hash'][:8]} {h['message']}" for h in hist)
