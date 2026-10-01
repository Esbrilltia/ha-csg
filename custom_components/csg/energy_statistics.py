"""Converge durable CSG daily facts into Recorder external energy statistics."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData, StatisticMeanType, StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, get_metadata, statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.unit_conversion import EnergyConverter

from .const import CONF_ELE_ACCOUNTS, CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS, DOMAIN
from .csg_client import CSGElectricityAccount
from .history_store import CSGHistoryStore

_LOGGER = logging.getLogger(__name__)
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_ABS_TOL = 1e-9
# Read the entire series, including rows outside the Store's known date range,
# so an extra earlier/later row cannot escape the non-destructive anomaly gate.
_QUERY_START = dt.datetime.min.replace(tzinfo=dt.UTC)


def statistic_metadata(account_number: str) -> StatisticMetaData:
    """Return stable, non-sensitive identity and the HA 2026.9.3 metadata."""
    digest = hashlib.sha256(account_number.encode("utf-8")).hexdigest()
    return StatisticMetaData(
        source=DOMAIN,
        statistic_id=f"{DOMAIN}:energy_{digest}",
        name=f"CSG energy {digest[:8]}",
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        unit_class=EnergyConverter.UNIT_CLASS,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
    )


def build_statistics(facts: Mapping[str, Mapping[str, Any]]) -> list[StatisticData]:
    """Build only known CSG days, including real zero, with Decimal accumulation."""
    total = Decimal(0)
    statistics = []
    for key, fact in sorted(facts.items()):
        day = dt.date.fromisoformat(key)
        if day.isoformat() != key or isinstance(fact["kwh"], bool):
            raise ValueError("Malformed daily fact")
        value = Decimal(str(fact["kwh"]))
        if not value.is_finite() or value < 0:
            raise ValueError("Invalid daily energy value")
        total += value
        state, cumulative = float(value), float(total)
        if not math.isfinite(state) or not math.isfinite(cumulative):
            raise ValueError("Daily energy exceeds Recorder numeric range")
        statistics.append(StatisticData(
            start=dt.datetime.combine(day, dt.time(), _CSG_TIME_ZONE),
            state=state, sum=cumulative,
        ))
    return statistics


def _compatible(actual: StatisticMetaData, desired: StatisticMetaData) -> bool:
    return all(actual.get(key) == desired[key] for key in (
        "source", "statistic_id", "unit_of_measurement", "unit_class", "mean_type",
    )) and actual.get("has_sum") is True


def _number(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _different_suffix(
    desired: Sequence[StatisticData], actual: Sequence[Mapping[str, Any]],
) -> list[StatisticData]:
    """Reject unsafe actual rows; otherwise return the earliest different suffix."""
    expected = {row["start"].timestamp() for row in desired}
    indexed = {}
    for row in actual:
        start, state, cumulative = row.get("start"), row.get("state"), row.get("sum")
        if not all(_number(value) for value in (start, state, cumulative)):
            raise ValueError("Malformed Recorder row")
        if start not in expected:
            raise ValueError("Recorder has a row without a corresponding daily fact")
        if start in indexed:
            raise ValueError("Duplicate Recorder start")
        indexed[start] = row
    for index, row in enumerate(desired):
        current = indexed.get(row["start"].timestamp())
        if current is None or any(not math.isclose(
            current[key], row[key], rel_tol=0, abs_tol=_ABS_TOL,
        ) for key in ("state", "sum")):
            return list(desired[index:])
    return []


class EnergyStatisticsBridge:
    """One entry's optional convergence worker, with no persistent checkpoint."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: CSGHistoryStore) -> None:
        self.hass = hass
        self.entry = entry
        self.history_store = store
        self.enabled = entry.data.get(CONF_SETTINGS, {}).get(CONF_ENERGY_STATISTICS_ENABLED, False) is True
        self._task: asyncio.Task[None] | None = None
        self._pending = False
        self._shutdown = False

    @callback
    def request_sync(self) -> None:
        """Collapse all requests during a pass into one following pass."""
        if not self.enabled or self._shutdown:
            return
        self._pending = True
        if self._task is None or self._task.done():
            self._task = self.entry.async_create_background_task(
                self.hass, self._async_run(), "CSG external energy statistics", eager_start=False,
            )

    async def _async_run(self) -> None:
        try:
            while self._pending and not self._shutdown:
                self._pending = False
                try:
                    await self._async_sync()
                except Exception:
                    _LOGGER.warning("CSG energy statistics pass unavailable", exc_info=True)
        finally:
            self._task = None

    async def async_shutdown(self) -> None:
        """Close requests and cancel comparisons; Recorder owns queued imports."""
        self._shutdown = True
        self._pending = False
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not task.cancelled():
                raise

    async def _async_sync(self) -> None:
        if not self.enabled or self._shutdown:
            return
        if not await self.history_store.async_ensure_persisted():
            _LOGGER.warning("CSG energy facts are not confirmed durable; import deferred")
            return
        recorder = get_instance(self.hass)
        if not recorder.async_db_ready.done() or not recorder.async_db_ready.result():
            _LOGGER.warning("CSG energy statistics Recorder database is not ready")
            return
        for value in self.entry.data[CONF_ELE_ACCOUNTS].values():
            # The stored account object's number is the identity, rather than
            # entry_id or a display label in the config-entry mapping.
            account = CSGElectricityAccount.load(value).account_number
            metadata = statistic_metadata(account)
            statistic_id = metadata["statistic_id"]
            try:
                desired = build_statistics(await self.history_store.async_daily_usage_snapshot(account))
                existing = await recorder.async_add_executor_job(partial(
                    get_metadata, self.hass, statistic_ids={statistic_id},
                ))
                current_meta = existing[statistic_id][1] if statistic_id in existing else None
                if current_meta is not None and not _compatible(current_meta, metadata):
                    _LOGGER.warning("Incompatible external statistics metadata for %s; skipped", statistic_id)
                    continue
                actual = await recorder.async_add_executor_job(
                    statistics_during_period, self.hass, _QUERY_START, None,
                    {statistic_id}, "hour", {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR},
                    {"state", "sum"},
                )
                suffix = _different_suffix(desired, actual.get(statistic_id, []))
                if self._shutdown:
                    return
                if suffix or (current_meta is not None and current_meta.get("name") != metadata["name"]):
                    async_add_external_statistics(self.hass, metadata, suffix)
            except Exception:
                _LOGGER.warning("CSG external energy statistics unsafe or unavailable for %s; skipped", statistic_id, exc_info=True)
