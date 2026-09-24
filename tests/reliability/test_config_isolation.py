"""The test/development process must never implicitly borrow production secrets."""
import os
from pathlib import Path
import subprocess
import sys


def test_explicit_env_file_is_respected(tmp_path):
    env_file = tmp_path / 'isolated.env'
    env_file.write_text('MEM_SYNC_IDENTITY_NAME=isolated-fixture-name\n')
    env = dict(os.environ, MEM_ENV_FILE=str(env_file),
               MEM_DATABASE_URL='postgresql+asyncpg://fixture:fixture@127.0.0.1:1/fixture',
               MEM_JWT_SECRET='isolated-fixture-signing-key-never-production',
               MEM_EMBED_API_KEY='', MEM_EMBED_API_BASE='', MEM_GITHUB_REMOTE='',
               MEM_UPSTREAM_BASE='')
    env.pop('MEM_SYNC_IDENTITY_NAME', None)
    proc = subprocess.run([sys.executable, '-c',
                           'from app.config import settings; print(settings.sync_identity_name)'],
                          cwd=Path(__file__).resolve().parents[2], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == 'isolated-fixture-name'
