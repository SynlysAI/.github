# Release Portal 候选审核摘要修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Sync 与 Backfill 创建或更新候选 PR 时，始终展示候选分支相对 `main` 的累计候选事件差异。

**Architecture:** 保持 `review_summary.py` 的显式 before/after 接口不变，只修正两份 GitHub Actions 工作流准备 before 文件的时机。before 从可信 `origin/main` 读取一次，候选分支仅恢复 after 和检查点；修复合并后再用独立数据 PR 恢复已回滚的 23 条候选事件。

**Tech Stack:** GitHub Actions YAML、Python 3.11、pytest、Git。

---

## 文件结构

- Modify: `tests/release_portal/test_workflows.py`：增加两份工作流共享的累计摘要基线断言。
- Modify: `.github/workflows/release-portal-sync.yml`：在恢复候选文件前从 `origin/main` 保存 before，不再用候选时间线覆盖。
- Modify: `.github/workflows/release-portal-backfill.yml`：采用与 Sync 相同的累计比较基线。
- Restore in a later data branch: `release-portal/candidates/timeline.json`、`release-portal/state/backfill.json`：从提交 `e41c759` 恢复已完成回填的数据。

### Task 1: 建立工作流累计基线回归测试

**Files:**
- Modify: `tests/release_portal/test_workflows.py`

- [ ] **Step 1: 写失败测试**

在 `test_sync_workflow_uses_app_token_schedule_and_candidate_pr` 和 `test_backfill_workflow_is_manual_and_limits_each_product_batch` 中调用：

```python
_assert_review_summary_compares_main_to_candidate(workflow)
```

新增辅助函数：

```python
def _assert_review_summary_compares_main_to_candidate(workflow: str) -> None:
    """确认审核摘要比较主分支与完整候选分支，而非最后一批增量。

    Args:
        workflow: 工作流 YAML 文本。
    """
    restore = workflow.index("仅恢复候选数据输入")
    fetch_candidate = workflow.index('if git ls-remote --exit-code --heads origin "$CANDIDATE_BRANCH"')
    summary = workflow.index("用主分支代码生成审核摘要")
    baseline_copy = (
        'git show origin/main:release-portal/candidates/timeline.json '
        '> "$RUNNER_TEMP/release-portal-before-timeline.json"'
    )
    candidate_copy = (
        'cp release-portal/candidates/timeline.json '
        '"$RUNNER_TEMP/release-portal-before-timeline.json"'
    )

    assert baseline_copy in workflow[restore:fetch_candidate]
    assert candidate_copy not in workflow[fetch_candidate:summary]
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/release_portal/test_workflows.py -q`

Expected: 两个工作流结构测试失败，因为当前 YAML 在恢复候选分支后用 `cp` 覆盖 before 文件。

- [ ] **Step 3: 提交测试保存点**

```bash
git add tests/release_portal/test_workflows.py
git commit -m "test: 覆盖候选摘要累计比较基线"
```

### Task 2: 修复 Sync 与 Backfill 的摘要基线

**Files:**
- Modify: `.github/workflows/release-portal-sync.yml`
- Modify: `.github/workflows/release-portal-backfill.yml`
- Test: `tests/release_portal/test_workflows.py`

- [ ] **Step 1: 写最小实现**

在两份工作流 `git checkout --detach origin/main` 后，用主分支候选文件初始化 before；不存在时保留空集合：

```bash
printf '{"schemaVersion": 1, "events": []}\n' > "$RUNNER_TEMP/release-portal-before-timeline.json"
if git cat-file -e origin/main:release-portal/candidates/timeline.json; then
  git show origin/main:release-portal/candidates/timeline.json > "$RUNNER_TEMP/release-portal-before-timeline.json"
fi
```

删除恢复候选文件之后的覆盖逻辑：

```bash
if [ -f release-portal/candidates/timeline.json ]; then
  cp release-portal/candidates/timeline.json "$RUNNER_TEMP/release-portal-before-timeline.json"
fi
```

- [ ] **Step 2: 运行定向测试并确认 GREEN**

Run: `python -m pytest tests/release_portal/test_workflows.py tests/release_portal/test_review_summary.py -q`

Expected: 全部通过。

- [ ] **Step 3: 运行完整 Release Portal 测试**

Run: `python -m pytest tests/release_portal -q`

Expected: 全部通过。

- [ ] **Step 4: 检查差异和敏感内容**

Run: `git diff --check && git diff --stat`

Expected: 只包含两份工作流和工作流测试，无空白错误。

- [ ] **Step 5: 提交实现**

```bash
git add .github/workflows/release-portal-sync.yml .github/workflows/release-portal-backfill.yml tests/release_portal/test_workflows.py
git commit -m "fix: 按累计差异生成候选审核摘要"
```

### Task 3: 推送自动化修复 PR

**Files:**
- No additional file changes.

- [ ] **Step 1: 验证分支提交和工作树**

Run: `git status --short --branch && git log --oneline origin/main..HEAD`

Expected: 工作树干净，分支只包含设计、测试和实现提交。

- [ ] **Step 2: 推送分支**

Run: `git push -u origin fix/release-portal-review-summary`

Expected: 远端分支创建成功。

- [ ] **Step 3: 创建修复 PR**

Run: `gh pr create --base main --head fix/release-portal-review-summary --title "fix: 修复 Release Portal 候选审核摘要" --body-file <prepared-body>`

Expected: PR 创建成功；若本机 `gh` 未认证，输出可直接打开的 compare URL，由用户在 GitHub 页面创建 PR。

### Task 4: 修复合并后恢复候选数据

**Files:**
- Restore: `release-portal/candidates/timeline.json`
- Restore: `release-portal/state/backfill.json`

- [ ] **Step 1: 从最新 main 创建数据分支**

```bash
git switch main
git pull --ff-only origin main
git switch -c automation/release-portal-candidates-v2
```

- [ ] **Step 2: 从已知回填提交恢复数据**

```bash
git restore --source=e41c759 -- release-portal/candidates/timeline.json release-portal/state/backfill.json
```

- [ ] **Step 3: 验证候选和状态**

Run: PowerShell 解析两个 JSON，断言候选事件为 23、6 个仓库均为 `completed: true`、中英文标题非空，并扫描 Token、完整 SHA、私有 URL 和私有正文标记。

Expected: 23 条事件、6/6 完成、无禁用字段匹配。

- [ ] **Step 4: 运行发布校验但不上传**

Run: `python -m scripts.release_portal.cli publish --validate-only`

Expected: 校验成功，不修改 R2。

- [ ] **Step 5: 提交并推送纯数据分支**

```bash
git add release-portal/candidates/timeline.json release-portal/state/backfill.json
git commit -m "chore: 恢复 Release Portal 回填候选数据"
git push -u origin automation/release-portal-candidates-v2
```

- [ ] **Step 6: 创建新的候选审核 PR**

Run: 创建 `automation/release-portal-candidates-v2 → main` PR，并在正文中写入相对 `main` 的累计统计：新增 23、修改 0、隐藏 0，以及各产品数量。

Expected: PR 仅包含候选时间线和回填状态；人工审核后再合并。

### Task 5: 合并候选数据后的发布验证

**Files:**
- No local file changes.

- [ ] **Step 1: 检查 Publish 工作流**

确认完整测试、Schema 校验、R2 上传和 manifest 最后更新均成功。

- [ ] **Step 2: 验证 R2 公共对象**

确认 `portal/v1/products.json`、`releases.json`、`timeline.json`、`faqs.json`、`meta.json`、`manifest.json` 存在，manifest 的 SHA-256/大小与对象一致，官网读取最新 generation。
