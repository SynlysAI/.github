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
    ai_policy: str

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
        """从 YAML 映射构造产品模型。

        Args:
            value: 产品字段映射。

        Returns:
            类型化的产品模型。

        Raises:
            ValueError: 输入不是映射或缺少必填字段。
        """
        if not isinstance(value, dict):
            raise ValueError("产品配置必须是映射")
        required = {"productId", "repository", "entryType", "webUrl", "name", "tagline", "category", "logo", "defaultBranch", "aiPolicy"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"产品缺少必填字段: {', '.join(sorted(missing))}")
        if not isinstance(value["name"], dict) or not isinstance(value["tagline"], dict):
            raise ValueError("name 和 tagline 必须是双语映射")
        return cls(
            product_id=value["productId"], repository=value["repository"],
            entry_type=value["entryType"], web_url=value["webUrl"],
            name=dict(value["name"]), tagline=dict(value["tagline"]),
            category=value["category"],
            logo=value.get("logo"),
            default_branch=value["defaultBranch"], ai_policy=value["aiPolicy"],
        )


@dataclass(frozen=True)
class Catalog:
    """产品注册表。"""

    schema_version: int
    products: tuple[Product, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Catalog":
        """从 YAML 根映射构造注册表。

        Args:
            value: 注册表根映射。

        Returns:
            类型化的注册表模型。

        Raises:
            ValueError: 根节点或产品列表类型错误，或缺少必填字段。
        """
        if not isinstance(value, dict):
            raise ValueError("catalog 配置必须是映射")
        missing = {"schemaVersion", "products"} - set(value)
        if missing:
            raise ValueError(f"catalog 缺少必填字段: {', '.join(sorted(missing))}")
        if not isinstance(value["products"], list):
            raise ValueError("products 必须是列表")
        products = tuple(Product.from_mapping(item) for item in value["products"])
        if not isinstance(value["schemaVersion"], int):
            raise ValueError("schemaVersion 必须是整数")
        return cls(schema_version=value["schemaVersion"], products=products)
