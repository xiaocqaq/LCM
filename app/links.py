"""文档间关系（links）的解析与应用。

设计取舍（对照 graph-memory 的做法）：

它把知识拆成 TASK/SKILL/EVENT 三类节点 + 五类边，靠 LLM 从对话里自动抽取，
再跑 Label Propagation 社区检测和 PageRank 排序。那套东西解决的是
"对话流水如何变成结构化知识"。

memorys 的输入不是对话流水 —— 是 agent 或人**已经想清楚了才写下**的成篇记忆。
所以不需要抽取层，也不需要社区检测（23 篇文档跑 PageRank 是自娱自乐）。

真正从实测数据里看到的问题只有两个：

1. **同题文档并存**。库里有 3 篇讲同一个项目工程结构的文档（相似度 0.47-0.60），
   小节标题几乎一一对应，是同一份知识写了三遍。检索时三篇都命中，
   agent 无从判断该信哪个 —— 这是错误信息，比没有信息更糟。
   graph-memory 用向量去重 + PATCHES 边解决。这里用 supersedes。

2. **importance 手填值失去区分度**。实测 96% 的文档填了 ≥4。
   人在写的时候都觉得自己写的重要。graph-memory 用 validatedCount
   （节点被重复提取到就 +1）替代，那是自动累积的客观信号。
   这里用 access_count：真正被检索取用过的才算有用。

所以只实现三种关系，且每种都必须改变检索行为 ——
一条边如果不影响"该给 agent 看什么"，它就只是装饰。
"""
from sqlalchemy import String, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .mdstore import sanitize_slug
from .models import Document, User


async def resolve_target(session: AsyncSession, user: User, target: str) -> Document | None:
    """把 links 里的 target 解析成文档。

    target 可以写 slug、标题，或 `#123` 形式的 id。
    宽松匹配是刻意的：这是给人手写 md 用的字段，
    要求写 slug 才能生效等于没人会用。
    """
    target = (target or "").strip()
    if not target:
        return None
    base = [Document.user_id == user.id, Document.deleted_at.is_(None)]

    if target.startswith("#") and target[1:].isdigit():
        return (await session.execute(select(Document).where(
            *base, Document.id == int(target[1:])))).scalars().first()

    # 精确 slug / 标题
    hit = (await session.execute(select(Document).where(
        *base, or_(Document.slug == target, Document.title == target)
    ).limit(1))).scalars().first()
    if hit:
        return hit

    # slug 规范化后再试（用户可能写了带空格的标题）
    slug = sanitize_slug(target)
    hit = (await session.execute(select(Document).where(
        *base, Document.slug == slug).limit(1))).scalars().first()
    if hit:
        return hit

    # 最后退到标题前缀匹配。限定唯一命中，多个就放弃 ——
    # 猜错关系比没关系更危险（supersedes 会让文档从检索里消失）。
    rows = (await session.execute(select(Document).where(
        *base, Document.title.ilike(f"{target}%")).limit(2))).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def apply_links(session: AsyncSession, user: User, doc: Document) -> dict:
    """把 doc.links 落成实际效果。返回本次生效/未解析的明细。

    只有 supersedes 有副作用（给目标打 superseded_by）。
    implements/relates 是纯读取期的关系，不改目标文档 ——
    这样即使关系写错了，也只影响检索的补充项，不会让别的文档消失。
    """
    resolved: list[dict] = []
    unresolved: list[dict] = []
    for link in (doc.links or []):
        t = link.get("type")
        tgt = link.get("target") or ""
        target_doc = await resolve_target(session, user, tgt)
        if not target_doc or target_doc.id == doc.id:
            # 自指也算未解析：一篇文档取代自己没有意义，且会把自己藏起来
            unresolved.append({**link, "reason": "找不到目标" if not target_doc else "指向自己"})
            continue
        entry = {**link, "target_id": target_doc.id, "target_title": target_doc.title}
        if t == "supersedes":
            target_doc.superseded_by = doc.id
        resolved.append(entry)
    return {"resolved": resolved, "unresolved": unresolved}


async def clear_supersede_marks(session: AsyncSession, doc: Document) -> int:
    """清掉本文档打出去的 supersede 标记。

    更新 links 或删除文档时必须调 —— 否则删掉一篇"取代者"之后，
    被它取代的文档会永久隐身，而且没有任何地方能看出为什么。
    """
    rows = (await session.execute(select(Document).where(
        Document.superseded_by == doc.id))).scalars().all()
    for r in rows:
        r.superseded_by = None
    return len(rows)


async def incoming_links(session: AsyncSession, user: User, doc: Document) -> list[dict]:
    """反向关系：谁指向了这篇文档。

    links 只存在源文档一侧（frontmatter 里），所以反查要扫 JSONB。
    23 篇文档规模下全表扫可接受；上千篇时给 links 建 GIN 索引。
    """
    rows = (await session.execute(select(Document).where(
        Document.user_id == user.id,
        Document.deleted_at.is_(None),
        Document.id != doc.id,
        func.cast(Document.links, String).contains(f'"{doc.slug}"')
        | func.cast(Document.links, String).contains(f'"#{doc.id}"')
        | func.cast(Document.links, String).contains(f'"{doc.title}"'),
    ))).scalars().all()
    out = []
    for r in rows:
        for link in (r.links or []):
            tgt = (link.get("target") or "").strip()
            if tgt in (doc.slug, doc.title, f"#{doc.id}"):
                out.append({
                    "type": link.get("type"), "note": link.get("note", ""),
                    "from_id": r.id, "from_title": r.title, "from_type": r.md_type,
                })
    return out


async def expand_by_links(
    session: AsyncSession, user: User, doc_ids: list[int], max_extra: int = 3
) -> list[Document]:
    """沿 implements 边把关联文档带出来。

    为什么只跟 implements：命中一篇实现记录时，背后的决策文档往往是
    agent 真正需要的（"为什么这么做"比"怎么做的"更难重建）。
    双向都跟 —— 命中决策时也该看到落地记录。

    relates 不参与：弱关联展开一层就会把半个库拖进来。
    深度只有一层，不做图遍历：memorys 的文档是成篇知识不是细粒度节点，
    两层展开等于把整个 project 都塞进上下文。
    """
    if not doc_ids:
        return []
    seeds = (await session.execute(select(Document).where(
        Document.id.in_(doc_ids)))).scalars().all()
    seed_ids = {d.id for d in seeds}
    picked: dict[int, Document] = {}

    # 正向：seed 的 links 里 implements 指向谁
    for d in seeds:
        for link in (d.links or []):
            if link.get("type") != "implements":
                continue
            t = await resolve_target(session, user, link.get("target") or "")
            if t and t.id not in seed_ids and t.superseded_by is None:
                picked[t.id] = t

    # 反向：谁 implements 了 seed
    for d in seeds:
        for inc in await incoming_links(session, user, d):
            if inc["type"] != "implements":
                continue
            if inc["from_id"] in seed_ids or inc["from_id"] in picked:
                continue
            t = (await session.execute(select(Document).where(
                Document.id == inc["from_id"]))).scalars().first()
            if t and t.superseded_by is None:
                picked[t.id] = t

    return list(picked.values())[:max_extra]
