# Release Portal 候选审核摘要修复设计

## 目标

候选分支经过多次 Sync 或 Backfill 更新时，PR 描述必须展示该分支相对 `main` 的累计候选事件差异，不能只展示最后一次运行的增量。

## 方案

- Sync 和 Backfill 工作流始终从可信的 `origin/main` 读取 `release-portal/candidates/timeline.json` 作为摘要比较基线；主分支尚无该文件时使用空集合。
- 候选分支只用于恢复候选时间线和回填检查点，不再覆盖摘要比较基线。
- `review_summary.py` 继续负责比较两个显式输入文件，不增加 Git 或 GitHub API 依赖。
- 自动化修复与候选数据恢复拆成两个 PR。修复合并后，再从已审核来源提交恢复 23 条候选事件和完整检查点，生成新的纯数据审核 PR。

## 验证

- 工作流测试断言 before 文件来自 `origin/main`，并且恢复候选分支后不会改写它。
- 摘要单元测试覆盖累计候选：主分支为空、候选已有事件、最后一次运行没有新增事件时，摘要仍报告累计新增数量。
- 运行 `tests/release_portal/test_workflows.py`、`tests/release_portal/test_review_summary.py`，随后运行完整 `tests/release_portal` 测试。

## 安全边界

- 不在日志或 PR 摘要中输出候选正文、私有 URL、完整 SHA 或密钥。
- 不修改 `release-portal/published/`，不删除或重置回填状态。
- 恢复候选数据前先验证 6 个仓库检查点均完成，并扫描公开禁用字段。
