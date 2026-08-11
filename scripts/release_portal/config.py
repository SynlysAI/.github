"""加载并校验 Release Portal 的 YAML 产品注册表。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .models import Catalog, Product

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "release-portal" / "catalog.yml"


def load_yaml(path: str | Path) -> dict[str, Any]:
    """读取 UTF-8 YAML 映射。

    Args:
        path: YAML 文件路径。

    Returns:
        解析后的根映射。

    Raises:
        ValueError: 文件根节点不是映射。
    """
    with Path(path).open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream) or {}
    if not isinstance(value, dict):
        raise ValueError(f"配置根节点必须是映射: {path}")
    return value


def load_catalog(path: str | Path = CATALOG_PATH) -> Catalog:
    """加载产品注册表并返回类型化模型。"""
    catalog = Catalog.from_mapping(load_yaml(path))
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: Catalog | dict[str, Any] | str | Path) -> None:
    """校验产品 ID、仓库、入口类型和 URL 等公开契约。

    Args:
        catalog: ``Catalog`` 实例、YAML 根映射或文件路径。

    Raises:
        ValueError: 注册表违反契约时抛出。
    """
    if isinstance(catalog, (str, Path)):
        catalog = Catalog.from_mapping(load_yaml(catalog))
    elif isinstance(catalog, dict):
        catalog = Catalog.from_mapping(catalog)
    products = catalog.products
    if catalog.schema_version != 1:
        raise ValueError("仅支持 schemaVersion: 1")
    if len(products) != 6:
        raise ValueError(f"产品数量必须为 6，实际为 {len(products)}")
    product_ids = [product.product_id for product in products]
    if len(set(product_ids)) != len(product_ids):
        raise ValueError("productId 必须唯一")
    repositories = [product.repository for product in products]
    if len(set(repositories)) != len(repositories):
        raise ValueError("repository 必须唯一")
    for product in products:
        if not product.product_id or not product.repository:
            raise ValueError("productId 和 repository 不能为空")
        if product.entry_type not in {"web", "download"}:
            raise ValueError(f"不支持的 entryType: {product.entry_type}")
        if product.entry_type == "web":
            if not product.web_url:
                raise ValueError(f"Web 产品必须提供 webUrl: {product.product_id}")
            parsed = urlparse(product.web_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(f"Web URL 必须使用 HTTPS: {product.product_id}")
        elif product.product_id == "smartaccess" and product.web_url is not None:
            raise ValueError("SmartAccess 仅允许下载入口")
        if product.ai_policy != "metadata-only":
            raise ValueError(f"aiPolicy 必须为 metadata-only: {product.product_id}")
        if not product.name.get("en"):
            raise ValueError(f"产品必须提供英文名: {product.product_id}")


def effective_logo(product: Product) -> dict[str, str]:
    """返回可公开展示的 Logo 信息，缺少产品 Logo 时使用品牌图标和英文名。"""
    return {"src": product.logo or "logo.png", "alt": product.name["en"]}
