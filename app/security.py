"""认证：三种凭据 → AuthContext
1. 本地 JWT（iss=memorys，登录/ai-login 签发，12h）
2. ai.xlingo.fun 的 access token（同一 HS256 secret，无 iss，直接验签后映射到已绑定用户）
3. API Key（X-API-Key: hk_***，agent/脚本用）

local 模式多一条零号路径：什么都不带 → 单用户上下文（见 local_context）。
那条路径的安全边界完全由「只监听 127.0.0.1」保证，见 app/local.py 的 _guard_bind。
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import IS_LOCAL, settings
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


async def authenticate_headers(authorization: str | None, api_key: str | None,
                               session: AsyncSession) -> AuthContext:
    """供 FastAPI 依赖与 MCP 中间件共用的鉴权核心。

    local 模式（MEM_MODE=local 且 MEM_LOCAL_OPEN=true）走 local_context()：
    没有凭据也直接放行成单用户。带了凭据仍走正常校验 —— 本地建了 API Key 的人
    通常是想给别的机器用，那种情况下凭据必须真的被验证。
    """
    if not api_key and not (authorization or "").strip():
        if IS_LOCAL and settings.local_open:
            return await local_context(session)
        raise HTTPException(401, "未认证：请带 Authorization: Bearer <token> 或 X-API-Key")

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
        # local 模式下没有上游可回调，到这里就该结束 —— 继续往下走会去
        # 请求一个空的 upstream_base，报出让人莫名其妙的连接错误
        if IS_LOCAL and not settings.upstream_base:
            raise HTTPException(401, "token 无效或已过期（本地模式没有上游账号体系）")
        # 2) xiaoai token（无 iss）
        try:
            claims = _decode(token, require_issuer=False)
        except pyjwt.PyJWTError:
            claims = None
        if claims and claims.get("token_type") == "access" and claims.get("user_id") is not None:
            user = (
                await session.execute(select(User).where(User.xiaoai_user_id == int(claims["user_id"])))
            ).scalar_one_or_none()
            if user:
                return AuthContext(user=user, via="jwt-xiaoai")
            # 首次带 ai.xlingo.fun 的 token 过来：验签已过，说明 token 确实是上游签的，
            # 但签名有效不等于会话还在（可能已登出/被吊销），所以回调上游 /me 复核，
            # 通过后直接开户，避免逼用户再去 Web UI 用密码登录一次。
            from . import service
            from .upstream import UpstreamAuthError, upstream_me
            try:
                profile = await upstream_me(token)
            except UpstreamAuthError as e:
                raise HTTPException(e.status, f"上游校验未通过：{e.message}")
            new_user = await service.upsert_user_from_xiaoai(session, profile)
            return AuthContext(user=new_user, via="jwt-xiaoai")
        raise HTTPException(401, "登录已过期或 token 无效")

    raise HTTPException(401, "未认证：请带 Authorization: Bearer <token> 或 X-API-Key")


# 本地单用户的 id 缓存。
#
# 不缓存的话每个请求都要 SELECT 一次 users —— 本地模式下这个用户永远存在、
# 永远是同一行，查了也是白查。缓存 id 而不是 ORM 对象：
# 对象跨 session 复用会带着过期的 identity map，改了 role 之类的字段读不到新值。
_local_uid: int | None = None


async def local_context(session: AsyncSession) -> AuthContext:
    """local 模式的单用户上下文，不存在就现开一个。

    xiaoai_user_id 用 0：那一列有 unique 约束且是 NOT NULL，本地模式没有上游 id，
    0 是个不会跟任何真实上游 id 撞的哨兵值（上游 id 从 1 开始）。
    """
    global _local_uid
    if _local_uid is not None:
        u = await session.get(User, _local_uid)
        if u:
            return AuthContext(user=u, via="local")
        _local_uid = None      # 库被换掉/重建了，重新查

    name = settings.local_user or "local"
    u = (await session.execute(select(User).where(User.username == name))).scalar_one_or_none()
    if not u:
        u = User(xiaoai_user_id=0, username=name, display_name=name, role="admin")
        session.add(u)
        await session.commit()
        await session.refresh(u)
    # git repo 必须在这里建。
    #
    # server 模式是在 upsert_user_from_xiaoai() 里建的（登录时），而本地单用户
    # 根本不走登录 —— 漏了这一步的后果很隐蔽：写文档一切正常（md 确实落盘了），
    # 但 try_commit_all 内部 catch 掉所有异常，所以「git 提交」静默不发生，
    # 版本历史永远是空的，而界面上完全看不出问题。实测就是这么发现的。
    #
    # ensure_repo 幂等，且 _local_uid 缓存让这段每个进程只跑一次。
    from . import gitsvc
    try:
        gitsvc.ensure_repo(settings.data_dir, u.id)
    except Exception:
        # 建 repo 失败不该挡住服务启动：没有 git 只是丢版本历史，
        # md 文件和检索都照常工作
        pass
    _local_uid = u.id
    return AuthContext(user=u, via="local")
