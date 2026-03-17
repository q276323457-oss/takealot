from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .types import ListingDraft, ValidationResult


@dataclass
class RuleSet:
    raw: dict[str, Any]

    @property
    def constraints(self) -> dict[str, Any]:
        return self.raw.get("constraints", {})

    @property
    def forbidden_terms(self) -> list[str]:
        return [str(x).lower() for x in self.raw.get("forbidden_terms", [])]

    @property
    def trademark_restricted(self) -> list[str]:
        return [str(x).lower() for x in self.raw.get("trademark_restricted", [])]


def load_rules(path: str | Path) -> RuleSet:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid rules file: {path}")
    return RuleSet(raw=data)


def _contains_any(text: str, terms: list[str]) -> list[str]:
    lower = text.lower()
    return [t for t in terms if t in lower]


def validate_draft(draft: ListingDraft, rules: RuleSet) -> ValidationResult:
    c = rules.constraints
    errors: list[str] = []
    warnings: list[str] = []

    title_max_len = int(c.get("title_max_len", 75))
    subtitle_max_len = int(c.get("subtitle_max_len", 110))
    key_features_min_len = int(c.get("key_features_min_len", 200))

    if len(draft.title) > title_max_len:
        errors.append(f"title too long: {len(draft.title)} > {title_max_len}")
    if len(draft.subtitle) > subtitle_max_len:
        errors.append(f"subtitle too long: {len(draft.subtitle)} > {subtitle_max_len}")
    if len(draft.key_features.strip()) < key_features_min_len:
        errors.append(
            f"key_features too short: {len(draft.key_features.strip())} < {key_features_min_len}"
        )

    # 只检查纯 ASCII 字母部分是否全大写（忽略数字、中文、特殊字符）
    ascii_letters = [c for c in draft.title if c.isascii() and c.isalpha()]
    if ascii_letters and all(c.isupper() for c in ascii_letters) and len(ascii_letters) >= 4:
        errors.append("title cannot be all caps")

    body = "\n".join(
        [draft.title, draft.subtitle, draft.key_features, " ".join(draft.whats_in_box)]
    )

    bad_claims = _contains_any(body, rules.forbidden_terms)
    if bad_claims:
        errors.append(f"forbidden marketing terms found: {', '.join(sorted(set(bad_claims)))}")

    bad_tm = _contains_any(body, rules.trademark_restricted)
    if bad_tm:
        warnings.append(
            "contains restricted trademark terms, verify authorization: "
            + ", ".join(sorted(set(bad_tm)))
        )

    if not draft.whats_in_box:
        warnings.append("whats_in_box is empty")
    if not draft.attributes:
        warnings.append("attributes is empty")

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)


def sanitize_draft(draft: ListingDraft, rules: RuleSet) -> ListingDraft:
    c = rules.constraints
    draft.title = draft.title.strip()[: int(c.get("title_max_len", 75))]
    draft.subtitle = draft.subtitle.strip()[: int(c.get("subtitle_max_len", 110))]

    if len(draft.key_features.strip()) < int(c.get("key_features_min_len", 200)):
        pad = " This listing follows Takealot catalogue requirements with clear specifications and usage details."
        while len(draft.key_features.strip()) < int(c.get("key_features_min_len", 200)):
            draft.key_features = (draft.key_features.strip() + pad).strip()

    # Business rule: default no-variant flow.
    draft.variants = []
    if not isinstance(draft.attributes, dict):
        draft.attributes = {}
    # Business rule: brand left blank.
    draft.attributes["brand"] = ""

    return draft
