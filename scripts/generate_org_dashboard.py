from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import tempfile
import textwrap
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


API_ROOT = "https://api.github.com"
USER_AGENT = "synlysai-org-dashboard/2.0"
CATALOG_PATH = Path(__file__).resolve().parents[1] / "release-portal" / "catalog.yml"


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def fmt_number(value: int | float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


def fmt_bytes(value: int) -> str:
    if value >= 1_073_741_824:
        return f"{value / 1_073_741_824:.2f} GB"
    if value >= 1_048_576:
        return f"{value / 1_048_576:.1f} MB"
    if value >= 1_024:
        return f"{value / 1_024:.1f} KB"
    return f"{value} B"


def days_since(value: datetime | None, now: datetime) -> str:
    if not value:
        return "n/a"
    days = max((now - value).days, 0)
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def wrap_label(value: str, width: int = 22) -> list[str]:
    return textwrap.wrap(value, width=width, break_long_words=False) or [value]


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def catalog_repository_allowlist(path: str | Path = CATALOG_PATH) -> set[str]:
    """从 Release Portal catalog 读取六个产品仓库名。

    组织统计看板默认统计全部仓库，不使用这份名单。

    Args:
        path: 产品注册表路径，默认使用仓库内的 ``catalog.yml``。

    Returns:
        catalog 中登记的 GitHub 仓库名称集合。
    """
    try:
        from scripts.release_portal.config import load_catalog
    except ModuleNotFoundError:
        from release_portal.config import load_catalog

    catalog = load_catalog(path)
    repositories: set[str] = set()
    for product in catalog.products:
        owner, separator, repository = product.repository.partition("/")
        if not separator or not owner or not repository:
            raise ValueError(f"catalog 仓库格式无效: {product.repository}")
        repositories.add(repository)
    return repositories


def parse_repo_allowlist(value: str | None) -> set[str]:
    """解析逗号分隔的仓库 allowlist 环境变量。

    Args:
        value: 环境变量原始值，逗号分隔仓库名；``all`` 或 ``*`` 表示不过滤。

    Returns:
        仓库名称集合；空集合表示不按 allowlist 过滤。
    """
    if not value or not value.strip():
        return set()
    stripped = value.strip()
    if stripped.lower() in {"all", "*"}:
        return set()
    return {name.strip() for name in stripped.split(",") if name.strip()}


def xml_escape(value: str) -> str:
    return html.escape(value, quote=True)


def svg_text(
    x: float,
    y: float,
    content: str,
    *,
    cls: str = "",
    anchor: str = "start",
    size: int | None = None,
) -> str:
    attrs = [f'x="{x}"', f'y="{y}"', f'text-anchor="{anchor}"']
    if cls:
        attrs.append(f'class="{cls}"')
    if size is not None:
        attrs.append(f'font-size="{size}"')
    return f"<text {' '.join(attrs)}>{xml_escape(content)}</text>"


def svg_rect(x: float, y: float, width: float, height: float, *, cls: str = "", rx: int = 24) -> str:
    class_attr = f' class="{cls}"' if cls else ""
    return f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{rx}"{class_attr} />'


@dataclass
class RepoStat:
    name: str
    display_name: str
    description: str
    default_branch: str
    stars: int
    forks: int
    total_commits: int
    recent_commits_30d: int
    code_bytes: int
    dominant_language: str
    topics: list[str]
    has_issues: bool
    has_projects: bool
    has_wiki: bool
    visibility: str
    subscribers_count: int
    license_name: str
    pushed_at: datetime | None
    is_private: bool


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def _request(self, url: str) -> urllib.request.Request:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers)

    def get_json_response(self, path: str) -> tuple[Any, Any]:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        request = self._request(url)
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            import json

            return json.loads(body), response

    def get_json(self, path: str) -> Any:
        payload, _ = self.get_json_response(path)
        return payload

    def get_json_safe(self, path: str, default: Any) -> Any:
        try:
            return self.get_json(path)
        except Exception:
            return default

    def paginate(self, path: str, *, max_pages: int | None = 10, strict: bool = False) -> list[Any]:
        """分页读取 GitHub 列表接口。

        Args:
            path: API 路径或完整 URL。
            max_pages: 最多读取的页数；``None`` 表示直到没有下一页。
            strict: 为真时，请求失败或响应不是列表会中止，而不是返回残缺结果。

        Returns:
            合并后的列表元素。
        """
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        items: list[Any] = []
        page_count = 0
        while url and (max_pages is None or page_count < max_pages):
            try:
                payload, response = self.get_json_response(url)
            except Exception as exc:
                if strict:
                    raise HistoryError("GitHub 仓库列表读取失败") from exc
                break
            if not isinstance(payload, list):
                if strict:
                    raise HistoryError("GitHub 仓库列表读取失败")
                break
            items.extend(payload)
            url = self._parse_next_link(response.headers.get("Link"))
            page_count += 1
        return items

    @staticmethod
    def _parse_next_link(link_header: str | None) -> str | None:
        if not link_header:
            return None
        for part in link_header.split(","):
            section = part.strip()
            if 'rel="next"' not in section:
                continue
            start = section.find("<")
            end = section.find(">")
            if start != -1 and end != -1 and end > start:
                return section[start + 1 : end]
        return None


@dataclass(frozen=True)
class CommitRecord:
    """一条已去重的提交。"""

    sha: str
    author_name: str
    author_email: str
    authored_at: datetime | None


class HistoryError(RuntimeError):
    """仓库提交历史无法按既定口径完整读取。"""


_NOREPLY_LOGIN = re.compile(r"^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$", re.IGNORECASE)
_LOG_FORMAT = "%H%x1f%an%x1f%ae%x1f%aI"
CommitSource = Callable[[Mapping[str, Any], Mapping[str, Any] | None], list[CommitRecord]]


def _git_env(token: str | None) -> dict[str, str]:
    """构造不把令牌写入命令行的 Git 环境。

    Args:
        token: GitHub 令牌；为空时不设置认证头。

    Returns:
        供 ``git`` 子进程使用的环境变量。
    """
    env = os.environ.copy()
    for key in list(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    config = [("protocol.file.allow", "always")]
    if token:
        config.append(("http.extraheader", f"AUTHORIZATION: bearer {token}"))
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(args: list[str], *, env: dict[str, str], repo_name: str) -> str:
    """执行 Git 命令，失败时只抛出不含令牌和远程输出的错误。

    Args:
        args: 不含 ``git`` 本身的参数。
        env: 子进程环境。
        repo_name: 用于错误信息的仓库名。

    Returns:
        标准输出文本。
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HistoryError(f"{repo_name}: git history failed") from exc
    if completed.returncode != 0:
        raise HistoryError(f"{repo_name}: git history failed ({completed.returncode})")
    return completed.stdout


def _require_remote(url: str, repo_name: str) -> str:
    """拒绝可能被当成参数或夹带空白的远程地址。

    Args:
        url: Git 远程地址。
        repo_name: 仓库名。

    Returns:
        原样返回的远程地址。
    """
    if not url or url.startswith("-") or any(char.isspace() for char in url):
        raise HistoryError(f"{repo_name}: git history failed")
    return url


def _require_branch(name: str, repo_name: str) -> str:
    """拒绝会破坏 refspec 的上游分支名。

    Args:
        name: 上游默认分支名。
        repo_name: 仓库名。

    Returns:
        原样返回的分支名。
    """
    invalid = set(" \t\n\r:~^:?*[\\")
    if not name or name.startswith("-") or any(char in invalid for char in name):
        raise HistoryError(f"{repo_name}: fork 上游信息缺失")
    return name


def _parse_commit_log(output: str) -> list[CommitRecord]:
    """把 ``git log`` 输出解析为按 SHA 去重的提交。

    Args:
        output: 使用单位分隔符的日志文本。

    Returns:
        去重后的提交记录。
    """
    records: list[CommitRecord] = []
    seen: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        sha, separator, remainder = line.partition("\x1f")
        author_name, separator, remainder = remainder.partition("\x1f")
        author_email, separator, authored_at = remainder.partition("\x1f")
        if not separator or not sha or sha in seen:
            continue
        seen.add(sha)
        records.append(
            CommitRecord(
                sha=sha,
                author_name=author_name or "unknown",
                author_email=author_email,
                authored_at=parse_iso8601(authored_at),
            )
        )
    return records


def collect_commits_with_git(
    clone_url: str,
    *,
    token: str | None = None,
    upstream_clone_url: str | None = None,
    upstream_default_branch: str | None = None,
    repo_name: str,
    is_fork: bool,
) -> list[CommitRecord]:
    """用无 blob 的 Git 历史统计全部分支提交。

    Args:
        clone_url: 仓库克隆地址。
        token: 可选 GitHub 令牌，只通过 ``http.extraheader`` 传递。
        upstream_clone_url: fork 的上游克隆地址。
        upstream_default_branch: fork 上游的默认分支，不能假定为 ``main``。
        repo_name: 出错时展示的仓库名。
        is_fork: 为真时只保留相对上游默认分支不可达的提交。

    Returns:
        按 SHA 去重的提交。没有分支时返回空列表。
    """
    remote = _require_remote(clone_url, repo_name)
    env = _git_env(token)
    heads = _run_git(["ls-remote", "--heads", remote], env=env, repo_name=repo_name)
    if not heads.strip():
        return []
    upstream_remote = None
    upstream_branch = None
    if is_fork:
        if not upstream_clone_url or not upstream_default_branch:
            raise HistoryError(f"{repo_name}: fork 上游信息缺失")
        upstream_remote = _require_remote(upstream_clone_url, repo_name)
        upstream_branch = _require_branch(upstream_default_branch, repo_name)
    with tempfile.TemporaryDirectory(prefix="org-dashboard-") as temporary:
        bare = str(Path(temporary) / "repo.git")
        _run_git(["init", "--bare", bare], env=env, repo_name=repo_name)
        _run_git(["-C", bare, "remote", "add", "origin", remote], env=env, repo_name=repo_name)
        _run_git(
            ["-C", bare, "fetch", "--filter=blob:none", "--no-tags", "origin", "+refs/heads/*:refs/heads/*"],
            env=env,
            repo_name=repo_name,
        )
        log_args = ["-C", bare, "log", "--branches", f"--format={_LOG_FORMAT}"]
        if upstream_remote and upstream_branch:
            _run_git(["-C", bare, "remote", "add", "upstream", upstream_remote], env=env, repo_name=repo_name)
            _run_git(
                [
                    "-C",
                    bare,
                    "fetch",
                    "--filter=blob:none",
                    "--no-tags",
                    "upstream",
                    f"+refs/heads/{upstream_branch}:refs/upstream/default",
                ],
                env=env,
                repo_name=repo_name,
            )
            log_args.extend(["--not", "refs/upstream/default"])
        return _parse_commit_log(_run_git(log_args, env=env, repo_name=repo_name))


def _load_fork_parent(client: GitHubClient, org: str, repo_name: str) -> dict[str, Any]:
    """读取 fork 上游仓库，缺少默认分支或克隆地址时失败。

    Args:
        client: GitHub API 客户端。
        org: 组织登录名。
        repo_name: fork 仓库名。

    Returns:
        含 ``clone_url`` 和 ``default_branch`` 的上游对象。
    """
    try:
        detail = client.get_json(f"/repos/{org}/{repo_name}")
    except Exception as exc:
        raise HistoryError(f"{repo_name}: fork 上游信息缺失") from exc
    parent = detail.get("parent") if isinstance(detail, dict) else None
    clone_url = str(parent.get("clone_url") or "") if isinstance(parent, dict) else ""
    default_branch = str(parent.get("default_branch") or "") if isinstance(parent, dict) else ""
    if not clone_url or not default_branch:
        raise HistoryError(f"{repo_name}: fork 上游信息缺失")
    return {"clone_url": clone_url, "default_branch": default_branch}


def _dedupe_commits(commits: list[CommitRecord]) -> list[CommitRecord]:
    """按 SHA 去掉同一仓库内的重复提交。

    Args:
        commits: 可能含重复 SHA 的提交。

    Returns:
        保留首次出现顺序的提交列表。
    """
    unique: list[CommitRecord] = []
    seen: set[str] = set()
    for commit in commits:
        if not commit.sha or commit.sha in seen:
            continue
        seen.add(commit.sha)
        unique.append(commit)
    return unique


def _author_key(email: str) -> str:
    """用小写邮箱作为作者主键，空邮箱单独归入未知作者。

    Args:
        email: Git 作者邮箱。

    Returns:
        聚合用的作者键。
    """
    normalized = email.strip().casefold()
    return normalized or "unknown"


def _noreply_login(email: str) -> str | None:
    """从 GitHub noreply 邮箱提取登录名。

    Args:
        email: Git 作者邮箱。

    Returns:
        登录名；不是 noreply 邮箱时返回 ``None``。
    """
    match = _NOREPLY_LOGIN.fullmatch(email.strip())
    if not match:
        return None
    return match.group(1)


def _default_commit_source(client: GitHubClient) -> CommitSource:
    """绑定当前令牌，生成默认的 Git 历史读取器。

    Args:
        client: 提供令牌的 GitHub 客户端。

    Returns:
        供仓库循环调用的提交读取函数。
    """

    def source(repo: Mapping[str, Any], parent: Mapping[str, Any] | None) -> list[CommitRecord]:
        upstream = parent or {}
        return collect_commits_with_git(
            str(repo.get("clone_url") or ""),
            token=client.token,
            upstream_clone_url=str(upstream.get("clone_url") or "") or None,
            upstream_default_branch=str(upstream.get("default_branch") or "") or None,
            repo_name=str(repo.get("name") or ""),
            is_fork=bool(repo.get("fork")),
        )

    return source


def collect_org_analytics(
    org: str,
    client: GitHubClient,
    *,
    repo_visibility: str,
    hide_private_repo_names: bool,
    include_forks: bool,
    repo_allowlist: set[str],
    commit_source: CommitSource | None = None,
) -> dict[str, Any]:
    """汇总组织仓库的全分支提交、作者和公开看板数据。

    Args:
        org: 组织登录名。
        client: GitHub API 客户端。
        repo_visibility: ``public``、``private`` 或 ``all``。
        hide_private_repo_names: 为真时脱敏私有仓库名称、描述和分支。
        include_forks: 为真时纳入 fork，并只统计相对上游默认分支的增量提交。
        repo_allowlist: 仓库名集合；空集合表示不过滤。
        commit_source: 可选提交读取器，缺省时用 Git 抓取全部分支。

    Returns:
        供 SVG 渲染的统计结果。
    """
    now = datetime.now(UTC)
    since_30 = now - timedelta(days=30)
    since_84 = now - timedelta(days=84)
    week_anchor = (since_84 - timedelta(days=since_84.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_buckets = [week_anchor + timedelta(days=7 * index) for index in range(12)]
    weekly_totals = Counter({bucket: 0 for bucket in week_buckets})
    source = commit_source or _default_commit_source(client)

    org_payload = client.get_json(f"/orgs/{org}")
    repos_payload = client.paginate(
        f"/orgs/{org}/repos?per_page=100&type={repo_visibility}&sort=updated",
        max_pages=None,
        strict=True,
    )
    normalized_org = org.casefold()
    repo_allowlist_normalized = {name.casefold() for name in repo_allowlist}
    skipped_repos = Counter()
    filtered_repos = []
    for repo in repos_payload:
        repo_name = repo.get("name") or ""
        repo_owner = ((repo.get("owner") or {}).get("login") or "").casefold()
        if repo_owner != normalized_org:
            skipped_repos["owner_mismatch"] += 1
            continue
        if repo.get("fork") and not include_forks:
            skipped_repos["fork"] += 1
            continue
        if repo_allowlist_normalized and repo_name.casefold() not in repo_allowlist_normalized:
            skipped_repos["not_allowlisted"] += 1
            continue
        filtered_repos.append(repo)
    repos_payload = filtered_repos

    contributor_totals: dict[str, dict[str, Any]] = {}
    language_totals: Counter[str] = Counter()
    repo_stats: list[RepoStat] = []
    issue_items: list[dict[str, Any]] = []
    pr_items: list[dict[str, Any]] = []
    release_items: list[dict[str, Any]] = []
    roadmap_items: list[dict[str, Any]] = []
    private_labels: dict[str, str] = {}
    if hide_private_repo_names:
        private_names = sorted(str(repo.get("name") or "") for repo in repos_payload if repo.get("private"))
        private_labels = {name: f"Private {index:02d}" for index, name in enumerate(private_names, start=1)}

    for repo in repos_payload:
        repo_name = repo["name"]
        is_private = bool(repo.get("private", False))
        private_mask = is_private and hide_private_repo_names
        if private_mask:
            display_name = private_labels[repo_name]
            display_description = "Private repository"
            display_default_branch = "hidden"
        else:
            display_name = repo_name
            display_description = repo.get("description") or "No description available."
            display_default_branch = repo.get("default_branch", "main")
        parent = _load_fork_parent(client, org, repo_name) if repo.get("fork") else None
        commits = _dedupe_commits(source(repo, parent))
        repo_commit_total = len(commits)
        repo_recent_30 = 0
        for commit in commits:
            author_email = commit.author_email or ""
            entry = contributor_totals.setdefault(
                _author_key(author_email),
                {"login": "", "commits": 0, "repos": set(), "names": Counter(), "noreply_login": None},
            )
            entry["commits"] += 1
            entry["repos"].add(display_name)
            noreply_login = _noreply_login(author_email)
            if noreply_login:
                entry["noreply_login"] = noreply_login
            else:
                entry["names"][commit.author_name or "unknown"] += 1
            authored_at = commit.authored_at
            if authored_at is None:
                continue
            if authored_at >= since_30:
                repo_recent_30 += 1
            bucket = (authored_at - timedelta(days=authored_at.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            if bucket in weekly_totals:
                weekly_totals[bucket] += 1

        languages = client.get_json_safe(f"/repos/{org}/{repo_name}/languages", {})
        if not isinstance(languages, dict):
            languages = {}
        code_bytes = sum(int(value) for value in languages.values())
        for language, size in languages.items():
            language_totals[language] += int(size)
        dominant_language = max(languages.items(), key=lambda item: item[1])[0] if languages else "n/a"

        issues = client.paginate(f"/repos/{org}/{repo_name}/issues?state=all&per_page=10&sort=updated&direction=desc", max_pages=1)
        for issue in issues:
            if issue.get("pull_request"):
                continue
            title = issue.get("title") or f"Issue #{issue.get('number', '?')}"
            issue_items.append(
                {
                    "display_repo": display_name,
                    "number": int(issue.get("number", 0)),
                    "display_title": f"private issue #{int(issue.get('number', 0))}" if private_mask else title,
                    "state": issue.get("state", "open"),
                    "updated_at": parse_iso8601(issue.get("updated_at")),
                }
            )

        pulls = client.paginate(f"/repos/{org}/{repo_name}/pulls?state=all&per_page=10&sort=updated&direction=desc", max_pages=1)
        for pull in pulls:
            title = pull.get("title") or f"PR #{pull.get('number', '?')}"
            pr_items.append(
                {
                    "display_repo": display_name,
                    "number": int(pull.get("number", 0)),
                    "display_title": f"private pr #{int(pull.get('number', 0))}" if private_mask else title,
                    "state": pull.get("state", "open"),
                    "draft": bool(pull.get("draft", False)),
                    "updated_at": parse_iso8601(pull.get("updated_at")),
                }
            )

        releases = client.paginate(f"/repos/{org}/{repo_name}/releases?per_page=5", max_pages=1)
        for release in releases:
            name = release.get("name") or release.get("tag_name") or "untitled release"
            release_items.append(
                {
                    "display_repo": display_name,
                    "display_name": "private release" if private_mask else name,
                    "tag_name": "hidden" if private_mask else (release.get("tag_name") or ""),
                    "draft": bool(release.get("draft", False)),
                    "prerelease": bool(release.get("prerelease", False)),
                    "published_at": parse_iso8601(release.get("published_at") or release.get("created_at")),
                }
            )

        milestones = client.paginate(f"/repos/{org}/{repo_name}/milestones?state=all&per_page=5&sort=due_on&direction=asc", max_pages=1)
        for milestone in milestones:
            title = milestone.get("title") or "untitled milestone"
            roadmap_items.append(
                {
                    "display_repo": display_name,
                    "display_title": "private milestone" if private_mask else title,
                    "state": milestone.get("state", "open"),
                    "open_issues": int(milestone.get("open_issues", 0)),
                    "closed_issues": int(milestone.get("closed_issues", 0)),
                    "due_on": parse_iso8601(milestone.get("due_on")),
                }
            )

        repo_stats.append(
            RepoStat(
                name=repo_name,
                display_name=display_name,
                description=display_description,
                default_branch=display_default_branch,
                stars=int(repo.get("stargazers_count", 0)),
                forks=int(repo.get("forks_count", 0)),
                total_commits=repo_commit_total,
                recent_commits_30d=repo_recent_30,
                code_bytes=code_bytes,
                dominant_language=dominant_language,
                topics=list(repo.get("topics") or []),
                has_issues=bool(repo.get("has_issues", False)),
                has_projects=bool(repo.get("has_projects", False)),
                has_wiki=bool(repo.get("has_wiki", False)),
                visibility=repo.get("visibility", "public"),
                subscribers_count=int(repo.get("subscribers_count", 0)),
                license_name=(repo.get("license") or {}).get("spdx_id") or "No license",
                pushed_at=parse_iso8601(repo.get("pushed_at")),
                is_private=is_private,
            )
        )

    for entry in contributor_totals.values():
        entry["repo_count"] = len(entry["repos"])
        if entry.get("noreply_login"):
            entry["login"] = entry["noreply_login"]
        elif entry["names"]:
            entry["login"] = entry["names"].most_common(1)[0][0]
        else:
            entry["login"] = "unknown"
        entry.pop("repos", None)
        entry.pop("names", None)
        entry.pop("noreply_login", None)

    contributors_sorted = sorted(contributor_totals.values(), key=lambda item: (-item["commits"], item["login"].lower()))
    repo_stats.sort(key=lambda item: (-item.total_commits, -item.recent_commits_30d, item.display_name.lower()))
    issue_items.sort(key=lambda item: item["updated_at"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    pr_items.sort(key=lambda item: item["updated_at"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    release_items.sort(key=lambda item: item["published_at"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    roadmap_items.sort(key=lambda item: (0 if item["state"] == "open" else 1, item["due_on"] or datetime.max.replace(tzinfo=UTC)))

    total_stars = sum(repo.stars for repo in repo_stats)
    total_forks = sum(repo.forks for repo in repo_stats)
    total_watchers = sum(repo.subscribers_count for repo in repo_stats)

    last_updated_header = None
    rate_reset = None
    try:
        _, response = client.get_json_response(f"/orgs/{org}")
        last_updated_header = response.headers.get("Date")
        rate_reset_raw = response.headers.get("X-RateLimit-Reset")
        if rate_reset_raw:
            rate_reset = datetime.fromtimestamp(int(rate_reset_raw), tz=UTC)
    except Exception:
        pass

    top_language = max(language_totals.items(), key=lambda item: item[1])[0] if language_totals else "n/a"
    return {
        "org": {
            "name": org_payload.get("name") or org_payload.get("login", org),
            "description": org_payload.get("description") or "",
        },
        "summary": {
            "repo_count": len(repo_stats),
            "public_repo_count": sum(1 for repo in repo_stats if not repo.is_private),
            "private_repo_count": sum(1 for repo in repo_stats if repo.is_private),
            "contributor_count": len(contributor_totals),
            "total_commits": sum(repo.total_commits for repo in repo_stats),
            "recent_commits_30d": sum(repo.recent_commits_30d for repo in repo_stats),
            "active_repos": sum(1 for repo in repo_stats if repo.recent_commits_30d > 0),
            "total_code_bytes": sum(repo.code_bytes for repo in repo_stats),
            "top_language": top_language,
            "total_stars": total_stars,
            "total_forks": total_forks,
            "total_watchers": total_watchers,
        },
        "contributors": contributors_sorted,
        "repos": repo_stats,
        "language_totals": dict(language_totals.most_common()),
        "issues": issue_items[:6],
        "pulls": pr_items[:6],
        "releases": release_items[:4],
        "roadmap": roadmap_items[:4],
        "weekly_totals": [{"week": bucket, "count": weekly_totals[bucket]} for bucket in week_buckets],
        "generated_at": datetime.now(UTC),
        "source_facts": {
            "api_date": parsedate_to_datetime(last_updated_header).astimezone(UTC) if last_updated_header else None,
            "rate_reset": rate_reset,
            "repo_visibility": repo_visibility,
            "hide_private_repo_names": hide_private_repo_names,
            "owner": org,
            "include_forks": include_forks,
            "repo_allowlist": sorted(repo_allowlist),
            "skipped_repos": dict(skipped_repos),
        },
    }


def panel_title(title: str, subtitle: str, x: float, y: float) -> str:
    return "\n".join([svg_text(x, y, title, cls="panel-title"), svg_text(x, y + 24, subtitle, cls="panel-subtitle")])


def render_dashboard(data: dict[str, Any]) -> str:
    width = 1440
    height = 1740
    parts: list[str] = []
    org = data["org"]
    summary = data["summary"]
    contributors = data["contributors"][:6]
    repos: list[RepoStat] = data["repos"]
    languages = list(data["language_totals"].items())[:6]
    weeks = data["weekly_totals"]
    issues = data["issues"]
    pulls = data["pulls"]
    releases = data["releases"]
    roadmap = data["roadmap"]
    generated_at: datetime = data["generated_at"]
    now = generated_at
    max_contrib_commits = max([entry["commits"] for entry in contributors] or [1]) or 1
    max_language_bytes = max([size for _, size in languages] or [1]) or 1
    total_weekly = sum(item["count"] for item in weeks)
    max_weekly = max([item["count"] for item in weeks] or [1]) or 1
    open_issues = sum(1 for item in issues if item["state"] == "open")
    open_prs = sum(1 for item in pulls if item["state"] == "open")
    release_count = len(releases)
    roadmap_open = sum(1 for item in roadmap if item["state"] == "open")
    visibility_mode = data["source_facts"]["repo_visibility"]
    include_forks = data["source_facts"]["include_forks"]

    parts.append(
        f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">{xml_escape(org["name"])} Engineering Command Center</title>
<desc id="desc">Command-center style analytics for {xml_escape(org["name"])} repositories.</desc>
<defs>
  <linearGradient id="bg" x1="0" x2="1" y1="0" y2="1">
    <stop offset="0%" stop-color="#02060b" />
    <stop offset="40%" stop-color="#07131b" />
    <stop offset="100%" stop-color="#030910" />
  </linearGradient>
  <linearGradient id="signal" x1="0" x2="1">
    <stop offset="0%" stop-color="#1d4ed8" />
    <stop offset="55%" stop-color="#22d3ee" />
    <stop offset="100%" stop-color="#86efac" />
  </linearGradient>
  <radialGradient id="haloA" cx="12%" cy="0%" r="80%">
    <stop offset="0%" stop-color="#0ea5e9" stop-opacity="0.22" />
    <stop offset="100%" stop-color="#0ea5e9" stop-opacity="0" />
  </radialGradient>
  <radialGradient id="haloB" cx="92%" cy="8%" r="55%">
    <stop offset="0%" stop-color="#22c55e" stop-opacity="0.18" />
    <stop offset="100%" stop-color="#22c55e" stop-opacity="0" />
  </radialGradient>
  <filter id="softGlow" x="-20%" y="-20%" width="140%" height="140%">
    <feGaussianBlur stdDeviation="8" result="blur" />
    <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
  </filter>
  <style>
    .bg {{ fill: url(#bg); }}
    .panel {{ fill: rgba(5, 12, 18, 0.78); stroke: rgba(120, 146, 167, 0.26); stroke-width: 1; }}
    .panel-bright {{ fill: rgba(5, 12, 18, 0.58); stroke: rgba(34, 211, 238, 0.32); stroke-width: 1; }}
    .panel-soft {{ fill: rgba(9, 18, 27, 0.84); stroke: rgba(74, 222, 128, 0.18); stroke-width: 1; }}
    .hero-kicker {{ fill: #7dd3fc; font: 700 13px 'Trebuchet MS', 'Segoe UI', sans-serif; letter-spacing: 0.38em; text-transform: uppercase; }}
    .hero-title {{ fill: #f8fafc; font: 700 54px 'Trebuchet MS', 'Segoe UI', sans-serif; }}
    .hero-subtitle {{ fill: #c5d3df; font: 400 18px 'Segoe UI', sans-serif; }}
    .command-label {{ fill: #77e4ff; font: 700 11px 'Trebuchet MS', 'Segoe UI', sans-serif; letter-spacing: 0.22em; text-transform: uppercase; }}
    .command-value {{ fill: #f8fafc; font: 700 22px 'Trebuchet MS', 'Segoe UI', sans-serif; }}
    .command-note {{ fill: #8da3b4; font: 400 12px 'Segoe UI', sans-serif; }}
    .metric-label {{ fill: #8ca1b3; font: 600 12px 'Trebuchet MS', 'Segoe UI', sans-serif; letter-spacing: 0.14em; text-transform: uppercase; }}
    .metric-value {{ fill: #f8fafc; font: 700 31px 'Trebuchet MS', 'Segoe UI', sans-serif; }}
    .panel-title {{ fill: #f8fafc; font: 700 22px 'Trebuchet MS', 'Segoe UI', sans-serif; }}
    .panel-subtitle {{ fill: #8ca1b3; font: 400 13px 'Segoe UI', sans-serif; }}
    .rank {{ fill: #7dd3fc; font: 700 12px 'Trebuchet MS', 'Segoe UI', sans-serif; letter-spacing: 0.14em; }}
    .table-name {{ fill: #f8fafc; font: 600 16px 'Segoe UI', sans-serif; }}
    .table-meta {{ fill: #9fb1c0; font: 400 13px 'Segoe UI', sans-serif; }}
    .table-value {{ fill: #f8fafc; font: 700 16px 'Trebuchet MS', 'Segoe UI', sans-serif; }}
    .tiny {{ fill: #7f93a4; font: 400 11px 'Segoe UI', sans-serif; }}
    .badge {{ fill: rgba(8, 18, 28, 0.92); stroke: rgba(93, 214, 255, 0.26); stroke-width: 1; }}
    .badge-title {{ fill: #f8fafc; font: 700 16px 'Trebuchet MS', 'Segoe UI', sans-serif; }}
    .badge-meta {{ fill: #8da3b4; font: 400 12px 'Segoe UI', sans-serif; }}
    .badge-chip {{ fill: rgba(20, 33, 44, 0.92); stroke: rgba(132, 204, 22, 0.24); stroke-width: 1; }}
    .badge-chip-text {{ fill: #b8f29e; font: 600 11px Consolas, 'SFMono-Regular', monospace; }}
    .repo-card-mono {{ fill: #9ae6b4; font: 600 12px Consolas, 'SFMono-Regular', monospace; }}
    .stream-label {{ fill: #f8fafc; font: 600 15px 'Segoe UI', sans-serif; }}
    .stream-state {{ fill: #8be9fd; font: 700 11px 'Trebuchet MS', 'Segoe UI', sans-serif; letter-spacing: 0.18em; text-transform: uppercase; }}
    .stream-copy {{ fill: #a9bac8; font: 400 12px 'Segoe UI', sans-serif; }}
    .footer {{ fill: #7f93a4; font: 400 12px 'Segoe UI', sans-serif; }}
  </style>
</defs>
<rect class="bg" width="{width}" height="{height}" rx="32" />
<rect x="0" y="0" width="{width}" height="{height}" fill="url(#haloA)" />
<rect x="0" y="0" width="{width}" height="{height}" fill="url(#haloB)" />
'''
    )

    for x in range(52, width, 96):
        parts.append(f'<line x1="{x}" y1="32" x2="{x}" y2="{height - 32}" stroke="rgba(74, 85, 104, 0.08)" stroke-width="1" />')
    for y in range(48, height, 96):
        parts.append(f'<line x1="32" y1="{y}" x2="{width - 32}" y2="{y}" stroke="rgba(74, 85, 104, 0.06)" stroke-width="1" />')

    parts.append(svg_rect(44, 44, 1352, 222, cls="panel-bright", rx=28))
    parts.extend([
        svg_text(74, 86, "ENGINEERING COMMAND CENTER", cls="hero-kicker"),
        svg_text(74, 146, org["name"], cls="hero-title"),
        svg_text(74, 181, org["description"] or "AI-native lab automation, tracked as a live engineering system.", cls="hero-subtitle"),
    ])

    command_boxes = [
        ("mission", f'{summary["repo_count"]} repos online', f'{summary["public_repo_count"]} public tracked'),
        ("signal", f'{fmt_number(summary["recent_commits_30d"])} / 30d', f'{fmt_number(total_weekly)} commits in rolling 12 weeks'),
        ("surface", f'{fmt_bytes(summary["total_code_bytes"])}', f'{summary["top_language"]} is the dominant language'),
        ("community", f'{fmt_number(summary["total_stars"])} stars', f'{fmt_number(summary["total_forks"])} forks · {fmt_number(summary["total_watchers"])} watchers'),
    ]
    box_x = 780
    for index, (label, value, note) in enumerate(command_boxes):
        x = box_x + (index % 2) * 286
        y = 70 + (index // 2) * 78
        parts.append(svg_rect(x, y, 258, 62, cls="panel-soft", rx=18))
        parts.append(svg_text(x + 16, y + 20, label, cls="command-label"))
        parts.append(svg_text(x + 16, y + 42, value, cls="command-value"))
        parts.append(svg_text(x + 16, y + 57, note, cls="command-note"))

    strip_y = 284
    for index, (label, value) in enumerate([
        ("COMMIT MASS", fmt_number(summary["total_commits"])),
        ("ACTIVE REPOS", fmt_number(summary["active_repos"])),
        ("OPEN ISSUES", fmt_number(open_issues)),
        ("OPEN PRS", fmt_number(open_prs)),
        ("RELEASES", fmt_number(release_count)),
        ("ROADMAP", fmt_number(roadmap_open)),
    ]):
        x = 52 + index * 223
        parts.append(svg_rect(x, strip_y, 206, 70, cls="panel", rx=18))
        parts.append(svg_text(x + 16, strip_y + 24, label, cls="metric-label"))
        parts.append(svg_text(x + 16, strip_y + 54, value, cls="metric-value"))

    parts.append(svg_rect(52, 376, 430, 448, cls="panel", rx=28))
    parts.append(panel_title("Contributor Radar", "Cross-repository contributor aggregation.", 80, 416))
    row_y = 468
    for index, contributor in enumerate(contributors):
        y = row_y + index * 58
        bar_width = 160 * contributor["commits"] / max_contrib_commits
        parts.append(svg_text(80, y, f'#{index + 1:02d}', cls="rank"))
        parts.append(svg_text(130, y, contributor["login"], cls="table-name"))
        parts.append(svg_text(130, y + 20, f'{contributor["repo_count"]} repos wired in', cls="table-meta"))
        parts.append(f'<rect x="254" y="{y - 11}" width="{bar_width:.1f}" height="8" rx="4" fill="url(#signal)" filter="url(#softGlow)" />')
        parts.append(svg_text(454, y, fmt_number(contributor["commits"]), cls="table-value", anchor="end"))
        parts.append(svg_text(454, y + 20, "commits", cls="table-meta", anchor="end"))
        parts.append(f'<line x1="80" y1="{y + 32}" x2="454" y2="{y + 32}" stroke="rgba(148, 163, 184, 0.08)" stroke-width="1" />')
    if not contributors:
        parts.append(svg_text(80, 488, "Awaiting contributor activity.", cls="table-meta"))

    parts.append(svg_rect(502, 376, 886, 448, cls="panel", rx=28))
    parts.append(panel_title("Project Pulse", "Repository badges, commit pressure, and capability flags.", 530, 416))
    badge_w = 414
    badge_h = 176
    for index, repo in enumerate(repos[:4]):
        bx = 530 + (index % 2) * (badge_w + 18)
        by = 458 + (index // 2) * (badge_h + 18)
        parts.append(svg_rect(bx, by, badge_w, badge_h, cls="badge", rx=22))
        parts.append(svg_text(bx + 18, by + 28, repo.display_name, cls="badge-title"))
        parts.append(svg_text(bx + badge_w - 18, by + 28, ("PRIVATE" if repo.is_private else repo.visibility.upper()), cls="stream-state", anchor="end"))
        for line_index, line in enumerate(wrap_label(truncate(repo.description, 92), width=46)[:2]):
            parts.append(svg_text(bx + 18, by + 50 + line_index * 16, line, cls="badge-meta"))
        parts.append(svg_text(bx + 18, by + 90, f"{repo.dominant_language} · {fmt_bytes(repo.code_bytes)} · {fmt_number(repo.total_commits)} commits", cls="repo-card-mono"))
        parts.append(svg_text(bx + 18, by + 108, f"+{repo.recent_commits_30d} / 30d · updated {days_since(repo.pushed_at, now)}", cls="badge-meta"))
        parts.append(svg_text(bx + badge_w - 18, by + 90, f"{fmt_number(repo.stars)}★  {fmt_number(repo.forks)} forks", cls="badge-meta", anchor="end"))
        parts.append(svg_text(bx + badge_w - 18, by + 108, f"{repo.license_name} · {repo.default_branch}", cls="badge-meta", anchor="end"))
        chip_x = bx + 18
        chips = [f"issues:{'on' if repo.has_issues else 'off'}", f"projects:{'on' if repo.has_projects else 'off'}", f"wiki:{'on' if repo.has_wiki else 'off'}"]
        if repo.topics:
            chips.append(truncate(repo.topics[0], 14))
        for chip in chips[:4]:
            chip_w = max(74, len(chip) * 7 + 18)
            parts.append(svg_rect(chip_x, by + 128, chip_w, 24, cls="badge-chip", rx=12))
            parts.append(svg_text(chip_x + 10, by + 145, chip, cls="badge-chip-text"))
            chip_x += chip_w + 8

    parts.append(svg_rect(52, 848, 430, 372, cls="panel", rx=28))
    parts.append(panel_title("Language Footprint", "Byte-weighted code distribution across tracked repositories.", 80, 888))
    total_language_bytes = sum(size for _, size in languages) or 1
    for index, (language, size) in enumerate(languages):
        y = 940 + index * 44
        bar_width = 170 * size / max_language_bytes
        share = 100 * size / total_language_bytes
        parts.append(svg_text(80, y, language, cls="table-name", size=15))
        parts.append(f'<rect x="208" y="{y - 11}" width="{bar_width:.1f}" height="9" rx="4" fill="url(#signal)" />')
        parts.append(svg_text(454, y, f"{share:.1f}%", cls="table-value", size=14, anchor="end"))
        parts.append(svg_text(454, y + 16, fmt_bytes(size), cls="table-meta", anchor="end"))

    parts.append(svg_rect(502, 848, 886, 372, cls="panel", rx=28))
    parts.append(panel_title("Activity Trail", "Rolling 12-week commit signal rendered as a live operations trace.", 530, 888))
    chart_x = 548
    chart_y = 948
    chart_width = 804
    chart_height = 194
    for step in range(5):
        y = chart_y + step * (chart_height / 4)
        value = round(max_weekly - (max_weekly * step / 4))
        parts.append(f'<line x1="{chart_x}" y1="{y:.1f}" x2="{chart_x + chart_width}" y2="{y:.1f}" stroke="rgba(148, 163, 184, 0.08)" stroke-width="1" />')
        parts.append(svg_text(chart_x - 12, y + 4, str(value), cls="tiny", anchor="end"))
    points: list[tuple[float, float]] = []
    for index, item in enumerate(weeks):
        x = chart_x + (chart_width * index / max(len(weeks) - 1, 1))
        y = chart_y + chart_height - (item["count"] / max_weekly) * chart_height
        points.append((x, y))
        parts.append(svg_text(x, chart_y + chart_height + 28, item["week"].strftime("%b %d"), cls="tiny", anchor="middle"))
    if points:
        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        area_points = " ".join([f"{chart_x:.1f},{chart_y + chart_height:.1f}", polyline, f"{chart_x + chart_width:.1f},{chart_y + chart_height:.1f}"])
        parts.append(f'<polygon points="{area_points}" fill="url(#signal)" fill-opacity="0.14" />')
        parts.append(f'<polyline points="{polyline}" fill="none" stroke="url(#signal)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" filter="url(#softGlow)" />')
        for x, y in points:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#02060b" stroke="#67e8f9" stroke-width="2" />')
    parts.append(svg_text(1346, 928, f"{total_weekly} commits / 12 weeks", cls="table-value", size=16, anchor="end"))
    parts.append(svg_text(1346, 946, f"peak week {max_weekly}", cls="table-meta", anchor="end"))

    parts.append(svg_rect(52, 1244, 668, 372, cls="panel", rx=28))
    parts.append(panel_title("Issue + PR Stream", "Latest collaboration events across repository surfaces.", 80, 1284))
    for index in range(3):
        issue = issues[index] if index < len(issues) else None
        pull = pulls[index] if index < len(pulls) else None
        row_top = 1338 + index * 88
        parts.append(f'<line x1="80" y1="{row_top - 18}" x2="692" y2="{row_top - 18}" stroke="rgba(148, 163, 184, 0.07)" stroke-width="1" />')
        if issue:
            parts.append(svg_text(80, row_top, f"{issue['display_repo']} · issue #{issue['number']}", cls="stream-state"))
            parts.append(svg_text(80, row_top + 22, truncate(issue["display_title"], 44), cls="stream-label"))
            parts.append(svg_text(80, row_top + 42, f"{issue['state']} · updated {days_since(issue['updated_at'], now)}", cls="stream-copy"))
        else:
            parts.append(svg_text(80, row_top + 20, "ISSUE CHANNEL", cls="stream-state"))
            parts.append(svg_text(80, row_top + 42, "Awaiting issue traffic", cls="stream-copy"))
        if pull:
            parts.append(svg_text(392, row_top, f"{pull['display_repo']} · pr #{pull['number']}", cls="stream-state"))
            parts.append(svg_text(392, row_top + 22, truncate(pull["display_title"], 42), cls="stream-label"))
            suffix = "draft" if pull["draft"] else pull["state"]
            parts.append(svg_text(392, row_top + 42, f"{suffix} · updated {days_since(pull['updated_at'], now)}", cls="stream-copy"))
        else:
            parts.append(svg_text(392, row_top + 20, "PR CHANNEL", cls="stream-state"))
            parts.append(svg_text(392, row_top + 42, "Awaiting pull request traffic", cls="stream-copy"))

    parts.append(svg_rect(740, 1244, 648, 372, cls="panel", rx=28))
    parts.append(panel_title("Release Lane + Roadmap", "Latest release artifacts and milestone intent from repository data.", 768, 1284))
    for index in range(4):
        item_y = 1336 + index * 68
        if index < len(releases):
            release = releases[index]
            release_state = "draft" if release["draft"] else "prerelease" if release["prerelease"] else "release"
            parts.append(svg_text(768, item_y, f"{release['display_repo']} · {release_state}", cls="stream-state"))
            parts.append(svg_text(768, item_y + 22, truncate(release["display_name"], 48), cls="stream-label"))
            parts.append(svg_text(768, item_y + 42, f"{release['tag_name'] or 'no tag'} · published {days_since(release['published_at'], now)}", cls="stream-copy"))
        else:
            parts.append(svg_text(768, item_y + 20, "RELEASE SLOT", cls="stream-state"))
            parts.append(svg_text(768, item_y + 42, "Awaiting first release artifact", cls="stream-copy"))
        if index < len(roadmap):
            milestone = roadmap[index]
            due = days_since(milestone["due_on"], now) if milestone["due_on"] else "no due date"
            parts.append(svg_text(1060, item_y, f"{milestone['display_repo']} · {milestone['state']}", cls="stream-state"))
            parts.append(svg_text(1060, item_y + 22, truncate(milestone["display_title"], 34), cls="stream-label"))
            parts.append(svg_text(1060, item_y + 42, f"{milestone['closed_issues']} closed / {milestone['open_issues']} open · {due}", cls="stream-copy"))
        else:
            parts.append(svg_text(1060, item_y + 20, "ROADMAP SLOT", cls="stream-state"))
            parts.append(svg_text(1060, item_y + 42, "Awaiting milestone configuration", cls="stream-copy"))

    fork_label = "incremental" if include_forks else "excluded"
    scope_note = (
        f"Scope={visibility_mode}; owner={data['source_facts']['owner']}; "
        f"branches=all; forks={fork_label}; deduped by SHA; git authors"
    )
    parts.append(svg_rect(52, 1640, 1336, 62, cls="panel-bright", rx=20))
    parts.append(svg_text(74, 1678, f"Telemetry source: git history. {scope_note}.", cls="footer"))
    privacy_note = "private names masked · " if data["source_facts"]["hide_private_repo_names"] else ""
    parts.append(svg_text(1362, 1678, f"{privacy_note}refreshed {generated_at.strftime('%Y-%m-%d %H:%M UTC')}", cls="footer", anchor="end"))
    parts.append("</svg>")
    return "\n".join(parts)


def write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_repo_allowlist() -> set[str]:
    """根据环境变量解析看板仓库 allowlist。

    Returns:
        仓库名称集合；未设置、空值、``all`` 或 ``*`` 都表示不过滤。
    """
    raw = os.getenv("REPO_ALLOWLIST")
    if raw is None or not raw.strip() or raw.strip().lower() in {"all", "*"}:
        return set()
    return parse_repo_allowlist(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a GitHub organization analytics SVG dashboard.")
    parser.add_argument("--org", default=os.getenv("GITHUB_REPOSITORY_OWNER") or "SynlysAI")
    parser.add_argument("--output", default="github-analytics.svg")
    args = parser.parse_args()

    token = os.getenv("METRICS_TOKEN") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repo_visibility = os.getenv("REPO_VISIBILITY", "public").strip().lower()
    if repo_visibility not in {"public", "private", "all"}:
        repo_visibility = "public"
    hide_private_repo_names = env_flag("HIDE_PRIVATE_REPO_NAMES", True)
    include_forks = env_flag("INCLUDE_FORKS", False)
    repo_allowlist = resolve_repo_allowlist()

    client = GitHubClient(token=token)
    data = collect_org_analytics(
        args.org,
        client,
        repo_visibility=repo_visibility,
        hide_private_repo_names=hide_private_repo_names,
        include_forks=include_forks,
        repo_allowlist=repo_allowlist,
    )
    svg = render_dashboard(data)
    output_path = os.path.abspath(args.output)
    write_file(output_path, svg)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
