"""认证：三种凭据 → AuthContext
1. 本地 JWT（iss=memorys，登录/ai-login 签发，12h）
2. ai.xlingo.fun 的 access token（同一 HS256 secret，无 iss，直接验签后映射到已绑定用户）
3. API Key（X-API-Key: hk_xxx，给 agent/脚本用）
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import ApiKey, User

ISSUER = "memorys"
ALG = "HS256"


@dataclass
class AuthContext:
    user: User
    via: str  # jwt-local | jwt-xiaoai | api-key


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """返回 (完整key, 前缀展示, hash)"""
    raw = "hk_" + secrets.token_hex(20)
    return raw, raw[:11], hash_key(raw)


def issue_local_jwt(user: User) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=settings.access_ttl_minutes)
    payload = {
        "iss": ISSUER,
        "sub": str(user.id),
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "token_type": "access",
        "iat": now,
        "exp": exp,
    }
    token = pyjwt.encode(payload, settings.jwt_secret, algorithm=ALG)
    return token, exp


def _decode(token: str, require_issuer: bool) -> dict:
    opts = {"require": ["exp"]}
    kwargs = {}
    if require_issuer:
        kwargs["issuer"] = ISSUER
        opts["require"] = ["exp", "iss"]
    return pyjwt.decode(token, settings.jwt_secret, algorithms=[ALG], options=opts, **kwargs)


async def authenticate_headers(authorization: str | None, api_key: str | None, session: AsyncSession) -> AuthContext:
    """供 FastAPI 依赖与 MCP 中间件共用的鉴权核心。"""
    if api_key:
        rec = (
            await session.execute(
                select(ApiKey).where(ApiKey.key_hash == hash_key(api_key.strip()), ApiKey.revoked_at.is_(None))
            )
        ).scalar_one_or_none()
        if rec:
            user = await session.get(User, rec.user_id)
            if user:
                await session.execute(update(ApiKey).where(ApiKey.id == rec.id).values(last_used_at=datetime.now(timezone.utc)))
                await session.commit()
                return AuthContext(user=user, via="api-key")
        raise HTTPException(401, "无效的 API Key")

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        # 1) 本地 JWT
        try:
            claims = _decode(token, require_issuer=True)
            user = await session.get(User, int(claims["sub"]))
            if user:
                return AuthContext(user=user, via="jwt-local")
        except pyjwt.PyJWTError:
            pass
        # 2) xiaoai token（无 iss）
        try:
            claims = _decode(token, require_issuer=False)
            if claims.get("token_type") == "access" and claims.get("user_id") is not None:
                user = (
                    await session.execute(select(User).where(User.xiaoai_user_id == int(claims["user_id"])))
                ).scalar_one_or_none()
                if user:
                    return AuthContext(user=user, via="jwt-xiaoai")
                raise HTTPException(401, "该 ai.xlingo.fun 账号尚未绑定知识库，请先用账号密码登录一次完成绑定")
        except pyjwt.PyJWTError:
            pass
        raise HTTPException(401, "登录已过期或 token 无效")

    raise HTTPException(401, "未认证：请带 Authorization: Bearer <token> 或 X-API-Key")
