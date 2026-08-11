"""Release Portal 产品注册表配置测试。"""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from scripts.release_portal.config import effective_logo, load_catalog, validate_catalog
from scripts.release_portal.models import Catalog


def test_catalog_has_six_unique_products_and_repositories() -> None:
    """注册表包含六个 productId 且仓库不重复。"""
    catalog = load_catalog()
    assert len(catalog.products) == 6
    assert len({product.product_id for product in catalog.products}) == 6
    assert len({product.repository for product in catalog.products}) == 6


def test_five_web_urls_are_https_and_smartaccess_is_download_only() -> None:
    """五个 Web 产品使用 HTTPS，SmartAccess 仅提供下载入口。"""
    catalog = load_catalog()
    web_products = [product for product in catalog.products if product.entry_type == "web"]
    assert len(web_products) == 5
    assert all(product.web_url.startswith("https://") for product in web_products)
    smartaccess = next(product for product in catalog.products if product.product_id == "smartaccess")
    assert smartaccess.entry_type == "download"
    assert smartaccess.web_url is None


def test_catalog_rejects_duplicate_product_ids() -> None:
    """重复 productId 应被拒绝。"""
    catalog = load_catalog()
    duplicate = list(catalog.products)
    duplicate[-1] = duplicate[0]
    with pytest.raises(ValueError, match="productId"):
        validate_catalog(Catalog(schema_version=1, products=tuple(duplicate)))


def test_catalog_allowlist_is_fixed() -> None:
    """产品入口必须匹配计划中的固定 allowlist。"""
    catalog = load_catalog()
    assert {
        (p.product_id, p.repository, p.entry_type, p.web_url) for p in catalog.products
    } == {
        ("ai4ms", "SynlysAI/AI4MS", "web", "https://ai4ms.xmuzc.com/"),
        ("spec-agent", "SynlysAI/Spec_Agent", "web", "https://specagent.xmuzc.com/"),
        ("poly-agent", "SynlysAI/Poly_Agent", "web", "https://specpoly.xmuzc.com/"),
        ("speclabos", "SynlysAI/SpecLabOS", "web", "https://speclabos.xmuzc.com/"),
        ("ragportal", "SynlysAI/RAGPortal", "web", "https://rag.xmuzc.com/"),
        ("smartaccess", "SynlysAI/SmartAccess", "download", None),
    }


def test_catalog_rejects_invalid_entry_and_required_fields() -> None:
    """错误入口、URL 和双语必填字段必须失败。"""
    raw = yaml.safe_load(Path("release-portal/catalog.yml").read_text(encoding="utf-8"))
    raw["products"][-1]["webUrl"] = "https://wrong.example/"
    with pytest.raises(ValueError):
        validate_catalog(raw)

    raw = yaml.safe_load(Path("release-portal/catalog.yml").read_text(encoding="utf-8"))
    raw["products"][0]["name"] = {"en": "AI4MS"}
    with pytest.raises(ValueError):
        validate_catalog(raw)


def test_schemas_are_closed_and_timeline_uses_short_sha() -> None:
    """公开 Schema 关闭未知字段，时间线只允许七位短 SHA。"""
    schema_dir = Path("release-portal/schemas")
    for path in schema_dir.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8-sig"))
        assert schema["additionalProperties"] is False
    timeline = json.loads((schema_dir / "timeline.schema.json").read_text(encoding="utf-8-sig"))
    pattern = timeline["$defs"]["source"]["properties"]["commitShas"]["items"]["pattern"]
    assert pattern == "^[0-9a-f]{7}$"


def test_missing_catalog_or_product_required_fields_are_rejected() -> None:
    """schemaVersion、defaultBranch、aiPolicy 缺失时必须失败。"""
    raw = yaml.safe_load(Path("release-portal/catalog.yml").read_text(encoding="utf-8"))
    for key in ("schemaVersion",):
        invalid = dict(raw)
        invalid.pop(key)
        with pytest.raises(ValueError):
            validate_catalog(invalid)
    for key in ("defaultBranch", "aiPolicy"):
        invalid = yaml.safe_load(Path("release-portal/catalog.yml").read_text(encoding="utf-8"))
        invalid["products"][0].pop(key)
        with pytest.raises(ValueError):
            validate_catalog(invalid)


def test_logo_null_uses_fallback() -> None:
    """Logo 为 null 时仍可校验并使用品牌回退。"""
    raw = yaml.safe_load(Path("release-portal/catalog.yml").read_text(encoding="utf-8"))
    raw["products"][0]["logo"] = None
    validate_catalog(raw)
    product = load_catalog().products[0]
    from scripts.release_portal.models import Product
    fallback = Product(product.product_id, product.repository, product.entry_type, product.web_url, product.name, product.tagline, product.category, None, product.default_branch, product.ai_policy)
    assert effective_logo(fallback)["src"] == "logo.png"


def test_jsonschema_rejects_empty_catalog_extra_field_and_full_sha() -> None:
    """JSON Schema 实际拒绝空列表、错误映射、未知字段和完整 SHA。"""
    schema_dir = Path("release-portal/schemas")
    product_schema = json.loads((schema_dir / "products.schema.json").read_text(encoding="utf-8-sig"))
    validator = Draft202012Validator(product_schema)
    raw = yaml.safe_load(Path("release-portal/catalog.yml").read_text(encoding="utf-8"))
    valid = {"schemaVersion": 1, "products": raw["products"]}
    assert not list(validator.iter_errors(valid))
    assert list(validator.iter_errors({"schemaVersion": 1, "products": []}))
    invalid = json.loads(json.dumps(valid))
    invalid["products"][0]["webUrl"] = "http://insecure.example/"
    assert list(validator.iter_errors(invalid))
    invalid = json.loads(json.dumps(valid))
    invalid["products"][0]["unexpected"] = True
    assert list(validator.iter_errors(invalid))

    timeline_schema = json.loads((schema_dir / "timeline.schema.json").read_text(encoding="utf-8-sig"))
    timeline_validator = Draft202012Validator(timeline_schema)
    event = {"schemaVersion": 1, "events": [{
        "id": "x", "productId": "ai4ms", "level": "commit", "occurredAt": "2026-01-01T00:00:00Z",
        "version": None, "changeType": "feature", "module": "general",
        "title": {"zh": "标题", "en": "Title"}, "summary": {"zh": "摘要", "en": "Summary"},
        "detailsMarkdown": {"zh": "", "en": ""}, "source": {"repository": "SynlysAI/AI4MS", "commitShas": ["a" * 40], "releaseUrl": None}, "pinned": False
    }]}
    assert list(timeline_validator.iter_errors(event))


def test_missing_logo_uses_brand_icon_and_english_name() -> None:
    """缺少 Logo 时只使用品牌图标和产品英文名。"""
    product = load_catalog().products[0]
    product_without_logo = type(product)(
        product_id=product.product_id,
        repository=product.repository,
        entry_type=product.entry_type,
        web_url=product.web_url,
        name=product.name,
        tagline=product.tagline,
        category=product.category,
        logo=None,
        default_branch=product.default_branch,
        ai_policy=product.ai_policy,
    )
    logo = effective_logo(product_without_logo)
    assert logo == {"src": "logo.png", "alt": product.name["en"]}


def test_catalog_path_is_repo_relative() -> None:
    """默认配置路径应指向仓库内的 catalog.yml。"""
    assert Path(load_catalog.__defaults__[0]).name == "catalog.yml"
