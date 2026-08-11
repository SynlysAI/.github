"""Release Portal 产品注册表配置测试。"""

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
