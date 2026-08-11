"""OpenAI 兼容接口的 Release Portal 双语文案语义整理。"""

from __future__ import annotations

import json
import os
import re
import time
from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

import requests

AI_BASE_URL_ENV = "AI_BASE_URL"
AI_MODEL_ENV = "AI_MODEL"
AI_API_KEY_ENV = "AI_API_KEY"
AI_PRIVATE_ENDPOINT_ALLOWLIST_ENV = "AI_PRIVATE_ENDPOINT_ALLOWLIST"
ALLOWED_CHANGE_TYPES = frozenset(
    {"feature", "algorithm", "performance", "bugfix", "architecture"}
)
MAX_TITLE_LENGTH = 200
MAX_SUMMARY_LENGTH = 1000
MAX_DETAILS_LENGTH = 4000
MAX_MODULE_LENGTH = 120
MAX_INPUT_ITEMS = 100
MAX_INPUT_TEXT_LENGTH = 2000
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_TOKEN_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|bearer\s+[A-Za-z0-9._-]+)\b",
    re.IGNORECASE,
)
_CODE_BLOCK_PATTERN = re.compile(r"```.*?```", re.DOTALL)
_DIFF_PATH_A = r'(?:(?:"a/(?:\\.|[^"\\])+")|(?:a/(?:\\.|[^\s])+))'
_DIFF_PATH_B = r'(?:(?:"b/(?:\\.|[^"\\])+")|(?:b/(?:\\.|[^\s])+))'
_DIFF_BLOCK_PATTERN = re.compile(
    rf"^diff --git {_DIFF_PATH_A} {_DIFF_PATH_B}.*?"
    rf"(?=^diff --git {_DIFF_PATH_A} {_DIFF_PATH_B}|\Z)",
    re.MULTILINE | re.DOTALL,
)
_DIFF_LINE_PATTERN = re.compile(
    r"^(?:diff --git a/[^ ]+ b/[^ ]+|index [0-9a-f]+\.\.[0-9a-f]+(?: \d+)?|--- a/[^ ]+|\+\+\+ b/[^ ]+|@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@).*$",
    re.MULTILINE,
)
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})
_BILINGUAL_KEYS = frozenset({"zh", "en"})
_RESULT_KEYS = frozenset(
    {"title", "summary", "detailsMarkdown", "changeType", "module"}
)
_TYPE_LABELS = {
    "feature": ("新增功能", "New feature"),
    "algorithm": ("算法改进", "Algorithm improvement"),
    "performance": ("性能优化", "Performance improvement"),
    "bugfix": ("问题修复", "Bug fix"),
    "architecture": ("架构调整", "Architecture change"),
}


class AIError(RuntimeError):
    """AI 语义整理失败的基类。"""


class AIConfigurationError(AIError, ValueError):
    """AI 配置缺失或端点不符合安全策略。"""


class AITransportError(AIError):
    """AI HTTP 传输不可恢复失败。"""


class AITimeoutError(AITransportError):
    """AI HTTP 请求在重试后仍然超时。"""


class AIResponseError(AIError, ValueError):
    """AI 返回内容不是符合约束的结构化结果。"""


def redact_text(value: Any, *, max_length: int = MAX_INPUT_TEXT_LENGTH) -> str:
    """脱敏并截断允许发送给模型的自然语言文本。

    Args:
        value: 原始文本或可转换为文本的值。
        max_length: 脱敏后文本的最大字符数。

    Returns:
        不包含邮箱、访问令牌和 URL 的文本。
    """
    text = str(value or "")
    text = _CODE_BLOCK_PATTERN.sub("[REDACTED_CODE]", text)
    text = _DIFF_BLOCK_PATTERN.sub("[REDACTED_DIFF]", text)
    text = _DIFF_LINE_PATTERN.sub("[REDACTED_DIFF]", text)
    text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    text = _TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
    text = _URL_PATTERN.sub("[REDACTED_URL]", text)
    text = "".join(char for char in text if char >= " " or char in "\n\t")
    return text.strip()[:max_length]


def build_ai_request(
    product_name: str,
    commit_messages: Iterable[Any] = (),
    pull_requests: Iterable[Any] = (),
    *,
    change_type: str,
    module: str,
) -> dict[str, Any]:
    """构建仅含批准字段的模型输入。

    Args:
        product_name: 产品公开名称。
        commit_messages: Commit message 文本或包含 ``message`` 的映射。
        pull_requests: PR 标题和描述映射，其他字段会被忽略。
        change_type: 确定性分类得到的变更类型。
        module: 确定性分类得到的模块名称。

    Returns:
        可安全序列化并发送给模型的输入映射。

    Raises:
        ValueError: 变更类型或模块不符合确定性候选约束。
    """
    if change_type not in ALLOWED_CHANGE_TYPES:
        raise ValueError("不支持的 changeType")
    safe_module = redact_text(module, max_length=MAX_MODULE_LENGTH)
    if not safe_module:
        raise ValueError("module 不能为空")
    safe_commits = []
    for item in list(commit_messages or ())[:MAX_INPUT_ITEMS]:
        if isinstance(item, Mapping):
            source = item.get("message", "")
        elif isinstance(item, str):
            source = item
        else:
            source = getattr(item, "message", "")
        text = redact_text(source)
        if text:
            safe_commits.append(text)
    safe_prs = []
    for item in list(pull_requests or ())[:MAX_INPUT_ITEMS]:
        if isinstance(item, Mapping):
            title = redact_text(item.get("title", ""))
            description = redact_text(item.get("body", item.get("description", "")))
        else:
            title = redact_text(getattr(item, "title", ""))
            description = redact_text(getattr(item, "body", getattr(item, "description", "")))
        if title or description:
            safe_prs.append({"title": title, "description": description})
    return {
        "productName": redact_text(product_name, max_length=MAX_MODULE_LENGTH),
        "commitMessages": safe_commits,
        "pullRequests": safe_prs,
        "changeType": change_type,
        "module": safe_module,
    }


def validate_ai_result(value: Any) -> dict[str, Any]:
    """校验模型结构化输出的 JSON Schema、长度和枚举值。

    Args:
        value: 解析后的模型 JSON 值。

    Returns:
        通过严格校验的深拷贝结果。

    Raises:
        AIResponseError: 字段、类型、长度或枚举不符合公开数据契约。
    """
    if not isinstance(value, Mapping):
        raise AIResponseError("AI 响应根节点必须是对象")
    keys = frozenset(value)
    if keys != _RESULT_KEYS:
        missing = ", ".join(sorted(_RESULT_KEYS - keys))
        unknown = ", ".join(sorted(keys - _RESULT_KEYS))
        if unknown:
            raise AIResponseError("AI 响应包含未知字段")
        detail = "; ".join(part for part in (
            f"缺少字段: {missing}" if missing else "",
            f"未知字段: {unknown}" if unknown else "",
        ) if part)
        raise AIResponseError(f"AI 响应不符合字段契约: {detail}")
    result: dict[str, Any] = {}
    for field, limit in (
        ("title", MAX_TITLE_LENGTH),
        ("summary", MAX_SUMMARY_LENGTH),
        ("detailsMarkdown", MAX_DETAILS_LENGTH),
    ):
        bilingual = value[field]
        if not isinstance(bilingual, Mapping) or frozenset(bilingual) != _BILINGUAL_KEYS:
            raise AIResponseError(f"{field} 必须且只能包含 zh/en")
        checked: dict[str, str] = {}
        for language in ("zh", "en"):
            text = bilingual[language]
            if not isinstance(text, str):
                raise AIResponseError(f"{field}.{language} 必须是字符串")
            if len(text) > limit:
                raise AIResponseError(f"{field}.{language} 超过最大长度 {limit}")
            if field != "detailsMarkdown" and not text.strip():
                raise AIResponseError(f"{field}.{language} 不能为空")
            checked[language] = text
        result[field] = checked
    change_type = value["changeType"]
    if not isinstance(change_type, str) or change_type not in ALLOWED_CHANGE_TYPES:
        raise AIResponseError("changeType 不在允许枚举中")
    module = value["module"]
    if not isinstance(module, str) or not module.strip() or len(module) > MAX_MODULE_LENGTH:
        raise AIResponseError("module 必须是非空且长度受限的字符串")
    result["changeType"] = change_type
    result["module"] = module
    return result


class AIClient:
    """通过 OpenAI 兼容 API 生成受约束的双语候选文案。"""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        *,
        session: Any | None = None,
        client: Any | None = None,
        transport: Any | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_factor: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        private_endpoint_allowlist: Iterable[str] = (),
        approved_endpoints: Iterable[str] | None = None,
    ) -> None:
        """初始化 AI 客户端及其可注入 HTTP 传输层。

        Args:
            base_url: OpenAI 兼容 API 根地址，缺省读取 ``AI_BASE_URL``。
            model: 模型名称，缺省读取 ``AI_MODEL``。
            api_key: API 密钥，缺省读取 ``AI_API_KEY``。
            session: 提供 ``post`` 方法的 requests/httpx 兼容客户端。
            client: ``session`` 的兼容别名。
            transport: ``session`` 的兼容别名。
            timeout: 单次请求超时秒数。
            max_retries: 可恢复失败后的额外重试次数。
            backoff_factor: 指数退避基数。
            sleep: 可注入等待函数，测试中可替换。
            private_endpoint_allowlist: 私有仓库允许使用的端点列表。
            approved_endpoints: ``private_endpoint_allowlist`` 的兼容别名。

        Returns:
            无返回值。
        """
        self.base_url = (base_url if base_url is not None else os.getenv(AI_BASE_URL_ENV, "")).rstrip("/")
        self.model = model if model is not None else os.getenv(AI_MODEL_ENV, "")
        self.api_key = api_key if api_key is not None else os.getenv(AI_API_KEY_ENV, "")
        supplied = [item for item in (session, client, transport) if item is not None]
        if len(supplied) > 1:
            raise ValueError("session、client 和 transport 只能提供一个")
        self.session = supplied[0] if supplied else None
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = backoff_factor
        self.sleep = sleep
        configured = approved_endpoints if approved_endpoints is not None else private_endpoint_allowlist
        if not configured:
            configured = tuple(
                item.strip()
                for item in os.getenv(AI_PRIVATE_ENDPOINT_ALLOWLIST_ENV, "").split(",")
                if item.strip()
            )
        self.private_endpoint_allowlist = frozenset(
            self._normalized_endpoint(item) for item in configured if str(item).strip()
        )

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "AIClient":
        """使用 AI_* 环境变量构建客户端。

        Args:
            **kwargs: 传给构造函数的额外参数，如测试传输层。

        Returns:
            已读取环境变量的 AI 客户端。
        """
        return cls(**kwargs)

    @staticmethod
    def _normalized_endpoint(value: str) -> str:
        """规范化端点以进行精确的私有仓库 allowlist 比较。

        Args:
            value: OpenAI 兼容 API 根地址。

        Returns:
            去除末尾斜杠后的 HTTPS 端点。

        Raises:
            AIConfigurationError: 地址不是带主机名的 HTTPS URL。
        """
        endpoint = str(value).strip().rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise AIConfigurationError("AI endpoint 必须是 HTTPS URL")
        return endpoint

    def _validate_configuration(self, *, repository_private: bool) -> None:
        """校验环境配置与私有仓库端点策略。

        Args:
            repository_private: 当前候选是否来自私有仓库。

        Returns:
            无返回值。

        Raises:
            AIConfigurationError: 配置缺失或私有仓库端点未获批准。
        """
        if not self.base_url or not self.model or not self.api_key:
            raise AIConfigurationError("AI_BASE_URL、AI_MODEL 和 AI_API_KEY 均为必填配置")
        endpoint = self._normalized_endpoint(self.base_url)
        if repository_private is not False and endpoint not in self.private_endpoint_allowlist:
            raise AIConfigurationError("私有仓库只能使用组织批准的 AI endpoint")

    def _headers(self) -> dict[str, str]:
        """构建不含候选数据的认证请求头。

        Args:
            无。

        Returns:
            OpenAI 兼容请求头映射。
        """
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _endpoint(self) -> str:
        """返回 chat completions 请求地址。

        Args:
            无。

        Returns:
            OpenAI 兼容 chat completions URL。
        """
        return f"{self.base_url}/chat/completions"

    def _post(self, payload: dict[str, Any]) -> Any:
        """发送模型请求并对超时、限流和服务端错误执行重试。

        Args:
            payload: OpenAI chat completions 请求体。

        Returns:
            HTTP 响应对象。

        Raises:
            AITimeoutError: 重试后仍超时。
            AITransportError: 网络或 HTTP 层不可恢复失败。
        """
        session = self.session if self.session is not None else requests.Session()
        last_exception: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = session.post(
                    self._endpoint(), headers=self._headers(), json=payload, timeout=self.timeout
                )
            except Exception as exc:  # 注入 requests 或 httpx 传输层时异常类型不同。
                last_exception = exc
                if self._is_timeout(exc):
                    if attempt < self.max_retries:
                        self.sleep(self._retry_delay(attempt))
                        continue
                    raise AITimeoutError("AI 请求超时且重试耗尽") from exc
                if self._is_retryable_exception(exc) and attempt < self.max_retries:
                    self.sleep(self._retry_delay(attempt))
                    continue
                raise AITransportError("AI 网络请求失败") from exc
            status = int(getattr(response, "status_code", 200))
            if 200 <= status < 300:
                return response
            retryable = status in _RETRYABLE_STATUS_CODES or status >= 500
            if retryable and attempt < self.max_retries:
                self.sleep(self._retry_delay(attempt, response))
                continue
            raise AITransportError(f"AI API 请求失败: HTTP {status}")
        raise AITransportError("AI 请求失败") from last_exception

    @staticmethod
    def _is_timeout(exc: Exception) -> bool:
        """判断 requests/httpx 兼容异常是否表示超时。

        Args:
            exc: 传输层抛出的异常。

        Returns:
            超时异常时返回 ``True``。
        """
        return isinstance(exc, TimeoutError) or "timeout" in exc.__class__.__name__.casefold()

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        """判断网络异常是否适合重试。

        Args:
            exc: 传输层抛出的异常。

        Returns:
            可恢复网络异常时返回 ``True``。
        """
        if isinstance(exc, requests.RequestException):
            return True
        name = exc.__class__.__name__.casefold()
        return any(token in name for token in ("connect", "connection", "network", "transport", "timeout"))

    def _retry_delay(self, attempt: int, response: Any | None = None) -> float:
        """计算注入式测试可控的指数退避等待时间。

        Args:
            attempt: 从零开始的重试序号。
            response: 可选 HTTP 响应，用于读取 ``Retry-After``。

        Returns:
            应等待的秒数。
        """
        headers = getattr(response, "headers", {}) if response is not None else {}
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass
        return self.backoff_factor * (2 ** attempt)

    @staticmethod
    def _response_content(response: Any) -> str:
        """提取 OpenAI chat completion 的唯一文本内容。

        Args:
            response: OpenAI 兼容 HTTP 响应。

        Returns:
            模型返回的 JSON 文本。

        Raises:
            AIResponseError: 响应不是预期的 chat completion 结构。
        """
        try:
            payload = response.json()
        except Exception as exc:
            raise AIResponseError("AI HTTP 响应不是 JSON") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIResponseError("AI 响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str):
            raise AIResponseError("AI 响应 content 必须是 JSON 字符串")
        return content

    @staticmethod
    def _parse_result(content: str) -> dict[str, Any]:
        """严格解析并校验模型输出 JSON。

        Args:
            content: 模型返回的原始文本。

        Returns:
            通过结构化校验的结果。

        Raises:
            AIResponseError: 文本不是合法或合规的 JSON。
        """
        try:
            value = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AIResponseError("AI 输出不是合法 JSON") from exc
        return validate_ai_result(value)

    def _request_payload(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """将白名单输入包装为 OpenAI chat completions 请求体。

        Args:
            request: 已脱敏的候选输入。

        Returns:
            OpenAI 兼容 JSON 请求体。
        """
        return {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return exactly one JSON object with title.zh, title.en, "
                        "summary.zh, summary.en, detailsMarkdown.zh, "
                        "detailsMarkdown.en, changeType, and module."
                    ),
                },
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
        }

    def generate(
        self,
        *,
        product_name: str,
        commit_messages: Iterable[Any] = (),
        pull_requests: Iterable[Any] = (),
        change_type: str,
        module: str,
        repository_private: bool = True,
        deterministic_candidate: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """生成合规双语文案，失败时保留确定性候选并标记复核原因。

        Args:
            product_name: 产品公开名称。
            commit_messages: 允许发送的脱敏 Commit message 来源。
            pull_requests: 允许发送的脱敏 PR 标题和描述来源。
            change_type: 确定性分类得到的变更类型。
            module: 确定性分类得到的模块名称。
            repository_private: 是否来自私有仓库。
            deterministic_candidate: 已生成的确定性候选事件。

        Returns:
            AI 结果，或含 ``reviewReason: ai_failed`` 的确定性候选。

        Raises:
            AIError: 未提供候选时 AI 请求或输出失败。
        """
        request = build_ai_request(
            product_name, commit_messages, pull_requests,
            change_type=change_type, module=module,
        )
        try:
            self._validate_configuration(repository_private=repository_private)
            response = self._post(self._request_payload(request))
            result = self._parse_result(self._response_content(response))
            # 分类和模块来自确定性规则，模型不能改变它们。
            result["changeType"] = request["changeType"]
            result["module"] = request["module"]
            return result
        except AIError:
            if deterministic_candidate is None:
                raise
            fallback = deepcopy(dict(deterministic_candidate))
            fallback["reviewReason"] = "ai_failed"
            return fallback

    def generate_candidate(self, **kwargs: Any) -> dict[str, Any]:
        """兼容候选事件语义整理调用的 ``generate`` 别名。

        Args:
            **kwargs: ``generate`` 接受的关键字参数。

        Returns:
            AI 文案或带人工复核原因的确定性候选。
        """
        return self.generate(**kwargs)

    def enrich_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        product_name: str,
        commit_messages: Iterable[Any] = (),
        pull_requests: Iterable[Any] = (),
        repository_private: bool = True,
    ) -> dict[str, Any]:
        """将 AI 文案合入确定性候选，失败时原样保留候选供人工审核。

        Args:
            candidate: 确定性聚合得到的候选事件。
            product_name: 产品公开名称。
            commit_messages: Commit message 来源。
            pull_requests: PR 标题和描述来源。
            repository_private: 是否来自私有仓库。

        Returns:
            已合入 AI 文案或标记 ``ai_failed`` 的候选事件。

        Raises:
            ValueError: 候选缺少确定性分类字段。
        """
        change_type = str(candidate.get("changeType") or "")
        module = str(candidate.get("module") or "")
        result = self.generate(
            product_name=product_name,
            commit_messages=commit_messages,
            pull_requests=pull_requests,
            change_type=change_type,
            module=module,
            repository_private=repository_private,
            deterministic_candidate=candidate,
        )
        if result.get("reviewReason") == "ai_failed":
            return result
        enriched = deepcopy(dict(candidate))
        enriched.update(result)
        return enriched


__all__ = [
    "AIClient",
    "AIError",
    "AIConfigurationError",
    "AITransportError",
    "AITimeoutError",
    "AIResponseError",
    "ALLOWED_CHANGE_TYPES",
    "build_ai_request",
    "redact_text",
    "validate_ai_result",
]
