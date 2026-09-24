"""TEST ONLY: loopback identity-provider fixture for browser login acceptance.
Not an alternative production authentication endpoint. Never deploy this script.
"""
import os
from fastapi import FastAPI, HTTPException
import jwt
from datetime import datetime, timedelta, timezone

if os.environ.get('MEM_PREVIEW_FIXTURE') != '1':
    raise RuntimeError('This test fixture requires MEM_PREVIEW_FIXTURE=1')
app = FastAPI()
PROFILE = {'id': 990024, 'username': 'preview', 'displayName': '隔离验收账号', 'role': 'user', 'email': ''}

@app.post('/api/v1/auth/login')
async def login(payload: dict):
    if payload != {'username': 'preview', 'password': 'preview-only-test'}:
        raise HTTPException(401, 'Invalid test credentials')
    now = datetime.now(timezone.utc)
    token = jwt.encode({'user_id':PROFILE['id'],'token_type':'access','iat':now,
                        'exp':now+timedelta(hours=2)}, os.environ['MEM_JWT_SECRET'], algorithm='HS256')
    return {'data': {'accessToken':token,'user':PROFILE}}

@app.get('/api/v1/me')
async def me():
    return {'data':PROFILE}
