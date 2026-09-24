# 可靠性与前端改进：验收及发布说明

## 范围与状态

本批以生产 `main` 为基准，在独立分支 `improvement/reliability-ui` 开发。**未改生产目录、未重启生产服务、未推送或发布 GitHub。** `local-mode`（SQLite）分支没有被覆盖，后续合并应单独运行 SQLite 验收，不应复制整份 PostgreSQL service.py。

## 修改内容

- JWT 严格按 issuer 分流，本地用户不存在不再回落上游身份；畸形 claims 返回认证错误。
- MCP 凭据错误与依赖故障分开返回 401/503，故障带请求编号，不向客户端暴露底层连接错误。
- `/api/health` 继续作为存活检查，新增 `/api/ready` 检查数据库与数据目录。
- Markdown 同步完整映射正文、标题、项目、类型、标签、重要度、关系及扩展字段；恢复路径使用磁盘新正文；保留 frontmatter 的编辑时间。
- 文件原子替换，数据库提交前失败补偿触碰过的文件；Git 失败记录日志并返回持久化状态。
- 按用户仓库串行处理变更，锁文件在仓库之外；阻塞 Git 操作离开事件循环。取消请求时持锁等待工作线程结束，切分支/拉取/合并完成后再同步索引。
- 新增完整文档 `revision`，Web UI 使用 `expectedRevision`，MCP 使用 `expected_revision` 检查并发。正文哈希接口保持兼容。
- 向量复用指纹包含实际输入（标题、小节与正文）、模型、接口、维度和预处理版本。
- bootstrap 为总结/决策/偏好保留代表性内容，估算涵盖完整 JSON、digest 与元数据。`token_estimate_method` 明确是字符启发式，不冒充模型 tokenizer。
- 文档真实总数与分页；新增项目统计接口，不再从首批 200 篇推断项目总数。未归项目独立筛选，真实名称不与占位符混用。
- 前端维持默认全部文档、文件夹进入平铺；增加页面标题、面包屑、加载更多、输入防抖、失败重试、持久化状态、唯一导航高亮、手机抽屉与触屏删除入口。
- 批量删除确认按真实总数，清空回收站使用必填的 `expected_revision` 校验文档 ID 与删除时间组成的集合快照，`expected_count` 辅助核对数量；相同数量但成员变化、同一文档恢复后再删都会拒绝过期确认。
- 验收脚本按退出码判定失败，默认不访问生产；依赖有锁文件；增加隔离 CI。

## 验收结果

- 隔离 PostgreSQL + HTTP + MCP 集成及单元回归：119 项通过，3 项子测试通过。
- Node 运行真实页面脚本的回归：24 项通过。
- 真实 Chrome（非伪造 DOM）在专用回环测试实例验证：
  - 测试身份提供者账号密码登录、刷新后恢复登录、默认全部文档。
  - 217 篇合成文档、单项目 205 篇：项目统计完整、首屏 50、加载更多 100。
  - 未归项目仅显示其 3 篇，返回全部清除筛选。
  - 编辑保存→磁盘/数据库更新→真实 Git 提交，并恢复测试原文。
  - 注入一次 HTTP 503：不登出，显示错误，重试可恢复。
  - 1440、768、390 宽度，浅色/暗色及手机导航；测得横向溢出为 0，无 Runtime 异常。
- 生产目录工作树保持干净。

上述测试身份及217篇文档均是明确构造的隔离夹具，不是生产内容。字体/刷新截图最初使用 Obscura 时出现存储丢失，最终验收全部改用真实 Chrome。未因此给生产前端增加“强制注入登录态”的绕过。

## 安全运行测试

```bash
uv venv --python 3.11 .venv
uv pip sync requirements.lock --python .venv/bin/python
PYTHON=.venv/bin/python bash tests/acceptance.sh --unit
```

集成测试要求专用回环 PostgreSQL 数据库，名称以 `memorys_test` 开头，预先启用 vector 和 pg_trgm。**禁止填写生产数据库。**

```bash
MEM_TEST_DATABASE_URL='postgresql+asyncpg://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/memorys_test' \
PYTHON=.venv/bin/python bash tests/acceptance.sh --integration
```

配置新增 `MEM_ENV_FILE`，设为空可禁止读取默认部署环境文件。pytest 默认仅收集 `tests/reliability/test_*.py`，不导入历史上带写入副作用的脚本。

## 已知边界（不是已解决的承诺）

1. 文件补偿是进程内机制，不是跨文件/数据库/Git 的分布式事务。断电或 SIGKILL 仍需从 Markdown 重建索引；Git 失败会明确告知，不保证每次都已有版本快照。
2. 锁对使用本代码且共享同一数据目录的服务 worker 有效。仓库自带定时推送脚本已走 REST，因此会经过同一用户锁；外部手工 git 命令不会自动遵守该锁协议，上线时避免与手工切分支并行。
3. 旧客户端若不传 expected_revision/expectedRevision，仍是兼容性的无版本条件更新；只传 expected_hash/expectedHash 仅保护正文。要保护标题和标签，必须使用 revision。
4. 更换 embedding 模型或维度后仍须显式重建向量索引；新指纹能阻止重建时误复用旧向量，不会自动后台重算全库。
5. token 数是明确标记的启发式估算，不保证所有模型的精确硬上限；小预算会规范化为最小可用预算。
6. 本轮没有引入任务队列或后台索引补偿任务。依赖失败时失败/降级状态可见，但不会宣称“排队中”然后丢任务。
7. 未上生产，也未验证 GitHub Actions 远端执行；这里只验证了本机等价的隔离测试命令。

## 发布门槛

后端升级需要短暂重启 MCP，必须先获用户确认。遵守生产发布约定：代码审查→GitHub 已发布产物→服务器拉取产物→验证就绪与 MCP 初始化/工具列表/只读调用。**不得直接把本工作树复制覆盖生产文件。**

新前端依赖新增项目统计接口，不能只把本轮 HTML 单独发到旧后端。

清空回收站接口有意收紧：先 `GET /api/v1/trash/snapshot` 取得 `{total,revision}`，展示确认后 POST `/api/v1/trash/empty?expected_count=...&expected_revision=...`。缺少快照返回 428；集合变化返回 409，必须重新获取并再次由用户确认，禁止自动重试删除。旧客户端仅传 count 也会被拒绝。

发布前备份数据库与 Markdown 数据，确认定时 Git 任务不会与切分支重叠。回退为拉取之前已发布版本；本轮没有新增数据库列，但新旧客户端的 revision 行为需要分别核对。
