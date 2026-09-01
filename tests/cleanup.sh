#!/usr/bin/env bash
# 清掉测试产生的用户/文档/分支/md 残留，保留真实数据。
# 测试跑完建议执行一次：bash tests/cleanup.sh
set -u
KEY=$(cat /root/.memorys-hermes-key)
BASE=https://repo.xlingo.fun
DATA=/var/lib/memorys/data/users
REAL_UID=u20        # 真实用户，其余目录都是测试产物
PATTERNS='公网-MCP|分支测试|只存在于分支|dd-a-|dd-b-|ui-demo|ui-w-|分支写入验证|UI分支写入|另存基线'

echo "=== 1. 切回主分支 ==="
curl -s -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"main"}' "$BASE/api/v1/branches/switch" >/dev/null
echo "current=$(curl -s -H "X-Api-Key: $KEY" "$BASE/api/v1/branches" \
  | grep -oP '"current":"\K[^"]+')"

echo "=== 2. 删测试分支（保留 main）==="
for b in $(curl -s -H "X-Api-Key: $KEY" "$BASE/api/v1/branches" \
           | grep -oP '"name":"\K[^"]+' | grep -vx main); do
  curl -s -X DELETE -H "X-Api-Key: $KEY" "$BASE/api/v1/branches/$b?force=true" >/dev/null
  echo "  已删分支 $b"
done

echo "=== 3. 清 DB（测试账号 + 测试标题）==="
PGPASSWORD=memorys psql -h 127.0.0.1 -p 54321 -U memorys -d memorys -q <<'SQL'
BEGIN;
DELETE FROM chunks WHERE document_id IN (
  SELECT d.id FROM documents d JOIN users u ON u.id=d.user_id
  WHERE u.xiaoai_user_id BETWEEN 999000 AND 999999);
DELETE FROM documents WHERE user_id IN (
  SELECT id FROM users WHERE xiaoai_user_id BETWEEN 999000 AND 999999);
DELETE FROM api_keys WHERE user_id IN (
  SELECT id FROM users WHERE xiaoai_user_id BETWEEN 999000 AND 999999);
DELETE FROM users WHERE xiaoai_user_id BETWEEN 999000 AND 999999;
DELETE FROM chunks WHERE document_id IN (
  SELECT id FROM documents WHERE title ~ '公网 MCP|分支测试|只存在于分支|分支写入验证|UI分支写入|另存基线|上游 token');
DELETE FROM documents WHERE title ~ '公网 MCP|分支测试|只存在于分支|分支写入验证|UI分支写入|另存基线|上游 token';
COMMIT;
SQL

echo "=== 4. 清磁盘 md 残留（git rm 保留可回溯）==="
cd "$DATA/$REAL_UID" 2>/dev/null || { echo "跳过：$REAL_UID 不存在"; exit 0; }
n=0
for f in $(ls -1 main/ 2>/dev/null | grep -E "$PATTERNS"); do
  git rm -q --cached "main/$f" 2>/dev/null
  rm -f "main/$f"
  n=$((n+1))
done
[ $n -gt 0 ] && git -c user.name=memorys -c user.email=memorys@local \
  commit -q -m "cleanup: 移除测试产生的临时记忆" && echo "  清掉 $n 个文件并提交"
[ $n -eq 0 ] && echo "  无残留"

echo "=== 5. 清测试用户目录 ==="
cd "$DATA" && for d in u*; do
  [ "$d" = "$REAL_UID" ] || { rm -rf "$d" && echo "  已删 $d"; }
done

echo "=== 6. 重建索引 ==="
curl -s -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"action":"reindex"}' "$BASE/api/v1/sync"; echo

echo "=== 最终状态 ==="
PGPASSWORD=memorys psql -h 127.0.0.1 -p 54321 -U memorys -d memorys -tAc \
  "SELECT (SELECT count(*) FROM users)||' users / '||(SELECT count(*) FROM documents)||' docs'"
ls -1 "$DATA/$REAL_UID/main/"
