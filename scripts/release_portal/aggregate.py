"""Release Portal 提交聚合和历史回填检查点。"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .classify import ClassifiedCommit, classify_commits

MAX_BACKFILL_BATCH = 500
STATE_PATH = Path(__file__).resolve().parents[2] / "release-portal" / "state" / "backfill.json"
SINGLETON_TYPES = {"feature", "algorithm", "performance", "bugfix"}


def _parse_datetime(value: str | None) -> datetime:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso_week_start(value: str | None) -> str:
    """返回 ISO 周对应的周一日期。

    Args:
        value: ISO 8601 日期时间文本。
    Returns:
        ``YYYY-MM-DD`` 格式的周一日期。
    """
    date = _parse_datetime(value).date()
    return (date - timedelta(days=date.weekday())).isoformat()


def _short_sha(sha: str) -> str:
    return sha[:7] if len(sha) > 7 else sha


def _event_text(change_type: str, module: str) -> tuple[dict[str, str], dict[str, str]]:
    labels = {"feature": ("新增功能", "New feature"), "algorithm": ("算法改进", "Algorithm improvement"), "performance": ("性能优化", "Performance improvement"), "bugfix": ("问题修复", "Bug fix"), "architecture": ("架构调整", "Architecture change")}
    label_zh, label_en = labels[change_type]
    return {"zh": f"{module} {label_zh}", "en": f"{module} {label_en}"}, {"zh": f"{label_zh}（{module}）", "en": f"{label_en} ({module})"}


def _as_dict(commit: ClassifiedCommit | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(commit, ClassifiedCommit):
        return commit.to_dict()
    return dict(commit)


def aggregate_commits(product_id: str, commits: Iterable[Any], *, releases: Iterable[Mapping[str, Any]] | None = None, overrides: Iterable[Mapping[str, Any]] = (), repository: str | None = None) -> list[dict[str, Any]]:
    """按产品、ISO 周、模块和变更类型生成确定性事件。

    Args:
        product_id: 产品标识。
        commits: Commit 对象或映射的迭代器。
        releases: 可选 Release 元数据（用于保留来源，不参与分组）。
        overrides: 显式隐藏、置顶或改类规则。
        repository: 可选公开仓库名；缺省回退为提交中的仓库或 product_id。
    Returns:
        按事件 ID 排序的公开候选事件列表。
    """
    classified = classify_commits(commits, product_id=product_id, overrides=overrides)
    groups: dict[tuple[str, str, str, str], list[ClassifiedCommit]] = defaultdict(list)
    for item in classified:
        groups[(product_id, iso_week_start(item.occurred_at), item.module, item.change_type)].append(item)
    events: list[dict[str, Any]] = []
    for (product, week, module, change_type), members in groups.items():
        members.sort(key=lambda item: (_parse_datetime(item.occurred_at), item.sha))
        if len(members) == 1 and change_type not in SINGLETON_TYPES:
            continue
        level = "aggregate" if len(members) >= 2 else "commit"
        event_id = f"{product}:{level}:{week}:{change_type}:{module}"
        first = members[0]
        title, summary = _event_text(change_type, module)
        occurred_at = f"{week}T08:00:00Z" if level == "aggregate" else (first.occurred_at or f"{week}T08:00:00Z")
        source_repository = next(
            (
                str(_as_dict(item).get("repository") or _as_dict(item).get("repo"))
                for item in members
                if _as_dict(item).get("repository") or _as_dict(item).get("repo")
            ),
            None,
        )
        source_repository = repository or source_repository or product_id
        events.append({
            "id": event_id,
            "productId": product,
            "level": level,
            "occurredAt": occurred_at,
            "version": None,
            "changeType": change_type,
            "module": module,
            "title": title,
            "summary": summary,
            "detailsMarkdown": {"zh": "", "en": ""},
            "source": {"repository": source_repository, "commitShas": [_short_sha(item.sha) for item in members], "releaseUrl": None},
            "pinned": any(item.pinned for item in members),
        })
    return sorted(events, key=lambda item: item["id"])


def initial_backfill_state(repositories: Iterable[str]) -> dict[str, Any]:
    """创建所有仓库均未开始的回填状态。"""
    return {"schemaVersion": 1, "repositories": {repo: {"cursor": None, "completed": False, "processed": 0, "watermark": {"sha": None, "publishedAt": None}} for repo in sorted(set(repositories))}}


def update_backfill_state(state: dict[str, Any], repository: str, commits: Iterable[Mapping[str, Any]], *, completed: bool = False, max_batch: int = MAX_BACKFILL_BATCH) -> dict[str, Any]:
    """更新单仓库回填游标、完成状态和增量水位。

    Args:
        state: 可变状态映射。
        repository: owner/name 仓库名。
        commits: 本批实际处理的提交。
        completed: 是否已完成该仓库回填。
        max_batch: 单次最多处理条数，默认 500。
    Returns:
        更新后的状态映射（与输入为同一对象）。
    """
    batch = [dict(item) for item in commits][:max(1, min(max_batch, MAX_BACKFILL_BATCH))]
    repositories = state.setdefault("repositories", {})
    current = repositories.setdefault(repository, {"cursor": None, "completed": False, "processed": 0, "processedShas": [], "watermark": {"sha": None, "publishedAt": None}})
    processed_shas = list(dict.fromkeys(str(sha) for sha in current.get("processedShas", []) if sha))
    known = set(processed_shas)
    new_batch: list[dict[str, Any]] = []
    for item in batch:
        sha = str(item.get("sha") or "")
        if not sha or sha in known:
            continue
        known.add(sha)
        new_batch.append(item)
        if len(new_batch) >= MAX_BACKFILL_BATCH:
            break
    if new_batch:
        processed_shas.extend(str(item["sha"]) for item in new_batch)
        current["processedShas"] = processed_shas
        current["cursor"] = str(new_batch[-1].get("sha") or current.get("cursor") or "") or None
        current["processed"] = len(processed_shas)
        newest = max(new_batch, key=lambda item: _parse_datetime(item.get("occurred_at") or item.get("occurredAt") or item.get("published_at") or item.get("publishedAt")))
        current["watermark"] = {"sha": str(newest.get("sha") or "") or None, "publishedAt": newest.get("published_at") or newest.get("publishedAt") or newest.get("occurred_at") or newest.get("occurredAt")}
    current["completed"] = bool(completed)
    return state


def load_backfill_state(path: str | Path = STATE_PATH) -> dict[str, Any]:
    """读取回填状态 JSON。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_backfill_state(state: Mapping[str, Any], path: str | Path = STATE_PATH) -> None:
    """以稳定格式写入回填状态 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = ["aggregate_commits", "iso_week_start", "initial_backfill_state", "update_backfill_state", "load_backfill_state", "save_backfill_state"]
