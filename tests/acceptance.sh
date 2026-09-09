#!/usr/bin/env bash
# memorys 全量验收：十六套测试（单测本地 + 其余打公网 HTTPS）+ 服务状态 + 公网端点 + Hermes 侧闭环
#
# 本地模式（SQLite）的两套是有条件跑的：
#   local_parity  —— 纯函数层随时可跑；端到端层要 SQLITE_BASE 有实例才比
#   local_mode    —— 需要本地实例在跑，没有就跳过（不算失败）
# 起本地实例：
#   MEM_MODE=local MEM_LOCAL_HOME=/tmp/memlocal-test \
#     .venv/bin/python -m uvicorn app.main:app --port 8650
# 用脚本自己的位置定位项目根，不写死 /opt/memorys。
# 写死的后果实测踩到了：在 git worktree（/opt/memorys-local）里跑这个脚本，
# 它 cd 回 /opt/memorys 去找那边不存在的测试文件，
# 于是新加的三套全部报 "can't open file" —— 而下面的 run 还照样打 FAIL=0。
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT" || exit 1
export PYTHONPATH="$ROOT"
export MEM_TEST_BASE=https://repo.xlingo.fun
KEY=$(cat /root/.memorys-hermes-key)
SQLITE_BASE=${SQLITE_BASE:-http://127.0.0.1:8650}
HARNESS_ERRORS=0

# 有几套（e2e / mcp / vec_search / local_parity 的 PG 对照…）要连生产 PG，
# 配置在部署目录的 .env 里。在 git worktree 里跑时本目录没有 .env，
# 那些套件会以"缺少必填配置 MEM_DATABASE_URL"中止 ——
# 修好 run 的退出码判定之前，这种中止会被打成 FAIL=0 ✅ 而完全看不见。
#
# 只在本目录确实没有 .env 时才借用部署目录的，绝不覆盖已有配置。
if [ ! -f "$ROOT/.env" ] && [ -f /opt/memorys/.env ]; then
  export MEM_ENV_FILE=/opt/memorys/.env
  set -a; . /opt/memorys/.env; set +a
  echo "（本目录无 .env，已借用 /opt/memorys/.env 跑需要 PG 的套件）"
fi

run() {
  local name="$1"; shift
  local out rc fails
  out=$("$@" 2>&1); rc=$?
  fails=$(echo "$out" | grep -c '^\[FAIL\]')

  # 退出码必须参与判定。
  #
  # 原来只数 [FAIL] 行数，于是"进程根本没跑起来"（文件不存在、import 失败、
  # 语法错误）会打印 FAIL=0 —— 一个永远不会红的测试等于没有测试。
  # 实测就是这么漏掉三套的：文件路径错了，输出是
  # "can't open file ...: [Errno 2]"，里面没有 [FAIL] 字样，照样 FAIL=0 ✅。
  if [ "$rc" -ne 0 ] && [ "$fails" -eq 0 ]; then
    HARNESS_ERRORS=$((HARNESS_ERRORS + 1))
    printf '%-22s ERROR   退出码 %s（没跑起来，不是断言失败）：%s\n' \
      "$name" "$rc" "$(echo "$out" | tail -1)"
    return
  fi
  if [ "$fails" -gt 0 ]; then
    HARNESS_ERRORS=$((HARNESS_ERRORS + 1))
  fi
  printf '%-22s FAIL=%s  %s\n' "$name" "$fails" "$(echo "$out" | tail -1)"
}

echo "===== 测试套件 ====="
run search_unit_test    .venv/bin/python tests/search_unit_test.py
run recall_public       .venv/bin/python tests/recall_public.py
run e2e_test            .venv/bin/python tests/e2e_test.py
run mcp_test            .venv/bin/python tests/mcp_test.py
run upstream_token_test .venv/bin/python tests/upstream_token_test.py
run public_mcp_test     .venv/bin/python tests/public_mcp_test.py "$KEY"
run git_push_test       .venv/bin/python tests/git_push_test.py
run branch_test         .venv/bin/python tests/branch_test.py
run branch_write_test   .venv/bin/python tests/branch_write_test.py
run bootstrap_budget    .venv/bin/python tests/bootstrap_budget_test.py
run rank_test           .venv/bin/python tests/rank_test.py
run vec_search          .venv/bin/python tests/vec_search_test.py
run links_test          .venv/bin/python tests/links_test.py
run smoke_ready         .venv/bin/python tests/smoke_ready.py

echo
echo "===== 本地模式（SQLite）====="
# 纯函数层 + PG 数值对照。这一套不依赖本地实例，任何时候都该绿
run local_parity        env PG_BASE=https://repo.xlingo.fun SQLITE_BASE="$SQLITE_BASE" \
                          .venv/bin/python tests/local_parity.py
if curl -s -o /dev/null --max-time 3 "$SQLITE_BASE/api/health"; then
  run local_mode        env BASE="$SQLITE_BASE" .venv/bin/python tests/local_mode_test.py
else
  printf '%-22s SKIP    本地实例未运行（%s）\n' "local_mode" "$SQLITE_BASE"
fi
# stdio 自己起子进程、自己用临时目录，不依赖任何在跑的实例 → 无条件跑
run mcp_stdio           .venv/bin/python tests/mcp_stdio_test.py
# 按项目软删 + 清空回收站 + favicon：需要本地实例（跟 local_mode 同一条件）
if curl -s -o /dev/null --max-time 3 "$SQLITE_BASE/api/health"; then
  run project_trash     env BASE="$SQLITE_BASE" DATA_DIR="${MEM_LOCAL_HOME:-/root/.memorys-demo}" \
                          .venv/bin/python tests/project_trash_test.py
fi

echo
echo "===== 向量覆盖率 ====="
.venv/bin/python tests/backfill_vectors.py --dry-run 2>&1 | head -2

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

echo
echo "===== 总判定 ====="
if [ "$HARNESS_ERRORS" -eq 0 ]; then
  echo "全部通过（无失败断言、无未跑起来的套件）"
  exit 0
fi
echo "有 $HARNESS_ERRORS 套存在失败或没跑起来 —— 往上翻 FAIL=/ERROR 行"
exit 1
