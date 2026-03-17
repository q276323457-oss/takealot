from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProductSource:
    source_url: str
    title: str
    category_path: list[str] = field(default_factory=list)
    subtitle: str = ""
    description: str = ""
    price_text: str = ""
    image_urls: list[str] = field(default_factory=list)
    sku_options: list[str] = field(default_factory=list)
    product_attrs: dict[str, str] = field(default_factory=dict)   # 1688 商品属性表
    packaging_info: list[dict[str, str]] = field(default_factory=list)  # 1688 包装信息（每规格一行）
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ListingDraft:
    title: str
    subtitle: str
    key_features: str
    whats_in_box: list[str]
    attributes: dict[str, str]
    variants: list[dict[str, str]]
    compliance_notes: list[str] = field(default_factory=list)
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    ok: bool
    run_dir: str
    source_file: str
    draft_file: str
    markdown_file: str
    image_files: list[str]
    action: str
    message: str
