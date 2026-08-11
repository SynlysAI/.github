"""Release Portal GitHub Actions 工作流结构测试。"""

import json
from pathlib import Path

from scripts.release_portal import cli
from scripts.release_portal.assets import InMemoryR2Client
from scripts.release_portal.github import Release, ReleaseAsset

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


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
    assert "AI_PRIVATE_ENDPOINT_ALLOWLIST" not in workflow
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

    monkeypatch.setattr(cli, "_download_release_asset", lambda *_args: downloaded)
    args = cli._parser().parse_args(
        ["sync", "--product", "ai4ms", "--candidates", str(tmp_path / "releases.json")]
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
    assert store.keys[-1] == "portal/v1/manifest.json"
    generation_prefix = store.keys[0].rsplit("/", 1)[0]
    assert generation_prefix.startswith("portal/v1/generations/")
    assert store.keys[:-1] == [
        f"{generation_prefix}/products.json",
        f"{generation_prefix}/releases.json",
        f"{generation_prefix}/timeline.json",
        f"{generation_prefix}/faqs.json",
        f"{generation_prefix}/meta.json",
    ]
    pointer = json.loads(store.bodies["portal/v1/manifest.json"])
    assert all(item["path"].startswith(f"{generation_prefix}/") for item in pointer["collections"].values())
    assert all(item["path"].startswith(f"{generation_prefix}/") for item in pointer["files"])


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
    assert not any(key == "portal/v1/products.json" for key in store.objects)


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
        ("SynlysAI/AI4MS", 2, 1, False),
        ("SynlysAI/AI4MS", 2, 2, False),
    ]
    assert all(call[1] <= 2 for call in client.calls)
