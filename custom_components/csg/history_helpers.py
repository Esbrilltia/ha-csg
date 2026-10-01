"""Shared calendar parsing and response-local official bill validation."""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .csg_client import WF_ATTR_CHARGE, WF_ATTR_KWH, WF_ATTR_MONTH

_LOGGER = logging.getLogger(__name__)


def parse_history_start_month(value: Any) -> tuple[int, int]:
    """Parse a locale-independent, strict YYYY-MM calendar month."""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}", value) is None
    ):
        raise ValueError("History start month must be YYYY-MM")
    day = dt.date.fromisoformat(f"{value}-01")
    return day.year, day.month


def month_key(month: tuple[int, int]) -> str:
    """Return the canonical month key."""
    return f"{month[0]:04d}-{month[1]:02d}"


def _parse_bill_month(value: Any) -> tuple[int, int] | None:
    """Accept only the API's YYYYMM and YYYY-MM calendar month formats."""
    text = str(value)
    if re.fullmatch(r"[0-9]{4}-?[0-9]{2}", text) is None:
        return None
    compact = text.replace("-", "")
    year, month = int(compact[:4]), int(compact[4:])
    try:
        dt.date(year, month, 1)
    except ValueError:
        return None
    return year, month


def collect_monthly_bill_candidates(
    by_month: Iterable[Any],
    account: str,
    year: int,
) -> dict[tuple[int, int], tuple[float | None, float | None]]:
    """Accept one candidate per requested-year month without response conflicts."""
    candidates: dict[tuple[int, int], dict[str, set[float]]] = {}
    for row in by_month:
        month = _parse_bill_month(
            row.get(WF_ATTR_MONTH) if isinstance(row, Mapping) else None
        )
        if month is None:
            _LOGGER.warning("Skipped malformed monthly bill month for %s/%s", account, year)
            continue
        if month[0] != year:
            _LOGGER.warning(
                "Skipped monthly bill %04d-%02d outside requested year %s for %s",
                month[0], month[1], year, account,
            )
            continue
        fields = candidates.setdefault(
            month, {WF_ATTR_KWH: set(), WF_ATTR_CHARGE: set()}
        )
        for key in fields:
            value = row.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number) and number >= 0:
                fields[key].add(number)

    accepted: dict[tuple[int, int], tuple[float | None, float | None]] = {}
    for month, fields in sorted(candidates.items()):
        if any(len(values) > 1 for values in fields.values()):
            _LOGGER.warning(
                "Skipped monthly bill conflict for %s/%04d-%02d", account, month[0], month[1]
            )
            continue
        accepted[month] = (
            next(iter(fields[WF_ATTR_KWH]), None),
            next(iter(fields[WF_ATTR_CHARGE]), None),
        )
    return accepted
