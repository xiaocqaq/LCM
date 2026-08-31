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


async def mcp_asgi_app(scope, receive, send):
    if scope["type"] != "http":
        await _inner(scope, receive, send)
        return

    h = _headers(scope)
    try:
        async with SessionLocal() as s:
            ctx = await authenticate_headers(h.get("authorization"), h.get("x-api-key"), s)
    except Exception as e:
        detail = getattr(e, "detail", None) or str(e) or "认证失败"
        await _send_401(send, f"MCP 认证失败：{detail}")
        return

    if ctx is None or getattr(ctx, "user", None) is None:
        await _send_401(send, AUTH_HINT)
        return

    token = current_auth.set(ctx)
    try:
        await _inner(scope, receive, send)
    finally:
        current_auth.reset(token)
