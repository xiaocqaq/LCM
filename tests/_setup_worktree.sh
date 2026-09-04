#!/usr/bin/env bash
# 给 local-mode worktree 建独立 venv（不复用 /opt/memorys/.venv，那是生产的）
set -u
cd /opt/memorys-local || exit 1
python3 -m venv .venv || exit 1
.venv/bin/pip install -q -r requirements.txt 2>&1 | grep -v '^\[notice\]' | tail -3
.venv/bin/python -c "import fastapi,sqlalchemy,aiosqlite,jieba,mcp;print('deps ok')"
