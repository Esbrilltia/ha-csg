"""Official closed-month CNY bills, independent of any tariff selection."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import (
    StatisticData, StatisticMeanType, StatisticMetaData,
)

from .const import DOMAIN

_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")


def cost_statistic_metadata(account_number: str) -> StatisticMetaData:
    """Use the full energy identity and Core 2026.9.3 Opower cost metadata.

    HA Energy interprets unitless external costs in its global currency. These
    facts are always CNY, so the Bridge must gate all cost access on CNY.
    """
    digest = hashlib.sha256(account_number.encode("utf-8")).hexdigest()
    return StatisticMetaData(
        source=DOMAIN,
        statistic_id=f"{DOMAIN}:cost_{digest}",
        name=f"CSG cost {digest[:8]}",
        unit_of_measurement=None,
        unit_class=None,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
    )


def build_cost_statistics(
    monthly_bills: Mapping[str, Mapping[str, Any]], today: dt.date,
) -> list[StatisticData]:
    """Build sparse monthly rows; holes stay unknown, revisions rebuild sums."""
    bills = []
    current_month = today.replace(day=1)
    for key, bill in monthly_bills.items():
        if not isinstance(key, str) or not isinstance(bill, Mapping):
            continue
        try:
            month = dt.date.fromisoformat(f"{key}-01")
        except ValueError:
            continue
        if month.isoformat()[:7] != key or month >= current_month:
            continue
        value = bill.get("cost_cny")
        if value is None or isinstance(value, bool):
            continue
        try:
            cost = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if cost.is_finite() and cost >= 0:
            bills.append((month, cost))

    total = Decimal(0)
    rows = []
    for month, cost in sorted(bills):
        total += cost
        state, cumulative = float(cost), float(total)
        if not math.isfinite(state) or not math.isfinite(cumulative):
            raise ValueError("Monthly cost exceeds Recorder numeric range")
        rows.append(StatisticData(
            start=dt.datetime.combine(month, dt.time(), _CSG_TIME_ZONE),
            state=state, sum=cumulative,
        ))
    return rows
