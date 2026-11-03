"""组织看板与 Release Portal CLI 兼容性测试。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

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

    def __init__(self, repos, members=None, commit_authors=None):
        """保存测试仓库、组织成员和提交作者映射。"""
        self._repos = repos
        self._members = members if members is not None else ["FangYikaii", "Local Dev"]
        self._commit_authors = commit_authors or {}

    def get_json(self, path):
        """返回组织元数据；fork 详情带上上游默认分支。"""
        if "/commits/" in path:
            login = self._commit_authors.get(path.rsplit("/", 1)[-1])
            return {"author": {"login": login} if login else None}
        if str(path).rsplit("/", 1)[-1] == "ForkedProject":
            return {
                "fork": True,
                "parent": {
                    "clone_url": "https://example.invalid/upstream.git",
                    "default_branch": "preview",
                },
            }
        return {"login": "SynlysAI", "name": "SynlysAI"}

    def get_json_safe(self, path, default):
        """返回提交作者详情或空语言统计。"""
        for sha, login in self._commit_authors.items():
            if str(path).endswith(f"/commits/{sha}"):
                return {"author": {"login": login}}
        return default

    def get_json_response(self, path):
        """返回无速率限制信息的响应。"""
        del path
        return {}, SimpleNamespace(headers={})

    def paginate(self, path, *, max_pages=10, strict=False):
        """返回配置的仓库列表，其余接口返回空。"""
        del max_pages, strict
        if path.startswith("/orgs/SynlysAI/repos"):
            return self._repos
        if path.startswith("/orgs/SynlysAI/members"):
            return [{"login": login} for login in self._members]
        return []


def _empty_history(repo, parent):
    """测试夹具：不读取真实 Git 历史。"""
    del repo, parent
    return []


def test_dashboard_counts_all_repos_by_default():
    """未配置 allowlist 时，公开、私有、归档与 Fork 仓库全部计入统计。"""
    client = FakeClient(_fake_repo_payload())
    data = dashboard.collect_org_analytics(
        "SynlysAI",
        client,
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=True,
        repo_allowlist=set(),
        commit_source=_empty_history,
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
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=False,
        repo_allowlist=set(),
        commit_source=_empty_history,
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

    def commits(repo, parent):
        """只给私有仓库一条未关联 GitHub 账号的提交。"""
        del parent
        if repo["name"] != "SmartAccess":
            return []
        return [
            dashboard.CommitRecord(
                sha="a" * 40,
                author_name="Local Dev",
                author_email="local@example.com",
                authored_at=datetime.now(UTC),
                author_login="Local Dev",
            )
        ]

    client = FakeClient(_fake_repo_payload())
    data = dashboard.collect_org_analytics(
        "SynlysAI",
        client,
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=True,
        repo_allowlist=allowlist,
        commit_source=commits,
        organization_members={"Local Dev"},
    )

    assert [repo.name for repo in data["repos"]] == ["SmartAccess", "AI4MS"]
    assert data["source_facts"]["skipped_repos"] == {"not_allowlisted": 3}
    private_repo = next(repo for repo in data["repos"] if repo.name == "SmartAccess")
    assert private_repo.display_name == "Private 01"
    assert private_repo.description == "Private repository"
    assert private_repo.default_branch == "hidden"
    assert private_repo.total_commits == 1
    assert data["summary"]["total_commits"] == 1
    assert data["summary"]["contributor_count"] == 1

    svg = dashboard.render_dashboard(data)
    assert "InternalResearch" not in svg
    assert "ArchivedProject" not in svg
    assert "ForkedProject" not in svg
    assert "SmartAccess" not in svg
    assert "local@example.com" not in svg
    assert "Local Dev" in svg
    assert "Private 01" in svg
    assert "private-main" not in svg
    assert "secret-release" not in svg
    assert "branches=all" in svg
    assert "forks=incremental" in svg
    assert "organization members only" in svg
    assert "visible org members" not in svg
    assert "non-tracked" not in svg


def test_fyk_and_fangyikaii_merge_into_one_contributor():
    """fyk 与 FangYikaii 的不同邮箱提交合并为同一个贡献者。"""

    def commits(repo, parent):
        """按仓库返回需要归并的作者提交。"""
        del parent
        now = datetime.now(UTC)
        if repo["name"] == "AI4MS":
            return [
                dashboard.CommitRecord(sha="a" * 40, author_name="fyk", author_email="fyk@example.com", authored_at=now),
                dashboard.CommitRecord(sha="b" * 40, author_name="Yikai Fang", author_email="fyk@example.com", authored_at=now),
            ]
        if repo["name"] == "ArchivedProject":
            return [
                dashboard.CommitRecord(
                    sha="c" * 40,
                    author_name="Yikai Fang",
                    author_email="342363948+FangYikaii@users.noreply.github.com",
                    authored_at=now,
                )
            ]
        if repo["name"] == "ForkedProject":
            return [
                dashboard.CommitRecord(sha="d" * 40, author_name="Other", author_email="other@example.com", authored_at=now)
            ]
        return []

    data = dashboard.collect_org_analytics(
        "SynlysAI",
        FakeClient(_fake_repo_payload()),
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=True,
        repo_allowlist=set(),
        commit_source=commits,
        organization_members={"FangYikaii"},
    )
    contributors = {item["login"]: item for item in data["contributors"]}

    assert "fyk" not in contributors
    assert contributors["FangYikaii"]["commits"] == 3
    assert contributors["FangYikaii"]["repo_count"] == 2
    assert "Other" not in contributors
    assert data["summary"]["contributor_count"] == 1
    svg = dashboard.render_dashboard(data)
    assert ">fyk<" not in svg
    assert "FangYikaii" in svg


def test_non_member_commits_are_filtered_by_github_author_login():
    """无法从 Git 邮箱识别时，按提交详情的 GitHub 登录名过滤外部作者。"""

    member_sha = "e" * 40
    external_sha = "f" * 40

    def commits(repo, parent):
        """在同一仓库返回组织成员和外部作者的提交。"""
        del parent
        if repo["name"] != "AI4MS":
            return []
        now = datetime.now(UTC)
        return [
            dashboard.CommitRecord(
                sha=member_sha,
                author_name="Unknown Member Name",
                author_email="member@company.example",
                authored_at=now,
            ),
            dashboard.CommitRecord(
                sha=external_sha,
                author_name="Yuxi",
                author_email="yuxi@external.example",
                authored_at=now,
            ),
        ]

    data = dashboard.collect_org_analytics(
        "SynlysAI",
        FakeClient(
            _fake_repo_payload(),
            members=["synlys-member"],
            commit_authors={member_sha: "synlys-member", external_sha: "yuxi"},
        ),
        repo_visibility="all",
        hide_private_repo_names=True,
        include_forks=True,
        repo_allowlist=set(),
        commit_source=commits,
    )

    ai4ms = next(repo for repo in data["repos"] if repo.name == "AI4MS")
    assert ai4ms.total_commits == 1
    assert data["summary"]["total_commits"] == 1
    assert [item["login"] for item in data["contributors"]] == ["synlys-member"]
    assert data["source_facts"]["filtered_commit_count"] == 1


def test_empty_member_list_stops_before_reading_history():
    """私有成员不可见时停止生成，避免把有效图表覆盖为零。"""
    with pytest.raises(dashboard.HistoryError, match="组织成员名单为空"):
        dashboard.collect_org_analytics(
            "SynlysAI",
            FakeClient(_fake_repo_payload(), members=[]),
            repo_visibility="all",
            hide_private_repo_names=True,
            include_forks=True,
            repo_allowlist=set(),
            commit_source=lambda *_args: pytest.fail("空成员名单不应读取历史"),
        )


def test_author_request_failure_stops_instead_of_silently_filtering(monkeypatch):
    """作者接口权限或限流错误不可当作外部成员而被静默吞掉。"""
    client = FakeClient([])

    def fail_request(_path):
        """模拟 GitHub 作者详情请求失败。"""
        raise PermissionError("forbidden")

    monkeypatch.setattr(client, "get_json", fail_request)
    commit = dashboard.CommitRecord("a" * 40, "Member", "member@example.com", None)
    with pytest.raises(dashboard.HistoryError, match="无法校验提交作者"):
        dashboard._is_organization_member_commit(client, "SynlysAI", "AI4MS", commit, {"member"}, {})


def test_author_email_resolution_is_cached_and_external_alias_cannot_bypass_filter(monkeypatch):
    """同一邮箱只解析一次，Git 显示名不能覆盖已确认的外部账号。"""
    client = FakeClient([])
    calls = []

    def author_response(path):
        """返回与组织成员同名但实际属于外部账号的作者。"""
        calls.append(path)
        return {"author": {"login": "yuxi"}}

    monkeypatch.setattr(client, "get_json", author_response)
    cache = {}
    for sha in ("a" * 40, "b" * 40):
        commit = dashboard.CommitRecord(sha, "fyk", "external@example.com", None)
        assert not dashboard._is_organization_member_commit(
            client, "SynlysAI", "plane", commit, {"fangyikaii"}, cache,
        )
    assert len(calls) == 1


def test_every_commit_filtered_stops_generation():
    """成员列表非空但所有作者均未匹配时，中止异常归零更新。"""
    def external_history(_repo, _parent):
        """返回外部作者的 noreply 提交。"""
        return [dashboard.CommitRecord("f" * 40, "Yuxi", "yuxi@users.noreply.github.com", None)]

    with pytest.raises(dashboard.HistoryError, match="全部提交被过滤"):
        dashboard.collect_org_analytics(
            "SynlysAI",
            FakeClient(_fake_repo_payload()[:1]),
            repo_visibility="all",
            hide_private_repo_names=True,
            include_forks=True,
            repo_allowlist=set(),
            commit_source=external_history,
        )


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
