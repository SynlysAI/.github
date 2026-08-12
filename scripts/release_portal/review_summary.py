"""使用受信任主分支代码生成候选审核 PR 摘要。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .config import load_catalog
from .publish import build_pr_summary, load_candidate_events


def render_pr_summary(before_path: str | Path, after_path: str | Path) -> str:
    """比较候选时间线并生成不包含正文的 Markdown 审核摘要。

    Args:
        before_path: 变更前 timeline.json 路径。
        after_path: 变更后 timeline.json 路径。

    Returns:
        只包含新增、修改、隐藏及 allowlist 产品计数的 Markdown 文本。
    """
    summary = build_pr_summary(
        load_candidate_events(before_path),
        load_candidate_events(after_path),
    )
    allowed_products = {product.product_id for product in load_catalog().products}
    lines = [
        "## Release Portal 审核摘要",
        "",
        "| 变更 | 数量 |",
        "| --- | ---: |",
        f"| 新增 | {summary['added']} |",
        f"| 修改 | {summary['modified']} |",
        f"| 隐藏 | {summary['hidden']} |",
        "",
        "| 产品 | 新增 | 修改 | 隐藏 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for product_id in sorted(allowed_products):
        impact = summary["products"].get(product_id, {})
        lines.append(
            "| {product} | {added} | {modified} | {hidden} |".format(
                product=product_id,
                added=int(impact.get("added", 0)),
                modified=int(impact.get("modified", 0)),
                hidden=int(impact.get("hidden", 0)),
            )
        )
    return "\n".join(lines) + "\n"


def write_pr_summary(before_path: str | Path, after_path: str | Path, output_path: str | Path) -> None:
    """将审核摘要写入指定 Markdown 文件。

    Args:
        before_path: 变更前 timeline.json 路径。
        after_path: 变更后 timeline.json 路径。
        output_path: 待写入的 Markdown 文件路径。

    Returns:
        无返回值。
    """
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_pr_summary(before_path, after_path), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    """构造审核摘要命令行参数解析器。

    Returns:
        配置好的参数解析器。
    """
    parser = argparse.ArgumentParser(prog="release-portal-review-summary")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数并生成候选审核摘要。

    Args:
        argv: 命令行参数；缺省读取 ``sys.argv``。

    Returns:
        成功时返回 0。
    """
    args = _parser().parse_args(argv)
    write_pr_summary(args.before, args.after, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
