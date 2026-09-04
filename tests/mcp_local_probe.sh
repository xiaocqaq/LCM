#!/usr/bin/env bash
# 实测本地 MCP：免鉴权 HTTP 能不能直接用、握手要不要 session、stdio 有没有入口
set -u
B=http://127.0.0.1:8650
CT='Content-Type: application/json'
AC='Accept: application/json, text/event-stream'

for i in $(seq 1 20); do
  curl -s -o /dev/null --max-time 2 "$B/api/health" && break
  sleep 1
done

echo "=== 1. 免鉴权 tools/list（不带任何 header 凭证）==="
curl -s --max-time 10 -X POST "$B/mcp/" -H "$CT" -H "$AC" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  > /tmp/mcp_tools.txt 2>&1
echo "  HTTP 体前 120 字: $(head -c 120 /tmp/mcp_tools.txt)"
echo "  工具数: $(grep -o '"name":"memory_[a-z_]*"' /tmp/mcp_tools.txt | sort -u | wc -l)"
echo "  工具名:"
grep -o '"name":"memory_[a-z_]*"' /tmp/mcp_tools.txt | sort -u | sed 's/"name"://;s/"//g;s/^/    /'

echo
echo "=== 2. 有没有 /mcp（不带斜杠）的重定向问题 ==="
echo -n "  POST /mcp  -> "
curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 -X POST "$B/mcp" -H "$CT" -H "$AC" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
echo -n "  POST /mcp/ -> "
curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 -X POST "$B/mcp/" -H "$CT" -H "$AC" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

echo
echo "=== 3. initialize 握手（客户端第一步）==="
curl -s -D /tmp/mcp_hdr.txt --max-time 10 -X POST "$B/mcp/" -H "$CT" -H "$AC" \
  -d '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  -o /tmp/mcp_init.txt
echo "  serverInfo: $(grep -o '"serverInfo":{[^}]*}' /tmp/mcp_init.txt)"
echo "  返回的 session id 头: $(grep -i 'mcp-session-id' /tmp/mcp_hdr.txt | tr -d '\r' || echo '（无）')"

echo
echo "=== 4. 真调一个工具（tools/call memory_search）==="
curl -s --max-time 15 -X POST "$B/mcp/" -H "$CT" -H "$AC" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_search","arguments":{"q":"本地模式","limit":2}}}' \
  > /tmp/mcp_call.txt 2>&1
echo "  返回前 200 字: $(head -c 200 /tmp/mcp_call.txt)"

echo
echo "=== 5. stdio 入口存在吗 ==="
cd /opt/memorys-local
if .venv/bin/python -c "import app.mcp_stdio" 2>/dev/null; then
  echo "  有 app.mcp_stdio"
else
  echo "  没有 app.mcp_stdio 模块"
fi
grep -rl 'run_stdio\|stdio_server' app/ 2>/dev/null | sed 's/^/  引用 stdio 的文件: /' || echo "  代码里没有任何 stdio 入口"

echo
echo "=== 6. system 端点看鉴权状态 ==="
curl -s --max-time 5 "$B/api/v1/system" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  mode=%s auth=%s' % (d['mode'], d['auth']))"
