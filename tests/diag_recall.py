"""诊断召回：对比索引侧 expand_tokens 与查询侧 tokenize 的重叠。

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/diag_recall.py
"""
from app.search import expand_tokens, to_tsquery, tokenize

QUERIES = [
    "记忆文件存在哪个目录",
    "数据存在哪里",
    "md 文件放哪",
    "MCP 怎么接入",
    "服务监听哪个端口",
    "怎么备份",
    # 反向用例：应当【不】命中，用来确认没有过度扩展
    "养猫要注意什么",
    "今天天气怎么样",
]

DOC = ("md 是 source of truth，落在 /var/lib/memorys/data/users/u<id>/，"
       "每用户独立 git repo，写入即提交。PG(54321/memorys) 只做检索索引。"
       "MCP streamable HTTP 端点 https://repo.xlingo.fun/mcp/ ，鉴权头 X-Api-Key。"
       "systemd 服务 memorys（127.0.0.1:8649），nginx 反代。")

idx = set(expand_tokens(DOC))
print(f"索引侧词数 {len(idx)}")
print("索引词:", " ".join(sorted(idx)))
print()

for q in QUERIES:
    qt = tokenize(q)
    overlap = idx.intersection(qt)
    verdict = "命中" if overlap else "不命中"
    print(f"[{verdict}] {q}")
    print(f"    查询词: {qt}")
    print(f"    重叠  : {sorted(overlap) if overlap else '—'}")
