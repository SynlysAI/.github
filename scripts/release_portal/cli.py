"""Release Portal 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from .assets import AssetUploader, InMemoryR2Client, StorageConfig

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_CANDIDATE_PATH = ROOT / "release-portal" / "candidates" / "manifest.json"


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
    return parser


def build_object_store(args: argparse.Namespace) -> InMemoryR2Client:
    """构造离线对象存储客户端。

    Args:
        args: CLI 参数命名空间。

    Returns:
        可替换的对象存储客户端；默认使用内存实现，不访问网络。
    """
    # 生产工作流通过依赖注入替换为 boto3/R2 客户端；CLI 默认离线可运行。
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
    client = object_store if object_store is not None else build_object_store(args)
    try:
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MANIFEST_CANDIDATE_PATH", "build_object_store", "main"]
