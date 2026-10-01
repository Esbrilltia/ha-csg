"""One sequential, resumable historical fact pass per config entry load."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_HISTORY_START_MONTH,
    CONF_SETTINGS,
    SETTING_UPDATE_TIMEOUT,
)
from .csg_client import CSGClient, CSGElectricityAccount
from .history_helpers import (
    collect_monthly_bill_candidates,
    month_key,
    parse_history_start_month,
)
from .history_store import CSGHistoryStore

_LOGGER = logging.getLogger(__name__)
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")


def historical_months(start: str, now: dt.datetime) -> list[tuple[int, int]]:
    """Return newest-first closed CSG months within the explicit user bound."""
    first = parse_history_start_month(start)
    today = now.astimezone(_CSG_TIME_ZONE).date()
    end = today.replace(day=1) - dt.timedelta(days=1)
    month = (end.year, end.month)
    months = []
    while month >= first:
        months.append(month)
        if month == first:
            break
        month = (month[0] - 1, 12) if month[1] == 1 else (month[0], month[1] - 1)
    return months


class HistoryCoordinator:
    """Collect historical facts without entities, timers, or ledger dependencies."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, store: CSGHistoryStore
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.history_store = store
        self._task: asyncio.Task[None] | None = None
        self._shutdown = False

    def start(self) -> asyncio.Task[None] | None:
        """Start at most one background pass for this lifecycle."""
        start = self.entry.data.get(CONF_SETTINGS, {}).get(CONF_HISTORY_START_MONTH)
        if self._shutdown or not start:
            return None
        if self._task is None:
            self._task = self.entry.async_create_background_task(
                self.hass,
                self._async_sync(),
                f"CSG history {self.entry.entry_id}",
                eager_start=False,
            )
        return self._task

    async def async_shutdown(self) -> None:
        """Cancel the pass and drain its current executor/storage operation."""
        self._shutdown = True
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _fetch(self, function: Any, *args: Any) -> Any:
        """Bound a request while retaining ownership of in-flight executor I/O."""
        if self._shutdown:
            raise asyncio.CancelledError
        job = asyncio.ensure_future(self.hass.async_add_executor_job(function, *args))
        try:
            async with asyncio.timeout(SETTING_UPDATE_TIMEOUT):
                return await asyncio.shield(job)
        except (asyncio.CancelledError, TimeoutError) as err:
            cancelled = isinstance(err, asyncio.CancelledError)
            # Executor threads cannot be cancelled. Drain before unload/reload or
            # another historical request, without consuming their returned facts.
            while not job.done():
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    cancelled = True
                    continue
                except Exception:
                    break
            if not job.cancelled():
                job.exception()
            if cancelled:
                raise asyncio.CancelledError
            raise

    async def _async_sync(self) -> None:
        start = self.entry.data.get(CONF_SETTINGS, {}).get(CONF_HISTORY_START_MONTH)
        if not start or self._shutdown:
            return
        try:
            months = historical_months(start, dt_util.utcnow())
        except ValueError:
            _LOGGER.warning("Invalid history start month; history pass disabled")
            return
        years = sorted({month[0] for month in months}, reverse=True)
        scopes = {year: [month for month in months if month[0] == year] for year in years}
        accounts = [
            CSGElectricityAccount.load(value)
            for value in self.entry.data[CONF_ELE_ACCOUNTS].values()
        ]
        pending = []
        for account in accounts:
            progress = self.history_store.history_progress(account.account_number)
            done_daily = set(progress.get("completed_daily_months", []))
            done_scopes = progress.get("bill_year_scopes", {})
            daily = [month for month in months if month_key(month) not in done_daily]
            bills = [
                year for year in years
                if not {month_key(month) for month in scopes[year]}.issubset(
                    done_scopes.get(str(year), [])
                )
            ]
            if daily or bills:
                pending.append((account, daily, bills))
        if not pending:
            return
        try:
            client = await self._fetch(
                CSGClient.load, {CONF_AUTH_TOKEN: self.entry.data[CONF_AUTH_TOKEN]}
            )
            if not await self._fetch(client.verify_login):
                raise ConfigEntryAuthFailed("Login expired")
            await self._fetch(client.initialize)
        except Exception:
            _LOGGER.exception("Could not initialize historical sync; checkpoints retained")
            return

        for account, daily, bills in pending:
            for month in daily:
                try:
                    _, rows = await self._fetch(
                        client.get_month_daily_usage_detail, account, month
                    )
                    await self.history_store.async_upsert_daily_usage(
                        account.account_number, month, rows
                    )
                    await self.history_store.async_reconcile_month(
                        account.account_number, month
                    )
                    if not await self.history_store.async_ensure_persisted():
                        _LOGGER.warning(
                            "Historical daily facts not durable for %s/%s",
                            account.account_number, month_key(month),
                        )
                        continue
                    if not await self.history_store.async_complete_history_unit(
                        account.account_number, daily_month=month
                    ):
                        _LOGGER.warning(
                            "Historical daily checkpoint not confirmed for %s/%s",
                            account.account_number, month_key(month),
                        )
                except Exception:
                    _LOGGER.exception(
                        "Historical daily unit failed for %s/%s; remains retryable",
                        account.account_number, month_key(month),
                    )
            for year in bills:
                try:
                    _, _, rows = await self._fetch(client.get_year_month_stats, account, year)
                    candidates = collect_monthly_bill_candidates(
                        rows, account.account_number, year
                    )
                    for month, values in candidates.items():
                        if month not in scopes[year] or values == (None, None):
                            continue
                        await self.history_store.async_upsert_monthly_bill(
                            account.account_number, month,
                            usage_kwh=values[0], cost_cny=values[1],
                        )
                        await self.history_store.async_reconcile_month(
                            account.account_number, month
                        )
                    if not await self.history_store.async_ensure_persisted():
                        _LOGGER.warning(
                            "Historical bill facts not durable for %s/%s",
                            account.account_number, year,
                        )
                        continue
                    if not await self.history_store.async_complete_history_unit(
                        account.account_number, bill_year=year, bill_months=scopes[year]
                    ):
                        _LOGGER.warning(
                            "Historical bill checkpoint not confirmed for %s/%s",
                            account.account_number, year,
                        )
                except Exception:
                    _LOGGER.exception(
                        "Historical bill unit failed for %s/%s; remains retryable",
                        account.account_number, year,
                    )
