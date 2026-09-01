"""给指定用户签一个网页登录用的 JWT，供浏览器免密调试。

用法：PYTHONPATH=/opt/memorys .venv/bin/python tests/mktok.py [user_id]
默认 user_id=20（xiao）。把输出塞进 localStorage.mem_token 即可直接进 UI。
"""
import asyncio
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.security import issue_local_jwt

UID = int(sys.argv[1]) if len(sys.argv) > 1 else 20


async def main():
    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.id == UID))).scalar_one()
        tok, _ = issue_local_jwt(u)
        print(tok)


asyncio.run(main())
