import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts.release_portal.assets import (
    AssetConflictError,
    AssetUploader,
    FilesystemR2Client,
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


class BotoStyleFake:
    """仅接受 boto3 关键字参数的最小 fake。"""

    def __init__(self, *, wrong_copy: bool = False):
        self.objects = {}
        self.calls = []
        self.wrong_copy = wrong_copy

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Bucket, Key))
        try:
            body, metadata = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FileNotFoundError(Key) from exc
        return {"ContentLength": len(body), "Metadata": metadata, "ContentType": metadata["ContentType"], "ContentDisposition": metadata["ContentDisposition"]}

    def put_object(self, *, Bucket, Key, Body, ContentType, ContentDisposition, Metadata):
        self.calls.append(("put", Bucket, Key, ContentType, ContentDisposition, Metadata))
        body = Body.read() if hasattr(Body, "read") else Body
        self.objects[(Bucket, Key)] = (body, {"ContentType": ContentType, "ContentDisposition": ContentDisposition, **Metadata})

    def copy_object(self, *, Bucket, CopySource, Key):
        self.calls.append(("copy", Bucket, CopySource, Key))
        body, metadata = self.objects[(Bucket, CopySource["Key"])]
        if self.wrong_copy:
            body = b"wrong"
            metadata = dict(metadata)
            metadata["sha256"] = "0" * 64
        self.objects[(Bucket, Key)] = (body, metadata)

    def delete_object(self, *, Bucket, Key):
        self.calls.append(("delete", Bucket, Key))
        self.objects.pop((Bucket, Key), None)


def test_boto_style_client_uses_keyword_parameters(tmp_path: Path):
    client = BotoStyleFake()
    result = AssetUploader(client, bucket="downloads").upload_release_asset(
        _asset_file(tmp_path), product_id="ai4ms", version="v1"
    )
    assert ("downloads", result["downloadPath"]) in [(call[1], call[2]) for call in client.calls if call[0] == "head"]
    assert any(call[0] == "copy" and call[2]["Bucket"] == "downloads" for call in client.calls)


def test_boto_style_missing_get_object_is_treated_as_no_snapshot(tmp_path: Path):
    class ClientError(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    class ReadableBoto(BotoStyleFake):
        def get_object(self, *, Bucket, Key):
            try:
                body, _ = self.objects[(Bucket, Key)]
            except KeyError as exc:
                raise ClientError() from exc
            return {"Body": io.BytesIO(body)}

    result = AssetUploader(ReadableBoto(), bucket="downloads").upload_release_asset(
        _asset_file(tmp_path), product_id="ai4ms", version="v1"
    )
    assert result["name"] == "SpecAgent-linux-amd64.tar.gz"


def test_wrong_copy_without_old_object_deletes_formal_key(tmp_path: Path):
    client = BotoStyleFake(wrong_copy=True)
    uploader = AssetUploader(client, bucket="downloads")
    with pytest.raises(RuntimeError, match="SHA-256"):
        uploader.upload_release_asset(_asset_file(tmp_path), product_id="ai4ms", version="v1")
    assert not any(key.endswith(".tmp") for _, key in client.objects)
    assert not any(key == "assets/ai4ms/v1/SpecAgent-linux-amd64.tar.gz" for _, key in client.objects)


def test_wrong_copy_restores_old_object(tmp_path: Path):
    client = InMemoryR2Client()
    uploader = AssetUploader(client, bucket="downloads")
    path = _asset_file(tmp_path, b"old")
    uploader.upload_release_asset(path, product_id="ai4ms", version="v1")
    path.write_bytes(b"new")

    class WrongCopy(InMemoryR2Client):
        def copy_object(self, bucket, source_key, destination_key):
            result = super().copy_object(bucket, source_key, destination_key)
            self.objects[(bucket, destination_key)] = b"wrong"
            return result

    replacement = WrongCopy()
    replacement.objects = dict(client.objects)
    replacement.metadata = dict(client.metadata)
    replacement.versions = dict(client.versions)
    uploader = AssetUploader(replacement, bucket="downloads")
    with pytest.raises(RuntimeError, match="SHA-256"):
        uploader.upload_release_asset(path, product_id="ai4ms", version="v1", replace=True)
    assert replacement.get_object("downloads", "assets/ai4ms/v1/SpecAgent-linux-amd64.tar.gz") == b"old"


def test_failed_replacement_prunes_versions_to_three(tmp_path: Path):
    class WrongCopy(InMemoryR2Client):
        wrong_copy = False

        def copy_object(self, bucket, source_key, destination_key):
            result = super().copy_object(bucket, source_key, destination_key)
            if self.wrong_copy:
                self.objects[(bucket, destination_key)] = b"wrong"
            return result

    client = WrongCopy()
    uploader = AssetUploader(client, bucket="downloads")
    path = _asset_file(tmp_path, b"one")
    for content in (b"one", b"two", b"three"):
        path.write_bytes(content)
        uploader.upload_release_asset(path, product_id="ai4ms", version="v1", replace=content != b"one")
    path.write_bytes(b"four")
    client.wrong_copy = True

    with pytest.raises(RuntimeError, match="SHA-256"):
        uploader.upload_release_asset(path, product_id="ai4ms", version="v1", replace=True)
    key = "assets/ai4ms/v1/SpecAgent-linux-amd64.tar.gz"
    assert len(client.list_object_versions("downloads", key)) <= 3


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


def test_filesystem_store_writes_stream_in_chunks(tmp_path: Path):
    class ChunkReader:
        def __init__(self):
            self.remaining = b"x" * (1024 * 1024 + 3)
            self.calls = []

        def read(self, size):
            self.calls.append(size)
            assert size == 1024 * 1024
            value, self.remaining = self.remaining[:size], self.remaining[size:]
            return value

    client = FilesystemR2Client(tmp_path / "store")
    reader = ChunkReader()
    client.put_object("downloads", "assets/ai4ms/v1/app.bin", reader, content_type="application/octet-stream", content_disposition="attachment", metadata={"sha256": "a" * 64})
    assert client.head_object("downloads", "assets/ai4ms/v1/app.bin")["ContentLength"] == 1024 * 1024 + 3
    assert reader.calls == [1024 * 1024, 1024 * 1024, 1024 * 1024]


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


def test_cli_uses_configured_r2_client_without_printing_credentials(monkeypatch):
    from scripts.release_portal import cli

    calls = {}

    class FakeR2:
        def __init__(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(cli, "Boto3R2Client", FakeR2)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "access-secret")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account-id")
    args = type("Args", (), {"store_root": None})()
    cli.build_object_store(args)
    assert calls["account_id"] == "account-id"
    assert calls["secret_access_key"] == "secret-value"


def test_cli_rejects_partial_r2_configuration(tmp_path: Path, monkeypatch, capsys):
    from scripts.release_portal import cli

    monkeypatch.setenv("R2_ACCESS_KEY_ID", "partial-secret")
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    exit_code = cli.main(["upload-asset", "--product", "ai4ms", "--version", "v1", "--channel", "manual", "--platform", "linux", "--file", str(_asset_file(tmp_path)), "--bucket", "downloads", "--store-root", str(tmp_path / "store")])
    assert exit_code == 1
    assert "partial-secret" not in capsys.readouterr().out
