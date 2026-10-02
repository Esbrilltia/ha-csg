"""Converge durable CSG daily usage and official monthly cost into Recorder."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from dataclasses import dataclass, field
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
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.unit_conversion import EnergyConverter
from homeassistant.util import dt as dt_util

from .const import CONF_ELE_ACCOUNTS, CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS, DEFAULT_ENERGY_STATISTICS_ENABLED, DOMAIN
from .csg_client import CSGElectricityAccount
from .cost_statistics import build_cost_statistics, cost_statistic_metadata
from .history_store import CSGHistoryStore

_LOGGER = logging.getLogger(__name__)
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_ABS_TOL = 1e-9
# Read the entire series, including rows outside the Store's known date range,
# so an extra earlier/later row cannot escape the non-destructive anomaly gate.
_QUERY_START = dt.datetime.min.replace(tzinfo=dt.UTC)
_IMPORT_LANES = "csg_energy_import_lanes"
_CONFIRMATION_TIMEOUT = 30
_FINALIZATION_TIMEOUT = 60


def _statistic_units(statistic_id: str) -> dict[str, str] | None:
    """Cost has no unit conversion; energy retains its existing kWh semantics."""
    if statistic_id.startswith(f"{DOMAIN}:cost_"):
        return None
    return {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR}


def _csg_today() -> dt.date:
    return dt_util.utcnow().astimezone(_CSG_TIME_ZONE).date()


@dataclass
class _ImportLane:
    """In-memory ownership survives entry runtime removal, never process exit."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    target: tuple[StatisticMetaData, list[StatisticData]] | None = None


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
            raise ValueError("Recorder has a row without a corresponding source fact")
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
        self.enabled = entry.data.get(CONF_SETTINGS, {}).get(CONF_ENERGY_STATISTICS_ENABLED, DEFAULT_ENERGY_STATISTICS_ENABLED) is True
        self._task: asyncio.Task[None] | None = None
        self._pending = False
        self._shutdown = False
        self._finalizing = False
        self._accepting = True
        self._owned: set[str] = set()
        self._currency_warned = False
        self._lanes: dict[str, _ImportLane] = hass.data.setdefault(_IMPORT_LANES, {})
        self._unsubscribe = None
        if self.enabled:
            self._unsubscribe = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)
            entry.async_on_unload(self._unsubscribe_stop)

    @callback
    def _unsubscribe_stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    @callback
    def stop_requests(self) -> None:
        """Close producers' request gate before their shutdown/drain."""
        self._accepting = False
        self._pending = False

    @callback
    def _on_stop(self, event) -> None:
        """A dying HA process must not wait indefinitely for its Recorder."""
        self.stop_requests()
        self._shutdown = True
        if self._task is not None:
            self._task.cancel()

    @callback
    def request_sync(self) -> None:
        """Collapse all requests during a pass into one following pass."""
        if not self.enabled or not self._accepting or self._shutdown:
            return
        self._pending = True
        if self._task is None or self._task.done():
            self._task = self.entry.async_create_background_task(
                self.hass, self._async_run(), "CSG external statistics", eager_start=False,
            )

    async def _async_run(self) -> None:
        try:
            while self._pending and not self._shutdown:
                self._pending = False
                try:
                    await self._async_sync()
                except Exception:
                    _LOGGER.warning("CSG external statistics pass unavailable", exc_info=True)
        finally:
            self._task = None

    async def async_shutdown(self) -> None:
        """Converge after producer drain, even when no old import is outstanding."""
        self.stop_requests()
        if self._shutdown:
            return
        task = self._task
        if not self.enabled or self.hass.is_stopping:
            self._shutdown = True
            if task is not None and not task.done():
                task.cancel()
        try:
            async with asyncio.timeout(_FINALIZATION_TIMEOUT):
                if task is not None:
                    await asyncio.shield(task)
                if not self._shutdown:
                    # Producers are quiescent. External admission stays closed;
                    # this unconditional internal pass ignores pending/ownership
                    # state and reuses the exact A-1 lane/readback protocol.
                    self._finalizing = True
                    task = self._task = self.entry.async_create_background_task(
                        self.hass, self._async_sync(),
                        "CSG final external energy statistics", eager_start=False,
                    )
                    await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                if task is not None:
                    task.cancel()
                raise
            if not self._shutdown or task is None or not task.cancelled():
                raise
        except BaseException:
            if task is not None and not task.done():
                task.cancel()
            raise
        finally:
            self._shutdown = True
            self._finalizing = False
            self._unsubscribe_stop()
            if task is not None and not task.done():
                # Give cancellation a turn, never an unbounded join. Store I/O
                # can drain physical ownership despite cancellation. Retain its
                # waiter and lane lock until it actually exits; shutdown prevents
                # it from enqueueing a fresh import after a failed finalization.
                await asyncio.sleep(0)
            if task is None or task.done():
                self._task = None
            else:
                task.add_done_callback(self._final_task_done)

    @callback
    def _final_task_done(self, task: asyncio.Task) -> None:
        if self._task is task:
            self._task = None
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _async_actual(self, recorder, statistic_id):
        self._require_cost_currency(statistic_id)
        existing = await recorder.async_add_executor_job(partial(
            get_metadata, self.hass, statistic_ids={statistic_id},
        ))
        metadata = existing[statistic_id][1] if statistic_id in existing else None
        self._require_cost_currency(statistic_id)
        actual = await recorder.async_add_executor_job(
            statistics_during_period, self.hass, _QUERY_START, None,
            {statistic_id}, "hour", _statistic_units(statistic_id),
            {"state", "sum"},
        )
        return metadata, actual.get(statistic_id, [])

    def _cost_allowed(self) -> bool:
        if getattr(getattr(self.hass, "config", None), "currency", None) == "CNY":
            return True
        if not self._currency_warned:
            self._currency_warned = True
            _LOGGER.warning(
                "CSG official costs are CNY; external cost statistics require HA currency CNY. "
                "Cost access is paused and existing statistics are retained; reload after changing currency",
            )
        return False

    def _require_cost_currency(self, statistic_id: str) -> None:
        if statistic_id.startswith(f"{DOMAIN}:cost_") and not self._cost_allowed():
            raise RuntimeError("CNY cost statistics access is paused")

    async def _async_confirm(self, recorder, lane: _ImportLane) -> None:
        """Read back the differing import target, never just the latest Store.

        One lane permits only one import in flight per statistic. HA imports
        commit their own transaction and retry by requeuing on failure. Seeing
        the complete target (including its previously different row/name) is
        the completion evidence. A queue/commit future is only a pacing hint:
        it can miss an executing import or precede a requeued retry.
        """
        # An unavailable/dropped import must not hold ordinary unload forever.
        # Timeout defers this account and RETAINS its ownership; it never proves
        # completion or permits a fresh comparison to bypass the old target.
        async with asyncio.timeout(_CONFIRMATION_TIMEOUT):
            await self._async_read_back(recorder, lane)

    async def _async_read_back(self, recorder, lane: _ImportLane) -> None:
        while lane.target is not None:
            # Core sets stop_requested only AFTER processing startup tasks;
            # even async_recorder_ready can precede that initialization. The
            # public thread liveness API is valid throughout that interval.
            if self.hass.is_stopping or not recorder.is_alive():
                raise RuntimeError("Recorder is stopping with an unconfirmed import")
            await recorder.async_block_till_done()
            metadata, desired = lane.target
            current, actual = await self._async_actual(recorder, metadata["statistic_id"])
            if current is not None and not _compatible(current, metadata):
                raise ValueError("Unconfirmed import has incompatible Recorder metadata")
            if current is not None and _compatible(current, metadata) and current.get("name") == metadata["name"]:
                if not _different_suffix(desired, actual):
                    lane.target = None
                    return
            # Retry pacing only; elapsed time never confirms an import.
            await asyncio.sleep(0.05)

    async def _async_sync(self) -> None:
        require_convergence = self._finalizing
        if not self.enabled or self._shutdown:
            return
        cost_enabled = self._cost_allowed()
        if not await self.history_store.async_ensure_persisted():
            if require_convergence:
                raise RuntimeError("Final facts are not confirmed durable")
            _LOGGER.warning("CSG facts are not confirmed durable; import deferred")
            return
        recorder = get_instance(self.hass)
        if not recorder.async_db_ready.done() or not recorder.async_db_ready.result():
            if require_convergence:
                raise RuntimeError("Final external statistics Recorder database is not ready")
            _LOGGER.warning("CSG external statistics Recorder database is not ready")
            return
        for value in self.entry.data[CONF_ELE_ACCOUNTS].values():
            # The stored account object's number is the identity, rather than
            # entry_id or a display label in the config-entry mapping.
            account = CSGElectricityAccount.load(value).account_number
            targets = [statistic_metadata(account)]
            if cost_enabled:
                targets.append(cost_statistic_metadata(account))
            for metadata in targets:
                await self._async_sync_statistic(recorder, account, metadata, require_convergence)

    async def _async_sync_statistic(
        self, recorder, account: str, metadata: StatisticMetaData, require_convergence: bool,
    ) -> None:
        """Reuse the audited lane/readback protocol independently for each ID."""
        statistic_id = metadata["statistic_id"]
        try:
            lane = self._lanes.setdefault(statistic_id, _ImportLane())
            async with lane.lock:
                # A new/reloaded Bridge inherits any unconfirmed old target
                # before it is allowed to compare against its latest Store.
                if lane.target is not None:
                    await self._async_confirm(recorder, lane)
                while not self._shutdown:
                    if not await self.history_store.async_ensure_persisted():
                        if require_convergence:
                            raise RuntimeError("Final facts are not confirmed durable")
                        return
                    if statistic_id.startswith(f"{DOMAIN}:cost_"):
                        desired = build_cost_statistics(
                            await self.history_store.async_monthly_bills_snapshot(account),
                            _csg_today(),
                        )
                    else:
                        desired = build_statistics(await self.history_store.async_daily_usage_snapshot(account))
                    self._require_cost_currency(statistic_id)
                    existing = await recorder.async_add_executor_job(partial(
                        get_metadata, self.hass, statistic_ids={statistic_id},
                    ))
                    current_meta = existing[statistic_id][1] if statistic_id in existing else None
                    if current_meta is not None and not _compatible(current_meta, metadata):
                        if require_convergence:
                            raise ValueError("Final statistics metadata is incompatible")
                        _LOGGER.warning("Incompatible external statistics metadata for %s; skipped", statistic_id)
                        break
                    self._require_cost_currency(statistic_id)
                    actual = await recorder.async_add_executor_job(
                        statistics_during_period, self.hass, _QUERY_START, None,
                        {statistic_id}, "hour", _statistic_units(statistic_id),
                        {"state", "sum"},
                    )
                    suffix = _different_suffix(desired, actual.get(statistic_id, []))
                    if self._shutdown:
                        return
                    if not suffix and (current_meta is None or current_meta.get("name") == metadata["name"]):
                        break
                    self._require_cost_currency(statistic_id)
                    async_add_external_statistics(self.hass, metadata, suffix)
                    # No await between public enqueue and ownership capture.
                    lane.target = (metadata, desired)
                    self._owned.add(statistic_id)
                    await self._async_confirm(recorder, lane)
                    # Always reread durable Store after confirmation, even
                    # without another request or while ordinary unload drains.
        except Exception:
            if require_convergence:
                raise
            _LOGGER.warning("CSG external statistics unsafe or unavailable for %s; skipped", statistic_id, exc_info=True)
