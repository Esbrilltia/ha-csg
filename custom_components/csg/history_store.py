"""Persistent fact store for China Southern Power Grid history."""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import math
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .csg_client import WF_ATTR_DATE, WF_ATTR_KWH

_LOGGER = logging.getLogger(__name__)

HISTORY_STORAGE_KEY = f"{DOMAIN}.history_store"
HISTORY_STORAGE_VERSION = 1
DAILY_USAGE_SOURCE = "daily_usage_api"
MONTHLY_BILL_SOURCE = "year_month_stats"
_DAILY_VALUE_ABS_TOL = 1e-9
_RECONCILIATION_ABS_TOL_KWH = 0.01
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DailyUsageUpsertResult:
    """Describe the material changes made by a daily-usage upsert."""

    inserted_dates: tuple[str, ...]
    updated_dates: tuple[str, ...]
    unchanged_dates: tuple[str, ...]
    missing_dates: tuple[str, ...]
    coverage_changed: bool
    earliest_changed_date: str | None


class CSGHistoryStore:
    """Persist CSG-published facts without inventing missing history."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(
            hass,
            HISTORY_STORAGE_VERSION,
            f"{HISTORY_STORAGE_KEY}.{entry_id}",
        )
        self._data: dict[str, Any] = {"accounts": {}}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load persisted history facts."""
        self._data = await self._store.async_load() or {"accounts": {}}
        self._data.setdefault("accounts", {})

    async def async_upsert_daily_usage(
        self,
        account: str,
        month: tuple[int, int],
        days: Iterable[Mapping[str, Any]],
    ) -> DailyUsageUpsertResult:
        """Upsert valid daily usage facts for exactly one requested month.

        Missing, NaN, infinite, negative, malformed, and out-of-month values never
        delete an already stored fact. Conflicting duplicate values in the same
        response are rejected for that date rather than resolved by item order.
        """
        year, month_number = _validate_month(month)
        month_key = _month_key(year, month_number)

        candidates: dict[str, float] = {}
        conflicts: set[str] = set()

        for item in days:
            day_key = _validated_day_key(
                item.get(WF_ATTR_DATE), year, month_number
            )
            if day_key is None:
                continue

            value = _nonnegative_finite(item.get(WF_ATTR_KWH))
            if value is None:
                continue

            if day_key in conflicts:
                continue

            if day_key not in candidates:
                candidates[day_key] = value
                continue

            if not math.isclose(
                candidates[day_key],
                value,
                rel_tol=0.0,
                abs_tol=_DAILY_VALUE_ABS_TOL,
            ):
                conflicts.add(day_key)
                candidates.pop(day_key, None)
                _LOGGER.warning(
                    "Conflicting daily usage values for %s on %s; skipped date",
                    account,
                    day_key,
                )

        async with self._lock:
            account_data = self._account(account)
            daily_usage = account_data["daily_usage"]
            previous_coverage = deepcopy(
                account_data["daily_coverage"].get(month_key)
            )
            timestamp = _utcnow_iso()

            inserted: list[str] = []
            updated: list[str] = []
            unchanged: list[str] = []

            for day_key in sorted(candidates):
                value = candidates[day_key]
                existing = daily_usage.get(day_key)

                if existing is None:
                    daily_usage[day_key] = {
                        "kwh": value,
                        "source": DAILY_USAGE_SOURCE,
                        "updated_at": timestamp,
                    }
                    inserted.append(day_key)
                    continue

                existing_value = _nonnegative_finite(existing.get("kwh"))
                if existing_value is not None and math.isclose(
                    existing_value,
                    value,
                    rel_tol=0.0,
                    abs_tol=_DAILY_VALUE_ABS_TOL,
                ):
                    unchanged.append(day_key)
                    continue

                daily_usage[day_key] = {
                    "kwh": value,
                    "source": DAILY_USAGE_SOURCE,
                    "updated_at": timestamp,
                }
                updated.append(day_key)

            coverage = self._build_coverage(
                account_data, year, month_number
            )
            coverage_changed = coverage != previous_coverage
            account_data["daily_coverage"][month_key] = coverage

            if inserted or updated or coverage_changed:
                await self._store.async_save(self._data)

            changed_dates = inserted + updated
            return DailyUsageUpsertResult(
                inserted_dates=tuple(inserted),
                updated_dates=tuple(updated),
                unchanged_dates=tuple(unchanged),
                missing_dates=tuple(coverage["missing_days"]),
                coverage_changed=coverage_changed,
                earliest_changed_date=(
                    min(changed_dates) if changed_dates else None
                ),
            )

    async def async_upsert_monthly_bill(
        self,
        account: str,
        month: tuple[int, int],
        *,
        usage_kwh: float | None,
        cost_cny: float | None,
    ) -> bool:
        """Upsert an official monthly bill without erasing known values."""
        year, month_number = _validate_month(month)
        month_key = _month_key(year, month_number)
        usage = _nonnegative_finite(usage_kwh)
        cost = _nonnegative_finite(cost_cny)

        async with self._lock:
            account_data = self._account(account)
            bills = account_data["monthly_bills"]
            existing = bills.get(month_key)
            merged = dict(existing or {})

            if usage is not None:
                merged["usage_kwh"] = usage
            if cost is not None:
                merged["cost_cny"] = cost

            if not merged:
                return False

            fact_changed = (
                existing is None
                or merged.get("usage_kwh") != existing.get("usage_kwh")
                or merged.get("cost_cny") != existing.get("cost_cny")
            )
            if not fact_changed:
                return False

            merged["source"] = MONTHLY_BILL_SOURCE
            merged["updated_at"] = _utcnow_iso()
            bills[month_key] = merged
            await self._store.async_save(self._data)
            return True

    async def async_reconcile_month(
        self,
        account: str,
        month: tuple[int, int],
        *,
        today: dt.date | None = None,
    ) -> dict[str, Any]:
        """Compare complete daily usage with the official monthly usage fact."""
        year, month_number = _validate_month(month)
        month_key = _month_key(year, month_number)
        today = today or _csg_today()

        async with self._lock:
            account_data = self._account(account)
            coverage = self._build_coverage(
                account_data, year, month_number
            )
            account_data["daily_coverage"][month_key] = coverage

            daily_values = [
                float(row["kwh"])
                for day, row in account_data["daily_usage"].items()
                if _day_in_month(day, year, month_number)
                and _nonnegative_finite(row.get("kwh")) is not None
            ]
            daily_sum = math.fsum(daily_values)

            bill = account_data["monthly_bills"].get(month_key)
            billed_usage = (
                _nonnegative_finite(bill.get("usage_kwh"))
                if bill is not None
                else None
            )

            is_current_month = (
                today.year == year and today.month == month_number
            )
            difference: float | None = None

            if is_current_month or billed_usage is None:
                usage_state = "pending"
            elif coverage["state"] != "complete":
                usage_state = "not_comparable"
            else:
                difference = daily_sum - billed_usage
                usage_state = (
                    "matched"
                    if math.isclose(
                        difference,
                        0.0,
                        rel_tol=0.0,
                        abs_tol=_RECONCILIATION_ABS_TOL_KWH,
                    )
                    else "mismatch"
                )

            reconciliation = {
                "daily_sum_kwh": daily_sum,
                "billed_usage_kwh": billed_usage,
                "difference_kwh": difference,
                "usage_state": usage_state,
                "checked_at": _utcnow_iso(),
            }
            account_data["monthly_reconciliation"][
                month_key
            ] = reconciliation
            await self._store.async_save(self._data)
            return deepcopy(reconciliation)

    def daily_usage(
        self, account: str, day: str
    ) -> dict[str, Any] | None:
        """Return one stored daily usage fact."""
        row = self._account(account)["daily_usage"].get(day)
        return deepcopy(row) if row is not None else None

    def monthly_bill(
        self, account: str, month: tuple[int, int]
    ) -> dict[str, Any] | None:
        """Return one stored monthly bill fact."""
        year, month_number = _validate_month(month)
        row = self._account(account)["monthly_bills"].get(
            _month_key(year, month_number)
        )
        return deepcopy(row) if row is not None else None

    def daily_coverage(
        self, account: str, month: tuple[int, int]
    ) -> dict[str, Any] | None:
        """Return persisted coverage metadata for a month."""
        year, month_number = _validate_month(month)
        row = self._account(account)["daily_coverage"].get(
            _month_key(year, month_number)
        )
        return deepcopy(row) if row is not None else None

    def monthly_reconciliation(
        self, account: str, month: tuple[int, int]
    ) -> dict[str, Any] | None:
        """Return the latest reconciliation result for a month."""
        year, month_number = _validate_month(month)
        row = self._account(account)["monthly_reconciliation"].get(
            _month_key(year, month_number)
        )
        return deepcopy(row) if row is not None else None

    def _account(self, account: str) -> dict[str, Any]:
        account_data = self._data.setdefault("accounts", {}).setdefault(
            account, {}
        )
        account_data.setdefault("daily_usage", {})
        account_data.setdefault("monthly_bills", {})
        account_data.setdefault("daily_coverage", {})
        account_data.setdefault("monthly_reconciliation", {})
        account_data.setdefault(
            "sync",
            {
                "last_recent_sync": None,
                "last_history_sync": None,
            },
        )
        return account_data

    @staticmethod
    def _build_coverage(
        account_data: dict[str, Any],
        year: int,
        month_number: int,
    ) -> dict[str, Any]:
        expected = _expected_dates(year, month_number)
        present = {
            day
            for day, row in account_data["daily_usage"].items()
            if day in expected
            and _nonnegative_finite(row.get("kwh")) is not None
        }
        missing = sorted(expected - present)
        valid = sorted(present)

        if not valid:
            state = "empty"
        elif not missing:
            state = "complete"
        else:
            state = "partial"

        return {
            "state": state,
            "expected_days": len(expected),
            "valid_days": len(valid),
            "missing_days": missing,
            "first_valid_date": valid[0] if valid else None,
            "last_valid_date": valid[-1] if valid else None,
        }


def _validate_month(month: tuple[int, int]) -> tuple[int, int]:
    year, month_number = month
    if year < 1 or not 1 <= month_number <= 12:
        raise ValueError(f"Invalid month: {month!r}")
    return year, month_number


def _month_key(year: int, month_number: int) -> str:
    return f"{year:04d}-{month_number:02d}"


def _validated_day_key(
    value: Any, year: int, month_number: int
) -> str | None:
    if value is None:
        return None
    try:
        day = dt.date.fromisoformat(str(value))
    except ValueError:
        _LOGGER.warning("Skipped malformed daily usage date: %r", value)
        return None
    if day.year != year or day.month != month_number:
        _LOGGER.warning(
            "Skipped daily usage date %s outside requested month %04d-%02d",
            day.isoformat(),
            year,
            month_number,
        )
        return None
    return day.isoformat()


def _nonnegative_finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _expected_dates(year: int, month_number: int) -> set[str]:
    days = calendar.monthrange(year, month_number)[1]
    return {
        dt.date(year, month_number, day).isoformat()
        for day in range(1, days + 1)
    }


def _day_in_month(
    day_key: str, year: int, month_number: int
) -> bool:
    try:
        day = dt.date.fromisoformat(day_key)
    except ValueError:
        return False
    return day.year == year and day.month == month_number


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _csg_today() -> dt.date:
    return dt.datetime.now(dt.UTC).astimezone(_CSG_TIME_ZONE).date()
