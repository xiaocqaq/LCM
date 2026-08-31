"""给 Web UI 浏览器实测造数据：建用户 + 写几篇不同类型的记忆，打印 JWT。

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/seed_ui.py
"""
import asyncio

DOCS = [
    dict(title="memorys 项目架构总结", type="project_summary", project="memorys",
         tags=["fastapi", "mcp", "postgres"], importance=5,
         content="# 定位\n\nmemorys 是 AI 跨会话知识库：md 文件是 source of truth，PG 只做检索索引。\n\n# 架构\n\nFastAPI 提供 REST，MCP server 以 ASGI 子应用挂在 /mcp，Web UI 是单文件 HTML。\n每个用户一个独立 git repo，写入即提交，可推 GitHub 长期备份。\n\n# 端口\n\n本地监听 8649，nginx 反代到 repo.xlingo.fun。"),
    dict(title="检索策略：为什么用 RRF 融合", type="decision", project="memorys",
         tags=["search", "jieba", "rrf"], importance=5,
         content="# 结论\n\n用 jieba 分词生成 tsvector 做关键词检索，pg_trgm 相似度兜底错别字，embedding 可选。\n三路结果用 RRF（倒数排名融合）合并，公式 score += 1/(60+rank+1)。\n\n# 原因\n\n没装 zhparser，PG 内置分词对中文无效；纯向量要外部 API 有成本，中文关键词召回已覆盖大部分场景。"),
    dict(title="用户偏好：改配置先备份", type="preference", project="",
         tags=["habit", "ops"], importance=4,
         content="改站点或配置之前先备份，变更只做新增一整段，方便整段删除回退。\n长任务中途不要停下来汇报，一口气做到实测完成再说。"),
    dict(title="PG 连接约定", type="fact", project="infra",
         tags=["postgres", "port"], importance=3,
         content="本机 PostgreSQL 18 监听 54321，不是默认 5432。宝塔原生安装，不是 Docker。\n超级用户操作要用 sudo -u postgres /www/server/pgsql/bin/psql -h 127.0.0.1 -p 54321。"),
    dict(title="怎么把 memorys 挂到 agent", type="howto", project="memorys",
         tags=["mcp", "client"], importance=4,
         content="# 步骤\n\n1. 在 Web UI 的「API Key」页创建一个 key\n2. MCP 客户端配置 streamable HTTP，地址 https://repo.xlingo.fun/mcp\n3. 请求头带 X-API-Key，或 Authorization: Bearer <key>\n4. 新会话开场先调 memory_bootstrap 拉项目上下文"),
]


async def main():
    from app.db import SessionLocal
    from app.models import User
    from app.security import issue_local_jwt
    from app import gitsvc, service
    from app.config import settings
    from sqlalchemy import select

    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.username == "uidemo"))).scalar_one_or_none()
        if not u:
            u = User(username="uidemo", display_name="UI 演示用户",
                     xiaoai_user_id=999777, email="uidemo@example.local", role="user")
            s.add(u)
            await s.commit()
            await s.refresh(u)
        gitsvc.ensure_repo(settings.data_dir, u.id)
        token, _ = issue_local_jwt(u)

        made = []
        for d in DOCS:
            payload = dict(d)
            payload["library"] = "main"
            payload["source"] = "seed"
            try:
                doc = await service.create_document(s, u, payload)
                made.append(doc.id)
            except service.DuplicateError as e:
                made.append(e.doc.id)

    print("USER_ID", u.id)
    print("TOKEN", token)
    print("DOCS", made)


asyncio.run(main())
