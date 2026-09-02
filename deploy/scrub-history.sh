#!/usr/bin/env bash
# 上传到公开仓库前清洗 git 历史里的密钥。
#
# 为什么必须做：仓库里有两处真实凭据从第一个 commit 起就在
#   1. .env.bak-* 两个文件（被误跟踪）—— 含 MEM_JWT_SECRET 与 PG 账密
#   2. app/config.py 的默认值 —— 同一个 JWT secret（还是与上游账号体系共享的那个）
# 只改当前文件没用，`git log -p` 里照样能翻出来。
#
# 做法：filter-branch 逐个 commit 重写树 —— 删掉 .env.bak-*，把 config.py 里的
# 密钥字面量替换成空串。16 个 commit，几秒钟。
set -euo pipefail
cd /opt/memorys

SECRET_RE='REDACTED'
DBCRED_RE='REDACTED@127\.0\.0\.1:54321/memorys'

echo "重写前：$(git rev-list --count HEAD) 个 commit"

export FILTER_BRANCH_SQUELCH_WARNING=1
git filter-branch --force --prune-empty --tree-filter '
  rm -f .env.bak-* 2>/dev/null || true
  if [ -f app/config.py ]; then
    sed -i \
      -e "s|REDACTED||g" \
      -e "s|postgresql+asyncpg://REDACTED@127\.0\.0\.1:54321/memorys||g" \
      app/config.py
  fi
' --tag-name-filter cat -- --all

echo "重写后：$(git rev-list --count HEAD) 个 commit"

# filter-branch 留下的备份 ref 会让旧对象继续可达，必须清掉
rm -rf .git/refs/original
git reflog expire --expire=now --all
git gc --prune=now --aggressive --quiet

echo
echo "=== 校验：全历史扫描密钥 ==="
HITS=$(git rev-list --all | while read c; do
  git grep -I -l -E "$SECRET_RE|$DBCRED_RE" "$c" 2>/dev/null
done | sort -u)
if [ -n "$HITS" ]; then
  echo "❌ 仍有残留："
  echo "$HITS"
  exit 1
fi
echo "✅ 全部 commit 均无 JWT secret / PG 账密"

echo
echo "=== 校验：.env* 是否还在历史里 ==="
LEFT=$(git rev-list --all --objects | grep -E '\.env(\.bak|$)' | grep -v '\.env\.example' || true)
if [ -n "$LEFT" ]; then
  echo "❌ 仍有 .env 文件对象："
  echo "$LEFT"
  exit 1
fi
echo "✅ 历史中无 .env / .env.bak-*"
