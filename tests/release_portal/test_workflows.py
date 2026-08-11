"""Release Portal GitHub Actions 工作流结构测试。"""

import json
from pathlib import Path

from scripts.release_portal import cli
from scripts.release_portal.assets import InMemoryR2Client
from scripts.release_portal.github import Release, ReleaseAsset

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
OPERATIONS_DOCUMENT = ROOT / "docs" / "release-portal-operations.md"


def _workflow(name: str) -> str:
    """读取指定工作流的 UTF-8 文本。

    Args:
        name: 工作流文件名。

    Returns:
        工作流 YAML 文本。
    """
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_sync_workflow_uses_app_token_schedule_and_candidate_pr():
    """同步任务应定时运行，并用 GitHub App 维护候选审核 PR。"""
    workflow = _workflow("release-portal-sync.yml")

    assert "cron: \"17 */6 * * *\"" in workflow
    assert "workflow_dispatch:" in workflow
    assert "actions/create-github-app-token@v1" in workflow
    assert "SYNLYSAI_APP_ID" in workflow
    assert "SYNLYSAI_APP_PRIVATE_KEY" in workflow
    for repository in ("AI4MS", "Spec_Agent", "Poly_Agent", "SpecLabOS", "RAGPortal", "SmartAccess"):
        assert repository in workflow
    assert "automation/release-portal-candidates" in workflow
    assert "python -m scripts.release_portal.cli sync" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "scripts.release_portal.review_summary" in workflow
    assert "--body-file" in workflow
    _assert_trusted_main_runs_cli(workflow, "sync")


def test_backfill_workflow_is_manual_and_limits_each_product_batch():
    """回填任务只能手动执行，且每次处理上限为 500 条提交。"""
    workflow = _workflow("release-portal-backfill.yml")

    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "push:" not in workflow
    assert "python -m scripts.release_portal.cli backfill" in workflow
    assert "--limit 500" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "automation/release-portal-candidates" in workflow
    assert "environment: release-portal-private-ai" in workflow
    assert "AI_PRIVATE_ENDPOINT_ALLOWLIST: ${{ vars.AI_PRIVATE_ENDPOINT_ALLOWLIST }}" in workflow
    assert "AI_PRIVATE_ENDPOINT_ALLOWLIST: ${{ vars.AI_BASE_URL }}" not in workflow
    assert "受保护审批配置" in workflow
    assert "不得从其填充" in workflow
    assert "scripts.release_portal.review_summary" in workflow
    assert "--body-file" in workflow
    _assert_trusted_main_runs_cli(workflow, "backfill")


def test_publish_workflow_validates_before_manifest_last_upload():
    """发布任务应先执行测试和校验，并由 CLI 保证 manifest 最后上传。"""
    workflow = _workflow("release-portal-publish.yml")

    assert "branches: [main]" in workflow
    assert "python -m pytest tests/release_portal -q" in workflow
    assert "publish --validate-only" in workflow
    assert "python -m scripts.release_portal.cli publish" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "R2_ACCESS_KEY_ID" in workflow
    assert "R2_SECRET_ACCESS_KEY" in workflow
    assert "CLOUDFLARE_ACCOUNT_ID" in workflow
    assert "R2_BUCKET" in workflow


def test_dependabot_covers_actions_and_python_dependencies():
    """Dependabot 应同时跟踪 Actions 与根目录 Python 依赖。"""
    content = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert 'package-ecosystem: "github-actions"' in content
    assert 'package-ecosystem: "pip"' in content
    assert 'directory: "/"' in content


def test_operations_document_reserves_protected_private_ai_allowlist():
    """运维说明应将私有 AI 白名单定义为独立的受保护 Environment 配置。"""
    content = OPERATIONS_DOCUMENT.read_text(encoding="utf-8")

    assert "AI_PRIVATE_ENDPOINT_ALLOWLIST" in content
    assert "release-portal-private-ai" in content
    assert "required reviewers" in content
    assert "不得由 AI_BASE_URL 填充" in content


def _assert_trusted_main_runs_cli(workflow: str, command: str) -> None:
    """确认带密钥 CLI 使用主分支代码，只读取候选数据文件。

    Args:
        workflow: 工作流 YAML 文本。
        command: CLI 子命令名。

    Returns:
        无返回值。
    """
    trusted_checkout = workflow.index("检出受信任主分支")
    restore = workflow.index("仅恢复候选数据输入")
    cli = workflow.index(f"python -m scripts.release_portal.cli {command}")
    assert trusted_checkout < restore < cli
    assert "ref: main" in workflow[trusted_checkout:restore]
    assert "git checkout --detach origin/main" in workflow[restore:cli]
    assert "release-portal/candidates/timeline.json" in workflow[restore:cli]
    assert "release-portal/candidates/releases.json" in workflow[restore:cli]
    assert "release-portal/state/backfill.json" in workflow[restore:cli]
    assert "git checkout -B" not in workflow[restore:cli]
    assert "git checkout -B" in workflow[cli:]


def test_sync_command_uploads_formal_asset_with_original_name(tmp_path: Path, monkeypatch, capsys):
    """同步命令应保留附件名，且候选数据不暴露 GitHub 下载地址。"""
    source = tmp_path / "payload.download"
    source.write_bytes(b"release asset")
    downloaded = tmp_path / "download" / "installer.exe"
    downloaded.parent.mkdir()
    downloaded.write_bytes(source.read_bytes())

    class FakeGitHubClient:
        """提供一个含正式 Release 的最小 GitHub 客户端。"""

        @staticmethod
        def list_releases(_repository):
            """返回单个正式 Release。"""
            return [
                Release(
                    id="1",
                    tag="v1.0.0",
                    name="Release 1",
                    body="公开说明",
                    published_at="2026-08-10T00:00:00Z",
                    release_url="https://github.com/SynlysAI/AI4MS/releases/tag/v1.0.0",
                    prerelease=False,
                    draft=False,
                    assets=(
                        ReleaseAsset(
                            name="installer.exe",
                            size=source.stat().st_size,
                            content_type="application/vnd.microsoft.portable-executable",
                            download_url="https://github.com/SynlysAI/AI4MS/releases/download/v1.0.0/installer.exe",
                            platform="windows",
                            architecture="x86_64",
                        ),
                    ),
                )
            ]

        @staticmethod
        def list_commits(_repository, *, include_pull_requests, stop_at_sha, max_items, page):
            """同步测试不产生新的提交。"""
            assert include_pull_requests is True
            assert max_items == 500
            assert page == 1
            assert stop_at_sha is None
            return []

    monkeypatch.setattr(cli, "_download_release_asset", lambda *_args: downloaded)
    (tmp_path / "backfill.json").write_text(
        '{"schemaVersion": 1, "repositories": {"SynlysAI/AI4MS": '
        '{"page": 1, "processedShas": [], "watermark": {}}}}\n',
        encoding="utf-8",
    )
    args = cli._parser().parse_args(
        [
            "sync",
            "--product",
            "ai4ms",
            "--candidates",
            str(tmp_path / "releases.json"),
            "--timeline",
            str(tmp_path / "timeline.json"),
            "--state",
            str(tmp_path / "backfill.json"),
        ]
    )

    assert cli._sync(args, object_store=InMemoryR2Client(), github_client=FakeGitHubClient()) == 0
    candidate = (tmp_path / "releases.json").read_text(encoding="utf-8")
    assert '"name": "installer.exe"' in candidate
    assert "browser_download_url" not in candidate
    assert "https://github.com/SynlysAI/AI4MS/releases/download" not in candidate
    assert '"status":"success"' in capsys.readouterr().out


def test_publish_command_uploads_manifest_after_all_other_collections(tmp_path: Path):
    """发布命令应先写唯一 generation，再最后更新根 manifest 指针。"""
    candidate = tmp_path / "timeline.json"
    releases = tmp_path / "releases.json"
    state = tmp_path / "backfill.json"
    candidate.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")
    releases.write_text('{"schemaVersion": 1, "releases": []}\n', encoding="utf-8")
    state.write_text('{"schemaVersion": 1, "repositories": {}}\n', encoding="utf-8")

    class RecordingStore:
        """记录 boto3 风格上传调用的对象存储替身。"""

        def __init__(self):
            """初始化上传 key 记录。"""
            self.keys: list[str] = []
            self.bodies: dict[str, bytes] = {}

        def put_object(self, *, Bucket, Key, Body, **_kwargs):  # noqa: N803
            """记录公开对象上传。"""
            assert Bucket == "downloads"
            assert isinstance(Body, bytes)
            self.keys.append(Key)
            self.bodies[Key] = Body

    store = RecordingStore()
    result = cli.main(
        [
            "publish",
            "--bucket",
            "downloads",
            "--candidates",
            str(candidate),
            "--releases",
            str(releases),
            "--state",
            str(state),
            "--output",
            str(tmp_path / "published"),
        ],
        object_store=store,
    )

    assert result == 0
    generation_prefix = store.keys[0].rsplit("/", 1)[0]
    assert generation_prefix.startswith("portal/v1/generations/")
    assert store.keys == [
        f"{generation_prefix}/products.json",
        f"{generation_prefix}/releases.json",
        f"{generation_prefix}/timeline.json",
        f"{generation_prefix}/faqs.json",
        f"{generation_prefix}/meta.json",
        "portal/v1/products.json",
        "portal/v1/releases.json",
        "portal/v1/timeline.json",
        "portal/v1/faqs.json",
        "portal/v1/meta.json",
        "portal/v1/manifest.json",
    ]
    assert store.keys[-1] == "portal/v1/manifest.json"
    pointer = json.loads(store.bodies["portal/v1/manifest.json"])
    assert all(item["path"].startswith(f"{generation_prefix}/") for item in pointer["collections"].values())
    assert all(item["path"].startswith(f"{generation_prefix}/") for item in pointer["files"])
    for filename in ("products.json", "releases.json", "timeline.json", "faqs.json", "meta.json"):
        assert store.bodies[f"portal/v1/{filename}"] == store.bodies[f"{generation_prefix}/{filename}"]
        assert (tmp_path / "published" / filename).is_file()


def test_publish_failure_does_not_replace_root_manifest_or_old_generation(tmp_path: Path):
    """generation 中途上传失败时，官网继续引用上一份完整快照。"""
    candidate = tmp_path / "timeline.json"
    releases = tmp_path / "releases.json"
    state = tmp_path / "backfill.json"
    candidate.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")
    releases.write_text('{"schemaVersion": 1, "releases": []}\n', encoding="utf-8")
    state.write_text('{"schemaVersion": 1, "repositories": {}}\n', encoding="utf-8")

    class FailingStore:
        """在第三次 generation 上传时失败的对象存储替身。"""

        def __init__(self):
            """初始化旧根 manifest 和旧 generation 快照。"""
            self.calls = 0
            self.objects = {
                "portal/v1/manifest.json": b'{"generation":"old"}\n',
                "portal/v1/generations/old/products.json": b"old-products\n",
                "portal/v1/generations/old/releases.json": b"old-releases\n",
                "portal/v1/generations/old/timeline.json": b"old-timeline\n",
                "portal/v1/generations/old/faqs.json": b"old-faqs\n",
                "portal/v1/generations/old/meta.json": b"old-meta\n",
                "portal/v1/products.json": b"old-products\n",
                "portal/v1/releases.json": b"old-releases\n",
                "portal/v1/timeline.json": b"old-timeline\n",
                "portal/v1/faqs.json": b"old-faqs\n",
                "portal/v1/meta.json": b"old-meta\n",
            }

        def put_object(self, *, Bucket, Key, Body, **_kwargs):  # noqa: N803
            """记录写入，并在第三次 generation 写入时抛出错误。"""
            assert Bucket == "downloads"
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("generation upload failed")
            self.objects[Key] = Body

    store = FailingStore()
    old_manifest = store.objects["portal/v1/manifest.json"]
    old_snapshot = {
        key: value
        for key, value in store.objects.items()
        if key.startswith("portal/v1/generations/old/")
    }
    result = cli.main(
        [
            "publish",
            "--bucket",
            "downloads",
            "--candidates",
            str(candidate),
            "--releases",
            str(releases),
            "--state",
            str(state),
            "--output",
            str(tmp_path / "published"),
        ],
        object_store=store,
    )

    assert result == 1
    assert store.objects["portal/v1/manifest.json"] == old_manifest
    assert {
        key: store.objects[key]
        for key in old_snapshot
    } == old_snapshot
    assert store.objects["portal/v1/products.json"] == b"old-products\n"
    assert store.objects["portal/v1/meta.json"] == b"old-meta\n"


def test_publish_compatibility_failure_keeps_old_manifest(tmp_path: Path):
    """稳定根兼容副本失败时，旧 generation manifest 指针保持不变。"""
    candidate = tmp_path / "timeline.json"
    releases = tmp_path / "releases.json"
    state = tmp_path / "backfill.json"
    candidate.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")
    releases.write_text('{"schemaVersion": 1, "releases": []}\n', encoding="utf-8")
    state.write_text('{"schemaVersion": 1, "repositories": {}}\n', encoding="utf-8")

    class CompatibilityFailingStore:
        """在第二个稳定根副本写入时失败。"""

        def __init__(self):
            """初始化旧 manifest 和旧 generation。"""
            self.calls = 0
            self.objects = {
                "portal/v1/manifest.json": b'{"generation":"old"}\n',
                **{
                    f"portal/v1/generations/old/{filename}": f"old-{filename}\n".encode()
                    for filename in ("products.json", "releases.json", "timeline.json", "faqs.json", "meta.json")
                },
            }

        def put_object(self, *, Bucket, Key, Body, **_kwargs):  # noqa: N803
            """记录上传并在第二个根兼容副本失败。"""
            assert Bucket == "downloads"
            self.calls += 1
            if self.calls == 7:
                raise RuntimeError("compatibility upload failed")
            self.objects[Key] = Body

    store = CompatibilityFailingStore()
    old_manifest = store.objects["portal/v1/manifest.json"]
    result = cli.main(
        [
            "publish",
            "--bucket",
            "downloads",
            "--candidates",
            str(candidate),
            "--releases",
            str(releases),
            "--state",
            str(state),
            "--output",
            str(tmp_path / "published"),
        ],
        object_store=store,
    )

    assert result == 1
    assert store.objects["portal/v1/manifest.json"] == old_manifest


def test_backfill_uses_bounded_page_and_advances_checkpoint(tmp_path: Path, monkeypatch):
    """回填每次只请求指定上限，并利用 state.page 推进下一批。"""
    state_path = tmp_path / "backfill.json"
    candidate_path = tmp_path / "timeline.json"
    state_path.write_text(
        """{
  "schemaVersion": 1,
  "repositories": {
    "SynlysAI/AI4MS": {
      "cursor": null,
      "page": 1,
      "completed": false,
      "processed": 0,
      "processedShas": [],
      "watermark": {"sha": null, "publishedAt": null}
    }
  }
}
""",
        encoding="utf-8",
    )
    candidate_path.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")

    class BatchedClient:
        """按页返回有限提交的 GitHub 客户端替身。"""

        def __init__(self):
            """初始化调用记录。"""
            self.calls: list[tuple[str, int, int, bool]] = []

        def list_commits(self, repository, *, max_items, page, include_pull_requests):
            """返回当前页的至多两条提交。"""
            self.calls.append((repository, max_items, page, include_pull_requests))
            records = {
                1: [
                    {"sha": "a" * 40, "message": "feat(core): one", "occurred_at": "2026-08-10T00:00:00Z"},
                    {"sha": "b" * 40, "message": "feat(core): two", "occurred_at": "2026-08-10T00:00:00Z"},
                ],
                2: [
                    {"sha": "c" * 40, "message": "fix(core): three", "occurred_at": "2026-08-10T00:00:00Z"},
                ],
            }
            return records[page][:max_items]

    monkeypatch.delenv("AI_API_KEY", raising=False)
    client = BatchedClient()
    args = cli._parser().parse_args(
        [
            "backfill",
            "--product",
            "ai4ms",
            "--limit",
            "2",
            "--state",
            str(state_path),
            "--candidates",
            str(candidate_path),
        ]
    )

    assert cli._backfill(args, github_client=client) == 0
    first_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert first_state["repositories"]["SynlysAI/AI4MS"]["page"] == 2
    assert cli._backfill(args, github_client=client) == 0
    second_state = json.loads(state_path.read_text(encoding="utf-8"))
    repository_state = second_state["repositories"]["SynlysAI/AI4MS"]
    assert repository_state["completed"] is True
    assert repository_state["processed"] == 3
    assert client.calls == [
        ("SynlysAI/AI4MS", 2, 1, True),
        ("SynlysAI/AI4MS", 2, 2, True),
    ]
    assert all(call[1] <= 2 for call in client.calls)


def test_backfill_full_batch_advances_by_consumed_github_pages(tmp_path: Path, monkeypatch):
    """500 条完整批次应消耗五页并让下一批从第六页开始。"""
    state_path = tmp_path / "backfill.json"
    candidate_path = tmp_path / "timeline.json"
    state_path.write_text(
        '{"schemaVersion": 1, "repositories": {"SynlysAI/AI4MS": {'
        '"page": 1, "processedShas": [], "watermark": {}}}}\n',
        encoding="utf-8",
    )
    candidate_path.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")

    class FullPageClient:
        """模拟每次刚好返回 500 条提交的分页客户端。"""

        def __init__(self):
            """初始化调用页号记录。"""
            self.pages: list[int] = []

        def list_commits(self, _repository, *, max_items, page, include_pull_requests):
            """为每个请求页返回互不重复的完整批次。"""
            assert max_items == 500
            assert include_pull_requests is True
            self.pages.append(page)
            offset = (page - 1) * 500
            return [
                {
                    "sha": f"{offset + index:040x}",
                    "message": "feat(core): batch",
                    "occurred_at": "2026-08-10T00:00:00Z",
                }
                for index in range(500)
            ]

    monkeypatch.delenv("AI_API_KEY", raising=False)
    args = cli._parser().parse_args(
        [
            "backfill",
            "--product",
            "ai4ms",
            "--limit",
            "500",
            "--state",
            str(state_path),
            "--candidates",
            str(candidate_path),
        ]
    )
    client = FullPageClient()

    assert cli._backfill(args, github_client=client) == 0
    assert cli._backfill(args, github_client=client) == 0
    assert client.pages == [1, 6]


def test_sync_adds_only_commits_newer_than_watermark(tmp_path: Path, monkeypatch):
    """同步应以水位停止读取，并只将新增提交合并入 timeline 候选。"""
    releases_path = tmp_path / "releases.json"
    timeline_path = tmp_path / "timeline.json"
    state_path = tmp_path / "backfill.json"
    old_sha = "a" * 40
    new_sha = "b" * 40
    releases_path.write_text('{"schemaVersion": 1, "releases": []}\n', encoding="utf-8")
    timeline_path.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "repositories": {
                    "SynlysAI/AI4MS": {
                        "page": 1,
                        "processed": 1,
                        "processedShas": [old_sha],
                        "watermark": {"sha": old_sha, "publishedAt": "2026-08-09T00:00:00Z"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class IncrementalClient:
        """提供一个水位之后的新提交。"""

        def __init__(self):
            """初始化水位调用记录。"""
            self.calls: list[tuple[str, bool, str | None, int]] = []

        @staticmethod
        def list_releases(_repository):
            """本测试不产生正式 Release。"""
            return []

        def list_commits(self, repository, *, include_pull_requests, stop_at_sha, max_items, page):
            """返回客户端已在旧水位前截断的新提交。"""
            self.calls.append((repository, include_pull_requests, stop_at_sha, max_items, page))
            assert max_items == 500
            return [
                {
                    "sha": new_sha,
                    "message": "feat(core): new incremental change",
                    "occurred_at": "2026-08-10T00:00:00Z",
                    "pull_requests": [{"title": "Incremental PR", "body": "PR body"}],
                }
            ]

    seen_prs: list[dict[str, str]] = []

    class RecordingAI:
        """记录 sync 传给 AI 的关联 PR。"""

        @classmethod
        def from_environment(cls):
            """返回记录客户端。"""
            return cls()

        @staticmethod
        def enrich_candidate(event, *, product_name, commit_messages, pull_requests, repository_private):
            """保留候选并记录 PR 参数。"""
            del product_name, commit_messages, repository_private
            seen_prs.extend(pull_requests)
            return event

    monkeypatch.setenv("AI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "AIClient", RecordingAI)
    args = cli._parser().parse_args(
        [
            "sync",
            "--product",
            "ai4ms",
            "--candidates",
            str(releases_path),
            "--timeline",
            str(timeline_path),
            "--state",
            str(state_path),
        ]
    )
    client = IncrementalClient()

    assert cli._sync(args, object_store=InMemoryR2Client(), github_client=client) == 0
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert client.calls == [("SynlysAI/AI4MS", True, old_sha, 500, 1)]
    assert [sha for event in timeline["events"] for sha in event["source"]["commitShas"]] == [new_sha[:7]]
    assert updated_state["repositories"]["SynlysAI/AI4MS"]["watermark"]["sha"] == new_sha
    assert seen_prs == [{"title": "Incremental PR", "body": "PR body"}]


def test_sync_continues_after_full_batch_without_losing_or_repeating_commits(tmp_path: Path, monkeypatch):
    """600 条新提交应分两轮处理，第二轮从第六页续读且不重复候选。"""
    releases_path = tmp_path / "releases.json"
    timeline_path = tmp_path / "timeline.json"
    state_path = tmp_path / "backfill.json"
    old_sha = "a" * 40
    new_shas = [f"{index + 1:07x}" + "0" * 33 for index in range(600)]
    releases_path.write_text('{"schemaVersion": 1, "releases": []}\n', encoding="utf-8")
    timeline_path.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "repositories": {
                    "SynlysAI/AI4MS": {
                        "page": 1,
                        "processed": 1,
                        "processedShas": [old_sha],
                        "watermark": {"sha": old_sha, "publishedAt": "2026-08-01T00:00:00Z"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class PaginatedIncrementalClient:
        """按 GitHub 页号模拟水位前的 600 条新提交。"""

        def __init__(self):
            """初始化调用记录。"""
            self.calls: list[tuple[int, str | None]] = []

        @staticmethod
        def list_releases(_repository):
            """本测试不生成 Release。"""
            return []

        def list_commits(self, _repository, *, max_items, page, include_pull_requests, stop_at_sha):
            """第 1 页请求返回五页结果，第 6 页请求返回剩余 100 条。"""
            assert max_items == 500
            assert include_pull_requests is True
            self.calls.append((page, stop_at_sha))
            selected = new_shas[:500] if page == 1 else new_shas[500:]
            return [
                {
                    "sha": sha,
                    "message": "feat(core): incremental batch",
                    "occurred_at": (
                        f"2026-08-10T{23 - index // 60:02d}:{59 - index % 60:02d}:00Z"
                    ),
                    "pull_requests": [],
                }
                for index, sha in enumerate(selected)
            ]

    monkeypatch.delenv("AI_API_KEY", raising=False)
    args = cli._parser().parse_args(
        [
            "sync",
            "--product",
            "ai4ms",
            "--candidates",
            str(releases_path),
            "--timeline",
            str(timeline_path),
            "--state",
            str(state_path),
        ]
    )
    client = PaginatedIncrementalClient()

    assert cli._sync(args, object_store=InMemoryR2Client(), github_client=client) == 0
    partial_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert partial_state["repositories"]["SynlysAI/AI4MS"]["watermark"]["sha"] == new_shas[499]
    assert cli._sync(args, object_store=InMemoryR2Client(), github_client=client) == 0
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    candidate_shas = [
        sha
        for event in timeline["events"]
        for sha in event["source"]["commitShas"]
    ]

    assert client.calls == [(1, old_sha), (6, old_sha)]
    assert len(candidate_shas) == 600
    assert set(candidate_shas) == {sha[:7] for sha in new_shas}
    assert final_state["repositories"]["SynlysAI/AI4MS"]["watermark"]["sha"] == new_shas[0]
    assert "syncPending" not in final_state["repositories"]["SynlysAI/AI4MS"]


def test_sync_pending_full_duplicate_page_advances_to_next_page(tmp_path: Path, monkeypatch):
    """续读页满 500 条但全部已处理时，也必须推进页号避免重复卡住。"""
    releases_path = tmp_path / "releases.json"
    timeline_path = tmp_path / "timeline.json"
    state_path = tmp_path / "backfill.json"
    old_sha = "a" * 40
    known_shas = [f"{index + 1:07x}" + "0" * 33 for index in range(1000)]
    releases_path.write_text('{"schemaVersion": 1, "releases": []}\n', encoding="utf-8")
    timeline_path.write_text('{"schemaVersion": 1, "events": []}\n', encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "repositories": {
                    "SynlysAI/AI4MS": {
                        "page": 1,
                        "processed": len(known_shas),
                        "processedShas": known_shas,
                        "watermark": {"sha": known_shas[499], "publishedAt": "2026-08-10T12:00:00Z"},
                        "syncPending": {
                            "frontier": {"sha": known_shas[0], "publishedAt": "2026-08-10T23:00:00Z"},
                            "nextPage": 6,
                            "stop": {"sha": old_sha, "publishedAt": "2026-08-01T00:00:00Z"},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class DuplicatePageClient:
        """第六页重复返回已处理满批，第十一页为空。"""

        def __init__(self):
            """初始化页号记录。"""
            self.pages: list[int] = []

        @staticmethod
        def list_releases(_repository):
            """本测试不产生 Release。"""
            return []

        def list_commits(self, _repository, *, max_items, page, include_pull_requests, stop_at_sha):
            """模拟连续分页中的重复完整页与最终短页。"""
            assert max_items == 500
            assert include_pull_requests is True
            assert stop_at_sha == old_sha
            self.pages.append(page)
            selected = known_shas[500:] if page == 6 else []
            return [
                {
                    "sha": sha,
                    "message": "feat(core): already processed",
                    "occurred_at": "2026-08-10T12:00:00Z",
                    "pull_requests": [],
                }
                for sha in selected
            ]

    monkeypatch.delenv("AI_API_KEY", raising=False)
    args = cli._parser().parse_args(
        [
            "sync",
            "--product",
            "ai4ms",
            "--candidates",
            str(releases_path),
            "--timeline",
            str(timeline_path),
            "--state",
            str(state_path),
        ]
    )
    client = DuplicatePageClient()

    assert cli._sync(args, object_store=InMemoryR2Client(), github_client=client) == 0
    intermediate = json.loads(state_path.read_text(encoding="utf-8"))
    assert intermediate["repositories"]["SynlysAI/AI4MS"]["syncPending"]["nextPage"] == 11
    assert cli._sync(args, object_store=InMemoryR2Client(), github_client=client) == 0
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert client.pages == [6, 11]
    assert "syncPending" not in final_state["repositories"]["SynlysAI/AI4MS"]
