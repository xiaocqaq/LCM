"""精确清掉本轮验收写入的测试文档（id 已知），不动其它回收站内容。

只删 documents 行和对应的 .trash 文件。不走 empty_trash —— 那个会全清。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/opt/memorys")
from sqlalchemy import text
from app.db import engine

IDS = [int(x) for x in sys.argv[1:]]
if not IDS:
    print("用法: python tests/prod_cleanup_ids.py <id> [<id> ...]")
    sys.exit(2)


async def main():
    async with engine.begin() as c:
        rows = (await c.execute(text(
            "SELECT id, title, project, rel_path, deleted_at FROM documents "
            "WHERE id = ANY(:ids)"
        ), {"ids": IDS})).fetchall()
        print(f"命中 {len(rows)} 篇：")
        for i, t, p, rp, d in rows:
            print(f"  id={i} project={p!r} deleted={bool(d)} title={t!r}")
        if not rows:
            return
        await c.execute(text("DELETE FROM chunks WHERE document_id = ANY(:ids)"),
                        {"ids": IDS})
        await c.execute(text("DELETE FROM documents WHERE id = ANY(:ids)"),
                        {"ids": IDS})
        print("  DB 行已删")

    # 文件：软删后在 .trash/{时间戳}_{原名}，原名 = rel_path 的 basename
    from app.config import settings
    from app.mdstore import user_root
    # 用户根：按 data_dir 找所有 .trash
    n_files = 0
    names = {Path(r[3]).name for r in rows if r[3]}
    for trash in Path(settings.data_dir).rglob(".trash"):
        for f in trash.iterdir():
            if f.is_file() and any(f.name.endswith("_" + n) or f.name == n for n in names):
                f.unlink()
                n_files += 1
                print(f"  删文件 {f}")
    print(f"  文件 {n_files} 个")

    async with engine.connect() as c:
        left = (await c.execute(text(
            "SELECT count(*) FROM documents WHERE id = ANY(:ids)"
        ), {"ids": IDS})).scalar()
        trash_n = (await c.execute(text(
            "SELECT count(*) FROM documents WHERE deleted_at IS NOT NULL"
        ))).scalar()
        alive = (await c.execute(text(
            "SELECT count(*) FROM documents WHERE deleted_at IS NULL"
        ))).scalar()
        print(f"  复核：残留 {left} | 回收站 {trash_n} 篇 | 活跃 {alive} 篇")


asyncio.run(main())
