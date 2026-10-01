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
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .csg_client import WF_ATTR_DATE, WF_ATTR_KWH
from .history_helpers import parse_history_start_month
from .history_io import HistoryStorageHass

_LOGGER = logging.getLogger(__name__)

HISTORY_STORAGE_KEY = f"{DOMAIN}.history_store"
HISTORY_STORAGE_VERSION = 1
DAILY_USAGE_SOURCE = "daily_usage_api"
MONTHLY_BILL_SOURCE = "year_month_stats"
_DAILY_VALUE_ABS_TOL = 1e-9
_RECONCILIATION_ABS_TOL_KWH = Decimal("0.01")
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
        # A separate public reader cannot return the writer's pending data.
        self._verification_store = Store(
            hass,
            HISTORY_STORAGE_VERSION,
            f"{HISTORY_STORAGE_KEY}.{entry_id}",
            read_only=True,
        )
        # Construct real Stores with real HA first, so the shared storage manager
        # remains attached to HA. Only these two Stores receive the I/O delegate.
        storage_hass = HistoryStorageHass(hass, f"{HISTORY_STORAGE_KEY}.{entry_id}")
        self._store.hass = storage_hass
        self._verification_store.hass = storage_hass
        self._persistence_pending = False
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

        candidate_sets: dict[str, set[float]] = {}

        for item in days:
            day_key = _validated_day_key(
                item.get(WF_ATTR_DATE), year, month_number
            )
            if day_key is None:
                continue

            value = _nonnegative_finite(item.get(WF_ATTR_KWH))
            if value is None:
                continue

            candidate_sets.setdefault(day_key, set()).add(value)

        candidates: dict[str, float] = {}
        for day_key, values in candidate_sets.items():
            # Approximate equality is not transitive. The full range must fit
            # the tolerance, independent of which candidate arrived first.
            smallest = min(values)
            if not math.isclose(
                smallest,
                max(values),
                rel_tol=0.0,
                abs_tol=_DAILY_VALUE_ABS_TOL,
            ):
                _LOGGER.warning(
                    "Conflicting daily usage values for %s on %s; skipped date",
                    account,
                    day_key,
                )
                continue
            # Choose an actual source value, not an average or first arrival.
            candidates[day_key] = smallest

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
                self._persistence_pending = True
            await self._async_save_pending()

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
                await self._async_save_pending()
                return False

            fact_changed = (
                existing is None
                or merged.get("usage_kwh") != existing.get("usage_kwh")
                or merged.get("cost_cny") != existing.get("cost_cny")
            )
            if not fact_changed:
                await self._async_save_pending()
                return False

            merged["source"] = MONTHLY_BILL_SOURCE
            merged["updated_at"] = _utcnow_iso()
            bills[month_key] = merged
            self._persistence_pending = True
            await self._async_save_pending()
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
                # Compare decimal business values, not a float subtraction
                # artifact at the inclusive 0.01 kWh boundary. Keep the stored
                # difference as a float for compatibility.
                decimal_difference = sum(
                    (Decimal(str(value)) for value in daily_values), Decimal(0)
                ) - Decimal(str(billed_usage))
                usage_state = (
                    "matched"
                    if abs(decimal_difference) <= _RECONCILIATION_ABS_TOL_KWH
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
            self._persistence_pending = True
            await self._async_save_pending()
            return deepcopy(reconciliation)

    async def _async_save_pending(self) -> None:
        """Verify writes using public Store APIs; caller holds the fact lock.

        HA 2024.12.5 logs and swallows WriteError. A normal save return is not
        confirmation. Its write path invalidates the shared read cache before
        writing, so the independent reader checks the persisted payload. Dirty
        facts remain in memory for a later upsert to retry without a fabricated
        revision. Confirmed no-op refetches incur no disk reads or writes.
        """
        if not self._persistence_pending:
            return
        if await self._async_save_verified(deepcopy(self._data)):
            self._persistence_pending = False

    async def async_ensure_persisted(self) -> bool:
        """Flush facts and confirm with the independent Store reader.

        Return False on unconfirmed readback; raised save errors also require a
        retry. Neither outcome permits a historical checkpoint to advance.
        """
        async with self._lock:
            await self._async_save_pending()
            return not self._persistence_pending

    async def async_daily_usage_snapshot(self, account: str) -> dict[str, dict[str, Any]]:
        """Return detached daily facts under the lock without creating account data.

        A write can begin after async_ensure_persisted releases its lock. Reject
        an unconfirmed newer payload here so consumers never publish such facts.
        """
        async with self._lock:
            if self._persistence_pending:
                raise HomeAssistantError("Daily usage snapshot is not confirmed durable")
            return deepcopy(self._data["accounts"].get(account, {}).get("daily_usage", {}))

    def history_progress(self, account: str) -> dict[str, Any]:
        """Return confirmed checkpoints only, detached from the mutable payload."""
        sync = self._account(account).get("sync")
        if not isinstance(sync, Mapping):
            _LOGGER.warning("Invalid history sync metadata; treating units as incomplete")
            return {}
        return _normalize_history_progress(sync.get("history_backfill", {}))

    async def async_complete_history_unit(
        self,
        account: str,
        *,
        daily_month: tuple[int, int] | None = None,
        bill_year: int | None = None,
        bill_months: Iterable[tuple[int, int]] = (),
    ) -> bool:
        """Commit a checkpoint only after facts are durable, then verify it.

        Bill checkpoints retain the requested scope so extensions within the same
        year remain discoverable. Failed or cancelled saves never install an
        unconfirmed checkpoint in memory; a durable checkpoint always follows
        durable facts, even if cancellation interrupts its acknowledgement.
        """
        if (daily_month is None) == (bill_year is None):
            raise ValueError("Specify exactly one history lane")
        daily_key = (
            _month_key(*_validate_month(daily_month)) if daily_month is not None else None
        )
        scope = sorted({_month_key(*_validate_month(month)) for month in bill_months})
        if bill_year is not None:
            dt.date(bill_year, 1, 1)
            if not scope or any(int(key[:4]) != bill_year for key in scope):
                raise ValueError("Bill scope must belong to the requested year")

        async with self._lock:
            self._account(account)
            await self._async_save_pending()
            if self._persistence_pending:
                return False
            payload = deepcopy(self._data)
            account_data = payload["accounts"][account]
            if not isinstance(account_data.get("sync"), Mapping):
                account_data["sync"] = {}
            sync = account_data["sync"]
            progress = _normalize_history_progress(sync.get("history_backfill", {}))
            sync["history_backfill"] = progress
            daily = set(progress.get("completed_daily_months", []))
            years = set(progress.get("completed_bill_years", []))
            scopes = progress.setdefault("bill_year_scopes", {})
            if daily_key is not None:
                daily.add(daily_key)
            else:
                years.add(bill_year)
                key = str(bill_year)
                scopes[key] = sorted(set(scopes.get(key, [])) | set(scope))
            progress["completed_daily_months"] = sorted(daily)
            progress["completed_bill_years"] = sorted(years)
            progress["last_completed_at"] = _utcnow_iso()
            sync["last_history_sync"] = progress["last_completed_at"]
            if not await self._async_save_verified(payload):
                return False
            self._data = payload
            return True

    async def _async_save_verified(self, payload: dict[str, Any]) -> bool:
        """Drain ordinary unload; hand global-stop ownership to the worker lane."""
        task = asyncio.create_task(self._async_write_and_verify(payload))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if getattr(getattr(self._store, "hass", None), "is_stopping", False):
                    # Core may stop waiting while a disk worker still runs. The
                    # path's worker lane retains ordering until actual I/O ends;
                    # cancel the coroutine promptly, without accepting a result
                    # or installing a checkpoint after global cancellation.
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        _LOGGER.exception("History storage task failed during global stop")
                    raise
                cancelled = True
            except Exception:
                break
        if cancelled:
            # Retrieve any exception before propagating cancellation.
            try:
                task.result()
            finally:
                raise asyncio.CancelledError
        return task.result()

    async def _async_write_and_verify(self, payload: dict[str, Any]) -> bool:
        """Save one snapshot and verify using public Store APIs."""
        await self._store.async_save(payload)
        try:
            persisted = await self._verification_store.async_load()
        except (HomeAssistantError, OSError):
            _LOGGER.exception("Could not verify history persistence; keeping save pending")
            return False
        if persisted == payload:
            return True
        _LOGGER.warning("History persistence not confirmed; keeping save pending")
        return False

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


def _normalize_history_progress(raw: Any) -> dict[str, Any]:
    """Retain only independently valid completion members; facts are untouched."""
    if not isinstance(raw, Mapping):
        _LOGGER.warning("Invalid history backfill metadata; treating units as incomplete")
        return {}
    if not raw:
        return {}
    invalid = False

    def months(value: Any, year: int | None = None) -> list[str]:
        nonlocal invalid
        if not isinstance(value, list):
            invalid = True
            return []
        accepted = set()
        for item in value:
            try:
                parsed_year, _ = parse_history_start_month(item)
            except ValueError:
                invalid = True
                continue
            if year is not None and parsed_year != year:
                invalid = True
                continue
            accepted.add(item)
        return sorted(accepted)

    daily = months(raw.get("completed_daily_months", []))
    raw_years = raw.get("completed_bill_years", [])
    years = set()
    if isinstance(raw_years, list):
        for item in raw_years:
            if type(item) is int and 1 <= item <= 9999:
                years.add(item)
            else:
                invalid = True
    else:
        invalid = True
    scopes = {}
    raw_scopes = raw.get("bill_year_scopes", {})
    if isinstance(raw_scopes, Mapping):
        for key, value in raw_scopes.items():
            if (
                not isinstance(key, str) or not 1 <= len(key) <= 4
                or not key.isascii() or not key.isdecimal()
                or not 1 <= int(key) <= 9999 or str(int(key)) != key
                or int(key) not in years
            ):
                invalid = True
                continue
            scopes[key] = months(value, int(key))
    else:
        invalid = True
    progress = {
        "completed_daily_months": daily,
        "completed_bill_years": sorted(years),
        "bill_year_scopes": scopes,
    }
    if isinstance(raw.get("last_completed_at"), str):
        progress["last_completed_at"] = raw["last_completed_at"]
    if invalid:
        _LOGGER.warning("Invalid history completion metadata; invalid members remain retryable")
    return progress
