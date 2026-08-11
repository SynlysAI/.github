"""Release 附件及手动资源的对象存储同步工具。

模块只依赖一个很小的 S3/R2 客户端抽象，生产环境可接入 boto3，测试环境使用
``InMemoryR2Client``，因此不会在单元测试中访问网络。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import posixpath
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Protocol
from urllib.parse import quote


class AssetConflictError(FileExistsError):
    """同一对象 key 已存在但内容不同。"""


class ObjectStore(Protocol):
    """上传器所需的最小对象存储接口。"""

    def head_object(self, bucket: str, key: str) -> Mapping[str, Any] | None:
        """读取对象元数据。"""

    def put_object(self, bucket: str, key: str, body: BinaryIO, **kwargs: Any) -> Any:
        """写入对象。"""

    def copy_object(self, bucket: str, source_key: str, destination_key: str) -> Any:
        """在同一 bucket 内复制对象。"""

    def delete_object(self, bucket: str, key: str) -> Any:
        """删除对象。"""


@dataclass(frozen=True)
class StorageConfig:
    """R2 对象版本策略配置。"""

    versioning: bool = True
    retain_versions: int = 3

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "StorageConfig":
        """从配置映射读取并校验版本策略。

        Args:
            value: 包含 ``versioning`` 和 ``retainVersions`` 的配置映射。

        Returns:
            校验后的存储配置。

        Raises:
            ValueError: 版本控制未开启或保留版本数不是三。
        """
        raw = dict(value or {})
        enabled = raw.get("versioning", raw.get("objectVersioning", True))
        retain = raw.get("retainVersions", raw.get("retain_versions", 3))
        if enabled is not True:
            raise ValueError("R2 Object Versioning 必须开启")
        try:
            retain_int = int(retain)
        except (TypeError, ValueError) as exc:
            raise ValueError("retainVersions 必须为整数 3") from exc
        if retain_int != 3:
            raise ValueError("retainVersions 必须为 3")
        return cls(versioning=True, retain_versions=retain_int)


class InMemoryR2Client:
    """支持版本的内存对象存储，用于测试和离线运行。"""

    def __init__(self) -> None:
        """初始化空 bucket、对象版本和调用记录。"""
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict[str, Any]] = {}
        self.versions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.put_calls: list[tuple[str, str]] = []
        self.versioning: dict[str, bool] = {}

    def head_object(self, bucket: str, key: str) -> Mapping[str, Any] | None:
        """读取当前对象元数据，不存在时返回 ``None``。

        Args:
            bucket: bucket 名称。
            key: 对象 key。

        Returns:
            对象元数据映射或 ``None``。
        """
        identity = (bucket, key)
        if identity not in self.objects:
            return None
        result = dict(self.metadata[identity])
        result["ContentLength"] = len(self.objects[identity])
        return result

    def put_object(self, bucket: str, key: str, body: BinaryIO | bytes, **kwargs: Any) -> dict[str, str]:
        """写入对象并保留一个版本。

        Args:
            bucket: bucket 名称。
            key: 对象 key。
            body: 二进制流或字节串。
            kwargs: ContentType、ContentDisposition 和 Metadata 等元数据。

        Returns:
            新建对象版本的标识。
        """
        data = body.read() if hasattr(body, "read") else bytes(body)
        metadata = dict(kwargs.get("metadata") or kwargs.get("Metadata") or {})
        content_type = kwargs.get("content_type", kwargs.get("ContentType"))
        disposition = kwargs.get("content_disposition", kwargs.get("ContentDisposition"))
        if content_type is not None:
            metadata["ContentType"] = content_type
        if disposition is not None:
            metadata["ContentDisposition"] = disposition
        identity = (bucket, key)
        version_id = uuid.uuid4().hex
        self.objects[identity] = data
        self.metadata[identity] = metadata
        self.versions.setdefault(identity, []).insert(0, {"VersionId": version_id, "Size": len(data), **metadata})
        self.versions[identity] = self.versions[identity][:3]
        self.put_calls.append(identity)
        return {"VersionId": version_id}

    def copy_object(self, bucket: str, source_key: str, destination_key: str) -> dict[str, str]:
        """复制对象并创建目标对象的新版本。

        Args:
            bucket: bucket 名称。
            source_key: 临时源 key。
            destination_key: 正式目标 key。

        Returns:
            新建对象版本的标识。
        """
        source = (bucket, source_key)
        if source not in self.objects:
            raise FileNotFoundError(source_key)
        return self.put_object(bucket, destination_key, self.objects[source], metadata=self.metadata[source])

    def delete_object(self, bucket: str, key: str) -> None:
        """删除当前对象及其版本记录。

        Args:
            bucket: bucket 名称。
            key: 对象 key。

        Returns:
            无返回值。
        """
        identity = (bucket, key)
        self.objects.pop(identity, None)
        self.metadata.pop(identity, None)
        self.versions.pop(identity, None)

    def get_object(self, bucket: str, key: str) -> bytes:
        """读取当前对象内容。

        Args:
            bucket: bucket 名称。
            key: 对象 key。

        Returns:
            对象二进制内容。

        Raises:
            FileNotFoundError: 对象不存在。
        """
        try:
            return self.objects[(bucket, key)]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    def list_object_versions(self, bucket: str, key: str) -> list[dict[str, Any]]:
        """列出对象最近保留的版本。

        Args:
            bucket: bucket 名称。
            key: 对象 key。

        Returns:
            按最新到最旧排序的版本元数据列表。
        """
        return list(self.versions.get((bucket, key), []))

    def prune_versions(self, bucket: str, key: str, *, keep: int = 3) -> None:
        """显式裁剪对象版本，只保留最近 ``keep`` 个。

        Args:
            bucket: bucket 名称。
            key: 对象 key。
            keep: 保留版本数。

        Returns:
            无返回值。
        """
        identity = (bucket, key)
        self.versions[identity] = self.versions.get(identity, [])[:keep]

    def get_bucket_versioning(self, bucket: str) -> dict[str, str]:
        """返回 bucket 的版本控制状态。"""
        return {"Status": "Enabled" if self.versioning.get(bucket, True) else "Suspended"}

    def put_bucket_versioning(self, bucket: str, status: str = "Enabled") -> None:
        """设置 bucket 的版本控制状态。"""
        self.versioning[bucket] = status.casefold() == "enabled"


def validate_storage_config(client: Any, bucket: str, config: StorageConfig | Mapping[str, Any] | None = None) -> StorageConfig:
    """校验并开启对象版本控制，不执行网络请求以外的副作用。

    Args:
        client: 兼容 S3/R2 的客户端抽象。
        bucket: bucket 名称。
        config: 版本策略配置或 ``StorageConfig``。

    Returns:
        经过校验的存储配置。

    Raises:
        ValueError: 配置不符合版本控制和保留三版本要求。
    """
    resolved = config if isinstance(config, StorageConfig) else StorageConfig.from_mapping(config)
    put_versioning = getattr(client, "put_bucket_versioning", None)
    if callable(put_versioning):
        put_versioning(bucket, status="Enabled")
    status_getter = getattr(client, "get_bucket_versioning", None)
    if callable(status_getter):
        status = status_getter(bucket)
        if str(status.get("Status", "")).casefold() != "enabled":
            raise ValueError("R2 Object Versioning 未启用")
    return resolved


def _safe_segment(value: str, field: str) -> str:
    """校验 key 路径片段，阻止路径穿越和绝对路径。

    Args:
        value: 待校验片段。
        field: 字段名称。

    Returns:
        原始片段。

    Raises:
        ValueError: 片段为空或包含路径分隔符。
    """
    text = str(value).strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        raise ValueError(f"{field} 包含非法路径片段")
    return text


def asset_key(product_id: str, version: str, asset_name: str) -> str:
    """生成 Release 附件的固定 R2 key。

    Args:
        product_id: 产品 ID。
        version: 发布版本。
        asset_name: 附件文件名。

    Returns:
        ``assets/{productId}/{version}/{assetName}`` key。
    """
    product = _safe_segment(product_id, "productId")
    release = _safe_segment(version, "version")
    name = _safe_segment(asset_name, "assetName")
    return posixpath.join("assets", product, release, name)


def _stream_digest(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """以流式方式计算本地文件 SHA-256 和字节数。

    Args:
        path: 本地文件路径。
        chunk_size: 每次读取的字节数。

    Returns:
        ``(sha256, size)`` 二元组。
    """
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _metadata_value(metadata: Mapping[str, Any] | None, name: str) -> Any:
    """以大小写不敏感方式读取 S3 元数据字段。"""
    if not metadata:
        return None
    for key, value in metadata.items():
        if str(key).casefold() == name.casefold():
            return value
    nested = metadata.get("Metadata")
    if isinstance(nested, Mapping):
        for key, value in nested.items():
            if str(key).casefold() == name.casefold():
                return value
    return None


def _guess_content_type(name: str) -> str:
    """按附件扩展名推断稳定的 Content-Type。

    Args:
        name: 文件名。

    Returns:
        MIME 类型字符串。
    """
    lower = name.casefold()
    if lower.endswith((".tar.gz", ".tgz")):
        return "application/gzip"
    if lower.endswith(".zip"):
        return "application/zip"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class AssetUploader:
    """将本地 Release 附件安全地同步到 R2。"""

    def __init__(self, client: ObjectStore, bucket: str, *, config: StorageConfig | Mapping[str, Any] | None = None) -> None:
        """初始化上传器并校验对象版本策略。

        Args:
            client: 对象存储客户端。
            bucket: bucket 名称。
            config: R2 版本控制配置。

        Returns:
            无返回值。
        """
        self.client = client
        self.bucket = _safe_segment(bucket, "bucket")
        self.config = validate_storage_config(client, self.bucket, config)

    def upload_release_asset(
        self,
        file_path: str | Path,
        *,
        product_id: str,
        version: str,
        platform: str | None = None,
        architecture: str | None = None,
        replace: bool = False,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """上传 GitHub Release 附件并返回公开元数据。

        Args:
            file_path: 本地附件路径。
            product_id: 产品 ID。
            version: Release 版本。
            platform: 平台名称，缺省时从文件名推断。
            architecture: 架构名称，缺省时从文件名推断。
            replace: 是否允许覆盖已有不同内容对象。
            content_type: 可选 MIME 类型。

        Returns:
            仅包含公开下载字段的附件元数据。

        Raises:
            AssetConflictError: 同名对象内容不同且未显式 replace。
            RuntimeError: 临时上传或校验失败。
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        name = _safe_segment(path.name, "assetName")
        key = asset_key(product_id, version, name)
        sha256, size = _stream_digest(path)
        existing = self._head(key)
        previous = self._snapshot(key)
        if existing is not None:
            old_sha = _metadata_value(existing, "sha256")
            old_size = _metadata_value(existing, "ContentLength")
            if str(old_sha or "") == sha256 and (old_size is None or int(old_size) == size):
                return self._public_result(key, name, size, sha256, platform, architecture)
            if not replace:
                raise AssetConflictError(f"对象已存在且内容不同: {key}")

        resolved_type = content_type or _guess_content_type(name)
        disposition = f'attachment; filename="{name}"'
        temp_key = f"{key}.tmp-{uuid.uuid4().hex}"
        metadata = {"sha256": sha256, "platform": platform or "unknown", "architecture": architecture or "unknown"}
        try:
            with path.open("rb") as stream:
                self._put(temp_key, stream, resolved_type, disposition, metadata)
            uploaded = self._head(temp_key)
            if uploaded is None or str(_metadata_value(uploaded, "sha256") or "") != sha256:
                raise RuntimeError("临时对象 SHA-256 校验失败")
            if int(_metadata_value(uploaded, "ContentLength") or -1) != size:
                raise RuntimeError("临时对象大小校验失败")
            self._copy(temp_key, key)
            final = self._head(key)
            if final is None or str(_metadata_value(final, "sha256") or "") != sha256:
                raise RuntimeError("正式对象 SHA-256 校验失败")
        except Exception:
            self._delete(temp_key)
            self._restore(key, previous)
            raise
        else:
            self._delete(temp_key)
        self._prune_versions(key)
        return self._public_result(key, name, size, sha256, platform, architecture)

    def upload_file(self, file_path: str | Path, **kwargs: Any) -> dict[str, Any]:
        """``upload_release_asset`` 的兼容别名。

        Args:
            file_path: 本地附件路径。
            kwargs: 上传参数。

        Returns:
            公开附件元数据。
        """
        return self.upload_release_asset(file_path, **kwargs)

    def _head(self, key: str) -> Mapping[str, Any] | None:
        """读取对象元数据并兼容 boto3 风格响应。

        Args:
            key: 对象 key。

        Returns:
            对象元数据或 ``None``。
        """
        try:
            return self.client.head_object(self.bucket, key)
        except (KeyError, FileNotFoundError):
            return None

    def _put(self, key: str, body: BinaryIO, content_type: str, disposition: str, metadata: Mapping[str, Any]) -> Any:
        """调用对象存储 put 接口。

        Args:
            key: 对象 key。
            body: 二进制输入流。
            content_type: MIME 类型。
            disposition: Content-Disposition 值。
            metadata: 对象自定义元数据。

        Returns:
            客户端写入响应。
        """
        try:
            return self.client.put_object(self.bucket, key, body, content_type=content_type, content_disposition=disposition, metadata=dict(metadata))
        except TypeError:
            return self.client.put_object(self.bucket, key, Body=body, ContentType=content_type, ContentDisposition=disposition, Metadata=dict(metadata))

    def _copy(self, source_key: str, destination_key: str) -> Any:
        """调用对象存储复制接口。

        Args:
            source_key: 临时源 key。
            destination_key: 正式目标 key。

        Returns:
            客户端复制响应。
        """
        try:
            return self.client.copy_object(self.bucket, source_key, destination_key)
        except TypeError:
            return self.client.copy_object(Bucket=self.bucket, CopySource={"Bucket": self.bucket, "Key": source_key}, Key=destination_key)

    def _delete(self, key: str) -> None:
        """尽力删除临时对象，不覆盖原始异常。

        Args:
            key: 待删除对象 key。

        Returns:
            无返回值。
        """
        try:
            try:
                self.client.delete_object(self.bucket, key)
            except TypeError:
                self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    def _prune_versions(self, key: str) -> None:
        """调用客户端抽象保留最近三个对象版本。

        Args:
            key: 对象 key。

        Returns:
            无返回值。
        """
        pruner = getattr(self.client, "prune_versions", None)
        if callable(pruner):
            pruner(self.bucket, key, keep=self.config.retain_versions)

    @staticmethod
    def _public_result(key: str, name: str, size: int, sha256: str, platform: str | None, architecture: str | None) -> dict[str, Any]:
        """组装公开附件字段。

        Args:
            key: 正式对象 key。
            name: 文件名。
            size: 文件大小。
            sha256: 文件 SHA-256。
            platform: 平台名称。
            architecture: 架构名称。

        Returns:
            公开附件元数据。
        """
        return {
            "downloadPath": key,
            "name": name,
            "size": size,
            "platform": platform or "unknown",
            "architecture": architecture or "unknown",
            "sha256": sha256,
        }

    def _snapshot(self, key: str) -> tuple[bytes, dict[str, Any]] | None:
        """读取可恢复对象客户端的当前快照。"""
        objects = getattr(self.client, "objects", None)
        metadata = getattr(self.client, "metadata", None)
        if isinstance(objects, dict) and isinstance(metadata, dict):
            identity = (self.bucket, key)
            if identity in objects:
                return bytes(objects[identity]), dict(metadata.get(identity, {}))
        return None

    def _restore(self, key: str, snapshot: tuple[bytes, dict[str, Any]] | None) -> None:
        """在正式复制后失败时恢复内存客户端的旧对象。"""
        if snapshot is None:
            return
        objects = getattr(self.client, "objects", None)
        metadata = getattr(self.client, "metadata", None)
        if isinstance(objects, dict) and isinstance(metadata, dict):
            identity = (self.bucket, key)
            objects[identity], metadata[identity] = snapshot


def public_asset_metadata(asset: Mapping[str, Any], *, product_id: str, version: str) -> dict[str, Any]:
    """将 GitHub 或内部附件映射脱敏为公开六字段。

    Args:
        asset: 内部附件映射，可包含私有 GitHub URL。
        product_id: 产品 ID。
        version: 版本号。

    Returns:
        只含 downloadPath、name、size、platform、architecture、sha256 的映射。

    Raises:
        ValueError: 缺少必要字段或 SHA-256 格式错误。
    """
    name = _safe_segment(str(asset.get("name") or ""), "assetName")
    try:
        size = int(asset.get("size", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("附件 size 必须为整数") from exc
    if size < 0:
        raise ValueError("附件 size 不能为负数")
    sha256 = str(asset.get("sha256") or "").lower()
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ValueError("附件 sha256 必须为 64 位十六进制")
    return {
        "downloadPath": asset_key(product_id, version, name),
        "name": name,
        "size": size,
        "platform": str(asset.get("platform") or "unknown"),
        "architecture": str(asset.get("architecture") or "unknown"),
        "sha256": sha256,
    }


def public_release_assets(release: Any, *, product_id: str, version: str) -> list[dict[str, Any]]:
    """将 Release 内的附件列表投影为公开元数据。

    Args:
        release: ``Release`` 对象或包含 ``assets`` 的映射。
        product_id: 产品 ID。
        version: 版本号。

    Returns:
        公开附件元数据列表，不包含 GitHub 下载 URL。
    """
    raw_assets = release.get("assets", []) if isinstance(release, Mapping) else getattr(release, "assets", ())
    return [public_asset_metadata(item.to_dict() if hasattr(item, "to_dict") else item, product_id=product_id, version=version) for item in raw_assets]


__all__ = [
    "AssetConflictError",
    "AssetUploader",
    "InMemoryR2Client",
    "ObjectStore",
    "StorageConfig",
    "asset_key",
    "public_asset_metadata",
    "public_release_assets",
    "validate_storage_config",
]
