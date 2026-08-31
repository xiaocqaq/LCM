"""上游（ai.xlingo.fun）token 认证路径的断言。

覆盖三种情形：
1. 已绑定用户的上游 token（无 iss）→ 直接放行，via=jwt-xiaoai，不回调上游
2. 未绑定的上游 token → 回调上游 /me 复核；伪造的 user_id 会被上游拒绝 → 401
3. 篡改签名 / 已过期 → 401

跑法：cd /opt/memorys && PYTHONPATH=/opt/memorys .venv/bin/python tests/upstream_token_test.py
"""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt as pyjwt

BASE = os.environ.get("MEM_TEST_BASE", "http://127.0.0.1:8649").rstrip("/")
FAIL = []


def check(name: str, cond, extra: object = ""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAIL.append(name)
    print(f"[{tag}] {name}{(' — ' + str(extra)) if extra else ''}")


def xiaoai_style_token(user_id: int, username: str, secret: str, *, ttl_min: int = 30) -> str:
    """模拟 xiaoai-chat 签发的 access token：同一 HS256 secret，没有 iss 声明。"""
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "user_id": user_id,
            "username": username,
            "role": "user",
            "session_id": "sess-test",
            "token_type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=ttl_min),
        },
        secret,
        algorithm="HS256",
    )


async def ensure_bound_user(username: str, xiaoai_uid: int) -> int:
    """直连 DB 造一个"已绑定"用户，模拟此前登录过一次的状态。"""
    from sqlalchemy import select

    from app import gitsvc
    from app.config import settings
    from app.db import SessionLocal
    from app.models import User

    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if not u:
            u = User(username=username, display_name=username, xiaoai_user_id=xiaoai_uid,
                     email=f"{username}@example.local", role="user")
            s.add(u)
            await s.commit()
        gitsvc.ensure_repo(settings.data_dir, u.id)
        return u.id


async def main():
    from app.config import settings

    secret = settings.jwt_secret
    bound_uid = 999501
    local_id = await ensure_bound_user("boundtest", bound_uid)
    print(f"已绑定用户 local_id={local_id} xiaoai_uid={bound_uid}")

    async with httpx.AsyncClient(timeout=30) as c:
        # --- 1. 已绑定用户的上游 token：直接放行，不该回调上游 ---
        tok = xiaoai_style_token(bound_uid, "boundtest", secret)
        r = await c.get(f"{BASE}/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"})
        body = r.json() if r.status_code == 200 else r.text
        check("已绑定用户的上游 token 放行", r.status_code == 200, body)
        check("认证来源标记为 jwt-xiaoai",
              isinstance(body, dict) and body.get("via") == "jwt-xiaoai",
              isinstance(body, dict) and body.get("via"))
        check("映射到正确的本地用户",
              isinstance(body, dict) and body.get("id") == local_id,
              isinstance(body, dict) and body.get("id"))

        # 能正常读写自己的库
        r = await c.post(f"{BASE}/api/v1/documents",
                         headers={"Authorization": f"Bearer {tok}"},
                         json={"title": f"上游 token 写入验证 {int(time.time())}", "type": "fact",
                               "content": "用 ai.xlingo.fun 的 access token 直接调 REST 写入的记忆。",
                               "project": "memorys"})
        check("上游 token 可写入", r.status_code == 200, r.status_code)
        r = await c.get(f"{BASE}/api/v1/search", params={"q": "上游 token"},
                        headers={"Authorization": f"Bearer {tok}"})
        n = len((r.json() or {}).get("results") or []) if r.status_code == 200 else 0
        check("上游 token 可检索", r.status_code == 200 and n > 0, f"n={n}")

        # --- 2. 未绑定的上游 token：回调上游 /me 复核 ---
        # 这里的 user_id 是编造的，上游根本没有这个会话，所以复核必然失败。
        # 真实用户拿真 token 过来时，这一步会通过并自动开户。
        fake = xiaoai_style_token(999888777, "__nosuchuser__", secret)
        r = await c.get(f"{BASE}/api/v1/auth/me", headers={"Authorization": f"Bearer {fake}"})
        detail = ""
        try:
            detail = (r.json() or {}).get("detail") or ""
        except Exception:
            detail = r.text
        check("未绑定+上游查不到 → 401", r.status_code == 401, r.status_code)
        check("错误提示指向上游校验", "上游校验" in str(detail), detail)

        # --- 3. 篡改签名 / 过期 ---
        bad = xiaoai_style_token(bound_uid, "boundtest", "wrong-secret-" + "x" * 32)
        r = await c.get(f"{BASE}/api/v1/auth/me", headers={"Authorization": f"Bearer {bad}"})
        check("签名被篡改的 token 拒绝", r.status_code == 401, r.status_code)

        expired = xiaoai_style_token(bound_uid, "boundtest", secret, ttl_min=-5)
        r = await c.get(f"{BASE}/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"})
        check("过期 token 拒绝", r.status_code == 401, r.status_code)

    print()
    if FAIL:
        print("❌ 失败项:", FAIL)
        raise SystemExit(1)
    print("✅ 上游 token 路径全部通过")


if __name__ == "__main__":
    asyncio.run(main())
