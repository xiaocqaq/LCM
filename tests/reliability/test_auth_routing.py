from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import HTTPException
from app import security


@pytest.mark.asyncio
@pytest.mark.parametrize('claims', [
    {'iss': 'memorys', 'sub': 'missing', 'user_id': 12, 'token_type': 'access'},
    {'iss': 'memorys', 'sub': '7', 'user_id': 12, 'token_type': 'refresh'},
    {'iss': 'memorys', 'user_id': 12, 'token_type': 'access'},
    {'iss': 'other', 'user_id': 12, 'token_type': 'access'},
    {'user_id': [], 'token_type': 'access'},
    {'user_id': True, 'token_type': 'access'},
])
async def test_invalid_claims_are_401_without_upstream_fallback(claims):
    token = jwt.encode({**claims, 'exp': 4102444800}, security.settings.jwt_secret, algorithm='HS256')
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=7)), execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await security.authenticate_headers('Bearer ' + token, None, session)
    assert error.value.status_code == 401
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_local_user_never_maps_to_upstream():
    token = jwt.encode({'iss': 'memorys', 'sub': '7', 'user_id': 12, 'token_type': 'access', 'exp': 4102444800}, security.settings.jwt_secret, algorithm='HS256')
    session = SimpleNamespace(get=AsyncMock(return_value=None), execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await security.authenticate_headers('Bearer ' + token, None, session)
    assert error.value.status_code == 401
    session.execute.assert_not_awaited()
