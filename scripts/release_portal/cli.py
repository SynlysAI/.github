"""Release Portal 命令行入口。"""

from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import requests

from .assets import AssetUploader, Boto3R2Client, FilesystemR2Client, InMemoryR2Client, StorageConfig
from .aggregate import (
    MAX_BACKFILL_BATCH,
    aggregate_commits,
    load_backfill_state,
    save_backfill_state,
    update_backfill_state,
)
from .ai import AIClient
from .classify import load_overrides as load_classification_overrides
from .config import load_catalog
from .github import GitHubClient
from .publish import (
    build_public_collections,
    load_candidate_events,
    load_overrides,
    write_publication_snapshot,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_CANDIDATE_PATH = ROOT / "release-portal" / "candidates" / "manifest.json"
CANDIDATE_TIMELINE_PATH = ROOT / "release-portal" / "candidates" / "timeline.json"
CANDIDATE_RELEASES_PATH = ROOT / "release-portal" / "candidates" / "releases.json"
BACKFILL_STATE_PATH = ROOT / "release-portal" / "state" / "backfill.json"
PUBLISHED_ROOT = ROOT / "release-portal" / "published"
PUBLIC_COLLECTION_FILENAMES = (
    "products.json",
    "releases.json",
    "timeline.json",
    "faqs.json",
    "meta.json",
    "manifest.json",
)
ALLOWED_RELEASE_ASSET_HOSTS = (
    "github.com",
    "api.github.com",
    "github-releases.githubusercontent.com",
    "objects.githubusercontent.com",
)


def _parser() -> argparse.ArgumentParser:
    """构造 Release Portal CLI 参数解析器。

    Returns:
        配置好的参数解析器。
    """
    parser = argparse.ArgumentParser(prog="release-portal")
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload-asset", help="上传手动资源并生成待审 manifest")
    upload.add_argument("--product", required=True, dest="product_id")
    upload.add_argument("--version", required=True)
    upload.add_argument("--channel", required=True)
    upload.add_argument("--platform", required=True)
    upload.add_argument("--architecture")
    upload.add_argument("--file", required=True, dest="file_path")
    upload.add_argument("--bucket", default=os.getenv("R2_BUCKET", "release-portal"))
    upload.add_argument("--store-root", default=os.getenv("R2_STORE_ROOT"))
    upload.add_argument("--content-type")
    upload.add_argument("--replace", action="store_true", help="显式允许覆盖同名不同内容")
    upload.add_argument("--manifest", default=None, help=argparse.SUPPRESS)

    sync = commands.add_parser("sync", help="同步正式 Release 元数据和附件")
    _add_product_argument(sync)
    _add_storage_arguments(sync)
    sync.add_argument("--candidates", default=str(CANDIDATE_RELEASES_PATH))

    backfill = commands.add_parser("backfill", help="回填并生成待审核 Commit 候选")
    _add_product_argument(backfill)
    backfill.add_argument("--limit", type=int, default=MAX_BACKFILL_BATCH)
    backfill.add_argument("--candidates", default=str(CANDIDATE_TIMELINE_PATH))
    backfill.add_argument("--state", default=str(BACKFILL_STATE_PATH))

    publish = commands.add_parser("publish", help="校验并按原子顺序发布公开数据")
    publish.add_argument("--candidates", default=str(CANDIDATE_TIMELINE_PATH))
    publish.add_argument("--releases", default=str(CANDIDATE_RELEASES_PATH))
    publish.add_argument("--state", default=str(BACKFILL_STATE_PATH))
    publish.add_argument("--output", default=str(PUBLISHED_ROOT))
    publish.add_argument("--prefix", default="portal/v1")
    publish.add_argument("--validate-only", action="store_true")
    _add_storage_arguments(publish)
    return parser


def _add_product_argument(parser: argparse.ArgumentParser) -> None:
    """为子命令添加产品选择参数。

    Args:
        parser: 需要添加参数的子命令解析器。

    Returns:
        无返回值。
    """
    parser.add_argument("--product", default="all", dest="product_id")


def _add_storage_arguments(parser: argparse.ArgumentParser) -> None:
    """为子命令添加对象存储相关参数。

    Args:
        parser: 需要添加参数的子命令解析器。

    Returns:
        无返回值。
    """
    parser.add_argument("--bucket", default=os.getenv("R2_BUCKET", "release-portal"))
    parser.add_argument("--store-root", default=os.getenv("R2_STORE_ROOT"))


def build_object_store(args: argparse.Namespace, *, require_remote: bool = False) -> Any:
    """构造离线对象存储客户端。

    Args:
        args: CLI 参数命名空间。
        require_remote: 是否要求已配置 R2；为真时禁止以内存实现替代。

    Returns:
        可替换的对象存储客户端；默认使用内存实现，不访问网络。
    """
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    configured = (access_key, secret_key, account_id)
    if any(configured) and not all(configured):
        raise ValueError("R2 配置不完整")
    if access_key and secret_key and account_id:
        return Boto3R2Client(access_key_id=access_key, secret_access_key=secret_key, account_id=account_id)
    if args.store_root:
        return FilesystemR2Client(args.store_root)
    if require_remote:
        raise ValueError("发布命令必须配置 R2 对象存储")
    # 无 R2 配置时离线运行，测试和开发环境不会访问网络。
    return InMemoryR2Client()


def _guess_architecture(file_path: str) -> str:
    """根据文件名推断 CPU 架构。"""
    lower = Path(file_path).name.casefold()
    if any(token in lower for token in ("arm64", "aarch64")):
        return "arm64"
    if any(token in lower for token in ("amd64", "x86_64", "x64")):
        return "x86_64"
    return "unknown"


def _append_manifest_candidate(path: str | Path, asset: dict[str, Any], *, product_id: str, version: str, channel: str) -> dict[str, Any]:
    """追加一条待审 manifest 变更并原子写入文件。

    Args:
        path: 候选 manifest 文件路径。
        asset: 已脱敏的公开附件元数据。
        product_id: 产品 ID。
        version: 版本号。
        channel: 资源渠道。

    Returns:
        写入后的候选 manifest 映射。
    """
    target = Path(path)
    if target.exists():
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("待审 manifest 不是有效 JSON") from exc
    else:
        value = {"schemaVersion": 1, "assets": []}
    if not isinstance(value, dict) or not isinstance(value.get("assets", []), list):
        raise ValueError("待审 manifest 格式无效")
    value.setdefault("assets", [])
    entry = dict(asset)
    entry.update({"productId": product_id, "version": version, "channel": channel})
    value.setdefault("schemaVersion", 1)
    if not any(
        isinstance(item, dict)
        and item.get("downloadPath") == entry.get("downloadPath")
        and item.get("sha256") == entry.get("sha256")
        and item.get("channel") == channel
        for item in value["assets"]
    ):
        value["assets"].append(entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return value


def _log(*, run_id: str, product_id: str, stage: str, count: int, duration_ms: int, status: str, error: str | None = None) -> None:
    """输出不包含密钥和私有正文的单行 JSON 日志。"""
    payload: dict[str, Any] = {"runId": run_id, "productId": product_id, "stage": stage, "count": count, "durationMs": duration_ms, "status": status}
    if error:
        payload["error"] = error[:200]
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _upload_asset(args: argparse.Namespace, *, object_store: Any | None = None) -> int:
    """执行 upload-asset 命令。

    Args:
        args: CLI 参数命名空间。
        object_store: 可选注入的对象存储客户端。

    Returns:
        成功返回 0，失败返回 1。
    """
    run_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        client = object_store if object_store is not None else build_object_store(args)
        uploader = AssetUploader(client, args.bucket, config=StorageConfig())
        result = uploader.upload_release_asset(
            args.file_path,
            product_id=args.product_id,
            version=args.version,
            platform=args.platform,
            architecture=args.architecture or _guess_architecture(args.file_path),
            replace=args.replace,
            content_type=args.content_type,
        )
        candidate_path = args.manifest or MANIFEST_CANDIDATE_PATH
        _append_manifest_candidate(candidate_path, result, product_id=args.product_id, version=args.version, channel=args.channel)
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        _log(run_id=run_id, product_id=args.product_id, stage="upload-asset", count=0, duration_ms=elapsed, status="failed", error=type(exc).__name__)
        return 1
    elapsed = int((time.perf_counter() - started) * 1000)
    _log(run_id=run_id, product_id=args.product_id, stage="upload-asset", count=1, duration_ms=elapsed, status="success")
    return 0


def _atomic_write_json(path: str | Path, value: Any) -> None:
    """以原子替换方式写入 UTF-8 JSON 文件。

    Args:
        path: 目标 JSON 文件路径。
        value: 待序列化的 JSON 兼容值。

    Returns:
        无返回值。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _load_collection(path: str | Path, key: str) -> dict[str, Any]:
    """读取候选集合，缺失时返回空的版本化集合。

    Args:
        path: 候选集合路径。
        key: 集合记录列表字段名。

    Returns:
        可安全修改的集合映射。

    Raises:
        ValueError: 文件存在但不是要求的 JSON 对象或列表字段。
    """
    target = Path(path)
    if not target.exists():
        return {"schemaVersion": 1, key: []}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("候选集合不是有效 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        raise ValueError("候选集合格式无效")
    return {"schemaVersion": 1, key: [dict(item) for item in value[key] if isinstance(item, dict)]}


def _selected_products(product_id: str) -> tuple[Any, ...]:
    """根据 CLI 选择返回 catalog 中的产品。

    Args:
        product_id: 单个产品 ID 或 ``all``。

    Returns:
        待处理产品元组。

    Raises:
        ValueError: 产品不在 catalog allowlist 中。
    """
    catalog = load_catalog()
    if product_id == "all":
        return catalog.products
    selected = tuple(item for item in catalog.products if item.product_id == product_id)
    if not selected:
        raise ValueError("产品不在 catalog allowlist 中")
    return selected


def _is_allowed_release_asset_url(value: str) -> bool:
    """判断 Release 附件下载地址是否属于 GitHub 受信任域名。

    Args:
        value: 待校验的下载地址。

    Returns:
        地址可用于下载时返回 ``True``。
    """
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and (
        host in ALLOWED_RELEASE_ASSET_HOSTS or host.endswith(".githubusercontent.com")
    )


def _download_release_asset(url: str, name: str, token: str | None) -> Path:
    """将受信任 GitHub Release 附件流式下载到临时文件。

    Args:
        url: GitHub 提供的附件下载地址。
        name: 原始附件文件名，仅用于临时文件后缀。
        token: GitHub App 安装令牌；不会写入日志或磁盘。

    Returns:
        已下载临时文件路径，调用方负责删除。

    Raises:
        ValueError: 下载地址不属于允许的 GitHub 域名。
        requests.RequestException: 请求或响应状态失败。
    """
    if not _is_allowed_release_asset_url(url):
        raise ValueError("Release 附件下载地址不受信任")
    safe_name = Path(name).name
    if not safe_name or safe_name != name:
        raise ValueError("Release 附件文件名无效")
    headers = {"Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(url, headers=headers, stream=True, timeout=(10, 120))
    try:
        response.raise_for_status()
        if not _is_allowed_release_asset_url(str(response.url)):
            raise ValueError("Release 附件重定向地址不受信任")
        directory = Path(tempfile.mkdtemp(prefix="release-portal-"))
        target = directory / safe_name
        try:
            with target.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        stream.write(chunk)
            return target
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    finally:
        response.close()


def _release_record(product: Any, release: Any, assets: list[dict[str, Any]]) -> dict[str, Any]:
    """将已同步附件的正式 Release 转换为候选集合记录。

    Args:
        product: catalog 中的产品对象。
        release: GitHub Release 归一化对象。
        assets: 已上传到 R2 的公开附件元数据。

    Returns:
        符合 releases.json Schema 的候选记录。
    """
    version = str(getattr(release, "tag", None) or getattr(release, "id", ""))
    return {
        "id": f"{product.product_id}:{release.id}",
        "productId": product.product_id,
        "version": version or None,
        "name": str(getattr(release, "name", "")),
        "body": str(getattr(release, "body", "")),
        "publishedAt": getattr(release, "published_at", None),
        "releaseUrl": getattr(release, "release_url", None),
        "prerelease": False,
        "draft": False,
        "assets": assets,
    }


def _sync(args: argparse.Namespace, *, object_store: Any | None = None, github_client: Any | None = None) -> int:
    """同步正式 GitHub Release 及其附件到 R2 候选集合。

    Args:
        args: CLI 参数命名空间。
        object_store: 可选注入对象存储客户端。
        github_client: 可选注入 GitHub 客户端。

    Returns:
        成功返回 0，失败返回 1。
    """
    run_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        products = _selected_products(args.product_id)
        client = github_client if github_client is not None else GitHubClient(os.getenv("GITHUB_TOKEN"))
        store = object_store if object_store is not None else build_object_store(args, require_remote=True)
        uploader = AssetUploader(store, args.bucket, config=StorageConfig())
        candidates = _load_collection(args.candidates, "releases")
        selected_ids = {product.product_id for product in products}
        records = [item for item in candidates["releases"] if item.get("productId") not in selected_ids]
        synced = 0
        for product in products:
            for release in client.list_releases(product.repository):
                if release.draft or release.prerelease:
                    continue
                version = str(release.tag or release.id)
                if not version:
                    raise ValueError("正式 Release 缺少版本标识")
                public_assets: list[dict[str, Any]] = []
                for asset in release.assets:
                    if not asset.download_url:
                        raise ValueError("Release 附件缺少下载地址")
                    temporary = _download_release_asset(
                        asset.download_url,
                        asset.name,
                        os.getenv("GITHUB_TOKEN"),
                    )
                    try:
                        public_assets.append(
                            uploader.upload_release_asset(
                                temporary,
                                product_id=product.product_id,
                                version=version,
                                platform=asset.platform,
                                architecture=asset.architecture,
                                content_type=asset.content_type,
                            )
                        )
                    finally:
                        temporary.unlink(missing_ok=True)
                        try:
                            temporary.parent.rmdir()
                        except OSError:
                            pass
                records.append(_release_record(product, release, public_assets))
                synced += 1
        records.sort(key=lambda item: (str(item.get("publishedAt") or ""), str(item.get("id") or "")), reverse=True)
        _atomic_write_json(args.candidates, {"schemaVersion": 1, "releases": records})
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        _log(run_id=run_id, product_id=args.product_id, stage="sync", count=0, duration_ms=elapsed, status="failed", error=type(exc).__name__)
        return 1
    elapsed = int((time.perf_counter() - started) * 1000)
    _log(run_id=run_id, product_id=args.product_id, stage="sync", count=synced, duration_ms=elapsed, status="success")
    return 0


def _merge_candidate_events(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按候选事件 ID 合并新旧事件并保留已聚合的短 SHA。

    Args:
        existing: 现有候选事件列表。
        additions: 当前回填批次产生的事件列表。

    Returns:
        按事件 ID 稳定排序的合并结果。
    """
    merged = {str(item.get("id") or ""): dict(item) for item in existing if item.get("id")}
    for item in additions:
        event = dict(item)
        event_id = str(event.get("id") or "")
        old = merged.get(event_id)
        if old:
            old_shas = list((old.get("source") or {}).get("commitShas") or [])
            new_source = dict(event.get("source") or {})
            new_source["commitShas"] = list(dict.fromkeys(old_shas + list(new_source.get("commitShas") or [])))
            event["source"] = new_source
            event["pinned"] = bool(event.get("pinned") or old.get("pinned"))
        merged[event_id] = event
    return [merged[event_id] for event_id in sorted(merged)]


def _enrich_events(events: list[dict[str, Any]], product: Any, commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """使用已配置 AI 为候选补全双语文案，失败时保留确定性候选。

    Args:
        events: 本批确定性候选事件。
        product: 当前 catalog 产品。
        commits: 本批脱敏前的 GitHub 提交映射。

    Returns:
        可供人工审核的候选事件列表。
    """
    if not os.getenv("AI_API_KEY"):
        return events
    client = AIClient.from_environment()
    by_sha = {str(item.get("sha") or "")[:7]: item for item in commits}
    enriched: list[dict[str, Any]] = []
    for event in events:
        sources = [by_sha[sha] for sha in (event.get("source") or {}).get("commitShas", []) if sha in by_sha]
        messages = [item.get("message") for item in sources]
        pull_requests = [pr for item in sources for pr in item.get("pull_requests", [])]
        enriched.append(
            client.enrich_candidate(
                event,
                product_name=str(product.name.get("en") or product.product_id),
                commit_messages=messages,
                pull_requests=pull_requests,
                repository_private=True,
            )
        )
    return enriched


def _backfill(args: argparse.Namespace, *, github_client: Any | None = None) -> int:
    """按仓库最多 500 条提交回填候选事件和检查点。

    Args:
        args: CLI 参数命名空间。
        github_client: 可选注入 GitHub 客户端。

    Returns:
        成功返回 0，失败返回 1。
    """
    run_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        limit = min(max(1, int(args.limit)), MAX_BACKFILL_BATCH)
        products = _selected_products(args.product_id)
        client = github_client if github_client is not None else GitHubClient(os.getenv("GITHUB_TOKEN"))
        state = load_backfill_state(args.state)
        candidate = _load_collection(args.candidates, "events")
        overrides = load_classification_overrides(ROOT / "release-portal" / "overrides.yml")
        additions: list[dict[str, Any]] = []
        processed = 0
        for product in products:
            repository_state = state.setdefault("repositories", {}).setdefault(product.repository, {})
            known = {str(sha) for sha in repository_state.get("processedShas", [])}
            all_commits: list[dict[str, Any]] = []
            for commit in client.list_commits(product.repository):
                value = commit.to_dict() if hasattr(commit, "to_dict") else dict(commit)
                value["repository"] = product.repository
                all_commits.append(value)
            batch = [item for item in all_commits if str(item.get("sha") or "") not in known][:limit]
            completed = len([item for item in all_commits if str(item.get("sha") or "") not in known]) <= len(batch)
            update_backfill_state(state, product.repository, batch, completed=completed, max_batch=limit)
            events = aggregate_commits(
                product.product_id,
                batch,
                overrides=overrides,
                repository=product.repository,
            )
            additions.extend(_enrich_events(events, product, batch))
            processed += len(batch)
        candidate["events"] = _merge_candidate_events(candidate["events"], additions)
        _atomic_write_json(args.candidates, candidate)
        save_backfill_state(state, args.state)
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        _log(run_id=run_id, product_id=args.product_id, stage="backfill", count=0, duration_ms=elapsed, status="failed", error=type(exc).__name__)
        return 1
    elapsed = int((time.perf_counter() - started) * 1000)
    _log(run_id=run_id, product_id=args.product_id, stage="backfill", count=processed, duration_ms=elapsed, status="success")
    return 0


def _source_watermarks(path: str | Path) -> dict[str, Any]:
    """从回填状态读取公开 meta 所需的仓库水位。

    Args:
        path: backfill.json 文件路径。

    Returns:
        仅包含仓库水位的映射。
    """
    state = load_backfill_state(path)
    repositories = state.get("repositories") if isinstance(state, dict) else {}
    if not isinstance(repositories, dict):
        raise ValueError("回填状态格式无效")
    return {
        str(repository): dict(value.get("watermark") or {})
        for repository, value in repositories.items()
        if isinstance(value, dict)
    }


def _put_public_object(client: Any, bucket: str, key: str, body: bytes) -> None:
    """上传公开 JSON 对象并兼容 boto3 与本地对象存储接口。

    Args:
        client: 对象存储客户端。
        bucket: 目标 bucket。
        key: 对象 key。
        body: UTF-8 JSON 字节。

    Returns:
        无返回值。
    """
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            CacheControl="public, max-age=300",
        )
    except TypeError:
        client.put_object(
            bucket,
            key,
            BytesIO(body),
            ContentType="application/json; charset=utf-8",
            CacheControl="public, max-age=300",
        )


def _safe_public_prefix(value: str) -> str:
    """校验公开数据 R2 前缀不含路径穿越片段。

    Args:
        value: 用户传入的 R2 前缀。

    Returns:
        去除首尾斜杠后的安全前缀。

    Raises:
        ValueError: 前缀为空或包含无效路径片段。
    """
    prefix = value.strip("/")
    parts = prefix.split("/")
    if not prefix or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("公开数据前缀无效")
    return prefix


def _publish(args: argparse.Namespace, *, object_store: Any | None = None) -> int:
    """校验候选、写入快照，并按 manifest 最后规则上传到 R2。

    Args:
        args: CLI 参数命名空间。
        object_store: 可选注入对象存储客户端。

    Returns:
        成功返回 0，失败返回 1。
    """
    run_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        events = load_candidate_events(args.candidates)
        releases = _load_collection(args.releases, "releases")
        collections = build_public_collections(
            events,
            overrides=load_overrides(ROOT / "release-portal" / "overrides.yml"),
            releases=releases,
            watermarks=_source_watermarks(args.state),
        )
        if args.validate_only:
            count = 0
        else:
            write_publication_snapshot(collections, args.output)
            client = object_store if object_store is not None else build_object_store(args, require_remote=True)
            prefix = _safe_public_prefix(args.prefix)
            output = Path(args.output)
            for filename in PUBLIC_COLLECTION_FILENAMES:
                _put_public_object(client, args.bucket, f"{prefix}/{filename}", (output / filename).read_bytes())
            count = len(PUBLIC_COLLECTION_FILENAMES)
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        _log(run_id=run_id, product_id="all", stage="publish", count=0, duration_ms=elapsed, status="failed", error=type(exc).__name__)
        return 1
    elapsed = int((time.perf_counter() - started) * 1000)
    _log(run_id=run_id, product_id="all", stage="publish", count=count, duration_ms=elapsed, status="success")
    return 0


def main(argv: Sequence[str] | None = None, *, object_store: Any | None = None) -> int:
    """解析参数并执行 Release Portal 命令。

    Args:
        argv: 命令行参数；缺省读取 ``sys.argv``。
        object_store: 测试或调用方注入的对象存储客户端。

    Returns:
        命令退出码。
    """
    args = _parser().parse_args(argv)
    if args.command == "upload-asset":
        return _upload_asset(args, object_store=object_store)
    if args.command == "sync":
        return _sync(args, object_store=object_store)
    if args.command == "backfill":
        return _backfill(args)
    if args.command == "publish":
        return _publish(args, object_store=object_store)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MANIFEST_CANDIDATE_PATH", "build_object_store", "main"]
