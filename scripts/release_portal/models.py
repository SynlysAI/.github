"""Release Portal 的内部配置数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Product:
    """描述一个可公开发布的产品及其入口。"""

    product_id: str
    repository: str
    entry_type: str
    web_url: str | None
    name: dict[str, str]
    tagline: dict[str, str]
    category: str
    logo: str | None
    default_branch: str
    ai_policy: str = "metadata-only"

    @property
    def productId(self) -> str:  # noqa: N802
        """返回公开契约中的 productId 字段。"""
        return self.product_id

    @property
    def entryType(self) -> str:  # noqa: N802
        """返回公开契约中的 entryType 字段。"""
        return self.entry_type

    @property
    def webUrl(self) -> str | None:  # noqa: N802
        """返回公开契约中的 webUrl 字段。"""
        return self.web_url

    @property
    def defaultBranch(self) -> str:  # noqa: N802
        """返回公开契约中的 defaultBranch 字段。"""
        return self.default_branch

    @property
    def aiPolicy(self) -> str:  # noqa: N802
        """返回公开契约中的 aiPolicy 字段。"""
        return self.ai_policy

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Product":
        """从 YAML 映射构造产品模型，兼容 camelCase 与 snake_case 字段。"""
        return cls(
            product_id=value.get("productId", value.get("product_id", "")),
            repository=value.get("repository", ""),
            entry_type=value.get("entryType", value.get("entry_type", "")),
            web_url=value.get("webUrl", value.get("web_url")),
            name=dict(value.get("name", {})),
            tagline=dict(value.get("tagline", value.get("positioning", {}))),
            category=value.get("category", ""),
            logo=value.get("logo"),
            default_branch=value.get("defaultBranch", value.get("default_branch", "main")),
            ai_policy=value.get("aiPolicy", value.get("ai_policy", "metadata-only")),
        )


@dataclass(frozen=True)
class Catalog:
    """产品注册表。"""

    schema_version: int
    products: tuple[Product, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Catalog":
        """从 YAML 根映射构造注册表。"""
        products = tuple(Product.from_mapping(item) for item in value.get("products", []))
        return cls(schema_version=int(value.get("schemaVersion", value.get("schema_version", 1))), products=products)
