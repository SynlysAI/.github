"""组织看板与 Release Portal CLI 兼容性测试。"""

import json
from types import SimpleNamespace

from scripts import generate_org_dashboard as dashboard
from scripts.release_portal import cli


def test_dashboard_allowlist_is_loaded_from_catalog_and_excludes_internal_repositories():
    """看板应只使用 catalog 中登记的六个仓库，不能被内部仓库污染。"""
    allowlist = dashboard.catalog_repository_allowlist()

    assert allowlist == {
        "AI4MS",
        "Spec_Agent",
        "Poly_Agent",
        "SpecLabOS",
        "RAGPortal",
        "SmartAccess",
    }

    class FakeClient:
        """为看板收集提供最小 GitHub API 替身。"""

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
            """返回登记仓库和一个未登记内部仓库。"""
            del max_pages
            if path.startswith("/orgs/SynlysAI/repos"):
                return [
                    {"name": "AI4MS", "owner": {"login": "SynlysAI"}, "private": False},
                    {"name": "InternalResearch", "owner": {"login": "SynlysAI"}, "private": True},
                ]
            return []

    data = dashboard.collect_org_analytics(
        "SynlysAI",
        FakeClient(),
        max_commit_pages=1,
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=False,
        repo_allowlist=allowlist | {"InternalResearch"},
    )

    assert [repo.name for repo in data["repos"]] == ["AI4MS"]
    assert "InternalResearch" not in dashboard.render_dashboard(data)


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
