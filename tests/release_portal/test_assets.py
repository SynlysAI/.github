import hashlib
import json
from pathlib import Path

import pytest

from scripts.release_portal.assets import (
    AssetConflictError,
    AssetUploader,
    InMemoryR2Client,
    StorageConfig,
    public_asset_metadata,
)


def _asset_file(tmp_path: Path, content: bytes = b"installer") -> Path:
    path = tmp_path / "SpecAgent-linux-amd64.tar.gz"
    path.write_bytes(content)
    return path


def test_upload_is_idempotent_and_sets_public_metadata(tmp_path: Path):
    client = InMemoryR2Client()
    uploader = AssetUploader(client, bucket="downloads")
    path = _asset_file(tmp_path)

    first = uploader.upload_release_asset(
        path, product_id="spec-agent", version="v1.0.0"
    )
    second = uploader.upload_release_asset(
        path, product_id="spec-agent", version="v1.0.0"
    )

    assert first == second
    assert first["downloadPath"] == "assets/spec-agent/v1.0.0/SpecAgent-linux-amd64.tar.gz"
    assert first["sha256"] == hashlib.sha256(b"installer").hexdigest()
    stored = client.head_object("downloads", first["downloadPath"])
    assert stored["ContentType"] == "application/gzip"
    assert stored["ContentDisposition"].startswith("attachment;")
    assert len(client.put_calls) == 2  # 临时对象和正式复制各一次，第二次调用不再上传


def test_same_name_different_content_rejects_without_replace(tmp_path: Path):
    client = InMemoryR2Client()
    uploader = AssetUploader(client, bucket="downloads")
    path = _asset_file(tmp_path, b"one")
    uploader.upload_release_asset(path, product_id="ai4ms", version="1")
    path.write_bytes(b"two")

    with pytest.raises(AssetConflictError):
        uploader.upload_release_asset(path, product_id="ai4ms", version="1")

    assert client.get_object("downloads", "assets/ai4ms/1/SpecAgent-linux-amd64.tar.gz") == b"one"


def test_replace_requires_flag_and_updates_sha(tmp_path: Path):
    client = InMemoryR2Client()
    uploader = AssetUploader(client, bucket="downloads")
    path = _asset_file(tmp_path, b"one")
    uploader.upload_release_asset(path, product_id="ai4ms", version="1")
    path.write_bytes(b"two")

    result = uploader.upload_release_asset(path, product_id="ai4ms", version="1", replace=True)
    assert result["sha256"] == hashlib.sha256(b"two").hexdigest()
    assert client.get_object("downloads", result["downloadPath"]) == b"two"
    assert len(client.list_object_versions("downloads", result["downloadPath"])) <= 3


def test_failed_copy_rolls_back_temporary_object(tmp_path: Path):
    class BrokenCopy(InMemoryR2Client):
        def copy_object(self, bucket, source_key, destination_key):
            raise RuntimeError("copy failed")

    client = BrokenCopy()
    uploader = AssetUploader(client, bucket="downloads")
    with pytest.raises(RuntimeError, match="copy failed"):
        uploader.upload_release_asset(_asset_file(tmp_path), product_id="ai4ms", version="1")
    assert client.objects == {}


def test_public_asset_metadata_does_not_expose_github_url():
    result = public_asset_metadata(
        {
            "name": "app.exe",
            "size": 4,
            "platform": "windows",
            "architecture": "x86_64",
            "sha256": "a" * 64,
            "download_url": "https://uploads.github.com/private-token",
        },
        product_id="smartaccess",
        version="v2",
    )
    assert set(result) == {"downloadPath", "name", "size", "platform", "architecture", "sha256"}
    assert "github" not in json.dumps(result).lower()


def test_storage_config_requires_versioning_and_retains_three_versions():
    config = StorageConfig.from_mapping({"versioning": True, "retainVersions": 3})
    assert config.versioning is True and config.retain_versions == 3
    with pytest.raises(ValueError):
        StorageConfig.from_mapping({"versioning": False, "retainVersions": 3})
    with pytest.raises(ValueError):
        StorageConfig.from_mapping({"versioning": True, "retainVersions": 2})


def test_cli_upload_asset_requires_fields_and_writes_candidate(tmp_path: Path, monkeypatch, capsys):
    from scripts.release_portal import cli

    client = InMemoryR2Client()
    monkeypatch.setattr(cli, "build_object_store", lambda args: client)
    candidate = tmp_path / "manifest.json"
    monkeypatch.setattr(cli, "MANIFEST_CANDIDATE_PATH", candidate)
    path = _asset_file(tmp_path)

    assert cli.main(["upload-asset", "--product", "ai4ms", "--version", "v1", "--channel", "manual", "--platform", "linux", "--file", str(path), "--bucket", "downloads"]) == 0
    line = capsys.readouterr().out.strip()
    log = json.loads(line)
    assert {"runId", "productId", "stage", "count", "durationMs", "status"} <= set(log)
    assert log["status"] == "success"
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    assert payload["assets"][0]["downloadPath"].startswith("assets/ai4ms/v1/")
