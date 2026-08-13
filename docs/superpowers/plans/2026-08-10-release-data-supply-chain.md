# AI4S Release 数据供应链实施计划

> **面向执行代理：** 必须使用 `subagent-driven-development`（推荐）或 `executing-plans`，按任务逐项实施并用复选框跟踪。

**目标：** 将 SynlysAI 六个产品仓库的 Release、附件、Commit 与 PR 元数据转化为经过审核的双语公开数据，并同步到 Cloudflare R2 供官网统一消费。

**架构：** `.github` 仓库是组织级产品注册表、GitHub 数据采集器、AI 语义聚合器和 GitOps 审核入口，不承担用户界面。正式 Release 元数据与附件通过校验后自动发布；Commit/PR 聚合结果只生成候选 PR，合并审核后才进入公开时间线。公开 JSON 和安装包发布到 R2，私有仓库令牌、AI 密钥与原始内部信息不进入官网仓库或浏览器。

**技术栈：** Python 3.11、GitHub REST API、OpenAI 兼容 API、JSON Schema、PyYAML、httpx、boto3、pytest、GitHub Actions、Cloudflare R2。

---

## 已确认决策

- 产品范围固定为 `AI4MS`、`Spec Agent`、`Poly Agent`、`SpecLabOS`、`RAGPortal`、`SmartAccess` 六个独立产品。
- `AI4MS`、`Spec Agent`、`Poly Agent`、`SpecLabOS`、`RAGPortal` 使用 Web 直达入口；`SmartAccess` 使用下载/部署入口。
- 首期采用 GitOps CMS：YAML/Markdown + Pull Request 审核，不建设独立运营后台。
- 正式 Release 自动发布；Commit/PR 经 AI 聚合后必须人工审核。
- 历史 Commit 全量回填，但按每仓库每次最多 500 条分批执行，直到检查点标记完成。
- 中英文同时发布；中文为审核主文本，英文为同一候选事件中的必填字段。
- GitHub 私有仓库只发送 Commit Message 和经脱敏的 PR 描述到模型，不发送代码、diff、文件内容、Issue 评论或附件。
- 所有安装包使用 R2 作为源站，官网只暴露统一下载路由，不暴露私有 GitHub 地址。

## 公共契约

R2 固定发布以下对象，所有文件均使用 UTF-8、ISO 8601 UTC 时间和 `schemaVersion: 1`：

```text
portal/v1/products.json
portal/v1/releases.json
portal/v1/timeline.json
portal/v1/faqs.json
portal/v1/meta.json
portal/v1/manifest.json        # 官网同域 Function 使用的原子聚合快照
assets/{productId}/{version}/{assetName}
```

`products.json` 的产品 ID 和入口固定如下：

| productId | GitHub 仓库 | entryType | webUrl |
| --- | --- | --- | --- |
| `ai4ms` | `SynlysAI/AI4MS` | `web` | `https://ai4ms.xmuzc.com/` |
| `spec-agent` | `SynlysAI/Spec_Agent` | `web` | `https://specagent.xmuzc.com/` |
| `poly-agent` | `SynlysAI/Poly_Agent` | `web` | `https://polyagent.xmuzc.com/` |
| `speclabos` | `SynlysAI/SpecLabOS` | `web` | `https://speclabos.xmuzc.com/` |
| `ragportal` | `SynlysAI/RAGPortal` | `web` | `https://rag.xmuzc.com/` |
| `smartaccess` | `SynlysAI/SmartAccess` | `download` | `null` |

公开时间线事件字段固定为：

```json
{
  "id": "spec-agent:aggregate:2026-08-03:performance:spectrum-parser",
  "productId": "spec-agent",
  "level": "aggregate",
  "occurredAt": "2026-08-03T08:00:00Z",
  "version": null,
  "changeType": "performance",
  "module": "spectrum-parser",
  "title": {"zh": "优化谱图解析性能", "en": "Improved spectrum parsing performance"},
  "summary": {"zh": "缩短批量谱图解析耗时。", "en": "Reduced batch spectrum parsing time."},
  "detailsMarkdown": {"zh": "", "en": ""},
  "source": {"repository": "SynlysAI/Spec_Agent", "commitShas": ["abcdef1"], "releaseUrl": null},
  "pinned": false
}
```

`changeType` 只允许 `feature`、`algorithm`、`performance`、`bugfix`、`architecture`；`level` 只允许 `release`、`aggregate`、`commit`。公开数据不得包含 GitHub Token、私有下载 URL、PR 内部链接、完整 SHA、作者邮箱或模型提示词。

## 文件结构

```text
release-portal/
  catalog.yml                  # 六产品注册表、入口、仓库、Logo 与同步策略
  faqs.yml                     # 双语 FAQ 与产品/版本关联
  overrides.yml                # 隐藏、置顶、改写、合并和类型纠正
  schemas/
    products.schema.json
    releases.schema.json
    timeline.schema.json
    faqs.schema.json
  state/
    backfill.json              # 各仓库历史回填游标和完成状态
  candidates/
    timeline.json              # 仅 PR 审核，不直接公开
  published/
    products.json
    releases.json
    timeline.json
    faqs.json
    meta.json
scripts/release_portal/
  __init__.py
  models.py                    # 内部数据模型与序列化边界
  config.py                    # YAML 加载、产品注册表校验
  github.py                    # GitHub REST 客户端、分页、限流和重试
  classify.py                  # Conventional Commits 与模块归类
  aggregate.py                 # 确定性聚合规则和候选事件生成
  ai.py                        # OpenAI 兼容接口和严格结构化输出
  assets.py                    # Release 附件校验、哈希和 R2 同步
  publish.py                   # 覆盖规则、公开脱敏、Schema 校验和发布
  cli.py                       # sync/backfill/review/publish/upload-asset 命令
tests/release_portal/
  fixtures/
  test_config.py
  test_github.py
  test_classify.py
  test_aggregate.py
  test_ai.py
  test_assets.py
  test_publish.py
.github/workflows/
  release-portal-sync.yml
  release-portal-backfill.yml
  release-portal-publish.yml
```

### 任务 1：建立产品注册表和版本化数据契约

**文件：**
- 新建：`release-portal/catalog.yml`
- 新建：`release-portal/faqs.yml`
- 新建：`release-portal/overrides.yml`
- 新建：`release-portal/schemas/*.schema.json`
- 新建：`scripts/release_portal/models.py`
- 新建：`scripts/release_portal/config.py`
- 测试：`tests/release_portal/test_config.py`

- [ ] 先写配置测试，验证六个 `productId` 唯一、仓库唯一、五个 Web URL 使用 HTTPS、SmartAccess 只有下载入口。
- [ ] 为产品、Release、时间线、FAQ 定义 JSON Schema，并使 `additionalProperties` 默认为 `false`，防止未审字段进入公开接口。
- [ ] 在 `catalog.yml` 写入上表六个产品、双语名称/一句话定位、产品分类、Logo 路径、默认分支、入口类型和 `aiPolicy: metadata-only`。
- [ ] 规定 Logo 缺失时使用 SynlysAI 品牌图标加产品英文名，不生成虚构产品 Logo。
- [ ] 运行 `python -m pytest tests/release_portal/test_config.py -q`，预期全部通过。
- [ ] 提交：`git commit -m "feat: 建立 Release Portal 产品与数据契约"`。

### 任务 2：实现可测试的 GitHub 组织数据采集层

**文件：**
- 新建：`scripts/release_portal/github.py`
- 新建：`tests/release_portal/fixtures/github_*.json`
- 测试：`tests/release_portal/test_github.py`
- 修改：`requirements.txt`
- 修改：`requirements-dev.txt`

- [ ] 用 fixture 先覆盖分页、空 Release、Draft/Pre-release、限流重试、私有仓库 404 和附件元数据归一化。
- [ ] 复用当前 `generate_org_dashboard.py` 的 REST 思路，但将客户端独立为 `GitHubClient`；所有函数添加中文 docstring、参数和返回值说明。
- [ ] 只读取注册表中的六个仓库；Release 读取名称、tag、正文、发布时间和附件，Commit 读取 SHA、时间和 message，PR 只在提交存在关联 PR 时读取标题与正文。
- [ ] 对 `403/429/5xx` 使用指数退避并尊重 `Retry-After`/`X-RateLimit-Reset`；不可恢复错误写入结构化日志并使工作流失败，不静默返回空列表。
- [ ] 使用 GitHub App 的只读安装令牌；不再依赖默认 `GITHUB_TOKEN` 跨仓库读取私有内容。
- [ ] 运行 `python -m pytest tests/release_portal/test_github.py -q`，预期全部通过。
- [ ] 提交：`git commit -m "feat: 添加跨仓库 Release 与 Commit 采集"`。

### 任务 3：实现全历史回填和确定性聚合

**文件：**
- 新建：`scripts/release_portal/classify.py`
- 新建：`scripts/release_portal/aggregate.py`
- 新建：`release-portal/state/backfill.json`
- 测试：`tests/release_portal/test_classify.py`
- 测试：`tests/release_portal/test_aggregate.py`

- [ ] 先写测试锁定类型映射：`feat`→`feature`、算法关键词→`algorithm`、`perf`→`performance`、`fix`→`bugfix`、`refactor`→`architecture`。
- [ ] 默认过滤 merge、revert、依赖升级、格式化、测试、文档、CI 和 bot 提交；`overrides.yml` 可显式恢复、隐藏或改类。
- [ ] 模块优先取 Conventional Commits scope，其次按首层目录映射；均无法判断时使用 `general`。
- [ ] 在 Release 之间按“产品 + ISO 周 + 模块 + 变更类型”聚合；同组 2 条及以上生成 `aggregate`，单条只有 `feature/algorithm/performance/bugfix` 才生成轻量 `commit`。
- [ ] 全历史回填每仓库每次最多处理 500 条并更新 `backfill.json`；增量同步以最后公开 SHA 和发布时间为水位，重复运行必须产生相同 ID 和排序。
- [ ] 运行聚合测试，覆盖跨周、跨 Release、重复提交、强制隐藏、置顶和断点恢复。
- [ ] 提交：`git commit -m "feat: 添加 Commit 全历史回填与聚合"`。

### 任务 4：接入 OpenAI 兼容语义整理

**文件：**
- 新建：`scripts/release_portal/ai.py`
- 新建：`tests/release_portal/test_ai.py`

- [ ] 用假模型响应先测试严格 JSON 解析、字段缺失、非法类型、超时、重试和不可恢复失败。
- [ ] 请求只包含产品名、脱敏 Commit Message、脱敏 PR 标题/描述、确定性分类和模块，不包含 diff、代码文件、作者邮箱、Issue 评论或附件。
- [ ] 模型必须返回 `title.zh/en`、`summary.zh/en`、`detailsMarkdown.zh/en`、`changeType`、`module`；返回值再次通过 Schema 与长度限制校验。
- [ ] AI 失败时保留确定性候选并标记 `reviewReason: ai_failed`，不得发布空文案或臆造指标。
- [ ] 通过 `AI_BASE_URL`、`AI_MODEL`、`AI_API_KEY` 配置模型；私有仓库只允许使用组织批准的兼容端点。
- [ ] 运行 `python -m pytest tests/release_portal/test_ai.py -q`，预期全部通过。
- [ ] 提交：`git commit -m "feat: 添加双语技术演进语义整理"`。

### 任务 5：实现候选审核、人工覆盖与公开脱敏

**文件：**
- 新建：`scripts/release_portal/publish.py`
- 新建：`release-portal/candidates/timeline.json`
- 新建：`release-portal/published/*.json`
- 测试：`tests/release_portal/test_publish.py`

- [ ] 先测试 `overrides.yml` 的 `hide`、`pin`、`replaceText`、`changeType`、`mergeInto` 五种操作和冲突检测。
- [ ] 候选文件保留短 SHA 与审核原因；公开文件只保留允许公开的仓库名、7 位短 SHA和公开 Release URL，不保留私有 PR URL。
- [ ] 发布前统一执行 Schema 校验、双语非空校验、重复 ID 校验、时间倒序校验和敏感模式扫描。
- [ ] `meta.json` 写入生成时间、源仓库水位、数据版本和各集合 SHA-256，官网可判断缓存与数据新鲜度。
- [ ] 发布脚本在单独校验五个集合后生成 `manifest.json`；先上传各集合，再上传 manifest 作为最后一步，因此官网永远只读取完整快照。
- [ ] 候选 PR 描述列出新增/修改/隐藏事件数量和每个产品的影响，不在日志中回显原始私有正文。
- [ ] 运行 `python -m pytest tests/release_portal/test_publish.py -q`，预期全部通过。
- [ ] 提交：`git commit -m "feat: 添加时间线审核与公开发布机制"`。

### 任务 6：实现 Release 附件与手动资源的 R2 同步

**文件：**
- 新建：`scripts/release_portal/assets.py`
- 测试：`tests/release_portal/test_assets.py`
- 修改：`scripts/release_portal/cli.py`

- [ ] 先用模拟 S3/R2 客户端测试幂等上传、同名不同内容拒绝覆盖、SHA-256、Content-Type、Content-Disposition 和失败回滚。
- [ ] GitHub Release 附件保存到 `assets/{productId}/{version}/{assetName}`；先流式计算哈希，再上传临时 key，校验后复制到正式 key。
- [ ] `releases.json` 的附件仅公开 `downloadPath`、文件名、大小、平台、架构和 SHA-256；不公开 GitHub 私有下载 URL。
- [ ] 提供 `upload-asset` CLI 供运营上传非 GitHub 资源；命令强制要求产品、版本、渠道、平台和文件，成功后生成待审 manifest 变更。
- [ ] 开启 R2 Object Versioning，并保留最近三个对象版本；资源替换必须显式使用 `--replace`，默认拒绝覆盖。
- [ ] 运行 `python -m pytest tests/release_portal/test_assets.py -q`，预期全部通过。
- [ ] 提交：`git commit -m "feat: 添加 Release 资源中转与手动上传"`。

### 任务 7：配置三条 GitHub Actions 发布链路

**文件：**
- 新建：`.github/workflows/release-portal-sync.yml`
- 新建：`.github/workflows/release-portal-backfill.yml`
- 新建：`.github/workflows/release-portal-publish.yml`
- 修改：`.github/dependabot.yml`

- [ ] `release-portal-sync.yml` 每 6 小时和手动触发：获取 GitHub App Token，自动同步正式 Release/附件到 R2，并创建或更新 `automation/release-portal-candidates` 审核 PR。
- [ ] `release-portal-backfill.yml` 仅手动触发：按产品或全部产品每次回填 500 条历史提交，上传候选 artifact 并更新同一个审核 PR，直到六仓库状态为 `complete`。
- [ ] `release-portal-publish.yml` 在主分支的 catalog/FAQ/override/published 文件变化时触发：跑全量测试和 Schema 校验，再依次上传五个集合、`meta.json`，最后上传 `manifest.json`；任一文件失败时不得更新 manifest。
- [ ] 配置 `concurrency`，同一发布组只允许一个任务运行；同步任务可取消旧运行，发布任务不可中途取消。
- [ ] 所需 Secrets 固定为 `SYNLYSAI_APP_ID`、`SYNLYSAI_APP_PRIVATE_KEY`、`AI_API_KEY`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`；普通仓库 Vars 固定为 `AI_BASE_URL`、`AI_MODEL`、`CLOUDFLARE_ACCOUNT_ID`、`R2_BUCKET`。`AI_PRIVATE_ENDPOINT_ALLOWLIST` 仅作为 `release-portal-private-ai` GitHub Environment 的受保护 Var 配置，必须启用 required reviewers，独立于且不得等于 `AI_BASE_URL` 自证。
- [ ] 将 GitHub Actions 和 Python 依赖纳入 Dependabot；工作流权限使用最小 `contents: write`、`pull-requests: write`，私有仓库读取由 GitHub App 安装权限控制。
- [ ] 用 `workflow_dispatch` 依次验收单产品增量、单产品回填、六产品发布和失败重试。
- [ ] 提交：`git commit -m "ci: 建立 Release Portal 自动同步与发布"`。

### 任务 8：更新组织首页、运维文档和可观测性

**文件：**
- 修改：`profile/README.md`
- 修改：`scripts/generate_org_dashboard.py`
- 新建：`docs/release-portal-operations.md`
- 新建：`tests/release_portal/test_dashboard_compat.py`

- [ ] 在组织首页增加官网 Release Portal 入口，并明确 GitHub 是来源、官网是统一公开渠道。
- [ ] 现有组织统计看板继续独立运行，只复用 `catalog.yml` 的仓库 allowlist，避免内部仓库误入公开 SVG。
- [ ] 运维文档写明新增产品、修改入口、隐藏节点、置顶节点、手动上传资源、回滚 R2 对象和处理 AI 失败的完整命令；开篇说明 `AI_PRIVATE_ENDPOINT_ALLOWLIST` 是仅限 `release-portal-private-ai`、启用 required reviewers 的 Environment Var，不属于普通仓库 Vars，且不得由 `AI_BASE_URL` 填充。
- [ ] 所有 CLI 输出 JSON 日志，至少包含 `runId`、`productId`、`stage`、`count`、`durationMs`、`status`，不得输出密钥或私有正文。
- [ ] 发布成功后写 GitHub Job Summary；连续两次同步失败时创建带 `release-portal` 标签的 Issue，恢复后自动关闭。
- [ ] 运行 `python -m pytest tests/release_portal -q` 与现有 dashboard 生成命令，预期测试通过且 SVG 可生成。
- [ ] 提交：`git commit -m "docs: 完善 Release Portal 运营与组织入口"`。

## 验收标准

- 六产品注册表与官网显示顺序一致，五个 Web 产品能直达，SmartAccess 只展示下载入口。
- 任一正式 Release 在 6 小时内进入 `releases.json`；安装包可通过 R2 下载且 SHA-256 一致。
- Commit/PR 候选不会未经 PR 合并进入 `timeline.json`。
- 全历史回填可中断恢复，重复执行不产生重复事件。
- 私有仓库内容不会出现在日志、公开 JSON、R2 元数据或 PR 公共描述中。
- 中英文标题与摘要均通过 Schema 校验；AI 失败不会阻塞 Release 同步。
- `products.json`、`releases.json`、`timeline.json`、`faqs.json` 和 `meta.json` 可由匿名读请求稳定获取。

## 上线顺序与回滚

1. 先创建 GitHub App、R2 Bucket、Secrets/Vars，并只对 `SmartAccess` 运行附件同步验证。
2. 发布 `products.json` 与空的 Release/Timeline/FAQ 集合，让官网先完成契约联调。
3. 开启六产品 Release 增量同步，再逐仓库执行全历史回填。
4. 审核并合并第一批历史候选后发布统一时间线，最后开启 6 小时定时任务。
5. 回滚时恢复 R2 上一版本 `portal/v1/*.json`；安装包对象不删除，只将对应版本从 `releases.json` 下架。

## 假设与边界

- 六个仓库名称按当前本地项目名处理；`SmartAccess` 首次正式发布前允许下载列表为空，官网显示“暂无公开版本”，不虚构版本号。
- 产品仓库首期无需修改；中央任务通过 GitHub App 定时轮询。未来若子仓库接入 Release 事件，仅作为降低延迟的增强，不改变数据契约。
- FAQ 由 `faqs.yml` 人工维护；GitHub Discussions/Issues 自动提炼属于二期扩展。
- 独立可视化 CMS、运营账号体系、审批角色和数据库不在首期范围内。
