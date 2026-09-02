#!/usr/bin/env bash
# 上传到公开仓库前清洗 git 历史里的密钥。
#
# 为什么必须做：改当前文件没用，`git log -p` 里照样翻得出来。
# 本仓库历史中的三处凭据：
#   1. .env.bak-*        两个被误跟踪的备份，含 JWT secret 与 PG 账密
#   2. app/config.py     同一个 JWT secret 写成了字段默认值
#   3. README.md         早期版本贴了完整 PG 连接串（含账密）
#
# 用法：
#   deploy/scrub-history.sh /path/to/secrets.txt
# secrets.txt 每行一个要清除的字面量（空行与 # 开头忽略）。
# 该文件自己**不要**进 git —— 这也是本脚本不硬编码密钥的原因：
# 早先版本把 secret 写在脚本里当 grep 模式，等于清完历史又在新 commit 里泄一次。
set -euo pipefail
cd "$(dirname "$0")/.."

SECRETS_FILE="${1:?用法: $0 <secrets.txt>}"
[ -f "$SECRETS_FILE" ] || { echo "找不到 $SECRETS_FILE"; exit 1; }

mapfile -t SECRETS < <(grep -vE '^\s*(#|$)' "$SECRETS_FILE")
[ "${#SECRETS[@]}" -gt 0 ] || { echo "$SECRETS_FILE 里没有内容"; exit 1; }
echo "将清除 ${#SECRETS[@]} 个字面量（值不打印）"

# 生成给 tree-filter 用的 sed 脚本。放在 /dev/shm 避免落盘。
SED_SCRIPT="$(mktemp /dev/shm/scrub-sed.XXXXXX)"
trap 'rm -f "$SED_SCRIPT"' EXIT
for s in "${SECRETS[@]}"; do
  # 转义 sed 分隔符与元字符
  esc=$(printf '%s' "$s" | sed -e 's/[][\.*^$/&|]/\\&/g')
  printf 's|%s|REDACTED|g\n' "$esc" >> "$SED_SCRIPT"
done

echo "重写前：$(git rev-list --count HEAD) 个 commit"
export FILTER_BRANCH_SQUELCH_WARNING=1
export SED_SCRIPT

git filter-branch --force --prune-empty --tree-filter '
  rm -f .env.bak-* 2>/dev/null || true
  for f in app/config.py README.md deploy/scrub-history.sh; do
    [ -f "$f" ] && sed -i -f "$SED_SCRIPT" "$f" || true
  done
' --tag-name-filter cat -- --all

echo "重写后：$(git rev-list --count HEAD) 个 commit"

# filter-branch 的备份 ref 会让旧对象继续可达，必须清掉再 gc
rm -rf .git/refs/original
git reflog expire --expire=now --all
git gc --prune=now --quiet

echo
echo "=== 校验：逐个 commit 扫全部字面量 ==="
BAD=0
for s in "${SECRETS[@]}"; do
  hits=$(git rev-list --all | while read -r c; do
    git grep -I -l -F "$s" "$c" 2>/dev/null
  done | sort -u)
  if [ -n "$hits" ]; then
    echo "❌ 仍有残留（${s:0:6}…）："
    echo "$hits"
    BAD=1
  fi
done
[ "$BAD" -eq 0 ] && echo "✅ 全部 commit 均无残留"

echo
echo "=== 校验：.env* 是否还在历史里 ==="
LEFT=$(git rev-list --all --objects | grep -E '\.env' | grep -v '\.env\.example' || true)
if [ -n "$LEFT" ]; then
  echo "❌ 仍有 .env 对象："; echo "$LEFT"; BAD=1
else
  echo "✅ 历史中无 .env / .env.bak-*"
fi

exit "$BAD"
