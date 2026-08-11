"""Release Portal GitHub Actions 工作流结构测试。"""

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
    assert "automation/release-portal-candidates" in workflow
    assert "python -m scripts.release_portal.cli sync" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "git ls-files --others" in workflow


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
    assert "AI_PRIVATE_ENDPOINT_ALLOWLIST" in workflow


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
    """发布命令应在其余五个对象成功上传后才写入 manifest。"""
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

        def put_object(self, *, Bucket, Key, Body, **_kwargs):  # noqa: N803
            """记录公开对象上传。"""
            assert Bucket == "downloads"
            assert isinstance(Body, bytes)
            self.keys.append(Key)

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
    assert store.keys == [
        "portal/v1/products.json",
        "portal/v1/releases.json",
        "portal/v1/timeline.json",
        "portal/v1/faqs.json",
        "portal/v1/meta.json",
        "portal/v1/manifest.json",
    ]
