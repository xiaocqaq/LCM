import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from app import mcp_app


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,status', [(RuntimeError('postgres password=secret'), 503), (HTTPException(401, 'bad credential secret'), 401), (HTTPException(502, 'upstream private address'), 503)])
async def test_auth_failure_status_and_safe_request_id(monkeypatch, caplog, failure, status):
    @asynccontextmanager
    async def session():
        yield object()
    monkeypatch.setattr(mcp_app, 'SessionLocal', session)
    monkeypatch.setattr(mcp_app, 'authenticate_headers', AsyncMock(side_effect=failure))
    messages = []
    async def send(message):
        messages.append(message)
    await mcp_app.mcp_asgi_app({'type': 'http', 'path': '/', 'headers': []}, AsyncMock(), send)
    assert messages[0]['status'] == status
    body = json.loads(messages[1]['body'])
    assert 'secret' not in str(body) and 'private address' not in str(body)
    request_id = dict(messages[0]['headers'])[b'x-request-id'].decode()
    assert request_id
    assert body['requestId'] == request_id
    if status == 503:
        assert request_id in caplog.text
