"""Release 附件及手动资源的对象存储同步工具。

模块只依赖一个很小的 S3/R2 客户端抽象，生产环境可接入 boto3，测试环境使用
``InMemoryR2Client``，因此不会在单元测试中访问网络。
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import posixpath
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Protocol
from urllib.parse import quote

CHUNK_SIZE = 1024 * 1024
APPLICATION_HISTORY_PREFIX = ".release-portal-history"


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

    def __init__(self, *, native_versioning: bool = True) -> None:
        """初始化空 bucket、对象版本和调用记录。"""
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict[str, Any]] = {}
        self.versions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.put_calls: list[tuple[str, str]] = []
        self.versioning: dict[str, bool] = {}
        self.supports_bucket_versioning = native_versioning
        self.supports_object_versioning = native_versioning

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

    def delete_object(self, bucket: str, key: str, **kwargs: Any) -> None:
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

    def list_objects_v2(self, bucket: str, prefix: str) -> list[str]:
        """列出匹配前缀的当前对象 key。

        Args:
            bucket: bucket 名称。
            prefix: 对象 key 前缀。

        Returns:
            按 key 排序的对象 key 列表。
        """
        return sorted(key for current_bucket, key in self.objects if current_bucket == bucket and key.startswith(prefix))

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


class Boto3R2Client:
    """基于 boto3 的 Cloudflare R2 对象存储适配器。"""

    def __init__(self, *, access_key_id: str, secret_access_key: str, account_id: str, endpoint_url: str | None = None, client: Any | None = None) -> None:
        """初始化 boto3 R2 客户端，不在初始化阶段执行网络请求。

        Args:
            access_key_id: R2 Access Key ID。
            secret_access_key: R2 Secret Access Key。
            account_id: Cloudflare Account ID。
            endpoint_url: 可选 R2 endpoint；缺省按 account_id 生成。
            client: 测试用 boto 风格客户端。

        Returns:
            无返回值。
        """
        if client is not None:
            self.client = client
            self.supports_bucket_versioning = False
            self.supports_object_versioning = False
            return
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("使用 R2 需要安装 boto3") from exc
        endpoint = endpoint_url or f"https://{account_id}.r2.cloudflarestorage.com"
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key)
        self.supports_bucket_versioning = False
        self.supports_object_versioning = False

    def head_object(self, bucket: str, key: str) -> Mapping[str, Any]:
        """读取对象元数据。"""
        return self.client.head_object(Bucket=bucket, Key=key)

    def put_object(self, bucket: str, key: str, body: BinaryIO, **kwargs: Any) -> Any:
        """使用 boto3 关键字参数写入对象。"""
        params: dict[str, Any] = {"Bucket": bucket, "Key": key, "Body": body}
        aliases = {
            "content_type": "ContentType",
            "content_disposition": "ContentDisposition",
            "cache_control": "CacheControl",
            "metadata": "Metadata",
        }
        for source, target in aliases.items():
            value = kwargs.get(source, kwargs.get(target))
            if value is not None:
                params[target] = value
        return self.client.put_object(**params)

    def copy_object(self, bucket: str, source_key: str, destination_key: str) -> Any:
        """使用 boto3 关键字参数复制对象。"""
        return self.client.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": source_key}, Key=destination_key)

    def delete_object(self, bucket: str, key: str, **kwargs: Any) -> Any:
        """删除对象当前版本。"""
        params = {"Bucket": bucket, "Key": key}
        if kwargs.get("version_id"):
            params["VersionId"] = kwargs["version_id"]
        return self.client.delete_object(**params)

    def get_object(self, bucket: str, key: str) -> Any:
        """读取对象内容用于完整性校验和失败恢复。"""
        response = self.client.get_object(Bucket=bucket, Key=key)
        return response.get("Body") if isinstance(response, Mapping) else response

    def list_object_versions(self, bucket: str, key: str) -> list[Mapping[str, Any]]:
        """列出指定对象版本。"""
        response = self.client.list_object_versions(Bucket=bucket, Prefix=key)
        return [item for item in response.get("Versions", []) if item.get("Key") == key]

    def list_objects_v2(self, bucket: str, prefix: str) -> list[str]:
        """分页列出指定前缀的对象 key。

        Args:
            bucket: bucket 名称。
            prefix: 对象 key 前缀。

        Returns:
            按服务端返回顺序排列的对象 key 列表。
        """
        keys: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                params["ContinuationToken"] = token
            response = self.client.list_objects_v2(**params)
            keys.extend(str(item["Key"]) for item in response.get("Contents", []) if item.get("Key"))
            if not response.get("IsTruncated"):
                return keys
            token = response.get("NextContinuationToken")
            if not token:
                return keys

    def prune_versions(self, bucket: str, key: str, *, keep: int = 3) -> None:
        """删除对象较旧版本，仅保留最近 ``keep`` 个。

        Args:
            bucket: bucket 名称。
            key: 对象 key。
            keep: 保留版本数。

        Returns:
            无返回值。
        """
        versions = sorted(self.list_object_versions(bucket, key), key=lambda item: item.get("LastModified", ""), reverse=True)
        for item in versions[keep:]:
            version_id = item.get("VersionId")
            if version_id:
                self.delete_object(bucket, key, version_id=version_id)

    def get_bucket_versioning(self, bucket: str) -> Mapping[str, Any]:
        """读取 bucket 版本控制状态。"""
        return self.client.get_bucket_versioning(Bucket=bucket)

    def put_bucket_versioning(self, bucket: str, status: str = "Enabled") -> Any:
        """开启 bucket 版本控制。"""
        return self.client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": status})


class FilesystemR2Client:
    """本地文件系统对象存储，用于 CLI 离线持久化。"""

    def __init__(self, root: str | Path) -> None:
        """初始化本地对象目录。

        Args:
            root: 对象根目录。

        Returns:
            无返回值。
        """
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.supports_bucket_versioning = True
        self.supports_object_versioning = False

    def _path(self, bucket: str, key: str) -> Path:
        """解析并校验 bucket/key 在本地根目录内。"""
        target = (self.root / _safe_segment(bucket, "bucket") / key).resolve()
        if self.root not in target.parents:
            raise ValueError("对象 key 超出本地存储根目录")
        return target

    def head_object(self, bucket: str, key: str) -> Mapping[str, Any] | None:
        """读取本地对象元数据。"""
        target = self._path(bucket, key)
        if not target.is_file():
            return None
        metadata_path = target.with_name(target.name + ".metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        metadata["ContentLength"] = target.stat().st_size
        return metadata

    def put_object(self, bucket: str, key: str, body: BinaryIO | bytes, **kwargs: Any) -> Any:
        """写入本地对象及元数据。"""
        target = self._path(bucket, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as output:
                if hasattr(body, "read"):
                    while chunk := body.read(CHUNK_SIZE):
                        output.write(chunk)
                else:
                    output.write(bytes(body))
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        metadata = dict(kwargs.get("metadata") or kwargs.get("Metadata") or {})
        metadata["ContentType"] = kwargs.get("content_type", kwargs.get("ContentType", metadata.get("ContentType")))
        metadata["ContentDisposition"] = kwargs.get("content_disposition", kwargs.get("ContentDisposition", metadata.get("ContentDisposition")))
        target.with_name(target.name + ".metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return {}

    def copy_object(self, bucket: str, source_key: str, destination_key: str) -> Any:
        """复制本地对象及其元数据。"""
        source = self._path(bucket, source_key)
        destination = self._path(bucket, destination_key)
        if not source.is_file():
            raise FileNotFoundError(source_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        source_meta = source.with_name(source.name + ".metadata.json")
        if source_meta.exists():
            shutil.copyfile(source_meta, destination.with_name(destination.name + ".metadata.json"))
        return {}

    def delete_object(self, bucket: str, key: str, **kwargs: Any) -> Any:
        """删除本地对象及元数据。"""
        target = self._path(bucket, key)
        target.unlink(missing_ok=True)
        target.with_name(target.name + ".metadata.json").unlink(missing_ok=True)
        return {}

    def get_object(self, bucket: str, key: str) -> BinaryIO:
        """读取本地对象内容。"""
        return self._path(bucket, key).open("rb")

    def list_objects_v2(self, bucket: str, prefix: str) -> list[str]:
        """列出本地存储中前缀匹配的对象 key。

        Args:
            bucket: bucket 名称。
            prefix: 对象 key 前缀。

        Returns:
            按 key 排序的对象 key 列表。
        """
        bucket_root = self.root / _safe_segment(bucket, "bucket")
        prefix_root = (bucket_root / prefix).resolve()
        if not prefix_root.exists():
            return []
        return sorted(
            path.relative_to(bucket_root).as_posix()
            for path in prefix_root.rglob("*")
            if path.is_file() and not path.name.endswith(".metadata.json")
        )

    def get_bucket_versioning(self, bucket: str) -> Mapping[str, Any]:
        """返回本地离线存储视作已启用版本控制。"""
        return {"Status": "Enabled"}

    def put_bucket_versioning(self, bucket: str, status: str = "Enabled") -> None:
        """记录本地离线存储版本控制设置。"""

    def prune_versions(self, bucket: str, key: str, *, keep: int = 3) -> None:
        """本地存储不保留历史版本，接口保持兼容。"""


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
    native_versioning = getattr(client, "supports_bucket_versioning", True)
    put_versioning = getattr(client, "put_bucket_versioning", None)
    if native_versioning and callable(put_versioning):
        put_versioning(bucket, status="Enabled")
    status_getter = getattr(client, "get_bucket_versioning", None)
    if native_versioning and callable(status_getter):
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
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text or '"' in text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{field} 包含非法路径片段")
    return text


def _safe_public_label(value: Any, field: str) -> str:
    """校验公开平台和架构标签，防止控制字符及 URL 注入。

    Args:
        value: 标签值。
        field: 字段名称。

    Returns:
        清理后的标签字符串。

    Raises:
        ValueError: 标签为空、包含控制字符或 URL。
    """
    text = str(value or "").strip()
    if not text or "://" in text or "/" in text or "\\" in text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{field} 包含非法公开内容")
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


def _stream_digest(path: Path, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
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


def _is_not_found_error(error: Exception) -> bool:
    """判断异常是否代表对象不存在。

    Args:
        error: 客户端异常。

    Returns:
        对象不存在时返回 ``True``。
    """
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        code = str((response.get("Error") or {}).get("Code", ""))
        return code.casefold() in {"404", "nosuchkey", "notfound", "nosuchobject"}
    return str(getattr(error, "code", "")).casefold() in {"404", "nosuchkey", "notfound"}


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
        resolved_platform = _safe_public_label(platform or "unknown", "platform")
        resolved_architecture = _safe_public_label(architecture or "unknown", "architecture")
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
        rollback_key: str | None = None
        history_key: str | None = None
        metadata = {"sha256": sha256, "platform": resolved_platform, "architecture": resolved_architecture}
        formal_written = False
        try:
            if existing is not None:
                if self._uses_application_history():
                    history_key = self._new_history_key(key)
                    rollback_key = history_key
                    self._copy(key, history_key)
                elif previous is None:
                    rollback_key = f"{key}.rollback-{uuid.uuid4().hex}"
                    self._copy(key, rollback_key)
            with path.open("rb") as stream:
                self._put(temp_key, stream, resolved_type, disposition, metadata)
            uploaded = self._head(temp_key)
            uploaded_digest = self._object_digest(temp_key)
            if uploaded_digest is not None and uploaded_digest != (sha256, size):
                raise RuntimeError("临时对象 SHA-256 校验失败")
            if uploaded is None or str(_metadata_value(uploaded, "sha256") or "") != sha256:
                raise RuntimeError("临时对象 SHA-256 校验失败")
            if int(_metadata_value(uploaded, "ContentLength") or -1) != size:
                raise RuntimeError("临时对象大小校验失败")
            self._copy(temp_key, key)
            formal_written = True
            final = self._head(key)
            final_digest = self._object_digest(key)
            if final_digest is not None and final_digest != (sha256, size):
                raise RuntimeError("正式对象 SHA-256 校验失败")
            if final is None or str(_metadata_value(final, "sha256") or "") != sha256:
                raise RuntimeError("正式对象 SHA-256 校验失败")
        except Exception as exc:
            cleanup_errors: list[Exception] = []
            temporary_error = self._delete(temp_key)
            if temporary_error is not None:
                cleanup_errors.append(temporary_error)
            if previous is not None:
                restore_error = self._restore(key, previous)
                if restore_error is not None:
                    cleanup_errors.append(restore_error)
            elif rollback_key is not None:
                try:
                    self._copy(rollback_key, key)
                except Exception as restore_error:
                    cleanup_errors.append(restore_error)
            elif formal_written or existing is None:
                deletion_error = self._delete(key)
                if deletion_error is not None:
                    cleanup_errors.append(deletion_error)
            if rollback_key is not None:
                backup_error = self._delete(rollback_key)
                if backup_error is not None:
                    cleanup_errors.append(backup_error)
            try:
                self._prune_retention(key)
            except Exception as prune_error:
                cleanup_errors.append(prune_error)
            if cleanup_errors and hasattr(exc, "add_note"):
                exc.add_note("附件回滚清理未完全完成")
            raise
        else:
            cleanup_errors: list[Exception] = []
            temporary_error = self._delete(temp_key)
            if temporary_error is not None:
                cleanup_errors.append(temporary_error)
            if rollback_key is not None and history_key is None:
                backup_error = self._delete(rollback_key)
                if backup_error is not None:
                    cleanup_errors.append(backup_error)
            try:
                self._prune_retention(key)
            except Exception as prune_error:
                cleanup_errors.append(prune_error)
            if cleanup_errors:
                raise RuntimeError("附件上传后清理失败") from cleanup_errors[0]
        return self._public_result(key, name, size, sha256, resolved_platform, resolved_architecture)

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
            try:
                return self.client.head_object(Bucket=self.bucket, Key=key)
            except TypeError:
                return self.client.head_object(self.bucket, key)
        except (KeyError, FileNotFoundError):
            return None
        except Exception as exc:
            if _is_not_found_error(exc):
                return None
            raise

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
            return self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type, ContentDisposition=disposition, Metadata=dict(metadata))

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

    def _delete(self, key: str) -> Exception | None:
        """尽力删除临时对象，不覆盖原始异常。

        Args:
            key: 待删除对象 key。

        Returns:
            删除失败时返回异常，否则返回 ``None``。
        """
        try:
            try:
                self.client.delete_object(self.bucket, key)
            except TypeError:
                self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            return exc
        return None

    def _read_object(self, key: str) -> bytes | None:
        """读取对象内容用于端到端 SHA 校验。

        Args:
            key: 对象 key。

        Returns:
            对象字节串；客户端不支持读取时返回 ``None``。
        """
        getter = getattr(self.client, "get_object", None)
        if not callable(getter):
            return None
        try:
            try:
                result = getter(Bucket=self.bucket, Key=key)
            except TypeError:
                result = getter(self.bucket, key)
        except (KeyError, FileNotFoundError):
            return None
        except Exception as exc:
            if _is_not_found_error(exc):
                return None
            raise
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        body = result.get("Body") if isinstance(result, Mapping) else None
        if hasattr(body, "read"):
            return body.read()
        return bytes(body) if isinstance(body, (bytes, bytearray)) else None

    def _object_digest(self, key: str) -> tuple[str, int] | None:
        """以固定块读取对象并计算 SHA-256 与大小。

        Args:
            key: 对象 key。

        Returns:
            ``(sha256, size)``；客户端不支持读取时返回 ``None``。
        """
        getter = getattr(self.client, "get_object", None)
        if not callable(getter):
            return None
        try:
            try:
                result = getter(Bucket=self.bucket, Key=key)
            except TypeError:
                result = getter(self.bucket, key)
        except (KeyError, FileNotFoundError):
            return None
        except Exception as exc:
            if _is_not_found_error(exc):
                return None
            raise
        body = result.get("Body") if isinstance(result, Mapping) else result
        digest = hashlib.sha256()
        size = 0
        try:
            if isinstance(body, (bytes, bytearray)):
                for offset in range(0, len(body), CHUNK_SIZE):
                    chunk = body[offset:offset + CHUNK_SIZE]
                    digest.update(chunk)
                    size += len(chunk)
            elif hasattr(body, "read"):
                while chunk := body.read(CHUNK_SIZE):
                    digest.update(chunk)
                    size += len(chunk)
            else:
                return None
        finally:
            closer = getattr(body, "close", None)
            if callable(closer):
                closer()
        return digest.hexdigest(), size

    def _uses_application_history(self) -> bool:
        """判断客户端是否需要应用层版本保留。

        Returns:
            不支持原生对象版本时返回 ``True``。
        """
        return getattr(self.client, "supports_object_versioning", True) is False

    @staticmethod
    def _history_prefix(key: str) -> str:
        """返回不公开的应用层历史对象前缀。"""
        return f"{APPLICATION_HISTORY_PREFIX}/{key}/"

    def _new_history_key(self, key: str) -> str:
        """生成应用层历史对象 key。"""
        return f"{self._history_prefix(key)}{time.time_ns()}-{uuid.uuid4().hex}"

    def _list_keys(self, prefix: str) -> list[str]:
        """调用客户端抽象列出前缀下的对象 key。"""
        lister = getattr(self.client, "list_objects_v2", None)
        if not callable(lister):
            return []
        try:
            result = lister(self.bucket, prefix)
        except TypeError:
            result = lister(Bucket=self.bucket, Prefix=prefix)
        if isinstance(result, Mapping):
            return [str(item["Key"]) for item in result.get("Contents", []) if item.get("Key")]
        return [str(key) for key in result]

    def _prune_application_history(self, key: str) -> None:
        """保留当前对象之外最近 ``retain_versions - 1`` 个私有历史副本。"""
        keep_history = max(0, self.config.retain_versions - 1)
        history_keys = sorted(self._list_keys(self._history_prefix(key)), reverse=True)
        for obsolete_key in history_keys[keep_history:]:
            error = self._delete(obsolete_key)
            if error is not None:
                raise error

    def _prune_retention(self, key: str) -> None:
        """按客户端能力保留原生或应用层最近版本。"""
        if self._uses_application_history():
            self._prune_application_history(key)
            return
        self._prune_versions(key)

    def _prune_versions(self, key: str) -> None:
        """调用客户端抽象保留最近三个对象版本。

        Args:
            key: 对象 key。

        Returns:
            无返回值。
        """
        if getattr(self.client, "supports_object_versioning", True) is False:
            return
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

    def _restore(self, key: str, snapshot: tuple[bytes, dict[str, Any]] | None) -> Exception | None:
        """在正式复制后失败时恢复内存客户端的旧对象。"""
        if snapshot is None:
            return None
        objects = getattr(self.client, "objects", None)
        metadata = getattr(self.client, "metadata", None)
        if isinstance(objects, dict) and isinstance(metadata, dict):
            identity = (self.bucket, key)
            objects[identity], metadata[identity] = snapshot
            return None
        body, old_metadata = snapshot
        content_type = str(_metadata_value(old_metadata, "ContentType") or "application/octet-stream")
        disposition = str(_metadata_value(old_metadata, "ContentDisposition") or "attachment")
        custom = old_metadata.get("Metadata") if isinstance(old_metadata.get("Metadata"), Mapping) else old_metadata
        try:
            self._put(key, io.BytesIO(body), content_type, disposition, custom)
        except Exception as exc:
            deletion_error = self._delete(key)
            return deletion_error or exc
        return None


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
        "platform": _safe_public_label(asset.get("platform") or "unknown", "platform"),
        "architecture": _safe_public_label(asset.get("architecture") or "unknown", "architecture"),
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
    "Boto3R2Client",
    "FilesystemR2Client",
    "InMemoryR2Client",
    "ObjectStore",
    "StorageConfig",
    "asset_key",
    "public_asset_metadata",
    "public_release_assets",
    "validate_storage_config",
]
