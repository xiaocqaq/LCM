"""核心业务：文档 CRUD（md 为准、DB 索引）、检索（关键词+trgm+RRF 混合）、bootstrap。"""
import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, desc, func, select, text
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.ext.asyncio import AsyncSession

from . import gitsvc
from . import links as links_mod
from .config import settings
from .mdstore import (
    LINK_TYPES,
    VALID_TYPES,
    compute_hash,
    doc_path,
    normalize_links,
    parse_document,
    render_document,
    sanitize_slug,
    split_chunks,
    user_root,
)
from .models import Chunk, Document, User
from .search import embed_query, embed_texts, expand_tokens, to_tsquery


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
    links = normalize_links(payload.get("links"))
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
        links=links,
        content=content,
        rel_path=f"{library}/{slug}.md",
        content_hash=compute_hash(content),
    )
    session.add(doc)
    await session.flush()

    # links 落效果：supersedes 会给目标打 superseded_by（把旧版本从检索里请出去）
    link_report = await links_mod.apply_links(session, user, doc)

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
    if links:
        meta["links"] = links
    p = doc_path(settings.data_dir, user.id, library, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_document(meta, content), encoding="utf-8")
    await _reindex_document(session, doc)
    await session.commit()
    gitsvc.try_commit_all(user_root(settings.data_dir, user.id), f"create: {title}")
    # 未解析的 link 挂在实例上供上层回报。不抛异常 —— 关系写错了文档本身还是有效的，
    # 但也不能静默丢弃，否则用户以为 supersedes 生效了、旧文档其实还在检索里。
    doc.link_report = link_report  # type: ignore[attr-defined]
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
    link_report = None
    if "links" in payload:
        # 换 links 前先撤掉旧的 supersede 标记，否则被"取代"的文档永久隐身：
        # 用户把 links 改成别的目标，旧目标的 superseded_by 还指着这篇，
        # 而 links 里已经没有那条关系了，从任何界面都看不出原因。
        await links_mod.clear_supersede_marks(session, doc)
        doc.links = normalize_links(payload.get("links"))
        link_report = await links_mod.apply_links(session, user, doc)
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
    if doc.links:
        meta["links"] = doc.links
    p = root / doc.rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_document(meta, doc.content), encoding="utf-8")
    await _reindex_document(session, doc)
    await session.commit()
    gitsvc.try_commit_all(root, f"update: {doc.title}")
    if link_report is not None:
        doc.link_report = link_report  # type: ignore[attr-defined]
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
    """软删除：md 文件移进 .trash/，DB 行打 deleted_at，chunk 一并删掉。

    chunk 必须删：它们已经检索不到（查询都带 deleted_at IS NULL），留着只会
    让"多少 chunk 有向量"这类统计失真，也白占 pgvector 的存储和 hnsw 索引。
    要恢复的话 restore 会重新 reindex，chunk 本来就是可重建的派生数据。
    """
    doc.deleted_at = _now()
    await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    # 删掉一篇"取代者"之后，被它取代的文档必须重新可见 ——
    # 否则旧版本永久隐身，而且没有任何地方能看出为什么。
    await links_mod.clear_supersede_marks(session, doc)
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


async def soft_delete_project(session: AsyncSession, user: User, project: str,
                              library: str | None = None) -> dict:
    """把一个项目下的所有文档移进回收站。

    刻意做成**软删除**（和单篇删除一致），不是直接抹掉：
    "删掉整个项目" 是这套 UI 里一次能毁掉最多东西的操作，几十篇记忆一次没了。
    进回收站的话 md 还在 .trash/、git 有记录、界面上能一键恢复；
    真要腾空间就去回收站点清空，那一步才不可逆。
    把"批量"和"不可逆"分成两步，是因为它们同时发生时用户没有纠错机会。

    project 传空字符串表示"未归项目"的那一堆（Document.project 默认就是 ""）。
    """
    conds = [Document.user_id == user.id, Document.deleted_at.is_(None),
             Document.project == project]
    if library:
        conds.append(Document.library == library)
    docs = (await session.execute(select(Document).where(*conds))).scalars().all()
    if not docs:
        return {"deleted": 0, "titles": []}

    root = user_root(settings.data_dir, user.id)
    trash = root / ".trash"
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    titles = []
    for doc in docs:
        doc.deleted_at = _now()
        await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        await links_mod.clear_supersede_marks(session, doc)
        titles.append(doc.title)
    await session.flush()

    for doc in docs:
        p = root / doc.rel_path
        if p.exists():
            trash.mkdir(parents=True, exist_ok=True)
            p.rename(trash / f"{stamp}_{p.name}")
    await session.commit()
    label = project or "未归项目"
    gitsvc.try_commit_all(root, f"delete project: {label}（{len(docs)} 篇）")
    return {"deleted": len(docs), "titles": titles}


async def empty_trash(session: AsyncSession, user: User) -> dict:
    """清空回收站：DB 行真删 + .trash/ 下的 md 真删。不可恢复（除了翻 git 历史）。

    两件事都要做，只做一件都会留下不一致：
      - 只删 DB → .trash 里的 md 越堆越多（实测线上已经攒了 346 个文件），
        而且 reindex 不会碰它们，等于永久占着磁盘却谁也看不见
      - 只删文件 → 回收站列表还在，点恢复会写回一个空文档

    git 历史刻意不动：那是最后一层兜底。真删的是工作区和索引，
    翻 git 仍然能找回内容 —— 所以这一步"不可逆"指的是 UI 里不可逆。
    """
    docs = (await session.execute(select(Document).where(
        Document.user_id == user.id, Document.deleted_at.is_not(None)
    ))).scalars().all()

    root = user_root(settings.data_dir, user.id)
    trash = root / ".trash"

    removed_files = 0
    for doc in docs:
        await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        await session.execute(delete(Document).where(Document.id == doc.id))

    # .trash 整目录清掉，不是按文档名逐个匹配。
    #
    # 按名字匹配对不上：软删除时文件被重命名成 "{时间戳}_{原名}"，而 DB 行的
    # rel_path 仍是原路径，同一路径反复建删会在 .trash 里留下多个同后缀文件，
    # 没法可靠地判断哪个属于哪一行。而"清空回收站"的语义本来就是全清，
    # 逐个匹配只会留下一堆认领不到的孤儿文件。
    if trash.exists():
        for f in sorted(trash.iterdir()):
            if f.is_file():
                f.unlink()
                removed_files += 1

    await session.commit()
    if docs or removed_files:
        gitsvc.try_commit_all(
            root, f"purge trash: {len(docs)} 篇 / {removed_files} 个文件")
    return {"purged": len(docs), "files": removed_files}


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
    """删旧 chunk，重切、重分词，重插。向量按内容复用，只对新内容调上游。

    向量复用是必须的，不是优化：一次全量 reindex 要给每个 chunk 调一次
    embedding 上游，而这个上游会随机 ReadTimeout（重试要等 30s+）。
    实测 23 篇文档全量 reindex 超过 7 分钟，直接把 `POST /api/v1/sync` 打成超时。
    而绝大多数 chunk 的文本根本没变 —— 切分规则一样、正文一样，就该沿用旧向量。

    复用的键是 chunk 正文本身（不是 seq）：加一节内容会让后面所有 chunk 的 seq
    整体位移，按 seq 复用等于把向量和内容错配，检索会返回莫名其妙的结果。

    do_embed=False 时只跳过"给新内容算向量"，已有向量仍然保留 ——
    早先的实现是先 DELETE 再重插、向量一起没，拿它刷同义词清空过全库向量。
    """
    # 先取旧 chunk 的 (内容 → 向量)。用原始 SQL 读 embedding 列的文本形式，
    # ORM 那边这一列是 Text（启动时 ALTER 成 vector），走 ORM 会拿到 str 或 None。
    old_vecs: dict[str, str] = {}
    rows = (await session.execute(
        text("SELECT content, embedding::text FROM chunks "
             "WHERE document_id = :d AND embedding IS NOT NULL"),
        {"d": doc.id},
    )).all()
    for content_, vec_ in rows:
        if content_ and vec_:
            old_vecs[content_] = vec_

    await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    pieces = split_chunks(doc.content)
    if not pieces:
        # 空内容也保留一个 chunk，防止文档整体消失
        pieces = [{"heading": "", "content": doc.content}]

    # 只给"旧向量里没有的内容"调上游
    need_idx = [i for i, p in enumerate(pieces) if p["content"] not in old_vecs]
    fresh: dict[int, list[float]] = {}
    if do_embed and need_idx:
        got = await embed_texts(
            [f"{doc.title}\n{pieces[i]['heading']}\n{pieces[i]['content']}" for i in need_idx])
        if got:
            for slot, i in enumerate(need_idx):
                v = got[slot] if slot < len(got) else None
                # 维度不符的单独剔掉而不是整批丢：hnsw 索引建在固定维度上，
                # 混入别的维度写入会被 pgvector 拒，但没理由因为一条坏的放弃其余好的。
                if v and len(v) == settings.embed_dim:
                    fresh[i] = v

    for seq, p in enumerate(pieces):
        lex = _tsvector_literal(f"{doc.title} {doc.project} {' '.join(doc.tags or [])} {p['heading']} {p['content']}")
        ch = Chunk(document_id=doc.id, seq=seq, heading=p["heading"], content=p["content"])
        session.add(ch)
        await session.flush()
        await session.execute(
            text("UPDATE chunks SET tsv = to_tsvector('simple', :lex) WHERE id = :id"),
            {"lex": lex, "id": ch.id},
        )
        reuse = old_vecs.get(p["content"])
        if reuse:
            await session.execute(
                text(f"UPDATE chunks SET embedding = CAST(:vec AS vector({settings.embed_dim})) WHERE id = :id"),
                {"vec": reuse, "id": ch.id},
            )
        elif seq in fresh:
            await session.execute(
                text(f"UPDATE chunks SET embedding = CAST(:vec AS vector({settings.embed_dim})) WHERE id = :id"),
                {"vec": "[" + ",".join(f"{x:.6f}" for x in fresh[seq]) + "]", "id": ch.id},
            )


async def _bump_access(doc_ids: list[int]) -> None:
    """给检索命中的文档累加 access_count。

    为什么用独立 session 而不是调用方的：检索是读路径，
    FastAPI 的 get_session 从不 commit（没有 autocommit），
    挂在调用方 session 上的 UPDATE 会随请求结束被丢掉 —— 实测就是这么失败的。
    也不该为了记账让检索变成写事务：调用方可能在一个更大的只读逻辑里。

    记账失败绝不能影响检索本身，所以整段包在 try 里。
    热度是锦上添花的信号，丢几次无所谓；搜不出结果是硬故障。
    """
    if not doc_ids:
        return
    from .db import SessionLocal
    try:
        async with SessionLocal() as s:
            await s.execute(
                sqlalchemy_update(Document)
                .where(Document.id.in_(doc_ids))
                .values(access_count=Document.access_count + 1, last_accessed_at=_now())
                .execution_options(synchronize_session=False)
            )
            await s.commit()
    except Exception:
        pass


async def search_chunks(session: AsyncSession, user: User, q: str, limit: int = 10,
                        library: str | None = None, project: str | None = None,
                        use_vector: bool = True,
                        include_superseded: bool = False) -> list[dict]:
    """关键词检索（tsvector OR + ts_rank）+ trgm 相似度兜底 + 可选向量，RRF 融合。

    默认排除被 supersedes 的文档。理由是实测踩到的问题：库里有三篇讲同一个
    项目工程结构的文档（标题相似度 0.47-0.60，小节几乎一一对应），
    检索时三篇全命中，agent 无从判断该信哪个 —— 那比没有信息更糟。
    标了 supersedes 之后旧版本退出检索，但文件还在、还能直接 memory_get 读。
    """
    tsq = to_tsquery(q)
    kw_rows: list[tuple] = []
    trgm_rows: list[tuple] = []
    conds = [Document.user_id == user.id, Document.deleted_at.is_(None)]
    if not include_superseded:
        conds.append(Document.superseded_by.is_(None))
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

    # 第三路：向量语义检索。没配 embedding 或上游挂了就返回 None，自动降级为两路。
    # 放在这里而不是并发跑：三路里只有这一路有网络往返（实测 280-800ms），
    # 而前两路是本地 PG 查询（个位数毫秒），并发省不下什么，反而让失败处理变复杂。
    vec_rows: list[tuple] = []
    # 预算保护放在这一层而不是只靠 embed_query 内部：向量路是整条检索里唯一
    # 会碰网络的部分，超时保护必须是结构性的 —— 换 embedding 实现、
    # 或者哪天有人在 semantic_search 里加了别的远程调用，这里依然兜得住。
    sem = None
    if use_vector:
        try:
            sem = await asyncio.wait_for(
                semantic_search(session, user, q, limit=limit * 3,
                                library=library, project=project),
                timeout=settings.embed_query_timeout + 1.0,
            )
        except (TimeoutError, asyncio.TimeoutError):
            sem = None
        except Exception:
            sem = None
    if sem:
        # semantic_search 返回的是已组装的 dict，这里只取排序用的 id/doc_id。
        #
        # 每篇文档在向量路里最多留 VEC_PER_DOC 个 chunk。这一条不是优化而是必须：
        # 长文档 chunk 多，就有更多机会挤进向量 top-N，每个 chunk 都独立贡献一份
        # RRF 分数，于是长文靠"票多"重新拿回了长度归一化刚刚抵掉的优势。
        # 实测没有这个约束时，28 chunk 的长文会把对题短文压到第二。
        # 关键词/trgm 两路不需要这条，它们在 SQL 里就按 ts_rank 排过序，
        # 长文的多个 chunk 不会全挤在前面。
        VEC_PER_DOC = 2
        seen_doc: dict[int, int] = {}
        for s in sem:
            did = s["document_id"]
            if seen_doc.get(did, 0) >= VEC_PER_DOC:
                continue
            seen_doc[did] = seen_doc.get(did, 0) + 1
            vec_rows.append((s["chunk_id"], did))

    # RRF 融合。三路等权 —— 关键词精确但对同义改写无能，trgm 抗错字但会被长文噪声带偏，
    # 向量懂语义但对专有名词/路径/端口这类字面量不敏感。谁都不该压倒另外两个。
    rrf: dict[int, dict] = {}
    doc_ids = set()
    by_id = {r[0]: r for r in kw_rows + trgm_rows}
    for rows in (kw_rows, trgm_rows, vec_rows):
        for rank, r in enumerate(rows):
            cid = r[0]
            # 向量路可能召回前两路没见过的 chunk，此时 row 只有 (id, doc_id)，
            # 缺 heading/content 等字段，需要从 sem 结果里补齐
            if cid not in by_id and sem:
                s = next(x for x in sem if x["chunk_id"] == cid)
                by_id[cid] = (cid, s["document_id"], s["seq"], s["heading"], s["content"], 0.0)
            e = rrf.setdefault(cid, {"row": by_id[cid], "score": 0.0})
            e["score"] += 1.0 / (60 + rank + 1)
            doc_ids.add(by_id[cid][1])

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
            # 注意这里用手填的 importance，不掺 access_count 的热度 ——
            # 检索里加热度会形成正反馈：排前面 → 被读到 → 排更前面，
            # 最后热门文档垄断所有查询。热度只用在 bootstrap 的取舍上。
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
    # 记账：真正进了结果集的文档 access_count +1。
    # 只统计返回给调用方的（不是所有召回的），且只算文档级不算 chunk 级 ——
    # 一篇文档出两个 chunk 不该记两次。
    hit_docs = {e["row"][1] for e in merged}
    if hit_docs:
        await _bump_access(list(hit_docs))
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


async def semantic_search(session: AsyncSession, user: User, q: str, limit: int = 10,
                          library: str | None = None, project: str | None = None) -> list[dict] | None:
    """embedding 向量检索（配置了才可用）。返回 None 表示不可用。

    library/project 过滤必须和 search_chunks 保持一致 —— 否则融合进 RRF 后
    向量路会把用户明确过滤掉的文档带回结果里。
    """
    from .search import EMBED_AVAILABLE
    if not EMBED_AVAILABLE:
        return None
    # 走 embed_query 而不是 embed_texts：检索有硬性时间预算，
    # 上游抖动时直接降级成两路关键词，不能让用户等重试
    qv = await embed_query(q)
    if not qv:
        return None
    vec_str = "[" + ",".join(f"{x:.6f}" for x in qv) + "]"
    dim = settings.embed_dim
    # 距离表达式必须逐字匹配 hnsw 索引的定义 `(embedding::vector(<dim>))`，
    # 否则 PG 认不出可用索引，静默退化成全表扫描 + 逐行算距离（不报错，只是慢）。
    # embedding IS NULL 的 chunk 必须排掉：向量列是可空的（上游挂了就不写），
    # NULL 参与排序会挤占 LIMIT 名额。
    params: dict = {"vec": vec_str, "uid": user.id, "lim": limit}
    # superseded 过滤必须和 search_chunks 一致。漏在这里等于给旧版本开了后门：
    # 关键词路排掉了，向量路又把它捞回来融进 RRF。
    extra = " AND d.superseded_by IS NULL"
    # 用 CAST(:x AS text) 而不是裸 :x —— asyncpg 对无类型参数推不出类型会报
    # AmbiguousParameterError（README 坑 1、2）
    if library:
        extra += " AND d.library = CAST(:lib AS text)"
        params["lib"] = library
    if project:
        extra += " AND d.project = CAST(:proj AS text)"
        params["proj"] = project
    sql = text(
        f"""
        SELECT c.id, c.document_id, c.seq, c.heading, c.content,
               1 - (c.embedding::vector({dim}) <=> CAST(:vec AS vector({dim}))) AS score
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.user_id = :uid AND d.deleted_at IS NULL AND c.embedding IS NOT NULL{extra}
        ORDER BY c.embedding::vector({dim}) <=> CAST(:vec AS vector({dim}))
        LIMIT :lim
        """
    )
    rows = (await session.execute(sql, params)).all()
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
    """项目压缩上下文包：按类型优先级 + 有效重要度排序，按 token 预算装箱。"""
    conds = [Document.user_id == user.id, Document.deleted_at.is_(None),
             # 被取代的旧版本不进开场包。给 agent 塞过时信息比少塞一篇危害大得多。
             Document.superseded_by.is_(None)]
    if project:
        conds.append(Document.project == project)
    else:
        conds.append(Document.library == "main")
    docs = (await session.execute(select(Document).where(*conds))).scalars().all()

    # 排序：类型优先级 → 有效重要度 → updated_at
    #
    # 有效重要度 = 手填 importance + 实际取用热度。
    # 为什么要掺一个热度：实测 96% 的文档手填 importance≥4（人在写的时候
    # 都觉得自己写的重要），这个字段已经没有区分度了。
    # graph-memory 用 validatedCount（节点被重复提取到就 +1）解决同一个问题 ——
    # 关键是那是**客观累积**的信号，不是写入时的自我评价。
    # 这里用 access_count 做同样的事，但权重压得很小（最多顶 1 级），
    # 因为热度只是"被读过"，不等于"重要"：一篇写错的文档也可能被反复检索到。
    def eff_importance(d: Document) -> float:
        heat = min(1.0, (d.access_count or 0) / 10.0)
        return d.importance + heat

    docs.sort(key=lambda d: (TYPE_PRIORITY.get(d.md_type, 9), -eff_importance(d),
                             -(d.updated_at.timestamp() if d.updated_at else 0)))

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
    # ── 关系补充：把 implements 关联的文档带进来 ──
    # 命中一篇实现记录时，背后的决策文档往往才是 agent 真正需要的
    # （"为什么这么做"比"怎么做的"更难从代码重建）。反之亦然。
    # 只在预算还有余量时补，且明确标注 via="implements"，
    # 让 agent 知道这几篇不是按重要度选出来的，而是被关联带出来的。
    linked_extra: list[dict] = []
    left = token_budget - used
    if picked and left > META_COST + MIN_SLICE:
        extras = await links_mod.expand_by_links(
            session, user, [e["id"] for e in picked], max_extra=3)
        chosen = {e["id"] for e in picked}
        for d in extras:
            if d.id in chosen:
                continue
            left = token_budget - used
            if left <= META_COST + MIN_SLICE:
                break
            body = d.content or ""
            content = body[:min(PER_DOC_CHARS, chars_for(left - META_COST))]
            entry = {
                "id": d.id, "title": d.title, "type": d.md_type, "project": d.project,
                "tags": d.tags, "importance": d.importance,
                "content": content, "truncated": len(content) < len(body),
                "via": "implements",
            }
            picked.append(entry)
            linked_extra.append({"id": d.id, "title": d.title})
            used += est_tokens(content) + META_COST

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
        # 哪几篇是被 implements 关系带出来的（不是按重要度选的）
        "linked_extra": linked_extra,
        "digest": "\n".join(digest_parts),
        "documents": picked,
    }


async def sync_from_disk(session: AsyncSession, user: User) -> dict:
    """磁盘 → DB：扫 md 文件重建索引（含 git pull 后）。返回统计。"""
    root = user_root(settings.data_dir, user.id)
    added = updated = removed = 0
    # links 要在全部文档都进 DB 之后再统一解析 —— 关系可能指向本次 sync
    # 里更靠后才扫到的文件，边扫边解析会有一半解析不出来。
    link_dirty: list[Document] = []
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
                doc.links = normalize_links(meta.get("links"))
                updated += 1
            elif doc:
                if doc.content_hash != h or doc.content != content:
                    doc.content = content
                    doc.title = title
                    doc.content_hash = h
                    doc.links = normalize_links(meta.get("links"))
                    doc.updated_at = _now()
                    updated += 1
                else:
                    # 内容没变但 frontmatter 的 links 可能改了（手改 md 或从别处 pull）
                    fm_links = normalize_links(meta.get("links"))
                    if fm_links != (doc.links or []):
                        doc.links = fm_links
                        updated += 1
                        link_dirty.append(doc)
                    continue
            else:
                doc = Document(
                    user_id=user.id, library=lib.name, slug=f.stem, title=title,
                    md_type=str(meta.get("type")) if str(meta.get("type")) in VALID_TYPES else "fact",
                    project=str(meta.get("project") or "")[:128],
                    tags=[str(t)[:32] for t in (meta.get("tags") or [])][:16],
                    importance=max(1, min(5, int(meta.get("importance") or 3))),
                    source=str(meta.get("source") or "disk")[:64],
                    links=normalize_links(meta.get("links")),
                    content=content, rel_path=rel + ".md", content_hash=h,
                )
                session.add(doc)
                added += 1
            await session.flush()
            link_dirty.append(doc)
            # 从磁盘 sync 走到这里说明内容真的变了（上面按 content_hash 比过），
            # 所以向量也该重算。传 do_embed=True —— 早先写的 False 会让
            # 每次 sync/reindex 静默清空全库向量，配上 embedding 后才暴露出来。
            await _reindex_document(session, doc, do_embed=True)
    # DB 有但磁盘没了 → 软删
    disk_paths = set()
    for lib in lib_dirs:
        for f in lib.glob("*.md"):
            disk_paths.add(f"{lib.name}/{f.stem}.md")
    alive = (await session.execute(select(Document).where(Document.user_id == user.id, Document.deleted_at.is_(None)))).scalars().all()
    for d in alive:
        if d.rel_path not in disk_paths:
            d.deleted_at = _now()
            # 消失的文档如果曾经取代过别的文档，被取代者要重新可见
            await links_mod.clear_supersede_marks(session, d)
            removed += 1
    await session.flush()

    # ── links 统一解析（必须在全部文档都进 DB 之后）──
    # 先清空所有 supersede 标记再重建，而不是增量改：md 是 source of truth，
    # 磁盘上没有的关系就不该在 DB 里留着。增量更新会让删掉一行 links 之后
    # 目标文档永久隐身。
    if link_dirty:
        for d in (await session.execute(select(Document).where(
                Document.user_id == user.id,
                Document.superseded_by.is_not(None)))).scalars().all():
            d.superseded_by = None
        await session.flush()
        all_alive = (await session.execute(select(Document).where(
            Document.user_id == user.id, Document.deleted_at.is_(None)))).scalars().all()
        link_unresolved = []
        for d in all_alive:
            if not d.links:
                continue
            rep = await links_mod.apply_links(session, user, d)
            for u in rep["unresolved"]:
                link_unresolved.append({"from": d.title, **u})
    else:
        link_unresolved = []

    await session.commit()
    return {"added": added, "updated": updated, "removed": removed,
            "link_unresolved": link_unresolved}


class DuplicateError(Exception):
    def __init__(self, doc: Document):
        self.doc = doc
        super().__init__("已存在同标题文档")


class ConflictError(Exception):
    pass
