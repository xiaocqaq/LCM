# 前端回归与验收

```sh
node --test tests/frontend/*.test.cjs
```

需要 Node 18+，无额外依赖。`navigation.test.cjs` 在 Node vm 中执行真实 HTML 内联脚本，使用最小 DOM/fetch/timer 夹具提供浏览器边界。覆盖导航、查询构造、渲染、请求竞态、分页、删除确认、持久化提示和自动启动；它不替代真实浏览器的 CSS、可访问性及布局验证。

当前结果：24 项通过、零失败。整体验收及真实 Chrome 检查见 `docs/IMPROVEMENTS.md`。

## 接口约定

- 总览与侧栏走 `/api/v1/projects` 全量统计，不从第一页文档推断数量。
- `null` 表示全部；`''` 表示未归项目（`unassigned=true`）。真实项目名称按字面传输，包含 `__none__`、`未归项目`、斜杠等也不变成占位符。
- 项目批量软删走 `DELETE /api/v1/projects?project=...` 或 `?unassigned=true`；确认数量使用完整未过滤项目总数。
- 回收站先 GET `/api/v1/trash/snapshot` 获取 `{total,revision}`，确认并手输“清空”后提交 `POST /api/v1/trash/empty?expected_count=N&expected_revision=...`。无版本不得发送删除；428/409 显示错误，刷新回收站，不自动重试删除。revision 覆盖全部文档 ID 和删除时间，不只比较总数。
- 普通文档保存传 `expectedRevision`；成功更新本地 revision，冲突保留编辑内容和原 revision。
- 保存提示读取 `persistenceStatus`；Git 失败时持续显示警告，不能宣称已提交。跨分支返回 `ok:false` 或缺失 commit 时不能显示保存成功。

## 真实浏览器验收（隔离环境）

执行 `tests/browser_smoke.py` 需专用回环测试实例、`tests/preview_auth_fixture.py` 测试身份提供者以及真实 Chrome CDP。所有浏览器写入只针对合成数据，不使用生产用户。

已验证：冷登录、刷新恢复、默认全部文档、205 篇项目的50/100分页、未归项目、返回全部、真实编辑保存和 Git 提交、503重试、390/768/1440宽度与浅暗主题、零横向溢出、无Runtime异常。

本次回收站集合版本补丁没有改变布局；补丁以真实 PostgreSQL HTTP 测试验证“恢复甲、删除乙而数量不变”仍拒绝旧确认，以及成功确认后仅清空当前已确认集合。前端使用真实脚本 vm 验证快照传递、取消、不自动重试和缺版本拒绝。
