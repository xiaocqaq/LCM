"""核心业务：文档 CRUD（md 为准、DB 索引）、检索（关键词+trgm+RRF 混合）、bootstrap。"""
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import gitsvc
from .config import settings
from .mdstore import (
    VALID_TYPES,
    compute_hash,
    doc_path,
    parse_document,
    render_document,
    sanitize_slug,
    split_chunks,
    user_root,
)
from .models import Chunk, Document, User
from .search import embed_texts, expand_tokens, to_tsquery


def _now():
    return datetime.now(timezone.utc)


def _tsvector_literal(content: str) -> str:
    """索引侧词串：走 expand_tokens（含路径段 + 概念同义词），空格分隔喂给 to_tsvector。

    注意用空格而不是 '|'：这里的产物是 to_tsvector 的输入（普通文本），
    不是 tsquery。用 '|' 会让管道符本身变成 lexeme。
    """
    words = expand_tokens(f"{content}")
    if not words:
        return ""
    return " ".join(w.replace("'", "") for w in words)


async def upsert_user_from_xiaoai(session: AsyncSession, u: dict) -> User:
    """按 xiaoai_user_id 同步/更新本地用户。"""
    xu = int(u["id"])
    user = (await session.execute(select(User).where(User.xiaoai_user_id == xu))).scalar_one_or_none()
    if not user:
        user = User(
            xiaoai_user_id=xu,
            username=str(u.get("username") or f"u{xu}"),
            display_name=str(u.get("displayName") or ""),
            email=str(u.get("email") or ""),
            role=str(u.get("role") or "user"),
        )
        session.add(user)
        await session.flush()
    else:
        user.username = str(u.get("username") or user.username)
        user.display_name = str(u.get("displayName") or user.display_name)
        user.email = str(u.get("email") or user.email)
        user.role = str(u.get("role") or user.role)
    user.last_login_at = _now()
    await session.commit()
    gitsvc.ensure_repo(settings.data_dir, user.id)
    return user


async def create_document(session: AsyncSession, user: User, payload: dict) -> Document:
    title = (payload.get("title") or "untitled").strip()[:256]
    library = sanitize_slug(payload.get("library") or "main")
    md_type = payload.get("type") if payload.get("type") in VALID_TYPES else "fact"
    project = (payload.get("project") or "").strip()[:128]
    tags = [str(t)[:32] for t in (payload.get("tags") or [])][:16]
    importance = max(1, min(5, int(payload.get("importance") or 3)))
    source = (payload.get("source") or "web")[:64]
    content = (payload.get("content") or "").strip()
    if not content:
        raise ValueError("content 不能为空")

    # 查重：同库 title 相同 → 返回已存在
    dup = (
        await session.execute(
            select(Document).where(
                Document.user_id == user.id,
                Document.library == library,
                Document.title == title,
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise DuplicateError(dup)

    # slug 去重：DB 已占用的 rel_path（含软删除行，唯一索引不区分）+ 磁盘现存文件都要避开
    taken = set(
        (
            await session.execute(
                select(Document.rel_path).where(
                    Document.user_id == user.id, Document.library == library
                )
            )
        ).scalars()
    )
    base = sanitize_slug(title)
    slug = base
    i = 1
    while (
        f"{library}/{slug}.md" in taken
        or doc_path(settings.data_dir, user.id, library, slug).exists()
    ):
        i += 1
        slug = f"{base}-{i}"

    doc = Document(
        user_id=user.id,
        library=library,
        slug=slug,
        title=title,
        md_type=md_type,
        project=project,
        tags=tags,
        importance=importance,
        source=source,
        content=content,
        rel_path=f"{library}/{slug}.md",
        content_hash=compute_hash(content),
    )
    session.add(doc)
    await session.flush()

    meta = {
        "id": f"mem_{uuid.uuid4().hex[:12]}",
        "title": title,
        "type": md_type,
        "project": project,
        "tags": tags,
        "importance": importance,
        "source": source,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    p = doc_path(settings.data_dir, user.id, library, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_document(meta, content), encoding="utf-8")
    await _reindex_document(session, doc)
    await session.commit()
    gitsvc.try_commit_all(user_root(settings.data_dir, user.id), f"create: {title}")
    return doc


async def update_document(session: AsyncSession, user: User, doc: Document, payload: dict, expected_hash: str | None = None) -> Document:
    if expected_hash and doc.content_hash and expected_hash != doc.content_hash:
        raise ConflictError("文档已被他人修改，请刷新后重试")
    root = user_root(settings.data_dir, user.id)

    if "title" in payload and payload["title"]:
        new_title = str(payload["title"]).strip()[:256]
        if new_title != doc.title:
            old_path = root / doc.rel_path
            doc.title = new_title
            new_slug = sanitize_slug(new_title)
            if new_slug != doc.slug:
                # slug 冲突检查
                np = doc_path(settings.data_dir, user.id, doc.library, new_slug)
                if np.exists() and np != old_path:
                    i = 1
                    while np.exists():
                        i += 1
                        new_slug = f"{sanitize_slug(new_title)}-{i}"
                        np = doc_path(settings.data_dir, user.id, doc.library, new_slug)
                doc.slug = new_slug
                doc.rel_path = f"{doc.library}/{new_slug}.md"
                if old_path.exists():
                    old_path.unlink()
    for k, col in [("type", "md_type"), ("project", "project"), ("importance", "importance"), ("source", "source"), ("tags", "tags"), ("library", "library")]:
        if k in payload and payload[k] is not None:
            if k == "type":
                if payload[k] in VALID_TYPES:
                    doc.md_type = payload[k]
            elif k == "importance":
                doc.importance = max(1, min(5, int(payload[k])))
            else:
                setattr(doc, col, payload[k])
    if "content" in payload and payload["content"] is not None:
        doc.content = str(payload["content"]).strip()
    doc.content_hash = compute_hash(doc.content)
    doc.updated_at = _now()
    await session.flush()

    # 重写 md（frontmatter 用当前 DB 元数据）
    meta = {
        "id": f"doc_{doc.id}",
        "title": doc.title,
        "type": doc.md_type,
        "project": doc.project,
        "tags": doc.tags,
        "importance": doc.importance,
        "source": doc.source,
        "created_at": doc.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if doc.created_at else "",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    p = root / doc.rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_document(meta, doc.content), encoding="utf-8")
    await _reindex_document(session, doc)
    await session.commit()
    gitsvc.try_commit_all(root, f"update: {doc.title}")
    return doc


async def write_to_branch(session: AsyncSession, user: User, payload: dict,
                          branch: str, doc: Document | None = None) -> dict:
    """把记忆写到**非当前分支**，不落盘、不进 DB。

    为什么不落盘也不进 DB：PG 索引和磁盘 md 都是"当前分支"的平铺视图。
    往别的分支写内容如果同时落到磁盘，工作区就变成了两个分支的混合体；
    如果同时进 DB，检索会返回当前分支上根本不存在的文档。
    所以这里只往 git 对象库提交，等用户切到那个分支（或合并过来）时，
    reindex 自然会把它纳入索引。

    doc 非空时是"另存到分支"：以该文档的路径和元数据为基础，套用 payload 的改动。
    """
    root = gitsvc.ensure_repo(settings.data_dir, user.id)
    cur = gitsvc.current_branch(root)
    if branch == cur:
        raise ValueError("目标分支就是当前分支，直接正常保存即可")

    title = (payload.get("title") or (doc.title if doc else "") or "untitled").strip()[:256]
    content = (payload.get("content") or (doc.content if doc else "") or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    library = sanitize_slug(payload.get("library") or (doc.library if doc else "main"))
    md_type = payload.get("type") if payload.get("type") in VALID_TYPES else (doc.md_type if doc else "fact")
    project = (payload.get("project") or (doc.project if doc else "") or "").strip()[:128]
    tags = [str(t)[:32] for t in (payload.get("tags") or (doc.tags if doc else []) or [])][:16]
    importance = max(1, min(5, int(payload.get("importance") or (doc.importance if doc else 3))))
    source = (payload.get("source") or "web")[:64]

    # 改标题时路径跟着变，否则沿用原路径（等于在该分支上更新同一篇）
    slug = sanitize_slug(title)
    rel_path = f"{library}/{slug}.md"

    # 目标分支上已有同路径文件 → 这次是更新，保留它原本的 created_at
    existing = gitsvc.read_file_from_branch(root, branch, rel_path)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if existing:
        try:
            old_meta, _ = parse_document(existing)
            created_at = old_meta.get("created_at") or created_at
        except Exception:
            pass

    meta = {
        "id": f"mem_{uuid.uuid4().hex[:12]}",
        "title": title,
        "type": md_type,
        "project": project,
        "tags": tags,
        "importance": importance,
        "source": source,
        "created_at": created_at,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    verb = "update" if existing else "create"
    res = gitsvc.commit_file_to_branch(
        root, branch, rel_path, render_document(meta, content),
        f"{verb}: {title} [分支 {branch}]")
    res.update({
        "title": title, "rel_path": rel_path, "action": verb,
        "current_branch": cur,
        "note": (f"已提交到分支 {branch}，当前分支 {cur} 不受影响。"
                 f"切到 {branch} 或把它合并过来之后，内容才会进入检索。"),
    })
    return res


async def soft_delete_document(session: AsyncSession, user: User, doc: Document) -> Document:
    doc.deleted_at = _now()
    await session.flush()
    root = user_root(settings.data_dir, user.id)
    p = root / doc.rel_path
    if p.exists():
        trash = root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        p.rename(trash / f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{p.name}")
    await session.commit()
    gitsvc.try_commit_all(root, f"delete: {doc.title}")
    return doc


async def restore_document(session: AsyncSession, user: User, doc: Document) -> Document:
    doc.deleted_at = None
    await session.flush()
    root = user_root(settings.data_dir, user.id)
    p = root / doc.rel_path
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_document(_doc_meta(doc), doc.content), encoding="utf-8")
    await _reindex_document(session, doc)
    await session.commit()
    gitsvc.try_commit_all(root, f"restore: {doc.title}")
    return doc


def _doc_meta(doc: Document) -> dict:
    return {
        "id": f"doc_{doc.id}",
        "title": doc.title,
        "type": doc.md_type,
        "project": doc.project,
        "tags": doc.tags,
        "importance": doc.importance,
        "source": doc.source,
        "created_at": doc.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if doc.created_at else "",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def _reindex_document(session: AsyncSession, doc: Document, do_embed: bool = True) -> None:
    """删旧 chunk，重切、重分词、可选 embedding，重插。"""
    await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    pieces = split_chunks(doc.content)
    if not pieces:
        # 空内容也保留一个 chunk，防止文档整体消失
        pieces = [{"heading": "", "content": doc.content}]
    embeds = None
    if do_embed:
        embeds = await embed_texts([f"{doc.title}\n{p['heading']}\n{p['content']}" for p in pieces])
    for seq, p in enumerate(pieces):
        lex = _tsvector_literal(f"{doc.title} {doc.project} {' '.join(doc.tags or [])} {p['heading']} {p['content']}")
        ch = Chunk(document_id=doc.id, seq=seq, heading=p["heading"], content=p["content"])
        session.add(ch)
        await session.flush()
        await session.execute(
            text("UPDATE chunks SET tsv = to_tsvector('simple', :lex) WHERE id = :id"),
            {"lex": lex, "id": ch.id},
        )
        if embeds:
            vec = embeds[seq]
            await session.execute(
                text("UPDATE chunks SET embedding = CAST(:vec AS vector) WHERE id = :id"),
                {"vec": "[" + ",".join(f"{x:.6f}" for x in vec) + "]", "id": ch.id},
            )


async def search_chunks(session: AsyncSession, user: User, q: str, limit: int = 10, library: str | None = None, project: str | None = None) -> list[dict]:
    """关键词检索（tsvector OR + ts_rank）+ trgm 相似度兜底，RRF 融合。"""
    tsq = to_tsquery(q)
    kw_rows: list[tuple] = []
    trgm_rows: list[tuple] = []
    conds = [Document.user_id == user.id, Document.deleted_at.is_(None)]
    if library:
        conds.append(Document.library == sanitize_slug(library))
    if project:
        conds.append(Document.project == project)

    if tsq:
        sql = (
            select(Chunk.id, Chunk.document_id, Chunk.seq, Chunk.heading, Chunk.content,
                   func.ts_rank(Chunk.tsv, text(f"to_tsquery('simple', :tsq)")).label("rank"))
            .join(Document, Document.id == Chunk.document_id)
            .where(*conds, Chunk.tsv.op("@@")(text(f"to_tsquery('simple', :tsq)")))
            .order_by(desc("rank")).limit(limit * 3)
        )
        kw_rows = (await session.execute(sql, {"tsq": tsq})).all()
    # trgm 兜底（错别字/部分词）
    trgm_sql = (
        select(Chunk.id, Chunk.document_id, Chunk.seq, Chunk.heading, Chunk.content,
               func.similarity(Chunk.content, text(":q")).label("rank"))
        .join(Document, Document.id == Chunk.document_id)
        .where(*conds, text("similarity(chunks.content, :q) > 0.2"))
        .order_by(desc("rank")).limit(limit * 3)
    )
    trgm_rows = (await session.execute(trgm_sql, {"q": q[:2000]})).all()

    # RRF 融合
    rrf: dict[int, dict] = {}
    doc_ids = set()
    for rows in (kw_rows, trgm_rows):
        for rank, r in enumerate(rows):
            e = rrf.setdefault(r[0], {"row": r, "score": 0.0})
            e["score"] += 1.0 / (60 + rank + 1)
            doc_ids.add(r[1])

    # ---- 长度归一化 + importance 加权 ----
    # 为什么需要：RRF 只看排名，不看文档体量。实测 17 个查询，一篇 15303 字符的长文
    # 出现在 13 个结果里、7 次排第一 —— 它长度是其他文档的 6-230 倍，词汇覆盖面天然大，
    # 靠"碰巧含有查询词"就挤掉了真正对题的短文（194 字符的《TTS 选型决策》那种）。
    # BM25 本来用 b 参数干这件事，但 ts_rank 没有，得自己补。
    if rrf:
        lens = dict((await session.execute(
            select(Document.id, func.length(Document.content))
            .where(Document.id.in_(list(doc_ids)))
        )).all())
        avg_len = (sum(lens.values()) / len(lens)) if lens else 1.0
        imps = dict((await session.execute(
            select(Document.id, Document.importance)
            .where(Document.id.in_(list(doc_ids)))
        )).all())
        for e in rrf.values():
            did = e["row"][1]
            dl = max(1, lens.get(did, 1))
            # 温和衰减：ratio 3 倍 → ×0.79，10 倍 → ×0.62。
            # 指数刻意压到 0.35，不是要把长文赶出结果（长文往往信息也多），
            # 只是抵掉它靠体量刷来的优势。
            e["score"] *= (avg_len / dl) ** 0.35 if dl > avg_len else 1.0
            # importance 你在认真填（1-5），但检索一直没用它。
            # 同分时高重要度该靠前，权重压在 ±10% 免得盖过相关性本身。
            e["score"] *= 1.0 + (imps.get(did, 3) - 3) * 0.05
    # 每篇文档最多占 PER_DOC_CAP 条：否则一篇长文档的多个 chunk 会吃满整个结果集，
    # agent 拿去恢复上下文时等于白烧 token。凑不满 limit 时再放宽补齐。
    PER_DOC_CAP = 2
    ranked = sorted(rrf.values(), key=lambda e: -e["score"])
    merged, spill, per_doc = [], [], {}
    for e in ranked:
        did = e["row"][1]
        if per_doc.get(did, 0) < PER_DOC_CAP:
            per_doc[did] = per_doc.get(did, 0) + 1
            merged.append(e)
        else:
            spill.append(e)
        if len(merged) >= limit:
            break
    if len(merged) < limit:
        merged.extend(spill[: limit - len(merged)])
    if not merged:
        return []
    docs = {d.id: d for d in (await session.execute(select(Document).where(Document.id.in_(list(doc_ids))))).scalars()}
    out = []
    for e in merged:
        r = e["row"]
        d = docs.get(r[1])
        if not d:
            continue
        out.append({
            "chunk_id": r[0], "document_id": r[1], "seq": r[2], "heading": r[3],
            "content": r[4], "score": round(e["score"], 6),
            "doc": {"id": d.id, "title": d.title, "type": d.md_type, "project": d.project,
                    "tags": d.tags, "importance": d.importance, "library": d.library,
                    "updated_at": d.updated_at.isoformat() if d.updated_at else None},
        })
    return out


async def semantic_search(session: AsyncSession, user: User, q: str, limit: int = 10) -> list[dict] | None:
    """embedding 向量检索（配置了才可用）。返回 None 表示不可用。"""
    from .search import EMBED_AVAILABLE
    if not EMBED_AVAILABLE:
        return None
    qvec = await embed_texts([q])
    if not qvec:
        return None
    vec_str = "[" + ",".join(f"{x:.6f}" for x in qvec[0]) + "]"
    sql = text(
        """
        SELECT c.id, c.document_id, c.seq, c.heading, c.content,
               1 - (c.embedding <=> CAST(:vec AS vector)) AS score
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.user_id = :uid AND d.deleted_at IS NULL
        ORDER BY c.embedding <=> CAST(:vec AS vector)
        LIMIT :lim
        """
    )
    rows = (await session.execute(sql, {"vec": vec_str, "uid": user.id, "lim": limit})).all()
    doc_ids = {r[1] for r in rows}
    docs = {d.id: d for d in (await session.execute(select(Document).where(Document.id.in_(list(doc_ids))))).scalars()}
    out = []
    for r in rows:
        d = docs.get(r[1])
        if not d:
            continue
        out.append({
            "chunk_id": r[0], "document_id": r[1], "seq": r[2], "heading": r[3],
            "content": r[4], "score": round(float(r[5]), 6),
            "doc": {"id": d.id, "title": d.title, "type": d.md_type, "project": d.project,
                    "tags": d.tags, "importance": d.importance, "library": d.library,
                    "updated_at": d.updated_at.isoformat() if d.updated_at else None},
        })
    return out


TYPE_PRIORITY = {"project_summary": 0, "decision": 1, "preference": 1, "howto": 2, "glossary": 3, "fact": 4}


async def bootstrap_context(session: AsyncSession, user: User, project: str | None, token_budget: int = 4000) -> dict:
    """项目压缩上下文包：总结+决策+偏好+术语，按 importance/updated_at 排序，按 token 预算截断。"""
    conds = [Document.user_id == user.id, Document.deleted_at.is_(None)]
    if project:
        conds.append(Document.project == project)
    else:
        conds.append(Document.library == "main")
    docs = (await session.execute(select(Document).where(*conds))).scalars().all()
    # 排序：类型优先级 → importance → updated_at
    docs.sort(key=lambda d: (TYPE_PRIORITY.get(d.md_type, 9), -d.importance, -(d.updated_at.timestamp() if d.updated_at else 0)))

    # 预算下限：低于这个数拼不出有意义的上下文，还不如报错让调用方改
    token_budget = max(200, int(token_budget))
    META_COST = 30          # 标题/类型/标签那几行的开销
    MIN_SLICE = 120         # 截断后至少留这么多 token，否则这篇没有信息量，不如不收

    def est_tokens(s: str) -> int:
        # 中文 ~1 token/字，英文 ~0.75 token/词：粗略 1.7 字/token 或 len*0.7
        return max(1, int(len(s) * 0.85))

    def chars_for(tok: int) -> int:
        # est_tokens 的反函数，用来按剩余预算切内容
        return max(1, int(tok / 0.85))

    PER_DOC_CHARS = 4000    # 单篇上限，防一篇超长文吃掉整个预算

    used = 0
    picked: list[dict] = []
    for d in docs:
        left = token_budget - used
        if left <= META_COST + MIN_SLICE:
            break               # 剩余空间连一段有意义的摘录都放不下，收工
        body = d.content or ""
        head = body[:PER_DOC_CHARS]
        if est_tokens(head) + META_COST <= left:
            # 放得下（可能仍因 PER_DOC_CHARS 上限而截断，那跟预算无关，继续收下一篇）
            content = head
            budget_capped = False
        else:
            # 放不下就按剩余预算切。
            # 原来这里是 `continue`（跳过这篇去看下一篇），导致两个问题：
            #   1) 排序失效 —— 收进来的是"能塞进缝隙的小文档"而不是"最重要的前 N 篇"
            #   2) 配合 `and picked` 的短路，首篇永远无条件全量收录，
            #      预算填 100 也能返回 3430 tokens
            content = body[:chars_for(left - META_COST)]
            budget_capped = True
        picked.append({
            "id": d.id, "title": d.title, "type": d.md_type, "project": d.project,
            "tags": d.tags, "importance": d.importance,
            "content": content, "truncated": len(content) < len(body),
        })
        used += est_tokens(content) + META_COST
        # 只有"被预算卡住"才停。因 PER_DOC_CHARS 截断不算 —— 那时预算还有富余，
        # 后面的文档照样能收（这里判断错会让大预算也只返回 1 篇）。
        if budget_capped:
            break
    # digest 是给 agent 直接塞进开场的，也得守预算 —— 它不能比 documents 还长
    digest_parts: list[str] = []
    dused = 0
    for e in picked:
        part = f"[{e['type']}] {e['title']}: {e['content'][:200]}"
        c = est_tokens(part)
        if dused + c > token_budget and digest_parts:
            break
        digest_parts.append(part)
        dused += c
    return {
        # project 留空时是「main 库全部」，不能把库名回填成项目名，否则
        # 调用方会以为存在一个叫 main 的项目。scope 明确区分两种范围。
        "project": project or "",
        "scope": f"project:{project}" if project else "library:main",
        "scope_label": f"项目 {project}" if project else "main 库全部",
        "token_budget": token_budget,
        "estimated_tokens": used,
        "document_count": len(picked),
        "total_documents": len(docs),
        "digest": "\n".join(digest_parts),
        "documents": picked,
    }


async def sync_from_disk(session: AsyncSession, user: User) -> dict:
    """磁盘 → DB：扫 md 文件重建索引（含 git pull 后）。返回统计。"""
    root = user_root(settings.data_dir, user.id)
    added = updated = removed = 0
    lib_dirs = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    for lib in lib_dirs:
        for f in lib.glob("*.md"):
            rel = f"{lib.name}/{f.stem}"
            try:
                meta, content = parse_document(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            title = str(meta.get("title") or f.stem)[:256]
            doc = (
                await session.execute(
                    select(Document).where(Document.user_id == user.id, Document.rel_path == rel + ".md")
                )
            ).scalar_one_or_none()
            h = compute_hash(content)
            if doc and doc.deleted_at:
                # 从磁盘恢复（用户在别的机器 push 过）
                doc.deleted_at = None
                updated += 1
            elif doc:
                if doc.content_hash != h or doc.content != content:
                    doc.content = content
                    doc.title = title
                    doc.content_hash = h
                    doc.updated_at = _now()
                    updated += 1
                else:
                    continue
            else:
                doc = Document(
                    user_id=user.id, library=lib.name, slug=f.stem, title=title,
                    md_type=str(meta.get("type")) if str(meta.get("type")) in VALID_TYPES else "fact",
                    project=str(meta.get("project") or "")[:128],
                    tags=[str(t)[:32] for t in (meta.get("tags") or [])][:16],
                    importance=max(1, min(5, int(meta.get("importance") or 3))),
                    source=str(meta.get("source") or "disk")[:64],
                    content=content, rel_path=rel + ".md", content_hash=h,
                )
                session.add(doc)
                added += 1
            await session.flush()
            await _reindex_document(session, doc, do_embed=False)
    # DB 有但磁盘没了 → 软删
    disk_paths = set()
    for lib in lib_dirs:
        for f in lib.glob("*.md"):
            disk_paths.add(f"{lib.name}/{f.stem}.md")
    alive = (await session.execute(select(Document).where(Document.user_id == user.id, Document.deleted_at.is_(None)))).scalars().all()
    for d in alive:
        if d.rel_path not in disk_paths:
            d.deleted_at = _now()
            removed += 1
    await session.commit()
    return {"added": added, "updated": updated, "removed": removed}


class DuplicateError(Exception):
    def __init__(self, doc: Document):
        self.doc = doc
        super().__init__("已存在同标题文档")


class ConflictError(Exception):
    pass
