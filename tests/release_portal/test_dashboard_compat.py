"""组织看板与 Release Portal CLI 兼容性测试。"""

import json
from types import SimpleNamespace

from scripts import generate_org_dashboard as dashboard
from scripts.release_portal import cli


def _fake_repo_payload():
    """构造覆盖公开、私有、归档与 Fork 的仓库载荷。"""
    return [
        {
            "name": "AI4MS",
            "owner": {"login": "SynlysAI"},
            "private": False,
            "description": "public materials informatics platform",
            "default_branch": "main",
        },
        {
            "name": "InternalResearch",
            "owner": {"login": "SynlysAI"},
            "private": True,
            "description": "内部私有配方",
            "default_branch": "secret-release",
        },
        {
            "name": "SmartAccess",
            "owner": {"login": "SynlysAI"},
            "private": True,
            "description": "另一个内部私有配方",
            "default_branch": "private-main",
        },
        {
            "name": "ArchivedProject",
            "owner": {"login": "SynlysAI"},
            "private": False,
            "archived": True,
            "description": "已归档项目",
            "default_branch": "main",
        },
        {
            "name": "ForkedProject",
            "owner": {"login": "SynlysAI"},
            "private": False,
            "fork": True,
            "description": "外部项目 Fork",
            "default_branch": "main",
        },
    ]


class FakeClient:
    """为看板收集提供最小 GitHub API 替身。"""

    def __init__(self, repos):
        self._repos = repos

    def get_json(self, path):
        """返回组织元数据。"""
        del path
        return {"login": "SynlysAI", "name": "SynlysAI"}

    def get_json_safe(self, path, default):
        """返回空语言统计。"""
        del path
        return default

    def get_json_response(self, path):
        """返回无速率限制信息的响应。"""
        del path
        return {}, SimpleNamespace(headers={})

    def paginate(self, path, *, max_pages=10):
        """返回配置的仓库列表，其余接口返回空。"""
        del max_pages
        if path.startswith("/orgs/SynlysAI/repos"):
            return self._repos
        return []


def test_dashboard_counts_all_repos_by_default():
    """未配置 allowlist 时，公开、私有、归档与 Fork 仓库全部计入统计。"""
    client = FakeClient(_fake_repo_payload())
    data = dashboard.collect_org_analytics(
        "SynlysAI",
        client,
        max_commit_pages=1,
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=True,
        repo_allowlist=set(),
    )

    assert [repo.name for repo in data["repos"]] == [
        "AI4MS",
        "ArchivedProject",
        "ForkedProject",
        "InternalResearch",
        "SmartAccess",
    ]
    assert data["summary"]["repo_count"] == 5
    assert data["summary"]["public_repo_count"] == 3
    assert data["summary"]["private_repo_count"] == 2
    assert data["source_facts"]["skipped_repos"] == {}


def test_dashboard_excludes_forks_when_disabled_but_keeps_archived():
    """include_forks=False 时排除 Fork，已归档仓库仍计入。"""
    client = FakeClient(_fake_repo_payload())
    data = dashboard.collect_org_analytics(
        "SynlysAI",
        client,
        max_commit_pages=1,
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=False,
        repo_allowlist=set(),
    )

    assert [repo.name for repo in data["repos"]] == [
        "AI4MS",
        "ArchivedProject",
        "InternalResearch",
        "SmartAccess",
    ]
    assert data["summary"]["repo_count"] == 4
    assert data["source_facts"]["skipped_repos"] == {"fork": 1}


def test_dashboard_optional_allowlist_filters_and_masks_private():
    """显式 allowlist 仅在提供时生效，私有仓库计入统计但正文脱敏。"""
    allowlist = dashboard.catalog_repository_allowlist()

    assert allowlist == {
        "AI4MS",
        "Spec_Agent",
        "Poly_Agent",
        "SpecLabOS",
        "RAGPortal",
        "SmartAccess",
    }

    client = FakeClient(_fake_repo_payload())
    data = dashboard.collect_org_analytics(
        "SynlysAI",
        client,
        max_commit_pages=1,
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=True,
        repo_allowlist=allowlist,
    )

    assert [repo.name for repo in data["repos"]] == ["AI4MS", "SmartAccess"]
    assert data["source_facts"]["skipped_repos"] == {"not_allowlisted": 3}
    private_repo = data["repos"][1]
    assert private_repo.display_name == "SmartAccess"
    assert private_repo.description == "Private repository"
    assert private_repo.default_branch == "hidden"

    svg = dashboard.render_dashboard(data)
    assert "InternalResearch" not in svg
    assert "ArchivedProject" not in svg
    assert "ForkedProject" not in svg
    assert "SmartAccess" in svg
    assert "private-main" not in svg
    assert "secret-release" not in svg


def test_backfill_cli_emits_json_log_without_private_error_text(tmp_path, monkeypatch, capsys):
    """回填命令失败时也只能输出结构化字段和错误类型。"""
    state = tmp_path / "backfill.json"
    candidate = tmp_path / "timeline.json"
    state.write_text('{"schemaVersion": 1, "repositories": {}}\n', encoding="utf-8")
    candidate.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")

    class FailingClient:
        """抛出包含私有正文的失败替身。"""

        def list_commits(self, *_args, **_kwargs):
            """抛出私有错误文本。"""
            raise RuntimeError("private body ghp_secret_should_not_log")

    monkeypatch.delenv("AI_API_KEY", raising=False)
    args = cli._parser().parse_args(
        [
            "backfill",
            "--product",
            "ai4ms",
            "--state",
            str(state),
            "--candidates",
            str(candidate),
        ]
    )

    assert cli._backfill(args, github_client=FailingClient()) == 1
    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert {"runId", "productId", "stage", "count", "durationMs", "status"} <= set(payload)
    assert payload["status"] == "failed"
    assert "private body" not in line
    assert "ghp_secret" not in line
