"""候选审核 PR 摘要测试。"""

import json
from pathlib import Path

from scripts.release_portal.review_summary import render_pr_summary


def _write_timeline(path: Path, events: list[dict]) -> None:
    """写入最小候选时间线。

    Args:
        path: timeline.json 路径。
        events: 候选事件列表。

    Returns:
        无返回值。
    """
    path.write_text(json.dumps({"schemaVersion": 1, "events": events}), encoding="utf-8")


def test_review_summary_contains_counts_without_candidate_text(tmp_path: Path):
    """摘要应显示各产品统计，不得回显候选标题或私有正文。"""
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    _write_timeline(before, [{"id": "old", "productId": "ai4ms", "title": "私有标题"}])
    _write_timeline(
        after,
        [
            {"id": "old", "productId": "ai4ms", "title": "更新后的私有标题"},
            {"id": "new", "productId": "spec-agent", "title": "不应回显"},
        ],
    )

    summary = render_pr_summary(before, after)

    assert "| 新增 | 1 |" in summary
    assert "| 修改 | 1 |" in summary
    assert "| ai4ms | 0 | 1 | 0 |" in summary
    assert "| spec-agent | 1 | 0 | 0 |" in summary
    assert "私有标题" not in summary
    assert "不应回显" not in summary
