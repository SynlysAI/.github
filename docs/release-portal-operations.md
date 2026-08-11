# Release Portal 运维说明

## 私有 AI 端点审批配置

`AI_PRIVATE_ENDPOINT_ALLOWLIST` 仅配置在 `release-portal-private-ai` GitHub Environment 中。该 Environment 必须启用 required reviewers，由审批者维护允许的 HTTPS 端点列表。

此变量不属于普通仓库 Vars，并且不得由 AI_BASE_URL 填充或复制。`AI_BASE_URL` 只选择本次使用的端点；白名单独立决定私有仓库候选是否允许发送至该端点。未获批准时回填命令必须保持 fail-closed，并将候选保留为确定性结果。
