"""为真实账号（ai.xlingo.fun 的 xiao，identity_users.id=2）预建 memorys 用户并签发一个 API Key。

这样 Hermes（agent 侧）用这个 Key 写进来的记忆，用户在 Web UI 用同一账号登录后能看到同一个库。
用户首次通过 /api/v1/auth/ai-login 登录时会按 xiaoai_user_id upsert，命中这条已存在的记录。
"""
import asyncio
import sys

sys.path.insert(0, "/opt/memorys")

from sqlalchemy import select

from app import gitsvc
from app.config import settings
from app.db import SessionLocal
from app.models import ApiKey, User
from app.security import new_api_key

XIAOAI_UID = 2
USERNAME = "xiao"
KEY_NAME = "hermes-agent"


async def main():
    async with SessionLocal() as s:
        u = (await s.execute(
            select(User).where(User.xiaoai_user_id == XIAOAI_UID)
        )).scalar_one_or_none()
        if not u:
            u = User(xiaoai_user_id=XIAOAI_UID, username=USERNAME,
                     display_name="xiao", email="", role="admin")
            s.add(u)
            await s.commit()
            await s.refresh(u)
            created = True
        else:
            created = False

        gitsvc.ensure_repo(settings.data_dir, u.id)

        # 同名 Key 已存在就吊销重发，避免堆积
        old = (await s.execute(
            select(ApiKey).where(ApiKey.user_id == u.id, ApiKey.name == KEY_NAME,
                                 ApiKey.revoked_at.is_(None))
        )).scalars().all()
        raw, prefix, h = new_api_key()
        k = ApiKey(user_id=u.id, name=KEY_NAME, prefix=prefix, key_hash=h)
        s.add(k)
        await s.commit()
        await s.refresh(k)

    print(f"user_id={u.id} xiaoai_uid={u.xiaoai_user_id} username={u.username} created={created}")
    print(f"existing_same_name_keys={len(old)}")
    print(f"KEY={raw}")
    with open("/root/.memorys-hermes-key", "w") as f:
        f.write(raw)
    print("key 已写入 /root/.memorys-hermes-key（权限自行收紧）")


asyncio.run(main())
