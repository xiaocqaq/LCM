"""stdio 传输的 MCP 入口：`python -m app.mcp_stdio`。

为什么需要它
------------
HTTP 传输要求先把服务跑起来（`python -m app.local`），客户端再连
`http://127.0.0.1:8650/mcp`。这在"我本来就要开 Web UI"时最省事，
但有两种情况 HTTP 走不通：

  1. 部分客户端只支持 stdio（配置里只有 `command`/`args`，没有 `url`）。
  2. 用户不想常驻一个服务 —— stdio 是客户端**按需拉起**子进程，
     退出时一起收掉，不占端口、不用管开没开。

所以这里提供第二条路：同一套工具、同一个库，换传输层。

与 HTTP 路径的三个差异（都是必须处理的，不是可选优化）
------------------------------------------------------
1. **没有 FastAPI lifespan**，建表/DDL 不会自动跑。stdio 很可能是用户
   第一次启动这个库（客户端拉起来就直接用了），库文件都还不存在，
   所以这里必须自己做一次和 lifespan 等价的初始化，否则第一次调用工具
   会撞 `no such table: documents`。

2. **没有请求头**，`mcp_app` 那套 `authenticate_headers` 中间件不参与。
   鉴权上下文改由这里直接注入：local 模式取单用户上下文；server 模式
   要求显式给 `MEM_API_KEY`（见下）。

3. **stdout 被 MCP 协议占用**。这是最容易踩的一条：stdio 传输把
   stdout 当作 JSON-RPC 信道，任何 `print()` 混进去都会让客户端解析失败，
   症状是客户端报 "unexpected token" 或干脆显示连接失败，而服务端看着一切正常。
   `_init_sqlite()` 里就有 `print("[memorys] SQLite 补列：…")` 这类正常日志。
   所以启动期间把 stdout 整体重定向到 stderr，协议接管后再放回。

server 模式下的 stdio
---------------------
也支持，但必须给 `MEM_API_KEY`（或 `MEM_MCP_TOKEN`）—— server 模式是多用户的，
没有凭证就无法确定"当前用户是谁"。这里不做任何猜测（比如取第一个用户），
猜错的后果是把 A 的记忆读给 B。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys


async def _bootstrap_db() -> None:
    """做一次与 FastAPI lifespan 等价的初始化。"""
    from sqlalchemy import text  # noqa: F401  （_init_pg/_init_sqlite 内部要用）

    from . import dialect
    from .config import DB_BACKEND
    from .db import engine
    from .main import _init_pg, _init_sqlite
    from .models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if DB_BACKEND == dialect.PG:
            await _init_pg(conn)
        else:
            await _init_sqlite(conn)


def _require_credentials() -> tuple[str, str]:
    """server 模式下先确认凭证存在，再谈连库。

    顺序很重要，而且是实测纠正过来的：原来先 `_bootstrap_db()` 再查凭证，
    结果没配凭证时用户看到的是一大段 asyncpg 的
    `ConnectionRefusedError: Connect call failed ('127.0.0.1', 1)` 栈，
    真正的原因（少了 MEM_API_KEY）被埋在最后一行之外。
    凭证是纯环境变量检查，不需要数据库，就该先做。
    """
    key = os.environ.get("MEM_API_KEY", "").strip()
    token = os.environ.get("MEM_MCP_TOKEN", "").strip()
    if not key and not token:
        print(
            "memorys: server 模式的 stdio 需要凭证。\n"
            "  设置 MEM_API_KEY=hk_xxx（推荐，Web UI 的「API Keys」页可生成）\n"
            "  或 MEM_MCP_TOKEN=<JWT>\n"
            "  纯本地使用请改用 local 模式：MEM_MODE=local，什么都不用配。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return key, token


async def _resolve_auth(key: str = "", token: str = ""):
    """确定当前用户，返回 AuthContext。

    local 模式：单用户，自动开户（同时会建 git repo）。
    server 模式：用 _require_credentials() 已取到的凭证去认证。
    """
    from .config import IS_LOCAL
    from .db import SessionLocal
    from .security import authenticate_headers, local_context

    async with SessionLocal() as s:
        if IS_LOCAL:
            return await local_context(s)

        ctx = await authenticate_headers(
            f"Bearer {token}" if token else None, key or None, s
        )
        if ctx is None or getattr(ctx, "user", None) is None:
            print("memorys: 凭证无效，认证失败。", file=sys.stderr)
            raise SystemExit(2)
        return ctx


async def _amain() -> None:
    from .config import IS_LOCAL

    # 凭证检查放在建库之前：见 _require_credentials 的 docstring。
    key = token = ""
    if not IS_LOCAL:
        key, token = _require_credentials()

    # 初始化期间把 stdout 挪到 stderr：见模块 docstring 第 3 点。
    with contextlib.redirect_stdout(sys.stderr):
        await _bootstrap_db()
        ctx = await _resolve_auth(key, token)

        from .config import DB_BACKEND, settings
        from .mcp_server import current_auth, mcp

        current_auth.set(ctx)
        print(
            f"[memorys] stdio 就绪：user={ctx.user.username} "
            f"backend={DB_BACKEND} data={settings.data_dir}",
            file=sys.stderr,
            flush=True,
        )

    # 到这里 stdout 交还给 MCP 协议，不能再往里写任何东西。
    await mcp.run_stdio_async()


def main() -> None:
    try:
        asyncio.run(_amain())
    except (KeyboardInterrupt, EOFError):
        # 客户端关掉 stdin 就是正常退出信号，不该打栈
        pass


if __name__ == "__main__":
    main()
