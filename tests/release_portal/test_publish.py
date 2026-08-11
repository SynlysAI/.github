"""Release Portal 候选审核和公开发布测试。"""

import json
import hashlib
from pathlib import Path

import pytest

from scripts.release_portal.publish import (
    apply_overrides,
    build_manifest,
    build_meta,
    sanitize_public_event,
    summarize_changes,
    validate_public_collections,
    validate_overrides,
    write_publication_snapshot,
)
from scripts.release_portal.config import load_catalog


def _event(event_id: str, when: str = "2026-08-03T08:00:00Z") -> dict:
    return {
        "id": event_id,
        "productId": "spec-agent",
        "level": "commit",
        "occurredAt": when,
        "version": None,
        "changeType": "feature",
        "module": "parser",
        "title": {"zh": "原标题", "en": "Original title"},
        "summary": {"zh": "原摘要", "en": "Original summary"},
        "detailsMarkdown": {"zh": "", "en": ""},
        "source": {
            "repository": "SynlysAI/Spec_Agent",
            "commitShas": ["abcdef1234567890"],
            "releaseUrl": "https://github.com/SynlysAI/Spec_Agent/releases/tag/v1",
            "prUrl": "https://github.com/SynlysAI/Spec_Agent/pull/3",
        },
        "pinned": False,
        "reviewReason": "人工审核通过",
    }


def test_all_five_override_operations_are_applied():
    events = [_event("hide"), _event("pin"), _event("replace"), _event("type"), _event("merge")]
    overrides = [
        {"id": "hide", "hide": True},
        {"id": "pin", "pin": True},
        {"id": "replace", "replaceText": {"title": {"zh": "改写", "en": "Rewritten"}}},
        {"id": "type", "changeType": "algorithm"},
        {"id": "merge", "mergeInto": "pin"},
    ]
    result = apply_overrides(events, overrides)
    assert [item["id"] for item in result] == ["pin", "replace", "type"]
    assert result[0]["pinned"] is True
    assert result[1]["title"]["en"] == "Rewritten"
    assert result[2]["changeType"] == "algorithm"


def test_conflicting_overrides_are_rejected():
    with pytest.raises(ValueError, match="冲突"):
        validate_overrides([{"id": "x", "hide": True, "pin": True}])
    with pytest.raises(ValueError, match="冲突"):
        validate_overrides([{"id": "x", "changeType": "feature"}, {"id": "x", "changeType": "bugfix"}])


def test_sanitization_keeps_public_metadata_only():
    public = sanitize_public_event(_event("x"))
    assert public["source"]["commitShas"] == ["abcdef1"]
    assert "prUrl" not in public["source"]
    assert public["source"]["releaseUrl"].startswith("https://github.com/")
    assert "reviewReason" not in public


def test_sanitization_rejects_unknown_repository_and_private_release_url():
    event = _event("internal")
    event["source"]["repository"] = "Internal/Secret"
    with pytest.raises(ValueError, match="来源仓库"):
        sanitize_public_event(event)
    event = _event("private-url")
    event["source"]["releaseUrl"] = "https://github.com/SynlysAI/Other/releases/tag/v1"
    public = sanitize_public_event(event)
    assert public["source"]["releaseUrl"] is None


def test_sensitive_pattern_is_rejected():
    event = _event("secret")
    event["summary"]["en"] = "token ghp_abcdefghijklmnopqrstuvwxyz"
    with pytest.raises(ValueError, match="敏感"):
        sanitize_public_event(event)


def test_schema_bilingual_duplicate_and_sort_validation():
    first = sanitize_public_event(_event("first", "2026-08-03T08:00:00Z"))
    second = sanitize_public_event(_event("second", "2026-08-04T08:00:00Z"))
    valid = {"timeline": {"schemaVersion": 1, "events": [second, first]}}
    validate_public_collections(valid, require_all=False)
    with pytest.raises(ValueError, match="时间倒序"):
        validate_public_collections({"timeline": {"schemaVersion": 1, "events": [first, second]}}, require_all=False)
    with pytest.raises(ValueError, match="重复"):
        validate_public_collections({"timeline": {"schemaVersion": 1, "events": [first, first]}}, require_all=False)
    invalid = dict(first)
    invalid["title"] = {"zh": "" , "en": "Title"}
    with pytest.raises(ValueError, match="双语"):
        validate_public_collections({"timeline": {"schemaVersion": 1, "events": [invalid]}}, require_all=False)


def test_meta_and_manifest_hashes_are_deterministic(tmp_path: Path):
    products = {"schemaVersion": 1, "products": [{
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
    } for product in load_catalog().products]}
    collections = {"products": products, "releases": {"schemaVersion": 1, "releases": []}, "timeline": {"schemaVersion": 1, "events": []}, "faqs": {"schemaVersion": 1, "faqs": []}}
    meta = build_meta(collections, watermarks={"SynlysAI/Spec_Agent": "abcdef1"}, generated_at="2026-08-10T00:00:00Z")
    assert meta["collections"]["products"]["sha256"]
    collections["meta"] = meta
    manifest = build_manifest(collections, generated_at="2026-08-10T00:00:00Z")
    assert manifest["collections"]["meta"]["sha256"]
    write_publication_snapshot(collections, tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 1
    for name, entry in manifest["collections"].items():
        content = (tmp_path / entry["path"]).read_bytes()
        assert len(content) == entry["bytes"]
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]


def test_meta_requires_strict_fields_and_collection_hashes():
    with pytest.raises(ValueError, match="meta"):
        validate_public_collections({"meta": {}}, require_all=False)


def test_products_bilingual_fields_cannot_be_empty():
    products = {"schemaVersion": 1, "products": [{
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
    } for product in load_catalog().products]}
    products["products"][0]["name"]["en"] = ""
    with pytest.raises(ValueError, match="双语"):
        validate_public_collections({"products": products}, require_all=False)


def test_summary_does_not_echo_private_body():
    summary = summarize_changes([_event("old")], [_event("new")])
    assert summary["added"] == 1
    assert summary["hidden"] == 1
    assert "prUrl" not in json.dumps(summary)
