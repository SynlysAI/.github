"""Release Portal AI 语义整理客户端测试。"""

from __future__ import annotations

import json

import pytest

from scripts.release_portal.ai import (
    AIClient,
    AIResponseError,
    AITimeoutError,
    validate_ai_result,
)


class FakeResponse:
    """最小 requests/httpx 兼容响应。"""

    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        self.headers = {}

    def json(self):
        if isinstance(self.payload, str):
            return json.loads(self.payload)
        return self.payload


class FakeSession:
    """记录请求并按顺序返回或抛出预置结果。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _model_response(**overrides):
    value = {
        "title": {"zh": "新增解析能力", "en": "Added parsing capability"},
        "summary": {"zh": "改善数据解析流程。", "en": "Improved the data parsing flow."},
        "detailsMarkdown": {"zh": "- 更新解析模块", "en": "- Updated parser module"},
        "changeType": "feature",
        "module": "parser",
    }
    value.update(overrides)
    return {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}


def _client(session, **kwargs):
    return AIClient(
        base_url="https://approved.example/v1",
        model="release-model",
        api_key="test-key",
        session=session,
        sleep=lambda _: None,
        **kwargs,
    )


def test_valid_response_is_strictly_parsed():
    session = FakeSession([FakeResponse(_model_response())])
    result = _client(session).generate(
        product_name="AI4MS",
        commit_messages=["feat(parser): add parser"],
        pull_requests=[{"title": "Parser", "body": "Improves parsing."}],
        change_type="feature",
        module="parser",
    )
    assert result["title"]["zh"] == "新增解析能力"
    assert result["changeType"] == "feature"


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({
            key: value for key, value in json.loads(
                _model_response()["choices"][0]["message"]["content"]
            ).items() if key != "module"
        }),
    ],
)
def test_non_json_or_missing_field_raises(payload):
    session = FakeSession([FakeResponse({"choices": [{"message": {"content": payload}}]})])
    with pytest.raises(AIResponseError):
        _client(session).generate(product_name="AI4MS", change_type="feature", module="parser")


def test_invalid_change_type_is_rejected():
    invalid = json.loads(_model_response()["choices"][0]["message"]["content"])
    invalid["changeType"] = "security"
    response = {"choices": [{"message": {"content": json.dumps(invalid)}}]}
    session = FakeSession([FakeResponse(response)])
    with pytest.raises(AIResponseError):
        _client(session).generate(product_name="AI4MS", change_type="feature", module="parser")


def test_timeout_retries_then_succeeds():
    import requests

    session = FakeSession([requests.Timeout("slow"), FakeResponse(_model_response())])
    result = _client(session, max_retries=1).generate(product_name="AI4MS", change_type="feature", module="parser")
    assert result["module"] == "parser"
    assert len(session.calls) == 2


def test_unrecoverable_timeout_keeps_deterministic_candidate():
    import requests

    session = FakeSession([requests.Timeout("slow"), requests.Timeout("slow")])
    candidate = {"title": {"zh": "确定性标题", "en": "Deterministic title"}, "module": "parser"}
    result = _client(session, max_retries=1).generate(
        product_name="AI4MS",
        change_type="feature",
        module="parser",
        deterministic_candidate=candidate,
    )
    assert result["title"]["zh"] == "确定性标题"
    assert result["reviewReason"] == "ai_failed"


def test_request_contains_only_redacted_allowlist_fields():
    session = FakeSession([FakeResponse(_model_response())])
    _client(session).generate(
        product_name="AI4MS",
        commit_messages=[{"message": "feat: add feature", "author_email": "person@example.com", "diff": "SECRET_DIFF"}],
        pull_requests=[{"title": "Add endpoint", "body": "Description https://private.example/x", "comments": "SECRET_COMMENT", "files": ["secret.py"], "attachments": ["x"]}],
        change_type="feature",
        module="api",
    )
    request = session.calls[0][1]["json"]
    serialized = json.dumps(request, ensure_ascii=False)
    assert "SECRET_DIFF" not in serialized
    assert "SECRET_COMMENT" not in serialized
    assert "secret.py" not in serialized
    assert "person@example.com" not in serialized
    assert "feat: add feature" in serialized
    assert "Add endpoint" in serialized


def test_private_repository_requires_approved_endpoint():
    session = FakeSession([FakeResponse(_model_response())])
    client = _client(session, private_endpoint_allowlist=["https://approved.example/v1"])
    client.generate(product_name="AI4MS", change_type="feature", module="parser", repository_private=True)

    forbidden = _client(FakeSession([]), private_endpoint_allowlist=["https://other.example/v1"])
    with pytest.raises(ValueError):
        forbidden.generate(product_name="AI4MS", change_type="feature", module="parser", repository_private=True)


def test_validate_ai_result_rejects_overlong_text():
    value = json.loads(_model_response()["choices"][0]["message"]["content"])
    value["title"]["zh"] = "x" * 201
    with pytest.raises(AIResponseError):
        validate_ai_result(value)
