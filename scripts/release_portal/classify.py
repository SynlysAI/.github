"""确定性地将 Git 提交归类为 Release Portal 候选事件。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

TYPE_MAP = {
    "feat": "feature",
    "feature": "feature",
    "fix": "bugfix",
    "bugfix": "bugfix",
    "perf": "performance",
    "performance": "performance",
    "refactor": "architecture",
    "architecture": "architecture",
}
ALGORITHM_KEYWORDS = (
    "algorithm", "算法", "model", "模型", "optimizer",
    "optimization", "优化器", "training", "训练", "embedding", "嵌入",
)
NOISE_TYPES = {"merge", "revert", "deps", "dependency", "dependencies", "format", "fmt", "test", "tests", "docs", "doc", "ci"}
_CONVENTIONAL = re.compile(r"^(?P<kind>[A-Za-z][\w-]*)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<subject>.+)$")


@dataclass(frozen=True)
class ClassifiedCommit:
    """保存提交的确定性分类结果。

    Args:
        sha: 提交 SHA（内部可为完整值，公开输出由聚合器截短）。
        message: 清理后的提交标题。
        occurred_at: ISO 8601 提交时间。
        change_type: 规范变更类型。
        module: 归属模块。
        release_id: 关联 Release 标识（若有）。
        product_id: 产品标识（若有）。
        repository: 来源仓库名（若有）。
        pinned: 是否置顶。
    """

    sha: str
    message: str
    occurred_at: str | None
    change_type: str
    module: str
    release_id: str | None = None
    product_id: str | None = None
    repository: str | None = None
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        """返回普通字典，便于 JSON 序列化。"""
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        """允许以映射方式读取结果字段。"""
        return getattr(self, key)


def _as_mapping(commit: Any) -> Mapping[str, Any]:
    """将提交对象归一化为字段映射。

    Args:
        commit: 提交映射、支持 ``to_dict`` 的对象或普通提交对象。
    Returns:
        包含可用提交字段的映射。
    """
    if isinstance(commit, Mapping):
        return commit
    if hasattr(commit, "to_dict"):
        return commit.to_dict()
    return {
        "sha": getattr(commit, "sha", ""),
        "message": getattr(commit, "message", ""),
        "occurred_at": getattr(commit, "occurred_at", None),
        "repository": getattr(commit, "repository", None) or getattr(commit, "repo", None),
    }


def _override_for(sha: str, overrides: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    """查找与指定 SHA 匹配的首个覆盖规则。

    Args:
        sha: 待匹配的提交 SHA。
        overrides: 覆盖规则迭代器。
    Returns:
        匹配规则；不存在时返回空映射。
    """
    for override in overrides:
        identifiers = {str(override.get(key, "")) for key in ("sha", "commitSha", "id")}
        shas = override.get("commitShas") or []
        if sha and (sha in identifiers or sha in {str(item) for item in shas}):
            return override
    return {}


def load_overrides(path: str | Path) -> list[dict[str, Any]]:
    """读取 overrides.yml。

    Args:
        path: YAML 文件路径。
    Returns:
        覆盖规则列表。
    """
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if isinstance(value, dict):
        value = value.get("overrides", [])
    return [dict(item) for item in value or [] if isinstance(item, Mapping)]


def _files(commit: Mapping[str, Any]) -> list[str]:
    """提取并统一提交变更文件路径。

    Args:
        commit: 包含 files、paths 或 changed_files 的提交映射。
    Returns:
        使用正斜杠的文件路径列表。
    """
    raw = commit.get("files") or commit.get("paths") or commit.get("changed_files") or []
    result = []
    for item in raw:
        path = item.get("filename") if isinstance(item, Mapping) else item
        if path:
            result.append(str(path).replace("\\", "/"))
    return result


def _is_noise(kind: str, subject: str, message: str, commit: Mapping[str, Any]) -> bool:
    """判断提交是否属于默认过滤的噪声类型。

    Args:
        kind: Conventional Commit 类型。
        subject: Conventional Commit 标题正文。
        message: 原始提交标题。
        commit: 用于识别机器人作者的提交映射。
    Returns:
        噪声提交时返回 ``True``。
    """
    lower = f"{kind} {subject} {message}".casefold()
    if kind.casefold() in NOISE_TYPES:
        return True
    if lower.startswith(("merge ", "revert ")) or "dependabot" in lower or "renovate" in lower:
        return True
    if any(re.search(pattern, lower) for pattern in (r"\bdependencies?\b", r"\bbump\s+.+\s+version", r"\bformat(?:ting)?\b", r"\blint(?:ing)?\b")):
        return True
    author = commit.get("author") or commit.get("committer") or {}
    author_text = str(author.get("login", "") if isinstance(author, Mapping) else author).casefold()
    return bool(commit.get("bot") or "[bot]" in author_text or author_text.endswith("bot"))


def classify_commit(commit: Any, *, product_id: str | None = None, overrides: Iterable[Mapping[str, Any]] = ()) -> ClassifiedCommit | None:
    """按 Conventional Commits 规则分类单个提交。

    Args:
        commit: ``Commit`` 对象或包含 sha/message/occurred_at/files 的映射。
        product_id: 可选产品标识。
        overrides: 显式覆盖规则。
    Returns:
        分类结果；被过滤或无法识别时返回 ``None``。
    """
    value = _as_mapping(commit)
    sha = str(value.get("sha") or "")
    message_lines = str(value.get("message") or "").splitlines()
    message = message_lines[0].strip() if message_lines else ""
    override = _override_for(sha, overrides)
    if override.get("hide") is True:
        return None
    if override.get("replaceText"):
        replacement = override["replaceText"]
        message = str(replacement.get("en") if isinstance(replacement, Mapping) else replacement)
    match = _CONVENTIONAL.match(message)
    # hide: false、restore/show 或显式改类都表示人工恢复该候选。
    restored = bool(override.get("restore") or override.get("show") or override.get("hide") is False or override.get("changeType"))
    if not match:
        # 非 Conventional 标题只有人工明确改类/恢复时才允许进入候选。
        if not restored or not override.get("changeType"):
            return None
        kind, scope, subject = "", None, message
    else:
        kind, scope, subject = match.group("kind").casefold(), match.group("scope"), match.group("subject").strip()
    if not restored and _is_noise(kind, subject, message, value):
        return None
    change_type = str(override.get("changeType") or "").strip() or TYPE_MAP.get(kind, "")
    if not change_type:
        return None
    if any(keyword.casefold() in f"{subject} {message}".casefold() for keyword in ALGORITHM_KEYWORDS) and not override.get("changeType"):
        change_type = "algorithm"
    if change_type not in {"feature", "algorithm", "performance", "bugfix", "architecture"}:
        return None
    module = str(override.get("module") or scope or "").strip()
    if not module:
        paths = _files(value)
        module = paths[0].split("/", 1)[0] if paths else "general"
    occurred_at = value.get("occurred_at") or value.get("occurredAt")
    occurred_at = str(occurred_at) if occurred_at else None
    return ClassifiedCommit(
        sha=sha,
        message=message,
        occurred_at=occurred_at,
        change_type=change_type,
        module=module,
        release_id=value.get("release_id") or value.get("releaseId"),
        product_id=product_id or value.get("product_id") or value.get("productId"),
        repository=value.get("repository") or value.get("repo"),
        pinned=bool(override.get("pin", False)),
    )


def classify_commits(commits: Iterable[Any], *, product_id: str | None = None, overrides: Iterable[Mapping[str, Any]] = ()) -> list[ClassifiedCommit]:
    """分类、去重并按时间和 SHA 稳定排序提交。

    Args:
        commits: 提交对象迭代器。
        product_id: 可选产品标识。
        overrides: 显式覆盖规则。
    Returns:
        去重后的分类提交列表。
    """
    override_list = list(overrides)
    unique: dict[str, ClassifiedCommit] = {}
    for commit in commits:
        value = classify_commit(commit, product_id=product_id, overrides=override_list)
        if value and value.sha not in unique:
            unique[value.sha] = value
    def sort_key(item: ClassifiedCommit) -> tuple[datetime, str]:
        try:
            parsed = datetime.fromisoformat((item.occurred_at or "").replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.min.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed, item.sha
    return sorted(unique.values(), key=sort_key)


__all__ = ["ClassifiedCommit", "classify_commit", "classify_commits", "load_overrides", "ALGORITHM_KEYWORDS"]
