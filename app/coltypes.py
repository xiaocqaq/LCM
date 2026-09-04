"""跨方言的列类型。

## 问题

models.py 原来直接 import 了 `postgresql.JSONB` 和 `postgresql.TSVECTOR`。
这两个类型在 SQLite 上直接建表失败（`UnsupportedCompilationError`），
所以本地模式跑不起来 —— 不是"性能差一点"，是连表都建不出来。

## 做法

用 SQLAlchemy 的 `with_variant`：同一个列声明，按方言编译成不同的底层类型。
业务代码读写的 Python 值不变（list/dict 进、list/dict 出），
不需要在 service 层到处判断后端。

TSVECTOR 特殊一点：SQLite 侧根本没有对应概念（关键词索引在独立的 FTS5 表里），
所以变体是 `Text` —— 这一列在 SQLite 上永远是 NULL，留着只为让 ORM 模型统一。
写入路径由 dialect.write_chunk_lexemes() 分流，不会去碰它。
"""
from sqlalchemy import JSON, BigInteger, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

# JSONB(PG) / JSON(SQLite)。
#
# 注意方向：基类型必须是通用的 JSON，PG 是它的 variant。
# 反过来写（JSONB.with_variant(JSON, "sqlite")）在 SQLite 上仍会尝试
# 编译 JSONB 而报错 —— with_variant 只在**命中的**方言上替换，
# 没命中时用基类型，所以基类型必须是那个能跑在所有方言上的。
JSONType = JSON().with_variant(JSONB(), "postgresql")

# tsvector 列。SQLite 侧退化成 Text 且始终为 NULL（见模块 docstring）。
TSVectorType = Text().with_variant(TSVECTOR(), "postgresql")

# 自增主键，用在预期行数很大的表上（chunks）。
#
# ⚠️ 这个变体是必须的，不是优化：SQLite 里**只有** `INTEGER PRIMARY KEY`
# 才是 rowid 的别名并自动分配值。`BIGINT PRIMARY KEY` 不是别名，
# 插入时不给 id 就直接 `NOT NULL constraint failed: chunks.id` ——
# 实测就是这么炸的，而且报错信息完全没提"类型不对"，看着像是代码忘了传 id。
#
# SQLite 的 INTEGER 本身就是变长的（最大 8 字节），所以退化成 Integer
# 不损失取值范围，只是声明上的差别。
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
