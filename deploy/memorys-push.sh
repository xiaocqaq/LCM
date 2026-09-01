#!/usr/bin/env bash
# memorys 定时推 GitHub。给 memorys-push.service 调用。
#
# 为什么走 REST 而不是直接 git push：
#   推送逻辑在 gitsvc.sync_to_github 里（分支按用户隔离、remote 的 {uid} 占位符替换、
#   HEAD:refs/heads/<branch> 的写法）。绕过它自己拼 git 命令，逻辑就有两份，
#   将来改一处忘一处。REST 打的是同一个入口，行为和你在网页上点「立即推送」完全一致。
#
# 多用户：每个用户一个 API key，逐个推。key 放在 /etc/memorys/push-keys
# （每行一个，# 开头是注释），没有该文件时退回单 key 文件。
set -uo pipefail

BASE="${MEM_PUSH_BASE:-http://127.0.0.1:8649}"
KEYS_FILE="${MEM_PUSH_KEYS:-/etc/memorys/push-keys}"
FALLBACK_KEY="/root/.memorys-hermes-key"
TIMEOUT=180

log(){ printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# 收集要推的 key
keys=()
if [ -r "$KEYS_FILE" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"; line="$(echo "$line" | tr -d '[:space:]')"
    [ -n "$line" ] && keys+=("$line")
  done < "$KEYS_FILE"
fi
if [ ${#keys[@]} -eq 0 ] && [ -r "$FALLBACK_KEY" ]; then
  k="$(tr -d '[:space:]' < "$FALLBACK_KEY")"
  [ -n "$k" ] && keys+=("$k")
fi
if [ ${#keys[@]} -eq 0 ]; then
  log "ERROR 没有可用的 API key（找过 $KEYS_FILE 和 $FALLBACK_KEY）"
  exit 1
fi

# 服务没起就别白跑，退非零让 systemd 记一笔
if ! curl -fsS --max-time 10 "$BASE/api/health" >/dev/null 2>&1; then
  log "ERROR memorys 服务不可用（$BASE/api/health）"
  exit 1
fi

fail=0
for key in "${keys[@]}"; do
  mask="${key:0:11}***"
  # 先把磁盘上的改动扫进索引再推。手工改过 md 文件的情况下这一步能兜住。
  curl -fsS --max-time $TIMEOUT -X POST \
    -H "X-Api-Key: $key" -H 'Content-Type: application/json' \
    -d '{"action":"reindex"}' "$BASE/api/v1/sync" >/dev/null 2>&1 \
    || log "WARN [$mask] reindex 失败，继续推送"

  out="$(curl -fsS --max-time $TIMEOUT -X POST \
        -H "X-Api-Key: $key" "$BASE/api/v1/sync/push" 2>&1)" || {
    log "ERROR [$mask] 推送请求失败：$(echo "$out" | head -c 200)"
    fail=$((fail+1)); continue
  }

  # ok 字段判成败。返回体里 error/hint 有诊断价值，原样带进日志。
  if echo "$out" | grep -q '"ok":[[:space:]]*true'; then
    log "OK [$mask] $(echo "$out" | grep -oP '"message":"\K[^"]+' || echo "$out" | head -c 160)"
  else
    log "FAIL [$mask] $(echo "$out" | head -c 300)"
    fail=$((fail+1))
  fi
done

[ $fail -gt 0 ] && { log "$fail 个用户推送失败"; exit 1; }
log "全部推送完成（${#keys[@]} 个用户）"
