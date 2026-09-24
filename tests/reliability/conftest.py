"""Safe-by-default regression suite: no production config, DB or embedding calls."""
import os
from pathlib import Path
import tempfile

# Apply BEFORE application module collection. Explicit test DB lives in a separate variable.
_TEMP = tempfile.TemporaryDirectory(prefix='memorys-regression-')
os.environ['MEM_ENV_FILE'] = ''
os.environ['MEM_DATABASE_URL'] = os.environ.get(
    'MEM_TEST_DATABASE_URL', 'postgresql+asyncpg://fixture:fixture@127.0.0.1:1/memorys_test')
os.environ['MEM_DATA_DIR'] = _TEMP.name
os.environ['MEM_JWT_SECRET'] = 'isolated-test-secret-not-valid-for-production-2026'
os.environ['MEM_EMBED_API_BASE'] = ''
os.environ['MEM_EMBED_API_KEY'] = ''
os.environ['MEM_GITHUB_REMOTE'] = ''
os.environ['MEM_UPSTREAM_BASE'] = ''
os.environ['MEM_ROOT_PATH'] = ''
os.environ['MEM_MCP_ALLOWED_HOSTS'] = '127.0.0.1:*,localhost:*,testserver'


def pytest_sessionfinish(session, exitstatus):
    _TEMP.cleanup()
