"""Tests for Home Assistant translation resources."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[1] / "custom_components" / "csg_plus"
PLACEHOLDER_PATTERN = re.compile(r"{([a-z_]+)}")


def _leaves(value: object, prefix: str = "") -> dict[str, str]:
    if not isinstance(value, dict):
        return {prefix: str(value)}
    return {
        key: text
        for name, child in value.items()
        for key, text in _leaves(child, f"{prefix}.{name}" if prefix else name).items()
    }


def test_translation_keys_and_placeholders_match_default_language() -> None:
    """Every runtime translation mirrors the default resource structure."""
    default = _leaves(json.loads((ROOT / "strings.json").read_text(encoding="utf-8")))
    for language in ("en", "zh-Hans"):
        translated = _leaves(
            json.loads((ROOT / "translations" / f"{language}.json").read_text(encoding="utf-8"))
        )
        assert default.keys() == translated.keys()
        for key, default_value in default.items():
            assert PLACEHOLDER_PATTERN.findall(default_value) == PLACEHOLDER_PATTERN.findall(
                translated[key]
            )


def test_runtime_english_translation_matches_default_resource() -> None:
    """Home Assistant loads en.json at runtime, so it must not drift from strings.json."""
    default = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((ROOT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert english == default
