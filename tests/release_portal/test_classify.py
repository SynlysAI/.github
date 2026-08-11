"""Commit 分类规则测试。"""

from scripts.release_portal.classify import classify_commit, classify_commits


def test_conventional_types_and_algorithm_keyword_are_deterministic():
    assert classify_commit({"sha": "a", "message": "feat(parser): add parser"}).change_type == "feature"
    assert classify_commit({"sha": "b", "message": "fix: handle timeout"}).change_type == "bugfix"
    assert classify_commit({"sha": "c", "message": "perf: speed up inference"}).change_type == "performance"
    assert classify_commit({"sha": "d", "message": "feat(model): improve algorithm optimizer"}).change_type == "algorithm"
    assert classify_commit({"sha": "e", "message": "refactor(api): simplify boundary"}).change_type == "architecture"


def test_filters_noise_and_uses_scope_then_first_directory():
    assert classify_commit({"sha": "m", "message": "Merge branch main"}) is None
    assert classify_commit({"sha": "r", "message": "Revert \"feat: x\""}) is None
    assert classify_commit({"sha": "t", "message": "test: add test"}) is None
    scoped = classify_commit({"sha": "s", "message": "feat(api): add endpoint", "files": ["web/routes.py"]})
    assert scoped.module == "api"
    by_path = classify_commit({"sha": "p", "message": "feat: add endpoint", "files": ["web/routes.py"]})
    assert by_path.module == "web"
    assert classify_commit({"sha": "g", "message": "feat: add endpoint"}).module == "general"


def test_overrides_can_restore_hide_change_type_and_pin():
    overrides = [
        {"sha": "hidden", "hide": True},
        {"sha": "restored", "restore": True, "changeType": "feature"},
        {"sha": "changed", "changeType": "algorithm", "module": "core", "pin": True},
    ]
    assert classify_commit({"sha": "hidden", "message": "docs: hidden"}, overrides=overrides) is None
    restored = classify_commit({"sha": "restored", "message": "docs: restored"}, overrides=overrides)
    assert restored is not None and restored.change_type == "feature"
    changed = classify_commit({"sha": "changed", "message": "feat: x"}, overrides=overrides)
    assert changed.change_type == "algorithm" and changed.module == "core" and changed.pinned is True


def test_classify_commits_is_sorted_and_deduplicated():
    values = classify_commits([
        {"sha": "b", "message": "feat: b", "occurred_at": "2026-01-02T00:00:00Z"},
        {"sha": "a", "message": "feat: a", "occurred_at": "2026-01-01T00:00:00Z"},
        {"sha": "a", "message": "feat: duplicate", "occurred_at": "2026-01-01T00:00:00Z"},
    ])
    assert [item.sha for item in values] == ["a", "b"]
