#!/usr/bin/env python3
"""给缺向量的 chunk 补向量。

用途：改过 embed_max_tokens / 换过 embedding 模型 / 之前上游挂了漏了一批，
用这个把 embedding IS NULL 的 chunk 补齐，不动已有向量、不重切 chunk。

跟 `POST /api/v1/sync {"action":"reindex"}` 的区别：sync 只处理**内容变过**的
文档（比对 content_hash），内容没变但缺向量的它不管 —— 这是刻意的，
否则每次 sync 都要为历史遗留的失败重试一遍。所以补向量要单独跑这个。

    .venv/bin/python tests/backfill_vectors.py            # 补全部
    .venv/bin/python tests/backfill_vectors.py --limit 50 # 只补 50 条
    .venv/bin/python tests/backfill_vectors.py --dry-run  # 只看有多少
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.config import DB_BACKEND, settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.dialect import PG, encode_vector  # noqa: E402
from app.search import EMBED_AVAILABLE, embed_texts, est_tokens  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多补多少条（0=全部）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=0, help="每批多少条（默认用配置里的值）")
    args = ap.parse_args()

    if not EMBED_AVAILABLE:
        print("未配置 embedding（MEM_EMBED_API_BASE / MEM_EMBED_API_KEY），无事可做。")
        return

    async with SessionLocal() as s:
        rows = (await s.execute(text(f"""
            SELECT ch.id, d.title, ch.heading, ch.content
            FROM chunks ch JOIN documents d ON d.id = ch.document_id
            WHERE d.deleted_at IS NULL AND ch.embedding IS NULL
            ORDER BY length(ch.content) DESC
            {'LIMIT ' + str(args.limit) if args.limit else ''}"""))).all()

        total = (await s.execute(text(
            "SELECT count(*) FROM chunks ch JOIN documents d ON d.id = ch.document_id "
            "WHERE d.deleted_at IS NULL"))).scalar()

        print(f"后端 {DB_BACKEND} | 模型 {settings.embed_model} "
              f"| token 预算 {settings.embed_max_tokens}")
        print(f"缺向量 {len(rows)} / 共 {total} chunk")
        if not rows:
            print("没有需要补的。")
            return
        if args.dry_run:
            print("\n最长的 5 条：")
            for cid, title, heading, content in rows[:5]:
                print(f"  id={cid} {len(content)}字 估{est_tokens(content)}tok "
                      f"《{(title or '')[:20]}》/ {(heading or '')[:20]}")
            return

        # 批大小走配置（上游有硬上限，阿里云百炼 25，超一条整批 400）
        bs = args.batch or settings.embed_batch_size
        done = failed = 0
        t0 = time.time()
        for i in range(0, len(rows), bs):
            chunk_rows = rows[i:i + bs]
            # 送进去的文本必须跟写入路径完全一致（title + heading + content），
            # 否则同一个 chunk 在"首次写入"和"补向量"两条路上得到不同的向量，
            # 检索结果会随"这条向量是谁算的"而变
            texts_ = [f"{t or ''}\n{h or ''}\n{c or ''}" for _, t, h, c in chunk_rows]
            vecs = await embed_texts(texts_)
            for slot, (cid, _t, _h, _c) in enumerate(chunk_rows):
                v = vecs[slot] if vecs and slot < len(vecs) else None
                if not v or len(v) != settings.embed_dim:
                    failed += 1
                    continue
                sql = (f"UPDATE chunks SET embedding = "
                       f"CAST(:vec AS vector({settings.embed_dim})) WHERE id = :id"
                       if DB_BACKEND == PG else
                       "UPDATE chunks SET embedding = :vec WHERE id = :id")
                await s.execute(text(sql), {"vec": encode_vector(v), "id": cid})
                done += 1
            await s.commit()
            print(f"  {min(i + bs, len(rows))}/{len(rows)}  成功 {done} 失败 {failed}"
                  f"  {time.time() - t0:.1f}s", flush=True)

        left = (await s.execute(text(
            "SELECT count(*) FROM chunks ch JOIN documents d ON d.id = ch.document_id "
            "WHERE d.deleted_at IS NULL AND ch.embedding IS NULL"))).scalar()
        cov = (await s.execute(text(
            "SELECT round(100.0 * count(*) FILTER (WHERE ch.embedding IS NOT NULL) "
            "/ greatest(count(*),1), 1) FROM chunks ch "
            "JOIN documents d ON d.id = ch.document_id WHERE d.deleted_at IS NULL"))).scalar()
        print(f"\n补完：成功 {done} 失败 {failed}，仍缺 {left}，覆盖率 {cov}%")
        print(f"耗时 {time.time() - t0:.1f}s")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
