"""GitHub REST 数据采集与归一化。"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import requests

from .config import CATALOG_PATH, load_catalog
from .models import Catalog, Product

API_ROOT = "https://api.github.com"
USER_AGENT = "synlysai-release-portal/1.0"
_RETRY_STATUSES = {403, 429}


class GitHubError(RuntimeError):
    """GitHub 请求不可恢复时抛出的结构化异常。"""

    def __init__(self, message: str, *, status: int | None = None,
                 method: str | None = None, path: str | None = None,
                 body: str | None = None, retryable: bool = False) -> None:
        """初始化 GitHub 请求异常。

        Args:
            message: 异常消息。
            status: GitHub HTTP 状态码。
            method: 触发异常的 HTTP 方法。
            path: 触发异常的 API 路径。
            body: 响应正文或其他错误详情。
            retryable: 是否可以通过重试恢复。
        Returns:
            无。
        Raises:
            无。
        """
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        """将异常转换为可记录的字典。"""
        return {"message": str(self), "status": self.status, "method": self.method,
                "path": self.path, "body": self.body, "retryable": self.retryable}


@dataclass(frozen=True)
class ReleaseAsset:
    """Release 附件的公开元数据。"""

    name: str
    size: int
    content_type: str | None
    download_url: str | None
    platform: str
    architecture: str

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容的附件映射。"""
        return asdict(self)


@dataclass(frozen=True)
class PullRequest:
    """提交关联的 PR 元数据。"""

    number: int
    title: str
    body: str
    url: str | None

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容的 PR 映射。"""
        return asdict(self)


@dataclass(frozen=True)
class Commit:
    """归一化提交元数据。"""

    sha: str
    occurred_at: str | None
    message: str
    pull_requests: tuple[PullRequest, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容的提交映射。"""
        value = asdict(self)
        value["pull_requests"] = [item.to_dict() for item in self.pull_requests]
        return value


@dataclass(frozen=True)
class Release:
    """归一化 GitHub Release 元数据。"""

    id: str
    tag: str | None
    name: str
    body: str
    published_at: str | None
    release_url: str | None
    prerelease: bool
    draft: bool
    assets: tuple[ReleaseAsset, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容的 Release 映射。"""
        value = asdict(self)
        value["assets"] = [asset.to_dict() for asset in self.assets]
        return value


def _asset_target(name: str) -> tuple[str, str]:
    """从附件名称推断平台和架构。

    Args:
        name: 附件名称。
    Returns:
        平台和架构二元组。
    """
    lower = name.lower()
    platform = "windows" if any(x in lower for x in ("win", ".exe", ".msi")) else "macos" if any(x in lower for x in ("darwin", "macos", ".dmg")) else "linux" if any(x in lower for x in ("linux", ".deb", ".rpm", ".appimage")) else "unknown"
    architecture = "arm64" if any(x in lower for x in ("arm64", "aarch64")) else "x86_64" if any(x in lower for x in ("x86_64", "amd64", "x64")) else "unknown"
    return platform, architecture


class GitHubClient:
    """GitHub REST 客户端。"""

    def __init__(self, token: str | None = None, *, session: requests.Session | Any | None = None,
                 api_root: str = API_ROOT, timeout: float = 30.0, max_retries: int = 3,
                 backoff_factor: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> None:
        """初始化客户端。\n\nArgs:\n    token: 只读安装令牌。\n    session: requests 兼容会话。\n    api_root: API 根地址。\n    timeout: 请求超时秒数。\n    max_retries: 最大重试次数。\n    backoff_factor: 退避基数。\n    sleep: 等待函数。\nReturns:\n    无。"""
        self.token = token
        self.session = session if session is not None else requests.Session()
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.sleep = sleep

    def _headers(self) -> dict[str, str]:
        """构造请求头。

Args:
    无。
Returns:
    请求头映射。"""
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _safe_body(self, body: Any) -> str:
        """脱敏异常正文中的当前访问令牌。

        Args:
            body: 原始响应正文。
        Returns:
            截断并脱敏后的正文。
        """
        value = str(body or "")[:1000]
        return value.replace(self.token, "[REDACTED]") if self.token else value

    def _validate_url(self, url: str, path: str) -> str:
        """确保分页 URL 与 API 根地址同源。

        Args:
            url: 待访问 URL。
            path: 原始 API 路径。
        Returns:
            通过校验的 URL。
        Raises:
            GitHubError: URL 指向外部主机。
        """
        root = urlparse(self.api_root)
        target = urlparse(url)
        if target.scheme != root.scheme or target.netloc != root.netloc:
            raise GitHubError("GitHub 分页 URL 不在 API 同源范围内", method="GET", path=path, body=self._safe_body(url))
        return url

    def _request(self, path: str) -> requests.Response:
        """请求 JSON 接口并处理限流及服务端暂时性错误。

        Args:
            path: API 相对路径或绝对 URL。
        Returns:
            requests.Response 实例。
        Raises:
            GitHubError: 请求失败且无法恢复时抛出。
        """
        url = path if path.startswith("http") else f"{self.api_root}{path}"
        self._validate_url(url, path)
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, headers=self._headers(), timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise GitHubError("GitHub 网络请求失败", method="GET", path=path, body=self._safe_body(exc), retryable=True) from exc
                self.sleep(self.backoff_factor * (2 ** attempt))
                continue
            status = int(response.status_code)
            if status < 400:
                return response
            retryable = status in _RETRY_STATUSES or status >= 500
            if retryable and attempt < self.max_retries:
                self.sleep(self._retry_delay(response, attempt))
                continue
            body = self._safe_body(getattr(response, "text", ""))
            raise GitHubError(f"GitHub API 请求失败: HTTP {status}", status=status, method="GET", path=path, body=body, retryable=retryable)
        raise AssertionError("unreachable")

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        """计算重试等待时间。

Args:
    response: HTTP 响应。
    attempt: 重试序号。
Returns:
    等待秒数。"""
        headers = {str(k).lower(): str(v) for k, v in getattr(response, "headers", {}).items()}
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    return max(0.0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        reset = headers.get("x-ratelimit-reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time())
            except ValueError:
                pass
        return self.backoff_factor * (2 ** attempt)

    def _json(self, path: str) -> Any:
        """请求并解析 JSON。

Args:
    path: API 路径。
Returns:
    JSON 值。"""
        response = self._request(path)
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise GitHubError("GitHub 返回了无效 JSON", status=response.status_code, method="GET", path=path, body=self._safe_body(getattr(response, "text", ""))) from exc

    @staticmethod
    def _next_link(value: str | None) -> str | None:
        """解析分页 Link。

Args:
    value: Link 响应头。
Returns:
    下一页 URL 或 None。"""
        if not value:
            return None
        for part in value.split(","):
            if 'rel="next"' in part:
                match = re.search(r"<([^>]+)>", part)
                if match:
                    return match.group(1)
        return None

    def _paginate(self, path: str) -> list[dict[str, Any]]:
        """读取并校验全部分页。

Args:
    path: 首页 API 路径。
Returns:
    对象列表。"""
        result: list[dict[str, Any]] = []
        next_path: str | None = path
        while next_path:
            response = self._request(next_path)
            try:
                payload = response.json()
            except (ValueError, TypeError) as exc:
                raise GitHubError("GitHub page returned invalid JSON", status=response.status_code, method="GET", path=next_path, body=self._safe_body(getattr(response, "text", ""))) from exc
            if not isinstance(payload, list):
                raise GitHubError("GitHub page response must be an array", status=response.status_code, method="GET", path=next_path, body=self._safe_body(getattr(response, "text", "")))
            for index, item in enumerate(payload):
                if not isinstance(item, dict):
                    raise GitHubError(f"GitHub page item must be an object: index={index}", status=response.status_code, method="GET", path=next_path, body=self._safe_body(getattr(response, "text", "")))
                result.append(item)
            next_path = self._next_link(response.headers.get("Link") or response.headers.get("link"))
            if next_path:
                next_path = self._validate_url(next_path, path)
        return result

    @staticmethod
    def _check_repository(repository: str, catalog: Catalog) -> Product:
        """检查仓库是否在 allowlist。

Args:
    repository: owner/name 仓库名。
    catalog: 产品注册表。
Returns:
    对应产品。"""
        for product in catalog.products:
            if product.repository.casefold() == repository.casefold():
                return product
        raise ValueError(f"仓库不在 catalog allowlist 中: {repository}")

    def list_releases(self, repository: str, *, catalog: Catalog | None = None) -> list[Release]:
        """读取仓库全部 Release（包含空、draft 和 pre-release）。

        Args:
            repository: ``owner/name`` 仓库名。
            catalog: 可选产品注册表，默认读取正式 catalog。
        Returns:
            归一化 Release 列表。
        """
        self._check_repository(repository, catalog or load_catalog(CATALOG_PATH))
        raw = self._paginate(f"/repos/{repository}/releases?per_page=100")
        return [self.normalize_release(item) for item in raw]

    def list_commits(self, repository: str, *, catalog: Catalog | None = None) -> list[Commit]:
        """读取仓库提交并补充存在关联 PR 的标题与正文。

        Args:
            repository: ``owner/name`` 仓库名。
            catalog: 可选产品注册表，默认读取正式 catalog。
        Returns:
            归一化 Commit 列表。
        """
        self._check_repository(repository, catalog or load_catalog(CATALOG_PATH))
        items = self._paginate(f"/repos/{repository}/commits?per_page=100")
        commits: list[Commit] = []
        for item in items:
            sha = str(item.get("sha") or "")
            if not sha:
                continue
            prs = self.list_commit_pull_requests(repository, sha)
            commit = self.normalize_commit(item, prs)
            commits.append(commit)
        return commits

    def list_commit_pull_requests(self, repository: str, sha: str, *, catalog: Catalog | None = None) -> list[PullRequest]:
        """读取提交关联 PR。

Args:
    repository: owner/name 仓库名。
    sha: 提交 SHA。
    catalog: 产品注册表。
Returns:
    PR 列表。"""
        self._check_repository(repository, catalog or load_catalog(CATALOG_PATH))
        items = self._paginate(f"/repos/{repository}/commits/{sha}/pulls")
        return [PullRequest(number=int(item.get("number", 0)), title=str(item.get("title") or ""), body=str(item.get("body") or ""), url=item.get("html_url")) for item in items]

    def collect_catalog(self, catalog: Catalog | None = None) -> dict[str, dict[str, list[Any]]]:
        """采集 catalog 六仓库。

Args:
    catalog: 产品注册表。
Returns:
    按产品分组的数据。"""
        current = catalog or load_catalog(CATALOG_PATH)
        return {product.product_id: {"releases": self.list_releases(product.repository, catalog=current), "commits": self.list_commits(product.repository, catalog=current)} for product in current.products}

    @staticmethod
    def normalize_release(item: dict[str, Any]) -> Release:
        """归一化 Release。

Args:
    item: GitHub JSON 对象。
Returns:
    Release 数据类。"""
        assets = []
        for raw in item.get("assets") or []:
            name = str(raw.get("name") or "")
            platform, architecture = _asset_target(name)
            assets.append(ReleaseAsset(name=name, size=int(raw.get("size") or 0), content_type=raw.get("content_type"), download_url=raw.get("browser_download_url"), platform=platform, architecture=architecture))
        return Release(id=str(item.get("id") or item.get("node_id") or item.get("tag_name") or ""), tag=item.get("tag_name"), name=str(item.get("name") or ""), body=str(item.get("body") or ""), published_at=item.get("published_at"), release_url=item.get("html_url"), prerelease=bool(item.get("prerelease", False)), draft=bool(item.get("draft", False)), assets=tuple(assets))

    @staticmethod
    def normalize_commit(item: dict[str, Any], pull_requests: Iterable[PullRequest] = ()) -> Commit:
        """归一化 Commit。

Args:
    item: GitHub JSON 对象。
    pull_requests: 关联 PR。
Returns:
    Commit 数据类。"""
        details = item.get("commit") or {}
        message = str(details.get("message") or item.get("message") or "")
        message = message.splitlines()[0] if message.splitlines() else ""
        author = details.get("author") or {}
        occurred_at = author.get("date") or (details.get("committer") or {}).get("date")
        return Commit(sha=str(item.get("sha") or ""), occurred_at=occurred_at, message=message, pull_requests=tuple(pull_requests))


__all__ = ["GitHubClient", "GitHubError", "Release", "ReleaseAsset", "Commit", "PullRequest"]
