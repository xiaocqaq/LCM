"""把 MCP server 包成带鉴权的 ASGI 子应用，挂到 FastAPI 的 /mcp。

流程：ASGI 中间件先从请求头取 Authorization/X-Api-Key 做认证，
把 AuthContext 塞进 contextvar，再交给 MCP 的 streamable HTTP app 处理。
未认证直接返回 401，不进 MCP 协议层。
"""
from __future__ import annotations

import json

from .db import SessionLocal
from .mcp_server import current_auth, mcp
from .security import authenticate_headers

_inner = mcp.streamable_http_app()
session_manager = mcp.session_manager

AUTH_HINT = "MCP 需要认证：请配置请求头 Authorization: Bearer <token> 或 X-Api-Key: <key>"


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _send_401(send, msg: str) -> None:
    body = json.dumps({"detail": msg}, ensure_ascii=False).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", b'Bearer realm="memorys"'),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _normalize(scope):
    """FastAPI mount 会把 /mcp 前缀剥掉，POST /mcp 到内层就成了空路径，
    内层 streamable_http_path="/" 于是回 307 重定向到 /mcp/。
    MCP 客户端一般不跟重定向（且 POST 跟随会丢 body），所以这里直接补成 "/"。
    """
    if not scope.get("path"):
        scope = dict(scope)
        scope["path"] = "/"
        raw = scope.get("raw_path")
        if raw is not None and not raw.endswith(b"/"):
            scope["raw_path"] = raw + b"/"
    return scope


async def mcp_asgi_app(scope, receive, send):
    if scope["type"] != "http":
        await _inner(scope, receive, send)
        return

    scope = _normalize(scope)
    h = _headers(scope)
    try:
        async with SessionLocal() as s:
            ctx = await authenticate_headers(h.get("authorization"), h.get("x-api-key"), s)
    except Exception as e:
        detail = getattr(e, "detail", None) or str(e) or "认证失败"
        # 401 必须说清是"哪种凭证"没通过，否则排查时完全看不出方向。
        #
        # 实测踩到的场景：local 模式明明免鉴权，客户端却偶发 401 —— 原因是
        # 客户端**带了**一个过期/无效的 Authorization 头（比如上一次连别的
        # 实例时缓存下来的），而 authenticate_headers 的规则是"带了凭证就必须
        # 验证通过"，于是免鉴权这条路根本不会走到。
        # 原来的信息只有"认证失败"，看不出客户端其实发了凭证，
        # 白排查了很久服务端并发和实例状态。
        got = []
        if h.get("authorization"):
            got.append("Authorization")
        if h.get("x-api-key"):
            got.append("X-Api-Key")
        why = f"（客户端发来的凭证：{', '.join(got)}）" if got else "（客户端没发任何凭证）"
        await _send_401(send, f"MCP 认证失败：{detail}{why}")
        return

    if ctx is None or getattr(ctx, "user", None) is None:
        await _send_401(send, AUTH_HINT)
        return

    token = current_auth.set(ctx)
    try:
        await _inner(scope, receive, send)
    finally:
        current_auth.reset(token)
