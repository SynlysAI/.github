"""GitHub 采集客户端测试。"""

import json
from pathlib import Path

import pytest

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
