"""Explicit current Guangzhou policies; never calculate historical bill costs.

Policy identifiers: 粤价〔2012〕135号, 粤发改价格〔2017〕498号,
粤发改价格函〔2021〕826号, 粤发改价格函〔2023〕553号,
粤发改价格〔2021〕331号. The final inclusive rates are also published in
Guangzhou's price list: https://www.gz.gov.cn/attachment/0/89/89550/6432486.pdf
Policy text and the first time-of-use, then ladder rule:
https://www.haizhu.gov.cn/gzhzfg/attachment/7/7570/7570103/9476645.pdf
All policies are static. No government website is queried at runtime.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

GUANGZHOU_AREA_CODE = "080000"
SCHEME_UNCONFIGURED = "unconfigured"
SCHEME_LADDER = "ladder"
SCHEME_COMBINED = "combined"
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_SOURCES = (
    "粤价〔2012〕135号", "粤发改价格〔2017〕498号",
    "粤发改价格函〔2021〕826号", "粤发改价格函〔2023〕553号",
    "粤发改价格〔2021〕331号", "Guangzhou inclusive electricity price list",
)
_RATES = MappingProxyType({
    "tier_1": Decimal("0.58886875"),
    "tier_2": Decimal("0.63886875"),
    "tier_3": Decimal("0.88886875"),
    "peak": Decimal("0.96596875"),
    "flat": Decimal("0.58886875"),
    "valley": Decimal("0.29876875"),
    "combined": Decimal("0.62586875"),
})
_SURCHARGES = (Decimal("0"), Decimal("0.05"), Decimal("0.30"))


@dataclass(frozen=True)
class TariffProfile:
    """Current policy capability, separate from the account's official facts."""

    profile_id: str
    area_code: str
    customer_type: str
    ladder_enabled: bool
    multi_person_allowance: Decimal
    tou_enabled: bool
    billing_period: str
    effective_from: dt.date
    source: tuple[str, ...]
    rate_table: Mapping[str, Decimal]

    def thresholds(self, month: int) -> tuple[Decimal, Decimal]:
        first, second = (260, 600) if 5 <= month <= 10 else (200, 400)
        return Decimal(first) + self.multi_person_allowance, Decimal(second) + self.multi_person_allowance

    def current_rate(self, tier: int | None, now: dt.datetime) -> Decimal | None:
        if not self.ladder_enabled:
            return self.rate_table["combined"]
        if isinstance(tier, bool) or tier not in (1, 2, 3):
            return None
        if self.tou_enabled:
            # Final inclusive prices, rather than multiplying the inclusive
            # flat price by an industrial ratio. Ladder surcharge comes last.
            return self.rate_table[tou_period(now)] + _SURCHARGES[tier - 1]
        return self.rate_table[f"tier_{tier}"]


def _profile(name: str, multi: bool = False, tou: bool = False, combined: bool = False) -> TariffProfile:
    effective = dt.date(2021, 10, 1) if tou else dt.date(2021, 6, 1) if multi else dt.date(2019, 7, 1)
    return TariffProfile(
        profile_id=f"guangzhou_{name}", area_code=GUANGZHOU_AREA_CODE,
        customer_type="residential", ladder_enabled=not combined,
        multi_person_allowance=Decimal(100 if multi else 0), tou_enabled=tou,
        billing_period="calendar_month", effective_from=effective,
        source=_SOURCES, rate_table=_RATES,
    )


GUANGZHOU_PROFILES = MappingProxyType({
    (SCHEME_LADDER, False, False): _profile("standard"),
    (SCHEME_LADDER, True, False): _profile("multi_person", multi=True),
    (SCHEME_LADDER, False, True): _profile("tou", tou=True),
    (SCHEME_LADDER, True, True): _profile("multi_person_tou", multi=True, tou=True),
    (SCHEME_COMBINED, False, False): _profile("combined", combined=True),
})


def validate_tariff_selection(selection: Any) -> dict[str, Any]:
    """Fail closed on unsupported or malformed combinations, without inference."""
    if not isinstance(selection, Mapping):
        raise ValueError("Invalid tariff selection")
    scheme = selection.get("scheme", SCHEME_UNCONFIGURED)
    multi, tou = selection.get("multi_person", False), selection.get("tou", False)
    if not isinstance(scheme, str):
        raise ValueError("Tariff scheme must be a string")
    if not isinstance(multi, bool) or not isinstance(tou, bool):
        raise ValueError("Tariff modifiers must be boolean")
    if scheme == SCHEME_UNCONFIGURED and not multi and not tou:
        return {"scheme": scheme, "multi_person": multi, "tou": tou}
    if (scheme, multi, tou) not in GUANGZHOU_PROFILES:
        raise ValueError("Unsupported tariff combination")
    return {"scheme": scheme, "multi_person": multi, "tou": tou}


def resolve_tariff_profile(area_code: str, selection: Any) -> TariffProfile | None:
    """Missing, invalid or non-Guangzhou selections stay unconfigured."""
    if area_code != GUANGZHOU_AREA_CODE:
        return None
    try:
        choice = validate_tariff_selection(selection)
    except (ValueError, TypeError):
        return None
    return GUANGZHOU_PROFILES.get((choice["scheme"], choice["multi_person"], choice["tou"]))


def tou_period(now: dt.datetime) -> str:
    """Return the residential period in Asia/Shanghai; there is no sharp peak."""
    if now.tzinfo is None:
        raise ValueError("Tariff time must be timezone aware")
    hour = now.astimezone(_CSG_TIME_ZONE).hour
    if hour < 8:
        return "valley"
    if 10 <= hour < 12 or 14 <= hour < 19:
        return "peak"
    return "flat"


@dataclass(frozen=True)
class LadderSnapshot:
    tier: int
    remaining_kwh: Decimal | None
    start_date: str | None


def _decimal_usage(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("Invalid usage")
    try:
        usage = Decimal(str(value))
    except (InvalidOperation, ValueError) as err:
        raise ValueError("Invalid usage") from err
    if not usage.is_finite() or usage < 0:
        raise ValueError("Invalid usage")
    return usage


def current_ladder(
    profile: TariffProfile, today: dt.date, usage_total: Any,
    usage_days: Sequence[Mapping[str, Any]],
) -> LadderSnapshot:
    """Use authoritative total for tier; require complete daily dates for start."""
    if not profile.ladder_enabled:
        raise ValueError("Profile has no ladder")
    usage = _decimal_usage(usage_total)
    first, second = profile.thresholds(today.month)
    tier = 1 if usage <= first else 2 if usage <= second else 3
    remaining = max(Decimal(0), (first if tier == 1 else second) - usage) if tier < 3 else None
    daily = {}
    duplicate = False
    published = set()
    for row in usage_days:
        if not isinstance(row, Mapping):
            continue
        try:
            key = row.get("date")
            day = dt.date.fromisoformat(str(key))
        except ValueError:
            continue
        if day.isoformat() != key or (day.year, day.month) != (today.year, today.month) or day > today:
            continue
        if day in published:
            duplicate = True
        published.add(day)
        try:
            value = _decimal_usage(row.get("kwh"))
        except ValueError:
            continue
        daily[day] = value

    start_date = None
    # Only known, valid dates can establish coverage. A missing date before the
    # newest published day makes even a seemingly visible crossing uncertain.
    if daily and not duplicate and len(daily) == max(published).day:
        if tier == 1:
            start_date = today.replace(day=1).isoformat()
        else:
            threshold = first if tier == 2 else second
            cumulative = Decimal(0)
            for day, value in sorted(daily.items()):
                cumulative += value
                if cumulative > threshold:
                    start_date = day.isoformat()
                    break
    return LadderSnapshot(tier, remaining, start_date)
