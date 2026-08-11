"""Release Portal 候选审核、公开脱敏和快照发布工具。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlparse

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from .config import EXPECTED_PRODUCTS, load_catalog

ROOT = Path(__file__).resolve().parents[2]
PORTAL_ROOT = ROOT / "release-portal"
SCHEMA_ROOT = PORTAL_ROOT / "schemas"
PUBLISHED_ROOT = PORTAL_ROOT / "published"
COLLECTION_FILES = {
    "products": "products.json",
    "releases": "releases.json",
    "timeline": "timeline.json",
    "faqs": "faqs.json",
    "meta": "meta.json",
}
PUBLIC_EVENT_FIELDS = {
    "id", "productId", "level", "occurredAt", "version", "changeType",
    "module", "title", "summary", "detailsMarkdown", "source", "pinned",
}
PUBLIC_SOURCE_FIELDS = {"repository", "commitShas", "releaseUrl"}
CHANGE_TYPES = {"feature", "algorithm", "performance", "bugfix", "architecture"}
OVERRIDE_OPERATIONS = {"hide", "pin", "replaceText", "changeType", "mergeInto"}
SENSITIVE_PATTERNS = (
    re.compile(r"(?:ghp_|github_pat_|gho_)[A-Za-z0-9_\-]+", re.IGNORECASE),
    re.compile(r"(?:sk-|sk_live_)[A-Za-z0-9_\-]+", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:authorization\s*:\s*bearer|api[_-]?key\s*[:=])", re.IGNORECASE),
    re.compile(r"https?://github\.com/[^\s/]+/[^\s/]+/(?:pull|issues)/\d+", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE),
)


def _canonical_bytes(value: Any) -> bytes:
    """将 JSON 值编码为稳定的 UTF-8 字节序列。

    Args:
        value: 待编码的 JSON 兼容值。

    Returns:
        排序键且无多余空白的 UTF-8 字节序列。
    """
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256(value: Any) -> str:
    """计算 JSON 值的 SHA-256。

    Args:
        value: 待哈希的 JSON 兼容值。

    Returns:
        小写十六进制 SHA-256 摘要。
    """
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _target_id(rule: Mapping[str, Any]) -> str:
    """取得覆盖规则的事件标识。

    Args:
        rule: 覆盖规则映射。

    Returns:
        事件 ID、SHA 或 commitSha 字符串。
    """
    for key in ("id", "eventId", "sha", "commitSha"):
        if rule.get(key):
            return str(rule[key])
    return ""


def validate_overrides(overrides: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """校验覆盖操作及同一事件的冲突。

    Args:
        overrides: 覆盖规则迭代器。

    Returns:
        去除非映射项后的规则副本。

    Raises:
        ValueError: 规则缺少目标、包含未知操作、操作冲突或值非法。
    """
    normalized: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    merge_graph: dict[str, str] = {}
    for index, raw in enumerate(overrides):
        if not isinstance(raw, Mapping):
            raise ValueError(f"覆盖规则[{index}]必须是映射")
        rule = dict(raw)
        operation = rule.pop("operation", None)
        if operation:
            if operation not in OVERRIDE_OPERATIONS:
                raise ValueError(f"覆盖规则[{index}]包含未知操作: {operation}")
            rule[operation] = rule.get("value", True)
            rule.pop("value", None)
        target = _target_id(rule)
        if not target:
            raise ValueError(f"覆盖规则[{index}]缺少事件 ID")
        operations = {key for key in OVERRIDE_OPERATIONS if key in rule and rule[key] is not None}
        unknown = set(rule) - (set(("id", "eventId", "sha", "commitSha", "commitShas", "reason", "restore", "show", "module")) | OVERRIDE_OPERATIONS)
        if unknown:
            raise ValueError(f"覆盖规则[{target}]包含未知字段: {', '.join(sorted(unknown))}")
        if not operations:
            raise ValueError(f"覆盖规则[{target}]未指定操作")
        if "hide" in operations and rule["hide"] is True and len(operations) > 1:
            raise ValueError(f"覆盖规则[{target}]存在冲突操作")
        if "changeType" in operations and rule["changeType"] not in CHANGE_TYPES:
            raise ValueError(f"覆盖规则[{target}]包含非法 changeType")
        if "mergeInto" in operations and str(rule["mergeInto"]) == target:
            raise ValueError(f"覆盖规则[{target}]不能合并到自身")
        if "mergeInto" in operations:
            merge_graph[target] = str(rule["mergeInto"])
            cursor = target
            visited: set[str] = set()
            while cursor in merge_graph:
                if cursor in visited:
                    raise ValueError(f"覆盖规则[{target}]存在合并冲突")
                visited.add(cursor)
                cursor = merge_graph[cursor]
        if "replaceText" in operations and not isinstance(rule["replaceText"], (Mapping, str)):
            raise ValueError(f"覆盖规则[{target}]的 replaceText 必须是映射或字符串")
        previous = seen.get(target)
        if previous is not None and previous != rule:
            raise ValueError(f"覆盖规则[{target}]存在冲突")
        seen[target] = rule
        normalized.append(rule)
    return normalized


def _find_event(events: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    """按事件 ID 或来源 SHA 查找事件。

    Args:
        events: 事件列表。
        target: 事件 ID 或提交 SHA。

    Returns:
        匹配的事件，未找到时返回 ``None``。
    """
    for event in events:
        if str(event.get("id", "")) == target:
            return event
        source = event.get("source") or {}
        shas = source.get("commitShas") or []
        if target in {str(sha) for sha in shas}:
            return event
    return None


def _replace_text(event: dict[str, Any], replacement: Any) -> None:
    """将覆盖文本写入事件的双语字段。

    Args:
        event: 待修改事件。
        replacement: 字段映射或同时替换 title/summary 的字符串。

    Returns:
        无返回值。
    """
    if isinstance(replacement, str):
        event["summary"] = {"zh": replacement, "en": replacement}
        return
    for field in ("title", "summary", "detailsMarkdown"):
        if field in replacement:
            value = replacement[field]
            if isinstance(value, Mapping):
                current = dict(event.get(field) or {})
                current.update({"zh": str(value.get("zh", current.get("zh", ""))), "en": str(value.get("en", current.get("en", "")))})
                event[field] = current
            else:
                event[field] = {"zh": str(value), "en": str(value)}


def apply_overrides(events: Iterable[Mapping[str, Any]], overrides: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按顺序应用隐藏、置顶、改写、改类和合并操作。

    Args:
        events: 候选事件迭代器。
        overrides: 已配置的覆盖规则迭代器。

    Returns:
        应用规则后的新事件列表。

    Raises:
        ValueError: 覆盖规则冲突或合并目标不存在。
    """
    result = [copy.deepcopy(dict(event)) for event in events]
    rules = validate_overrides(overrides)
    for rule in rules:
        target = _target_id(rule)
        event = _find_event(result, target)
        if event is None:
            raise ValueError(f"覆盖目标不存在: {target}")
        if rule.get("hide") is True:
            result.remove(event)
            continue
        if rule.get("pin") is True:
            event["pinned"] = True
        if rule.get("replaceText") is not None:
            _replace_text(event, rule["replaceText"])
        if rule.get("changeType") is not None:
            event["changeType"] = rule["changeType"]
        if rule.get("mergeInto") is not None:
            destination = _find_event(result, str(rule["mergeInto"]))
            if destination is None or destination is event:
                raise ValueError(f"合并目标不存在或无效: {rule['mergeInto']}")
            source = event.get("source") or {}
            target_source = destination.setdefault("source", {})
            target_shas = list(target_source.get("commitShas") or [])
            for sha in source.get("commitShas") or []:
                if sha not in target_shas:
                    target_shas.append(sha)
            target_source["commitShas"] = target_shas
            destination["pinned"] = bool(destination.get("pinned") or event.get("pinned"))
            result.remove(event)
    return result


def load_overrides(path: str | Path = PORTAL_ROOT / "overrides.yml") -> list[dict[str, Any]]:
    """读取并校验 overrides.yml。

    Args:
        path: overrides.yml 文件路径。

    Returns:
        已校验的覆盖规则列表。
    """
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if isinstance(value, Mapping):
        value = value.get("overrides", [])
    if not isinstance(value, list):
        raise ValueError("overrides 必须是列表")
    return validate_overrides(value)


def _short_sha(value: Any) -> str | None:
    """把 SHA 转成公开使用的七位前缀。

    Args:
        value: 原始 SHA 值。

    Returns:
        合法的七位十六进制 SHA；非法值返回 ``None``。
    """
    text = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{7,40}", text):
        return text[:7]
    return None


def _is_public_release_url(value: str, repository: str) -> bool:
    """判断 URL 是否为指定 allowlist 仓库的公开 Release 页面。

    Args:
        value: 待校验的 Release URL。
        repository: ``owner/repo`` 格式的允许仓库名。

    Returns:
        URL 可安全公开时返回 ``True``，否则返回 ``False``。
    """
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {"github.com", "www.github.com"}:
        return False
    owner, repo = repository.split("/", 1)
    decoded_path = unquote(parsed.path)
    if not decoded_path.startswith("/"):
        return False
    path_without_root = decoded_path[1:]
    segments = path_without_root.split("/")
    if any(not part or part in {".", ".."} for part in segments):
        return False
    return len(segments) >= 3 and segments[:3] == [owner, repo, "releases"]


def sanitize_public_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """移除候选中的私有字段并限制公开来源信息。

    Args:
        event: 内部候选事件。

    Returns:
        符合公开时间线字段约束的脱敏事件。

    Raises:
        ValueError: 事件包含无法安全公开的敏感内容。
    """
    public = {key: copy.deepcopy(event[key]) for key in PUBLIC_EVENT_FIELDS if key in event}
    source = dict(public.get("source") or {})
    public_source: dict[str, Any] = {}
    repository = str(source.get("repository") or "")
    if repository not in {value[0] for value in EXPECTED_PRODUCTS.values()}:
        raise ValueError("敏感或未知来源仓库不能公开")
    public_source["repository"] = repository
    raw_shas = source.get("commitShas") or source.get("sha") or source.get("commitSha") or []
    if isinstance(raw_shas, str):
        raw_shas = [raw_shas]
    shas = [_short_sha(value) for value in raw_shas]
    public_source["commitShas"] = [value for value in shas if value]
    release_url = source.get("releaseUrl")
    if isinstance(release_url, str):
        if _is_public_release_url(release_url, repository):
            public_source["releaseUrl"] = release_url
        else:
            public_source["releaseUrl"] = None
    else:
        public_source["releaseUrl"] = None
    public["source"] = public_source
    _scan_sensitive(public)
    return public


def _scan_sensitive(value: Any, path: str = "root") -> None:
    """递归扫描字符串中的令牌、私有链接和完整 SHA。

    Args:
        value: 待扫描的 JSON 兼容值。
        path: 当前值的字段路径。

    Returns:
        无返回值。

    Raises:
        ValueError: 发现敏感模式。
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            _scan_sensitive(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_sensitive(item, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(value):
                raise ValueError(f"敏感内容出现在 {path}")


def _check_bilingual(value: Any, path: str) -> None:
    """检查双语字段存在且均非空。

    Args:
        value: 双语字段值。
        path: 字段路径。

    Returns:
        无返回值。

    Raises:
        ValueError: 双语字段缺少 zh/en 或内容为空。
    """
    if not isinstance(value, Mapping) or not str(value.get("zh", "")).strip() or not str(value.get("en", "")).strip():
        raise ValueError(f"双语字段不能为空: {path}")


def validate_public_collections(collections: Mapping[str, Mapping[str, Any]], *, require_all: bool = True) -> None:
    """校验公开五集合的 Schema、双语内容、ID 唯一性、排序和敏感模式。

    Args:
        collections: 以 products/releases/timeline/faqs/meta 为键的集合映射。
        require_all: 是否要求五个集合全部存在，默认为 ``True``。

    Returns:
        无返回值。

    Raises:
        ValueError: 集合不符合公开契约。
    """
    required = set(COLLECTION_FILES) if require_all else set(collections) & set(COLLECTION_FILES)
    missing = required - set(collections)
    if missing:
        raise ValueError(f"公开集合缺失: {', '.join(sorted(missing))}")
    for name in ("products", "releases", "timeline", "faqs"):
        if name not in collections:
            continue
        schema_path = SCHEMA_ROOT / f"{name}.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
        errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(collections[name]), key=lambda error: list(error.path))
        if errors:
            raise ValueError(f"Schema 校验失败 ({name})，错误数: {len(errors)}")
    if "meta" in collections:
        _validate_meta(collections["meta"])
        base_names = ("products", "releases", "timeline", "faqs")
        if all(name in collections for name in base_names):
            records = collections["meta"]["collections"]
            for name in base_names:
                expected_hash = _sha256(collections[name])
                expected_count = _collection_count(name, collections[name])
                if records[name]["sha256"] != expected_hash:
                    raise ValueError(f"meta 集合哈希不匹配: {name}")
                if records[name]["count"] != expected_count:
                    raise ValueError(f"meta 集合计数不匹配: {name}")
    for name, collection in collections.items():
        _scan_sensitive(collection)
        items = collection.get("products" if name == "products" else "releases" if name == "releases" else "events" if name == "timeline" else "faqs" if name == "faqs" else "")
        if not isinstance(items, list):
            continue
        ids = [str(item.get("id") or item.get("productId")) for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{name} 存在重复 ID")
        for item in items:
            fields = ("name", "tagline") if name == "products" else ("title", "summary") if name == "timeline" else ("question", "answer") if name == "faqs" else ()
            for field in fields:
                _check_bilingual(item.get(field), f"{name}.{item.get('id') or item.get('productId')}.{field}")
    for name, key in (("timeline", "occurredAt"), ("releases", "publishedAt")):
        if name not in collections:
            continue
        items = collections[name].get("events" if name == "timeline" else "releases", [])
        dates = [item.get(key) for item in items if item.get(key)]
        if dates != sorted(dates, reverse=True):
            raise ValueError(f"{name} 时间倒序校验失败")


def _validate_meta(meta: Mapping[str, Any]) -> None:
    """严格校验 meta.json 的发布契约。

    Args:
        meta: meta.json 映射。

    Returns:
        无返回值。

    Raises:
        ValueError: meta 缺少字段或字段类型、格式不正确。
    """
    if not isinstance(meta, Mapping) or set(meta) != {"schemaVersion", "generatedAt", "dataVersion", "sourceWatermarks", "collections"}:
        raise ValueError("meta 字段不完整")
    if meta.get("schemaVersion") != 1:
        raise ValueError("meta schemaVersion 必须为 1")
    generated = meta.get("generatedAt")
    try:
        parsed = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("meta generatedAt 必须为 UTC ISO 8601")
    if not isinstance(meta.get("dataVersion"), str) or not meta["dataVersion"].strip():
        raise ValueError("meta dataVersion 不能为空")
    if not isinstance(meta.get("sourceWatermarks"), Mapping):
        raise ValueError("meta sourceWatermarks 必须是映射")
    records = meta.get("collections")
    required = {"products", "releases", "timeline", "faqs"}
    if not isinstance(records, Mapping) or set(records) != required:
        raise ValueError("meta collections 必须包含四个基础集合")
    for name in required:
        item = records[name]
        if not isinstance(item, Mapping) or set(item) != {"sha256", "count"}:
            raise ValueError(f"meta collections.{name} 字段不完整")
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError(f"meta collections.{name}.sha256 非法")
        if not isinstance(item.get("count"), int) or isinstance(item.get("count"), bool) or item["count"] < 0:
            raise ValueError(f"meta collections.{name}.count 非法")


def build_meta(collections: Mapping[str, Mapping[str, Any]], *, watermarks: Mapping[str, Any] | None = None, generated_at: str | None = None, data_version: str = "1") -> dict[str, Any]:
    """生成包含水位和集合哈希的 meta.json 内容。

    Args:
        collections: products/releases/timeline/faqs 集合。
        watermarks: 源仓库水位映射。
        generated_at: 可选生成时间；缺省使用当前 UTC 时间。
        data_version: 数据版本标识。

    Returns:
        meta.json 对应的映射。
    """
    timestamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    records = {name: {"sha256": _sha256(value), "count": _collection_count(name, value)} for name, value in collections.items() if name != "meta"}
    return {"schemaVersion": 1, "generatedAt": timestamp, "dataVersion": data_version, "sourceWatermarks": dict(watermarks or {}), "collections": records}


def _collection_count(name: str, value: Mapping[str, Any]) -> int:
    """取得集合内的记录数。

    Args:
        name: 集合名称。
        value: 集合内容。

    Returns:
        记录数量。
    """
    key = "products" if name == "products" else "releases" if name == "releases" else "events" if name == "timeline" else "faqs" if name == "faqs" else None
    return len(value.get(key, [])) if key else 1


def build_manifest(collections: Mapping[str, Mapping[str, Any]], *, generated_at: str | None = None, data_version: str = "1") -> dict[str, Any]:
    """生成五集合原子快照 manifest.json。

    Args:
        collections: 包含五个公开集合的映射。
        generated_at: 可选生成时间。
        data_version: 数据版本标识。

    Returns:
        manifest.json 对应的映射，含每个文件的 SHA-256 和字节数。
    """
    timestamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    records: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    for name, filename in COLLECTION_FILES.items():
        if name not in collections:
            raise ValueError(f"manifest 缺少集合: {name}")
        encoded = _canonical_bytes(collections[name])
        entry = {"path": filename, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
        records[name] = entry
        files.append({"name": filename, **entry})
    return {"schemaVersion": 1, "generatedAt": timestamp, "dataVersion": data_version, "collections": records, "files": files}


def write_publication_snapshot(collections: Mapping[str, Mapping[str, Any]], output_dir: str | Path = PUBLISHED_ROOT) -> dict[str, Any]:
    """校验集合并以 manifest 最后替换的方式写入公开目录。

    Args:
        collections: 五个公开集合内容。
        output_dir: 公开 JSON 输出目录。

    Returns:
        写入的 manifest 映射。
    """
    data = {name: copy.deepcopy(dict(value)) for name, value in collections.items()}
    base_names = ("products", "releases", "timeline", "faqs")
    if all(name in data for name in base_names):
        previous_meta = data.get("meta") or {}
        data["meta"] = build_meta(
            {name: data[name] for name in base_names},
            watermarks=previous_meta.get("sourceWatermarks") or {},
            generated_at=previous_meta.get("generatedAt"),
            data_version=str(previous_meta.get("dataVersion") or "1"),
        )
    validate_public_collections(data)
    manifest = build_manifest(data, generated_at=(data.get("meta") or {}).get("generatedAt"), data_version=(data.get("meta") or {}).get("dataVersion", "1"))
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".release-portal-", dir=str(target.parent)))
    try:
        for name, filename in COLLECTION_FILES.items():
            payload = manifest if name == "manifest" else data[name]
            (temporary / filename).write_bytes(_canonical_bytes(payload))
        (temporary / "manifest.json").write_bytes(_canonical_bytes(manifest))
        for name, filename in COLLECTION_FILES.items():
            os.replace(temporary / filename, target / filename)
        os.replace(temporary / "manifest.json", target / "manifest.json")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return manifest


def summarize_changes(before: Iterable[Mapping[str, Any]], after: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """统计新增、修改、隐藏事件及产品影响，不包含私有正文。

    Args:
        before: 发布前事件。
        after: 发布后事件。

    Returns:
        仅含计数、事件 ID 和产品影响的摘要映射。
    """
    old = {str(item.get("id")): item for item in before}
    new = {str(item.get("id")): item for item in after}
    added = sorted(set(new) - set(old))
    hidden = sorted(set(old) - set(new))
    modified = sorted(key for key in set(old) & set(new) if _canonical_bytes(old[key]) != _canonical_bytes(new[key]))
    impact: dict[str, dict[str, int]] = {}
    for status, ids in (("added", added), ("modified", modified), ("hidden", hidden)):
        source = new if status != "hidden" else old
        for event_id in ids:
            product = str(source[event_id].get("productId") or "unknown")
            impact.setdefault(product, {"added": 0, "modified": 0, "hidden": 0})[status] += 1
    return {"added": len(added), "modified": len(modified), "hidden": len(hidden), "eventIds": {"added": added, "modified": modified, "hidden": hidden}, "products": impact}


def build_pr_summary(before: Iterable[Mapping[str, Any]], after: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """生成审核 PR 使用的公开摘要。

    Args:
        before: 发布前事件。
        after: 发布后事件。

    Returns:
        不回显原始正文的变更摘要。
    """
    return summarize_changes(before, after)


def load_candidate_events(path: str | Path) -> list[dict[str, Any]]:
    """读取候选时间线 JSON。

    Args:
        path: 候选 timeline.json 路径。

    Returns:
        候选事件列表。
    """
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(value, Mapping):
        return [dict(item) for item in value.get("events", [])]
    if isinstance(value, list):
        return [dict(item) for item in value]
    raise ValueError("候选时间线必须是对象或数组")


def prepare_public_timeline(events: Iterable[Mapping[str, Any]], overrides: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """应用审核规则并生成按时间倒序排列的公开时间线。

    Args:
        events: 候选事件迭代器。
        overrides: 覆盖规则迭代器。

    Returns:
        可写入 timeline.json 的公开集合。
    """
    prepared = [sanitize_public_event(event) for event in apply_overrides(events, overrides)]
    prepared.sort(key=lambda item: str(item.get("occurredAt") or ""), reverse=True)
    return {"schemaVersion": 1, "events": prepared}


def build_public_collections(
    events: Iterable[Mapping[str, Any]],
    *,
    overrides: Iterable[Mapping[str, Any]] = (),
    products: Mapping[str, Any] | None = None,
    releases: Mapping[str, Any] | None = None,
    faqs: Mapping[str, Any] | None = None,
    watermarks: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
    data_version: str = "1",
) -> dict[str, Any]:
    """从候选事件构建完整五集合快照。

    Args:
        events: 候选事件迭代器。
        overrides: 覆盖规则迭代器。
        products: 可选 products.json 内容，缺省读取 catalog.yml。
        releases: 可选 releases.json 内容，缺省为空集合。
        faqs: 可选 faqs.json 内容，缺省读取 faqs.yml。
        watermarks: 源仓库水位。
        generated_at: 生成时间。
        data_version: 数据版本。

    Returns:
        已生成 meta 的五集合映射。
    """
    if products is None:
        catalog = load_catalog()
        products = {
            "schemaVersion": 1,
            "products": [
                {
                    "productId": product.product_id,
                    "repository": product.repository,
                    "entryType": product.entry_type,
                    "webUrl": product.web_url,
                    "name": product.name,
                    "tagline": product.tagline,
                    "category": product.category,
                    "logo": product.logo,
                    "defaultBranch": product.default_branch,
                    "aiPolicy": product.ai_policy,
                }
                for product in catalog.products
            ],
        }
    if releases is None:
        releases = {"schemaVersion": 1, "releases": []}
    if faqs is None:
        faq_path = PORTAL_ROOT / "faqs.yml"
        faq_value = yaml.safe_load(faq_path.read_text(encoding="utf-8")) or {"schemaVersion": 1, "faqs": []}
        faqs = faq_value if isinstance(faq_value, Mapping) else {"schemaVersion": 1, "faqs": []}
    base = {"products": dict(products), "releases": dict(releases), "timeline": prepare_public_timeline(events, overrides), "faqs": dict(faqs)}
    base["meta"] = build_meta(base, watermarks=watermarks, generated_at=generated_at, data_version=data_version)
    validate_public_collections(base)
    return base


__all__ = [
    "apply_overrides", "build_manifest", "build_meta", "build_pr_summary",
    "build_public_collections", "load_candidate_events", "load_overrides",
    "prepare_public_timeline", "sanitize_public_event", "summarize_changes",
    "validate_overrides", "validate_public_collections", "write_publication_snapshot",
]
