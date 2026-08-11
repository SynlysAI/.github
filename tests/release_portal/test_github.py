"""GitHub 采集客户端测试。"""

import json
from pathlib import Path

import pytest
import requests

from scripts.release_portal.github import GitHubClient, GitHubError

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FalseySession(FakeSession):
    def __bool__(self):
        return False


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_release_pagination_and_normalization():
    session = FakeSession([
        FakeResponse(payload=fixture("github_releases_page1.json"), headers={"Link": '<https://api.test/repos/SynlysAI/AI4MS/releases?page=2>; rel="next"'}),
        FakeResponse(payload=fixture("github_releases_page2.json")),
    ])
    client = GitHubClient(token="app-token", session=session, api_root="https://api.test", sleep=lambda _: None)
    releases = client.list_releases("SynlysAI/AI4MS")
    assert len(releases) == 3
    assert releases[1].prerelease is True and releases[2].draft is True
    assert releases[0].assets[0].platform == "linux"
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer app-token"


def test_empty_release_list_is_valid():
    session = FakeSession([FakeResponse(payload=[])])
    client = GitHubClient(session=session, api_root="https://api.test", sleep=lambda _: None)
    assert client.list_releases("SynlysAI/AI4MS") == []


def test_commit_and_associated_pr_are_normalized():
    session = FakeSession([FakeResponse(payload=fixture("github_commits.json")), FakeResponse(payload=fixture("github_commit_pulls.json"))])
    client = GitHubClient(session=session, api_root="https://api.test", sleep=lambda _: None)
    commits = client.list_commits("SynlysAI/AI4MS")
    assert commits[0].sha.startswith("a")
    assert commits[0].message == "feat: add parser"
    assert commits[0].pull_requests[0].title == "Add parser"


def test_commit_batch_stops_at_limit_without_per_commit_pr_requests():
    """历史回填批次应受记录和分页上限约束，不为每条提交读取 PR。"""
    payload = [
        {"sha": character * 40, "commit": {"message": f"feat: {character}", "author": {"date": "2026-08-10T00:00:00Z"}}}
        for character in ("a", "b", "c")
    ]
    session = FakeSession([
        FakeResponse(
            payload=payload,
            headers={"Link": '<https://api.test/repos/SynlysAI/AI4MS/commits?per_page=100&page=2>; rel="next"'},
        )
    ])
    client = GitHubClient(session=session, api_root="https://api.test", sleep=lambda _: None)

    commits = client.list_commits(
        "SynlysAI/AI4MS",
        max_items=2,
        page=1,
        include_pull_requests=False,
    )

    assert [commit.sha for commit in commits] == ["a" * 40, "b" * 40]
    assert len(session.calls) == 1
    assert len(session.calls) <= 2
    assert "page=1" in session.calls[0][0]


def test_rate_limit_retry_after_and_unrecoverable_error():
    session = FakeSession([FakeResponse(status=429, payload={"message": "rate"}, headers={"Retry-After": "2"}), FakeResponse(payload=[])])
    waits = []
    client = GitHubClient(session=session, api_root="https://api.test", sleep=waits.append)
    assert client.list_releases("SynlysAI/AI4MS") == []
    assert waits == [2.0]

    failed = FakeSession([FakeResponse(status=404, payload={"message": "Not Found"}, text="Not Found")])
    client = GitHubClient(session=failed, api_root="https://api.test", sleep=lambda _: None)
    with pytest.raises(GitHubError) as error:
        client.list_releases("SynlysAI/AI4MS")
    assert error.value.status == 404 and error.value.retryable is False


def test_repository_outside_catalog_is_rejected_without_request():
    session = FakeSession([])
    client = GitHubClient(session=session, api_root="https://api.test")
    with pytest.raises(ValueError):
        client.list_releases("evil/private")
    assert session.calls == []


def test_403_and_5xx_use_exponential_backoff():
    session = FakeSession([
        FakeResponse(status=403, payload={}), FakeResponse(status=500, payload={}),
        FakeResponse(payload=[]),
    ])
    waits = []
    client = GitHubClient(session=session, api_root="https://api.test", sleep=waits.append, backoff_factor=2)
    assert client.list_releases("SynlysAI/AI4MS") == []
    assert waits == [2, 4]


def test_rate_limit_reset_header_is_respected(monkeypatch):
    monkeypatch.setattr("scripts.release_portal.github.time.time", lambda: 100)
    session = FakeSession([FakeResponse(status=429, payload={}, headers={"X-RateLimit-Reset": "145"}), FakeResponse(payload=[])])
    waits = []
    client = GitHubClient(session=session, api_root="https://api.test", sleep=waits.append)
    assert client.list_releases("SynlysAI/AI4MS") == []
    assert waits == [45.0]


def test_network_error_and_malformed_json_are_structured():
    session = FakeSession([requests.ConnectionError("offline")])
    client = GitHubClient(session=session, api_root="https://api.test", max_retries=0, sleep=lambda _: None)
    with pytest.raises(GitHubError) as network_error:
        client.list_releases("SynlysAI/AI4MS")
    assert network_error.value.method == "GET" and network_error.value.retryable is True

    class BrokenResponse(FakeResponse):
        def json(self):
            raise ValueError("bad json")

    client = GitHubClient(session=FakeSession([BrokenResponse(text="not-json")]), api_root="https://api.test", sleep=lambda _: None)
    with pytest.raises(GitHubError) as json_error:
        client.list_releases("SynlysAI/AI4MS")
    assert json_error.value.status == 200 and json_error.value.path.startswith("/repos/")


def test_commit_author_date_falls_back_to_committer_date():
    commit = GitHubClient.normalize_commit({
        "sha": "b" * 40,
        "commit": {"message": "fix: x", "author": {"date": None}, "committer": {"date": "2026-02-01T00:00:00Z"}},
    })
    assert commit.occurred_at == "2026-02-01T00:00:00Z"


def test_external_pagination_link_is_rejected_without_second_request():
    session = FakeSession([FakeResponse(payload=[], headers={"Link": '<https://evil.example/next>; rel="next"'})])
    client = GitHubClient(token="secret-token", session=session, api_root="https://api.test", sleep=lambda _: None)
    with pytest.raises(GitHubError) as error:
        client.list_releases("SynlysAI/AI4MS")
    assert error.value.status is None and "secret-token" not in str(error.value)
    assert len(session.calls) == 1


def test_empty_commit_message_normalizes_to_empty_and_falsey_session_is_kept():
    session = FalseySession([FakeResponse(payload=[])])
    client = GitHubClient(session=session, api_root="https://api.test", sleep=lambda _: None)
    assert client.session is session
    assert GitHubClient.normalize_commit({"sha": "c" * 40, "commit": {"message": ""}}).message == ""


def test_error_body_redacts_token():
    token = "secret-token"
    response = FakeResponse(status=404, payload={}, text=f"authorization={token}")
    client = GitHubClient(token=token, session=FakeSession([response]), api_root="https://api.test", sleep=lambda _: None)
    with pytest.raises(GitHubError) as error:
        client.list_releases("SynlysAI/AI4MS")
    assert token not in str(error.value)
    assert token not in str(error.value.to_dict())
