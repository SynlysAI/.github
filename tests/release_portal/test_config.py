"""Release Portal 产品注册表配置测试。"""

import json
from pathlib import Path

import pytest

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
    raw = {"schemaVersion": 1, "products": [p.__dict__ for p in load_catalog().products]}
    raw["products"][-1]["web_url"] = "https://wrong.example/"
    with pytest.raises(ValueError):
        validate_catalog(raw)

    raw = {"schemaVersion": 1, "products": [p.__dict__ for p in load_catalog().products]}
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
    )
    logo = effective_logo(product_without_logo)
    assert logo == {"src": "logo.png", "alt": product.name["en"]}


def test_catalog_path_is_repo_relative() -> None:
    """默认配置路径应指向仓库内的 catalog.yml。"""
    assert Path(load_catalog.__defaults__[0]).name == "catalog.yml"
