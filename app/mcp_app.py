"""把 MCP server 包成带鉴权的 ASGI 子应用，挂到 FastAPI 的 /mcp。

流程：ASGI 中间件先从请求头取 Authorization/X-Api-Key 做认证，
把 AuthContext 塞进 contextvar，再交给 MCP 的 streamable HTTP app 处理。
未认证直接返回 401，不进 MCP 协议层。
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import HTTPException

logger = logging.getLogger(__name__)

from .db import SessionLocal
from .mcp_server import current_auth, mcp
from .security import authenticate_headers

_inner = mcp.streamable_http_app()
session_manager = mcp.session_manager

AUTH_HINT = "MCP 需要认证：请配置请求头 Authorization: Bearer <token> 或 X-Api-Key: <key>"


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _send_error(send, status: int, msg: str, request_id: str) -> None:
    body = json.dumps({"detail": msg, "requestId": request_id}, ensure_ascii=False).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"x-request-id", request_id.encode()),
            *([(b"www-authenticate", b'Bearer realm="memorys"')] if status == 401 else []),
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
    request_id = uuid.uuid4().hex
    try:
        async with SessionLocal() as s:
            ctx = await authenticate_headers(h.get("authorization"), h.get("x-api-key"), s)
    except Exception as e:
        if isinstance(e, HTTPException) and e.status_code == 401:
            await _send_error(send, 401, AUTH_HINT, request_id)
        else:
            logger.exception("MCP authentication unavailable request_id=%s", request_id)
            await _send_error(send, 503, "认证服务暂不可用", request_id)
        return

    if ctx is None or getattr(ctx, "user", None) is None:
        await _send_error(send, 401, AUTH_HINT, request_id)
        return

    token = current_auth.set(ctx)
    try:
        await _inner(scope, receive, send)
    finally:
        current_auth.reset(token)
