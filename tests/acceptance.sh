#!/usr/bin/env bash
# memorys 全量验收：七套测试（单测本地 + 其余打公网 HTTPS）+ 服务状态 + 公网端点 + Hermes 侧闭环
cd /opt/memorys || exit 1
export PYTHONPATH=/opt/memorys
export MEM_TEST_BASE=https://repo.xlingo.fun
KEY=$(cat /root/.memorys-hermes-key)

run() {
  local name="$1"; shift
  local out
  out=$("$@" 2>&1)
  printf '%-22s FAIL=%s  %s\n' "$name" "$(echo "$out" | grep -c '^\[FAIL\]')" "$(echo "$out" | tail -1)"
}

echo "===== 测试套件 ====="
run search_unit_test    .venv/bin/python tests/search_unit_test.py
run recall_public       .venv/bin/python tests/recall_public.py
run e2e_test            .venv/bin/python tests/e2e_test.py
run mcp_test            .venv/bin/python tests/mcp_test.py
run upstream_token_test .venv/bin/python tests/upstream_token_test.py
run public_mcp_test     .venv/bin/python tests/public_mcp_test.py "$KEY"
run git_push_test       .venv/bin/python tests/git_push_test.py

echo
echo "===== 服务 / 自启 ====="
printf 'enabled=%s active=%s\n' "$(systemctl is-enabled memorys)" "$(systemctl is-active memorys)"

echo
echo "===== 公网端点 ====="
for p in /api/health / /api/docs; do
  printf '%-14s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' "https://repo.xlingo.fun$p")"
done
printf '%-14s %s\n' "/mcp(noauth)" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST https://repo.xlingo.fun/mcp \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}')"
printf '%-14s %s\n' "/mcp(key)" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST https://repo.xlingo.fun/mcp \
      -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')"

echo
echo "===== Hermes 侧闭环 ====="
hermes mcp test memorys 2>&1 | grep -E 'Connected|Tools discovered|failed'
