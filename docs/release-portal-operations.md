# Release Portal 运维说明

Release Portal 以 GitHub 仓库作为唯一来源，以 GitOps Pull Request 作为审核入口，官网通过 R2 的 `portal/v1/manifest.json` 读取统一公开快照。不要直接编辑 `release-portal/published/`，也不要把 GitHub 下载地址、私有正文、完整 SHA 或任何令牌写入候选、日志和公开对象。

生产官网域名必须由部署配置注入；本仓库不为官网域名提供默认值，也不应在文档、日志或数据契约中猜测域名。

## 私有 AI 端点审批配置

`AI_PRIVATE_ENDPOINT_ALLOWLIST` 仅配置在 `release-portal-private-ai` GitHub Environment 中。该 Environment 必须启用 required reviewers，由审批者维护允许的 HTTPS 端点列表。

此变量不属于普通仓库 Vars，并且不得由 AI_BASE_URL 填充或复制。`AI_BASE_URL` 只选择本次使用的端点；白名单独立决定私有仓库候选是否允许发送至该端点。未获批准时回填命令必须保持 fail-closed，并将候选保留为确定性结果。

## 日常检查

在仓库根目录执行以下检查，确认依赖和公开契约没有回归：

```bash
python -m pytest tests/release_portal -q
python scripts/generate_org_dashboard.py --org SynlysAI --output github-analytics.svg
```

所有 `scripts.release_portal.cli` 命令输出单行 JSON，至少包含 `runId`、`productId`、`stage`、`count`、`durationMs` 和 `status`。失败日志只记录异常类型，不回显请求正文、响应、下载地址或环境变量。

## 新增产品或修改入口

1. 在 `release-portal/catalog.yml` 中修改产品注册表。产品 ID、仓库、官网入口和顺序必须继续符合六产品 allowlist；Web 入口只能使用 HTTPS，`smartaccess` 保持 `download` 且 `webUrl: null`。
2. 同步修改相关 Schema 或 FAQ 后运行 `python -m pytest tests/release_portal/test_config.py tests/release_portal/test_publish.py -q`。
3. 提交 Pull Request。合并到 `main` 后，发布工作流会再次执行全量测试和 Schema 校验。

组织统计看板独立于 Release Portal 发布链路，但会读取同一份 `catalog.yml` 的六仓库 allowlist。不要通过 `REPO_ALLOWLIST` 扩大看板范围，内部仓库不会进入公开 SVG。

## 隐藏、置顶和改写节点

在 `release-portal/overrides.yml` 增加候选事件 ID 的覆盖规则：

```yaml
- id: spec-agent:aggregate:2026-08-03:performance:spectrum-parser
  hide: true
- id: ai4ms:commit:abcdef1
  pin: true
- id: ai4ms:commit:abcdef1
  replaceText:
    title: {zh: "修订后的标题", en: "Revised title"}
```

合并审核 PR 前运行 `python -m pytest tests/release_portal/test_publish.py -q`。`hide` 不会删除候选数据；只有发布后才从公开时间线移除。冲突覆盖和循环 `mergeInto` 会使发布失败。

## 手动上传资源

为资源准备产品、版本、渠道、平台和文件后执行：

```bash
python -m scripts.release_portal.cli upload-asset \
  --product smartaccess --version v1.0.0 --channel manual \
  --platform linux --architecture x86_64 --file ./dist/SmartAccess-linux-amd64.tar.gz \
  --bucket "$R2_BUCKET"
```

命令会计算 SHA-256，先写临时对象，再校验后写入 `assets/{productId}/{version}/{assetName}`，并在候选 manifest 中追加待审记录。同名不同内容默认拒绝覆盖；确认替换时必须显式增加 `--replace`。不要把 GitHub 私有下载 URL 作为参数或日志内容。

## 发布和 R2 回滚

发布前可以只校验而不上传：

```bash
python -m scripts.release_portal.cli publish --validate-only
python -m scripts.release_portal.cli publish --bucket "$R2_BUCKET"
```

发布先上传五个集合和 `meta.json`，最后更新 `portal/v1/manifest.json`。若中途失败，根 manifest 仍指向上一份完整快照。R2 启用对象版本控制并保留最近三个版本；需要回滚时先查看版本，再把已知正常版本复制回同一对象：

```bash
export R2_ENDPOINT="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
aws s3api list-object-versions --bucket "$R2_BUCKET" --prefix portal/v1/manifest.json --endpoint-url "$R2_ENDPOINT"
aws s3api copy-object --bucket "$R2_BUCKET" \
  --copy-source "$R2_BUCKET/portal/v1/manifest.json?versionId=$KNOWN_GOOD_VERSION" \
  --key portal/v1/manifest.json --endpoint-url "$R2_ENDPOINT"
```

安装包对象不删除；若某个版本有问题，只从公开 `releases.json` 下架对应记录，并保留 R2 对象以便审计。

## AI 失败处理

AI 只整理已脱敏的 Commit Message、PR 标题/描述和确定性分类。端点超时、返回非法 JSON 或未通过白名单时，候选保留确定性文本并标记 `reviewReason: ai_failed`，不会阻塞正式 Release 同步，也不会发布空文案。先检查 `AI_BASE_URL` 与受保护 Environment，再使用相同 state 重新执行：

```bash
python -m scripts.release_portal.cli backfill \
  --product spec-agent --limit 500 \
  --state release-portal/state/backfill.json \
  --candidates release-portal/candidates/timeline.json
```

不要删除 `backfill.json` 或候选文件来“重试”；检查点和短 SHA 用于幂等恢复。AI 日志只显示阶段、计数、耗时和错误类型。

## 自动告警

同步工作流每 6 小时运行一次，也支持 `workflow_dispatch`。同步步骤失败会保留失败结论；连续两次失败时创建带 `release-portal` 标签的通用 Issue，恢复成功后自动关闭未关闭的同类 Issue。Issue 正文不包含仓库正文、令牌、私有 URL 或附件内容。发布成功后，Job Summary 会记录公开快照已在 manifest 最后一步更新。

## 安全边界

- GitHub App 安装令牌只用于六个产品仓库和本 `.github` 仓库，权限按工作流最小化配置。
- 公开集合只允许 catalog 中登记的仓库名、七位短 SHA 和公开 Release URL。
- 任何命令失败都应返回非零退出码；不要用空集合掩盖 API、R2 或 Schema 错误。
