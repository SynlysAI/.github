"""Commit 聚合与回填状态测试。"""

from scripts.release_portal.aggregate import aggregate_commits, update_backfill_state


def _commit(sha, when, release, message="feat(core): change"):
    return {"sha": sha, "message": message, "occurred_at": when, "release_id": release}


def test_aggregates_across_releases_and_iso_weeks_are_separate():
    commits = [
        _commit("a", "2026-01-05T00:00:00Z", "r1"),
        _commit("b", "2026-01-06T00:00:00Z", "r2"),
        _commit("c", "2026-01-12T00:00:00Z", "r3"),
    ]
    events = aggregate_commits("p", commits)
    assert [(event["level"], event["source"]["commitShas"]) for event in events] == [
        ("aggregate", ["a", "b"]),
        ("commit", ["c"]),
    ]


def test_duplicate_commits_and_hidden_override_do_not_change_output():
    commits = [_commit("a", "2026-01-05T00:00:00Z", "r1"), _commit("a", "2026-01-05T00:00:00Z", "r1")]
    assert len(aggregate_commits("p", commits)) == 1
    assert aggregate_commits("p", [_commit("a", "2026-01-05T00:00:00Z", "r1")], overrides=[{"sha": "a", "hide": True}]) == []


def test_pinned_override_is_propagated_and_backfill_checkpoint_is_resumable():
    event = aggregate_commits("p", [_commit("a", "2026-01-05T00:00:00Z", "r1")], overrides=[{"sha": "a", "pin": True}])[0]
    assert event["pinned"] is True
    state = {"schemaVersion": 1, "repositories": {"repo": {"cursor": None, "completed": False, "watermark": {"sha": None, "publishedAt": None}}}}
    update_backfill_state(state, "repo", [{"sha": "a", "occurred_at": "2026-01-05T00:00:00Z"}], completed=False)
    assert state["repositories"]["repo"]["cursor"] == "a"
    update_backfill_state(state, "repo", [], completed=True)
    assert state["repositories"]["repo"]["completed"] is True
