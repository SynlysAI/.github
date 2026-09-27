"""组织看板的 Git 全分支历史测试。"""

import os
import subprocess
from pathlib import Path

import pytest

from scripts import generate_org_dashboard as dashboard


def _git_env() -> dict[str, str]:
    """允许测试使用本地 file 协议。

    Returns:
        Git 子进程环境。
    """
    env = os.environ.copy()
    for key in list(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "protocol.file.allow"
    env["GIT_CONFIG_VALUE_0"] = "always"
    return env


def _git(repo: Path, *args: str) -> str:
    """在临时仓库执行 Git 命令。

    Args:
        repo: 仓库目录。
        args: Git 参数。

    Returns:
        标准输出文本。
    """
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return completed.stdout.strip()


def _init_repo(path: Path, branch: str = "main") -> None:
    """创建可被 blobless fetch 读取的本地仓库。

    Args:
        path: 仓库目录。
        branch: 初始分支名。
    """
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", branch, str(path)], check=True, capture_output=True, text=True, env=_git_env())
    _git(path, "config", "user.email", "dev@example.com")
    _git(path, "config", "user.name", "Dev")
    _git(path, "config", "uploadpack.allowFilter", "true")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(repo: Path, message: str, *, allow_empty: bool = False) -> str:
    """写入一条提交并返回 SHA。

    Args:
        repo: 仓库目录。
        message: 提交说明。
        allow_empty: 是否允许空提交。

    Returns:
        新提交的完整 SHA。
    """
    args = ["commit", "--allow-empty", "-m", message] if allow_empty else ["commit", "-m", message]
    if not allow_empty:
        file_path = repo / "tree.txt"
        file_path.write_text(message + "\n", encoding="utf-8")
        _git(repo, "add", "tree.txt")
    _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD")


def test_non_fork_counts_every_branch_once_including_merge_and_empty(tmp_path: Path):
    """功能分支、合并提交和空提交计入，共享提交只计一次，tag-only 提交不计。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    shared = _commit(repo, "shared", allow_empty=True)
    _git(repo, "checkout", "-b", "feature")
    feature_only = _commit(repo, "feature only")
    _git(repo, "checkout", "main")
    main_only = _commit(repo, "main only", allow_empty=True)
    _git(repo, "checkout", "-b", "merged")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    merge_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    tag_only = _commit(repo, "tag only", allow_empty=True)
    _git(repo, "tag", "dangling")
    _git(repo, "reset", "--hard", "HEAD~1")

    records = dashboard.collect_commits_with_git(str(repo), repo_name="repo", is_fork=False)
    shas = {record.sha for record in records}

    assert shared in shas
    assert feature_only in shas
    assert main_only in shas
    assert merge_commit in shas
    assert tag_only not in shas
    assert len(records) == len(shas) == 4


def test_fork_counts_only_commits_unreachable_from_upstream_default(tmp_path: Path):
    """上游默认分支历史不计，fork 分支增量去重，上游不可读时失败。"""
    upstream = tmp_path / "upstream"
    _init_repo(upstream, branch="preview")
    upstream_commit = _commit(upstream, "upstream base")
    fork = tmp_path / "fork"
    subprocess.run(["git", "clone", str(upstream), str(fork)], check=True, capture_output=True, text=True, env=_git_env())
    _git(fork, "config", "user.email", "fork@example.com")
    _git(fork, "config", "uploadpack.allowFilter", "true")
    _git(fork, "config", "user.name", "Fork Dev")
    _git(fork, "checkout", "-b", "develop")
    first = _commit(fork, "fork increment")
    _git(fork, "branch", "topic")
    second = _commit(fork, "develop only")

    records = dashboard.collect_commits_with_git(
        str(fork),
        upstream_clone_url=str(upstream),
        upstream_default_branch="preview",
        repo_name="plane",
        is_fork=True,
    )
    shas = [record.sha for record in records]

    assert upstream_commit not in shas
    assert shas.count(first) == 1
    assert second in shas
    assert len(shas) == 2

    with pytest.raises(dashboard.HistoryError, match="plane: git history failed") as error:
        dashboard.collect_commits_with_git(
            str(fork),
            token="ghp_supersecret_token",
            upstream_clone_url=str(tmp_path / "missing-upstream"),
            upstream_default_branch="preview",
            repo_name="plane",
            is_fork=True,
        )
    assert "ghp_supersecret_token" not in str(error.value)


def test_repository_without_branches_has_no_commits(tmp_path: Path):
    """没有任何分支的仓库提交数为 0。"""
    repo = tmp_path / "empty"
    _init_repo(repo)
    assert dashboard.collect_commits_with_git(str(repo), repo_name="empty", is_fork=False) == []
