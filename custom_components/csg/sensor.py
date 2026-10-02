"""Sensors for the China Southern Power Grid integration."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import requests
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, STATE_UNAVAILABLE, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.components import persistent_notification
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    ATTR_KEY_CURRENT_LADDER_START_DATE,
    ATTR_KEY_MONTH_BILLING_DELAY,
    ATTR_KEY_SETTLEMENT_DATE,
    ATTR_KEY_YEAR_BILLING_DELAY,
    CONF_AUTH_TOKEN,
    CONF_BILLING_UPDATE_TIME,
    CONF_ELE_ACCOUNTS,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    SETTING_UPDATE_TIMEOUT,
    STORAGE_KEY,
    STORAGE_VERSION,
    DEFAULT_BILLING_UPDATE_TIME,
    SUFFIX_ARR,
    SUFFIX_BAL,
    SUFFIX_CURRENT_LADDER,
    SUFFIX_CURRENT_LADDER_REMAINING_KWH,
    SUFFIX_CURRENT_LADDER_TARIFF,
    SUFFIX_ENERGY_TOTAL,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_MONTH_KWH,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_SETTLED_COST_TOTAL,
    SUFFIX_THIS_MONTH_COST,
    SUFFIX_THIS_MONTH_KWH,
    SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from .csg_client import (
    WF_ATTR_CHARGE,
    WF_ATTR_DATE,
    WF_ATTR_KWH,
    WF_ATTR_MONTH,
    WF_ATTR_LADDER,
    WF_ATTR_LADDER_REMAINING_KWH,
    WF_ATTR_LADDER_START_DATE,
    WF_ATTR_LADDER_TARIFF,
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
)

from .history_store import CSGHistoryStore
from .energy_statistics import EnergyStatisticsBridge
from .history_helpers import (
    collect_monthly_bill_candidates as _collect_monthly_bill_candidates,
)

_LOGGER = logging.getLogger(__name__)
_BILLING_DELAY = 2
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_KEY_YESTERDAY_DATE = "_yesterday_usage_date"

# Guangzhou residential single-household tariff.
# This installation has no multi-person allowance or time-of-use tariff.
_GZ_BASE_TARIFF = 0.58886875
_GZ_TIER2_TARIFF = _GZ_BASE_TARIFF + 0.05
_GZ_TIER3_TARIFF = _GZ_BASE_TARIFF + 0.30
FETCH_EXCEPTIONS = (CSGAPIError, asyncio.TimeoutError, ValueError, requests.RequestException)


@dataclass(frozen=True)
class SensorDescription:
    """Metadata for a CSG sensor."""

    suffix: str
    translation_key: str
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None
    attributes_key: str | None = None


ENERGY_TOTAL = SensorDescription(
    SUFFIX_ENERGY_TOTAL,
    "energy_total",
    SensorDeviceClass.ENERGY,
    UnitOfEnergy.KILO_WATT_HOUR,
    SensorStateClass.TOTAL_INCREASING,
    "mdi:lightning-bolt",
)
SETTLED_COST_TOTAL = SensorDescription(
    SUFFIX_SETTLED_COST_TOTAL,
    "settled_cost_total",
    SensorDeviceClass.MONETARY,
    "CNY",
    SensorStateClass.TOTAL_INCREASING,
    "mdi:currency-cny",
)
REALTIME_DESCRIPTIONS = (
    SensorDescription(SUFFIX_YESTERDAY_KWH, "yesterday_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-arrow-left"),
    SensorDescription(SUFFIX_BAL, "balance", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:wallet"),
    SensorDescription(SUFFIX_ARR, "arrears", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:cash-remove"),
)
CURRENT_DESCRIPTIONS = (
    SensorDescription(SUFFIX_CURRENT_LADDER, "current_ladder", icon="mdi:stairs", attributes_key=ATTR_KEY_CURRENT_LADDER_START_DATE),
    SensorDescription(SUFFIX_CURRENT_LADDER_REMAINING_KWH, "current_ladder_remaining", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:lightning-bolt-circle"),
    SensorDescription(SUFFIX_CURRENT_LADDER_TARIFF, "current_ladder_tariff", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:currency-cny"),
)
BILLING_DESCRIPTIONS = (
    SensorDescription(SUFFIX_LATEST_DAY_KWH, "latest_settlement_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-check", ATTR_KEY_SETTLEMENT_DATE),
    SensorDescription(SUFFIX_LATEST_DAY_COST, "latest_settlement_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-check", ATTR_KEY_SETTLEMENT_DATE),
    SensorDescription(SUFFIX_THIS_MONTH_KWH, "this_month_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-month", ATTR_KEY_MONTH_BILLING_DELAY),
    SensorDescription(SUFFIX_THIS_MONTH_COST, "this_month_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-month", ATTR_KEY_MONTH_BILLING_DELAY),
    SensorDescription(SUFFIX_LAST_MONTH_KWH, "last_month_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-minus"),
    SensorDescription(SUFFIX_LAST_MONTH_COST, "last_month_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-minus"),
    SensorDescription(SUFFIX_THIS_YEAR_KWH, "this_year_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-range", ATTR_KEY_YEAR_BILLING_DELAY),
    SensorDescription(SUFFIX_THIS_YEAR_COST, "this_year_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-range", ATTR_KEY_YEAR_BILLING_DELAY),
    SensorDescription(SUFFIX_LAST_YEAR_KWH, "last_year_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-arrow-left"),
    SensorDescription(SUFFIX_LAST_YEAR_COST, "last_year_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-arrow-left"),
)


class EnergyLedger:
    """Persist source values used to build and correct energy statistics."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}")
        self._data: dict[str, Any] = {"accounts": {}}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {"accounts": {}}
        self._data.setdefault("accounts", {})

    async def async_record_realtime(self, account: str, day: str, value: float) -> float:
        async with self._lock:
            ledger = self._account(account)
            ledger.setdefault("energy_started_on", day)
            realtime = ledger.setdefault("realtime", {})
            realtime[day] = value

            reported_days = ledger.setdefault("reported_realtime", {})
            counted_days = ledger.setdefault("counted_realtime", {})

            # A billing row only locks realtime updates after this day has
            # already been accounted for. This allows recovery when Billing
            # arrived before the first usable realtime reading.
            if (
                day in ledger.setdefault("billing", {})
                and day in reported_days
            ):
                await self._store.async_save(self._data)
                return float(ledger.get("energy_total", 0))
            reported = float(reported_days.get(day, 0))
            if value > reported:
                ledger["energy_total"] = float(ledger.get("energy_total", 0)) + value - reported
                reported_days[day] = value
                counted_days[day] = value
                # The ramp starts when the reading arrives. Revisions keep the
                # original anchor because moving it would step the exposed total
                # backwards, and a drop on a total increasing sensor is booked as
                # a meter reset.
                ledger.setdefault("counted_at", {}).setdefault(
                    day, dt_util.utcnow().isoformat()
                )
            billing_row = ledger.setdefault("billing", {}).get(day)

            if (
                billing_row is not None
                and WF_ATTR_KWH in billing_row
                and day in counted_days
            ):
                pending = ledger.setdefault("pending_corrections", {})

                previous, current = pending.get(
                    day,
                    ({}, dict(billing_row)),
                )

                previous = dict(previous)
                current = dict(current)

                # Billing may have arrived before the first usable realtime
                # reading. Once realtime establishes what was actually counted,
                # replace the missing correction baseline with that counted value.
                previous[WF_ATTR_KWH] = float(counted_days[day])
                current[WF_ATTR_KWH] = float(billing_row[WF_ATTR_KWH])

                pending[day] = (previous, current)
            await self._store.async_save(self._data)
            return float(ledger.setdefault("energy_total", 0.0))

    async def async_record_billing(
        self, account: str, days: Iterable[dict[str, float | str]]
    ) -> tuple[float, dict[str, tuple[dict[str, float], dict[str, float]]]]:
        async with self._lock:
            ledger = self._account(account)
            billing = ledger.setdefault("billing", {})
            reported_energy_days = ledger.setdefault("reported_realtime", {})
            reported_cost_days = ledger.setdefault("reported_cost_days", {})
            pending = ledger.setdefault("pending_corrections", {})
            changed: dict[str, tuple[dict[str, float], dict[str, float]]] = {}
            for item in days:
                day = str(item[WF_ATTR_DATE])
                values = {key: float(item[key]) for key in (WF_ATTR_KWH, WF_ATTR_CHARGE) if key in item}
                existing = billing.get(day)
                previous = existing

                if previous is None:
                    counted_usage = ledger.setdefault(
                        "counted_realtime", {}
                    ).get(day)
                    reported_usage = reported_energy_days.get(day)
                    realtime_usage = ledger.setdefault(
                        "realtime", {}
                    ).get(day)

                    baseline_usage = (
                        counted_usage
                        if counted_usage is not None
                        else reported_usage
                        if reported_usage is not None
                        else realtime_usage
                    )

                    previous = (
                        {WF_ATTR_KWH: float(baseline_usage)}
                        if baseline_usage is not None
                        else {}
                    )
                merged = {**previous, **values}
                if existing != merged:
                    billing[day] = merged
                if previous != merged:
                    changed[day] = (previous, merged)
                usage = merged.get(WF_ATTR_KWH)
                reported_usage = reported_energy_days.get(day)
                if usage is not None and (
                    reported_usage is not None
                    or day >= ledger.get("energy_started_on", "9999-12-31")
                ):
                    baseline = float(reported_usage or 0)
                    reported_energy_days[day] = usage
                    if WF_ATTR_KWH not in previous:
                        changed[day] = ({**previous, WF_ATTR_KWH: baseline}, merged)
                charge = merged.get(WF_ATTR_CHARGE)
                reported_charge = reported_cost_days.get(day)
                if charge is not None and reported_charge != charge:
                    if reported_charge is None:
                        ledger["settled_cost_total"] = float(
                            ledger.get("settled_cost_total", 0)
                        ) + charge
                    reported_cost_days[day] = charge
                if previous != merged:
                    correction_previous, correction_current = changed.get(
                        day, (previous, merged)
                    )
                    original, _ = pending.get(
                        day, (correction_previous, correction_current)
                    )
                    pending[day] = (original, correction_current)
            await self._store.async_save(self._data)
            corrections = {
                day: (dict(previous), dict(current))
                for day, (previous, current) in pending.items()
            }
            return float(ledger.setdefault("settled_cost_total", 0.0)), corrections

    async def async_acknowledge_corrections(
        self,
        account: str,
        acknowledgements: dict[str, set[str]],
    ) -> None:
        """Record individual Recorder adjustments without losing failed ones."""
        async with self._lock:
            pending = self._account(account).setdefault("pending_corrections", {})
            for day, keys in acknowledgements.items():
                if day not in pending:
                    continue
                previous, current = pending[day]
                previous = dict(previous)
                for key in keys:
                    if key in current:
                        previous[key] = current[key]
                pending[day] = (previous, current)
                if all(
                    key not in previous or current.get(key, 0) == previous[key]
                    for key in (WF_ATTR_KWH, WF_ATTR_CHARGE)
                ):
                    del pending[day]
            await self._store.async_save(self._data)

    def billing_days(self, account: str) -> dict[str, dict[str, float]]:
        return self._account(account).get("billing", {})

    def energy_total(self, account: str) -> float | None:
        value = self._account(account).get("energy_total")
        return float(value) if value is not None else None

    def energy_counted_at(
        self,
        account: str,
        day: str,
    ) -> dt.datetime | None:
        """Return when a realtime day was first counted into the running total."""
        value = (
            self._account(account)
            .get("counted_at", {})
            .get(day)
        )

        if not value:
            return None

        try:
            counted_at = dt.datetime.fromisoformat(str(value))
        except ValueError:
            return None

        if counted_at.tzinfo is None:
            counted_at = counted_at.replace(tzinfo=dt.timezone.utc)

        return counted_at

    def energy_total_at(self, account: str, when: dt.datetime) -> float | None:
        """Estimate today's cumulative value from the latest complete day.

        The API reports complete daily totals. The exposed sensor advances that
        latest total linearly during the following day while keeping the ledger
        itself authoritative for Recorder corrections.
        """

        total = self.energy_total(account)
        if total is None:
            return None
        realtime = self._account(account).get("realtime", {})
        if not realtime:
            return total
        latest_day = max(realtime)
        # Billing-only days are deliberately excluded from energy_total. Only
        # interpolate a day whose realtime contribution is in the ledger total.
        ledger = self._account(account)
        latest_value = ledger.get("counted_realtime", {}).get(latest_day)
        if latest_value is None and latest_day not in ledger.get("billing", {}):
            # Ledgers from before counted_realtime only have this safe legacy
            # marker when no bill has locked the day's realtime contribution.
            latest_value = ledger.get("reported_realtime", {}).get(latest_day)
        if latest_value is None:
            return total
        latest_value = float(latest_value)
        local = when.astimezone(_CSG_TIME_ZONE)
        if local.date() != dt.date.fromisoformat(latest_day) + dt.timedelta(days=1):
            return total
        fraction = _ramp_fraction(ledger, latest_day, local)
        return max(0.0, total - latest_value + latest_value * fraction)

    def _account(self, account: str) -> dict[str, Any]:
        return self._data["accounts"].setdefault(account, {})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up CSG sensors."""
    if not entry.data[CONF_ELE_ACCOUNTS]:
        return
    ledger = EnergyLedger(hass, entry.entry_id)
    await ledger.async_load()
    history_store = hass.data[DOMAIN][entry.entry_id]["history_store"]
    bridge = hass.data[DOMAIN][entry.entry_id].get("energy_statistics_bridge")
    realtime = RealtimeCoordinator(hass, entry, ledger, history_store, bridge)
    current = CurrentCoordinator(hass, entry, ledger)
    billing = BillingCoordinator(hass, entry, ledger, history_store, bridge)
    hass.data[DOMAIN][entry.entry_id]["realtime_coordinator"] = realtime
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})[
        "billing_coordinator"
    ] = billing
    await realtime.async_refresh()
    await current.async_refresh()
    await billing.async_refresh()
    billing.start_daily_refresh()
    entities: list[CSGSensor] = []
    for account in entry.data[CONF_ELE_ACCOUNTS]:
        entities.extend(
            [CSGSensor(realtime, account, ENERGY_TOTAL), CSGSensor(billing, account, SETTLED_COST_TOTAL)]
        )
        entities.extend(CSGSensor(realtime, account, description) for description in REALTIME_DESCRIPTIONS)
        entities.extend(CSGSensor(current, account, description) for description in CURRENT_DESCRIPTIONS)
        entities.extend(CSGSensor(billing, account, description) for description in BILLING_DESCRIPTIONS)
    async_add_entities(entities)


class CSGSensor(CoordinatorEntity, SensorEntity):
    """A sensor backed by a CSG data coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, account: str, description: SensorDescription) -> None:
        super().__init__(coordinator)
        self._account = account
        self._description = description
        self._attr_unique_id = f"{DOMAIN}.{account}.{description.suffix}"
        self._attr_translation_key = description.translation_key
        self._attr_translation_placeholders = {"account": account}
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_icon = description.icon
        self._attributes_key = description.attributes_key
        self._value_present = False
        self._unsub_interpolation = None
        self._unsub_yesterday_guard = None
        self._update_from_coordinator()

    async def async_added_to_hass(self) -> None:
        """Register local state refresh timers."""
        await super().async_added_to_hass()

        if self._description.suffix == SUFFIX_ENERGY_TOTAL:
            self._unsub_interpolation = async_track_time_interval(
                self.hass,
                self._handle_interpolation_tick,
                timedelta(minutes=5),
            )

        if self._description.suffix == SUFFIX_YESTERDAY_KWH:
            self._unsub_yesterday_guard = async_track_time_interval(
                self.hass,
                self._handle_yesterday_guard_tick,
                timedelta(minutes=1),
            )

    async def async_will_remove_from_hass(self) -> None:
        """Stop local refresh timers when the entity is removed."""
        if self._unsub_interpolation:
            self._unsub_interpolation()
            self._unsub_interpolation = None

        if self._unsub_yesterday_guard:
            self._unsub_yesterday_guard()
            self._unsub_yesterday_guard = None

        await super().async_will_remove_from_hass()

    @callback
    def _handle_interpolation_tick(self, _now: dt.datetime) -> None:
        """Write the estimated cumulative value without making an API call."""
        if self._description.suffix == SUFFIX_ENERGY_TOTAL:
            self._update_from_coordinator()
            self.async_write_ha_state()

    @callback
    def _handle_yesterday_guard_tick(
        self,
        _now: dt.datetime,
    ) -> None:
        """Invalidate yesterday usage after the CSG calendar day changes."""
        self._update_from_coordinator()
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._account)},
            name=f"CSGAccount-{self._account}",
            manufacturer="CSG",
            model="CSG Virtual Electricity Meter",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_coordinator()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether this sensor has a current value."""
        return super().available and self._value_present

    def _update_from_coordinator(self) -> None:
        """Synchronize the cached state with the coordinator's latest data."""
        coordinator_data = self.coordinator.data or {}
        account_data = coordinator_data.get(self._account, {})

        value = account_data.get(self._description.suffix)

        if (
            value is not None
            and value != STATE_UNAVAILABLE
            and self._description.suffix == SUFFIX_YESTERDAY_KWH
        ):
            value_day = account_data.get(_KEY_YESTERDAY_DATE)

            expected_day = (
                _csg_today() - dt.timedelta(days=1)
            ).isoformat()

            if value_day != expected_day:
                value = STATE_UNAVAILABLE

        if (
            value is not None
            and value != STATE_UNAVAILABLE
            and self._description.suffix == SUFFIX_ENERGY_TOTAL
        ):
            ledger = getattr(self.coordinator, "ledger", None)

            if ledger is not None:
                value = ledger.energy_total_at(
                    self._account,
                    dt_util.utcnow(),
                )

        self._value_present = (
            value is not None
            and value != STATE_UNAVAILABLE
        )

        if self._value_present:
            self._attr_native_value = value
            self._attr_extra_state_attributes = (
                account_data.get(
                    self._attributes_key,
                    {},
                )
                if self._attributes_key
                else {}
            )
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}


class CSGCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Shared CSG coordinator functions."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ledger: EnergyLedger, name: str) -> None:
        self.entry = entry
        self.ledger = ledger
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=name,
            update_interval=timedelta(
                seconds=entry.data[CONF_SETTINGS][CONF_UPDATE_INTERVAL]
            ),
        )

    async def _client(self) -> CSGClient:
        try:
            client = await self.hass.async_add_executor_job(
                CSGClient.load, {CONF_AUTH_TOKEN: self.entry.data[CONF_AUTH_TOKEN]}
            )
            if not await self.hass.async_add_executor_job(client.verify_login):
                raise ConfigEntryAuthFailed("Login expired")
            await self.hass.async_add_executor_job(client.initialize)
        except ConfigEntryAuthFailed:
            raise
        except FETCH_EXCEPTIONS as err:
            self._notify_failure("all", "connection", err)
            raise UpdateFailed(f"Unable to initialize CSG client: {err}") from err
        self._clear_failure("all", "connection")
        return client

    async def _fetch(self, function: Any, *args: Any) -> Any:
        async with asyncio.timeout(SETTING_UPDATE_TIMEOUT):
            return await self.hass.async_add_executor_job(function, *args)

    def _accounts(self) -> Iterable[CSGElectricityAccount]:
        return (CSGElectricityAccount.load(value) for value in self.entry.data[CONF_ELE_ACCOUNTS].values())

    def _notify_failure(self, account: str, kind: str, err: Exception) -> None:
        """Make transient cloud failures visible without discarding all entities."""
        persistent_notification.async_create(
            self.hass,
            f"CSG {kind} request for account {account} failed: {err}",
            title="China Southern Power Grid update failed",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_{kind}_{account}",
        )

    def _clear_failure(self, account: str, kind: str) -> None:
        persistent_notification.async_dismiss(
            self.hass,
            f"{DOMAIN}_{self.entry.entry_id}_{kind}_{account}",
        )


class CSGFactCoordinator(CSGCoordinator):
    """Drain admitted refreshes before the entry's final statistics pass."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fact_updates: set[asyncio.Task] = set()
        self._fact_updates_drained = asyncio.Event()
        self._fact_updates_drained.set()

    async def _async_refresh(self, *args, **kwargs) -> None:
        # Every production refresh route (scheduled, explicit and debounced)
        # reaches this method. Core's shutdown only closes future refreshes;
        # it does not await an already running _async_update_data.
        if self._shutdown_requested:
            return
        task = asyncio.current_task()
        self._fact_updates.add(task)
        self._fact_updates_drained.clear()
        try:
            await super()._async_refresh(*args, **kwargs)
        finally:
            self._fact_updates.remove(task)
            if not self._fact_updates:
                self._fact_updates_drained.set()

    async def async_shutdown(self) -> None:
        """Close refresh admission, then wait for all admitted fact writers."""
        self._shutdown_requested = True
        try:
            await super().async_shutdown()
        except Exception:
            _LOGGER.warning("CSG refresh cleanup failed; cancelling admitted fact writers", exc_info=True)
            await self.async_abort()
            return
        if self.hass.is_stopping:
            for task in self._fact_updates:
                task.cancel()
            return
        try:
            await asyncio.wait_for(self._fact_updates_drained.wait(), SETTING_UPDATE_TIMEOUT)
        except Exception:
            _LOGGER.warning("CSG fact writers did not drain; cancelling admitted refreshes", exc_info=True)
            await self.async_abort()

    async def async_abort(self) -> None:
        """Stop coroutines, allowing already owned Store writes to drain."""
        self._shutdown_requested = True
        for task in tuple(self._fact_updates):
            task.cancel()
        if not self.hass.is_stopping:
            # A cancelled cloud await cannot consume a later executor response.
            # If Store I/O already began, M3 retains physical ownership and the
            # refresh's finally only retires after that persistence drain exits.
            await self._fact_updates_drained.wait()


class RealtimeCoordinator(CSGFactCoordinator):
    """Fetch balance and latest published daily usage."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        ledger: EnergyLedger,
        history_store: CSGHistoryStore,
        bridge: EnergyStatisticsBridge | None = None,
    ) -> None:
        super().__init__(
            hass,
            entry,
            ledger,
            f"CSG realtime {entry.data[CONF_USERNAME]}",
        )
        self.history_store = history_store
        self.energy_statistics_bridge = bridge

    def _ledger_total(self, account: str) -> Any:
        """Return the ledger's running total, or unavailable without one."""
        total = self.ledger.energy_total(account)
        return total if total is not None else STATE_UNAVAILABLE

    def _mark_yesterday_unavailable(
        self,
        account_data: dict[str, Any],
        account: str,
    ) -> None:
        """Hide yesterday's usage while preserving the running energy total."""
        account_data[SUFFIX_YESTERDAY_KWH] = STATE_UNAVAILABLE
        account_data[_KEY_YESTERDAY_DATE] = None
        account_data[SUFFIX_ENERGY_TOTAL] = self._ledger_total(account)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        client = await self._client()
        data: dict[str, dict[str, Any]] = {}

        today = _csg_today()
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        previous_month = today.replace(day=1) - dt.timedelta(days=1)

        months = [
            (today.year, today.month),
            (previous_month.year, previous_month.month),
        ]

        for account in self._accounts():
            account_data: dict[str, Any] = {}

            try:
                balance, arrears = await self._fetch(
                    client.get_balance_and_arrears,
                    account,
                )
                account_data.update(
                    {
                        SUFFIX_BAL: balance,
                        SUFFIX_ARR: arrears,
                    }
                )
                self._clear_failure(
                    account.account_number,
                    "balance",
                )
            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning(
                    "Could not update balance for %s: %s",
                    account.account_number,
                    err,
                )
                account_data.update(
                    {
                        SUFFIX_BAL: STATE_UNAVAILABLE,
                        SUFFIX_ARR: STATE_UNAVAILABLE,
                    }
                )
                self._notify_failure(
                    account.account_number,
                    "balance",
                    err,
                )

            latest_usage: dict[str, Any] | None = None
            yesterday_usage: float | None = None
            usage_failed = False

            for year, month in months:
                try:
                    _, usage_days = await self._fetch(
                        client.get_month_daily_usage_detail,
                        account,
                        (year, month),
                    )
                except FETCH_EXCEPTIONS as err:
                    _LOGGER.warning(
                        "Could not update daily usage for %s/%s-%02d: %s",
                        account.account_number,
                        year,
                        month,
                        err,
                    )
                    usage_failed = True
                    self._notify_failure(
                        account.account_number,
                        "usage",
                        err,
                    )
                    continue

                await _async_shadow_write(
                    self.history_store.async_upsert_daily_usage(
                        account.account_number, (year, month), usage_days
                    )
                )
                if self.energy_statistics_bridge is not None:
                    self.energy_statistics_bridge.request_sync()

                valid_days = [
                    item
                    for item in usage_days
                    if item.get(WF_ATTR_DATE) is not None
                    and item.get(WF_ATTR_KWH) is not None
                ]

                if not valid_days:
                    continue

                latest_usage = max(
                    valid_days,
                    key=lambda item: str(item[WF_ATTR_DATE]),
                )

                for item in valid_days:
                    if str(item[WF_ATTR_DATE]) == yesterday:
                        yesterday_usage = float(item[WF_ATTR_KWH])
                        break

                # Months are checked newest first. Once one contains published
                # daily data, an older month cannot contain a newer reading.
                break

            if latest_usage is None:
                # Requests may have succeeded even though the latest daily
                # reading has not been published yet.
                self._mark_yesterday_unavailable(
                    account_data,
                    account.account_number,
                )
            else:
                latest_day = str(latest_usage[WF_ATTR_DATE])
                latest_kwh = float(latest_usage[WF_ATTR_KWH])

                account_data[SUFFIX_ENERGY_TOTAL] = (
                    await self.ledger.async_record_realtime(
                        account.account_number,
                        latest_day,
                        latest_kwh,
                    )
                )

                if yesterday_usage is not None:
                    account_data[SUFFIX_YESTERDAY_KWH] = yesterday_usage
                    account_data[_KEY_YESTERDAY_DATE] = yesterday
                else:
                    account_data[SUFFIX_YESTERDAY_KWH] = STATE_UNAVAILABLE
                    account_data[_KEY_YESTERDAY_DATE] = None

            if not usage_failed:
                self._clear_failure(
                    account.account_number,
                    "usage",
                )

            data[account.account_number] = account_data

        return data


class CurrentCoordinator(CSGCoordinator):
    """Calculate current Guangzhou residential ladder from current-month usage."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        ledger: EnergyLedger,
    ) -> None:
        super().__init__(
            hass,
            entry,
            ledger,
            f"CSG current {entry.data[CONF_USERNAME]}",
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        client = await self._client()
        today = _csg_today()
        data: dict[str, dict[str, Any]] = {}

        for account in self._accounts():
            # This local calculation is intentionally limited to the
            # Guangzhou account/rules verified for this installation.
            if account.area_code != "080000":
                data[account.account_number] = {
                    suffix: STATE_UNAVAILABLE
                    for suffix in (
                        SUFFIX_CURRENT_LADDER,
                        SUFFIX_CURRENT_LADDER_REMAINING_KWH,
                        SUFFIX_CURRENT_LADDER_TARIFF,
                    )
                }
                continue

            try:
                usage_total, usage_days = await self._fetch(
                    client.get_month_daily_usage_detail,
                    account,
                    (today.year, today.month),
                )

                ladder = _guangzhou_residential_ladder(
                    today,
                    usage_total,
                    usage_days,
                )

                data[account.account_number] = _ladder_data(ladder)

                self._clear_failure(account.account_number, "ladder")

            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning(
                    "Could not calculate ladder for %s: %s",
                    account.account_number,
                    err,
                )

                data[account.account_number] = {
                    suffix: STATE_UNAVAILABLE
                    for suffix in (
                        SUFFIX_CURRENT_LADDER,
                        SUFFIX_CURRENT_LADDER_REMAINING_KWH,
                        SUFFIX_CURRENT_LADDER_TARIFF,
                    )
                }

                self._notify_failure(
                    account.account_number,
                    "ladder",
                    err,
                )

        return data


class BillingCoordinator(CSGFactCoordinator):
    """Fetch delayed bill data and import corrections into Recorder statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        ledger: EnergyLedger,
        history_store: CSGHistoryStore,
        bridge: EnergyStatisticsBridge | None = None,
    ) -> None:
        super().__init__(hass, entry, ledger, f"CSG billing {entry.data[CONF_USERNAME]}")
        self.history_store = history_store
        self.energy_statistics_bridge = bridge
        self.update_interval = None
        update_time = dt.time.fromisoformat(
            entry.data[CONF_SETTINGS].get(
                CONF_BILLING_UPDATE_TIME, DEFAULT_BILLING_UPDATE_TIME
            )
        )
        self._billing_update_time = update_time
        self._unsub_daily_refresh = None

    def start_daily_refresh(self) -> None:
        """Register the fixed-time callback after the initial refresh succeeds."""
        if self._unsub_daily_refresh is None:
            self._unsub_daily_refresh = async_track_time_change(
                self.hass,
                self._handle_daily_refresh,
                hour=self._billing_update_time.hour,
                minute=self._billing_update_time.minute,
                second=self._billing_update_time.second,
            )

    async def _handle_daily_refresh(self, _now: dt.datetime) -> None:
        """Refresh delayed billing data at noon in Home Assistant's timezone."""
        await self.async_refresh()

    async def async_shutdown(self) -> None:
        """Cancel the fixed-time billing refresh callback."""
        try:
            if self._unsub_daily_refresh:
                unsubscribe, self._unsub_daily_refresh = self._unsub_daily_refresh, None
                unsubscribe()
        except Exception:
            _LOGGER.warning("CSG billing timer cleanup failed; closing refresh admission", exc_info=True)
        finally:
            await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        client = await self._client()
        now = _csg_today()
        previous = now.replace(day=1) - dt.timedelta(days=1)
        months = [(now.year, now.month), (previous.year, previous.month)]
        data: dict[str, dict[str, Any]] = {}
        for account in self._accounts():
            account_data = await self._update_account(client, account, months)
            data[account.account_number] = account_data
        return data

    async def _update_account(
        self,
        client: CSGClient,
        account: CSGElectricityAccount,
        months: list[tuple[int, int]],
    ) -> dict[str, Any]:
        data: dict[str, Any] = {}
        daily: list[dict[str, float | str]] = []
        current_month = None
        last_month = None
        usage_failed = False

        for year, month in months:
            usage_total = None
            usage_days: list[dict[str, Any]] = []
            usage_ok = False

            try:
                usage_total, usage_days = await self._fetch(
                    client.get_month_daily_usage_detail,
                    account,
                    (year, month),
                )
                usage_ok = True
            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning(
                    "Could not update usage for %s/%s-%02d: %s",
                    account.account_number,
                    year,
                    month,
                    err,
                )
                usage_failed = True
                self._notify_failure(
                    account.account_number,
                    "billing",
                    err,
                )

            if usage_ok:
                await _async_shadow_write(
                    self.history_store.async_upsert_daily_usage(
                        account.account_number, (year, month), usage_days
                    )
                )
                if self.energy_statistics_bridge is not None:
                    self.energy_statistics_bridge.request_sync()
                merged = _merge_daily_days(
                    usage_days,
                    [],
                )
                daily.extend(merged)

                values = (
                    usage_total,
                    None,
                    {},
                    merged,
                )

                if (year, month) == months[0]:
                    current_month = values
                else:
                    last_month = values

        if not usage_failed:
            self._clear_failure(
                account.account_number,
                "billing",
            )

        has_current_settlement_day = False

        if current_month:
            usage_total, cost_total, ladder, current_days = current_month

            data.update(
                {
                    SUFFIX_THIS_MONTH_KWH: (
                        usage_total
                        if usage_total is not None
                        else STATE_UNAVAILABLE
                    ),
                    SUFFIX_THIS_MONTH_COST: (
                        cost_total
                        if cost_total is not None
                        else STATE_UNAVAILABLE
                    ),
                    ATTR_KEY_MONTH_BILLING_DELAY: {
                        ATTR_KEY_MONTH_BILLING_DELAY: _BILLING_DELAY
                    },
                }
            )

            _set_latest_day(data, current_days)
            has_current_settlement_day = bool(current_days)

        else:
            data.update(
                {
                    suffix: STATE_UNAVAILABLE
                    for suffix in (
                        SUFFIX_THIS_MONTH_KWH,
                        SUFFIX_THIS_MONTH_COST,
                        SUFFIX_LATEST_DAY_KWH,
                        SUFFIX_LATEST_DAY_COST,
                    )
                }
            )

        if last_month:
            last_usage, last_cost = last_month[:2]

            data[SUFFIX_LAST_MONTH_KWH] = (
                last_usage
                if last_usage is not None
                else STATE_UNAVAILABLE
            )
            data[SUFFIX_LAST_MONTH_COST] = (
                last_cost
                if last_cost is not None
                else STATE_UNAVAILABLE
            )

            if not has_current_settlement_day:
                _set_latest_day(data, last_month[3])

        else:
            data.update(
                {
                    SUFFIX_LAST_MONTH_KWH: STATE_UNAVAILABLE,
                    SUFFIX_LAST_MONTH_COST: STATE_UNAVAILABLE,
                }
            )

        # The ledger can safely accept usage-only daily rows.
        # Do not expose a fake ¥0 settled-cost total if no charge data exists.
        total_cost, changed_days = await self.ledger.async_record_billing(
            account.account_number,
            daily,
        )

        has_cost_data = any(WF_ATTR_CHARGE in item for item in daily)

        if has_cost_data:
            data[SUFFIX_SETTLED_COST_TOTAL] = total_cost
        else:
            data[SUFFIX_SETTLED_COST_TOTAL] = STATE_UNAVAILABLE

        # Corrections are independent of whether this refresh returned charge rows.
        acknowledgements = await self._async_correct_statistics(
            account.account_number,
            changed_days,
        )

        if acknowledgements:
            await self.ledger.async_acknowledge_corrections(
                account.account_number,
                acknowledgements,
            )

        await self._add_year_data(client, account, data)
        for month in months:
            await _async_shadow_write(
                self.history_store.async_reconcile_month(account.account_number, month)
            )
        return data

    async def _add_year_data(
        self,
        client: CSGClient,
        account: CSGElectricityAccount,
        data: dict[str, Any],
    ) -> None:
        now = _csg_today()
        previous_month = now.replace(day=1) - dt.timedelta(days=1)
        previous_month_key = f"{previous_month.year}{previous_month.month:02d}"

        for year, usage_suffix, cost_suffix in (
            (
                now.year,
                SUFFIX_THIS_YEAR_KWH,
                SUFFIX_THIS_YEAR_COST,
            ),
            (
                now.year - 1,
                SUFFIX_LAST_YEAR_KWH,
                SUFFIX_LAST_YEAR_COST,
            ),
        ):
            try:
                cost, usage, by_month = await self._fetch(
                    client.get_year_month_stats,
                    account,
                    year,
                )

                data[usage_suffix] = usage
                data[cost_suffix] = cost

                # Resolve this response's candidates before any monthly revision.
                for month, values in _collect_monthly_bill_candidates(
                    by_month, account.account_number, year
                ).items():
                    await _async_shadow_write(
                        self.history_store.async_upsert_monthly_bill(
                            account.account_number,
                            month,
                            usage_kwh=values[0],
                            cost_cny=values[1],
                        )
                    )

                for month_data in by_month:
                    if not isinstance(month_data, Mapping):
                        continue
                    month_key = str(
                        month_data.get(WF_ATTR_MONTH, "")
                    ).replace("-", "")

                    if month_key == previous_month_key:
                        data[SUFFIX_LAST_MONTH_COST] = month_data.get(
                            WF_ATTR_CHARGE,
                            STATE_UNAVAILABLE,
                        )
                        break

                if year == now.year:
                    billing_through = (
                        now.replace(day=1) - dt.timedelta(days=1)
                    )
                    data[ATTR_KEY_YEAR_BILLING_DELAY] = {
                        ATTR_KEY_YEAR_BILLING_DELAY:
                            billing_through.strftime("%Y-%m")
                    }

            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning(
                    "Could not update year billing for %s/%s: %s",
                    account.account_number,
                    year,
                    err,
                )
                data[usage_suffix] = STATE_UNAVAILABLE
                data[cost_suffix] = STATE_UNAVAILABLE

    async def _async_statistic_sum_at(
        self,
        statistic_id: str,
        start: dt.datetime,
        period: Literal["5minute", "hour"],
    ) -> float | None:
        """Return the Recorder sum at one fixed statistics interval."""
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
        except ImportError:
            return None

        recorder = get_instance(self.hass)

        if period == "5minute":
            probe_start = dt_util.as_utc(start)
            probe_end = probe_start + dt.timedelta(minutes=5)
        else:
            probe_start = dt_util.as_utc(
                start.replace(
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            )
            probe_end = probe_start + dt.timedelta(hours=1)

        try:
            result = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                probe_start,
                probe_end,
                {statistic_id},
                period,
                None,
                {"sum"},
            )
        except Exception:
            _LOGGER.exception(
                "Could not read Recorder statistic %s at %s",
                statistic_id,
                probe_start,
            )
            return None

        rows = result.get(statistic_id, [])

        if not rows:
            return None

        expected_start = probe_start.timestamp()

        row = next(
            (
                item
                for item in rows
                if math.isclose(
                    float(item.get("start", -1)),
                    expected_start,
                    rel_tol=0.0,
                    abs_tol=0.5,
                )
            ),
            None,
        )

        if row is None:
            return None

        value = row.get("sum")

        return float(value) if value is not None else None

    async def _async_correct_statistics(
        self,
        account: str,
        changed_days: dict[str, tuple[dict[str, float], dict[str, float]]],
    ) -> dict[str, set[str]]:
        """Adjust corrected daily energy and cost sums through Recorder."""
        if not changed_days:
            return {}
        try:
            from homeassistant.components.recorder import get_instance
        except ImportError:
            _LOGGER.warning("Recorder external statistics API unavailable; skipped bill correction")
            return {}
        try:
            statistics = (
                (self._statistic_id(account, SUFFIX_ENERGY_TOTAL), WF_ATTR_KWH, "kWh"),
                (self._statistic_id(account, SUFFIX_SETTLED_COST_TOTAL), WF_ATTR_CHARGE, "CNY"),
            )
        except ValueError as err:
            _LOGGER.warning("Skipped bill correction: %s", err)
            return {}
            
        recorder = get_instance(self.hass)
        acknowledgements: dict[str, set[str]] = {}

        for day, (previous, current) in changed_days.items():
            fallback_start = dt.datetime.combine(
                dt.date.fromisoformat(day),
                dt.time.min,
                tzinfo=_CSG_TIME_ZONE,
            )

            for statistic_id, key, unit in statistics:
                if key not in previous:
                    continue

                start = fallback_start

                if key == WF_ATTR_KWH:
                    counted_at = self.ledger.energy_counted_at(
                        account,
                        day,
                    )

                    if counted_at is not None:
                        counted_at = counted_at.astimezone(
                            _CSG_TIME_ZONE
                        )

                        # Recorder short-term statistics use five-minute
                        # intervals. Start from the interval in which this
                        # realtime contribution first entered the running total.
                        minute = (
                            counted_at.minute
                            - counted_at.minute % 5
                        )

                        start = counted_at.replace(
                            minute=minute,
                            second=0,
                            microsecond=0,
                        )

                adjustment = current.get(key, 0) - previous[key]

                if not adjustment:
                    acknowledgements.setdefault(day, set()).add(key)
                    continue

                probe_period: Literal["5minute", "hour"] = "5minute"

                before_sum = await self._async_statistic_sum_at(
                    statistic_id,
                    start,
                    probe_period,
                )

                if before_sum is None:
                    probe_period = "hour"
                    before_sum = await self._async_statistic_sum_at(
                        statistic_id,
                        start,
                        probe_period,
                    )

                if before_sum is None:
                    _LOGGER.warning(
                        "Could not find a stable Recorder statistic for %s "
                        "at or after correction start; keeping correction pending",
                        statistic_id,
                    )
                    continue

                try:
                    recorder.async_adjust_statistics(
                        statistic_id,
                        start,
                        adjustment,
                        unit,
                    )

                    await recorder.async_block_till_done()

                except Exception:
                    # Keep this statistic's correction pending.
                    _LOGGER.exception(
                        "Could not correct bill statistic %s",
                        statistic_id,
                    )
                    continue

                after_sum = await self._async_statistic_sum_at(
                    statistic_id,
                    start,
                    probe_period,
                )

                if after_sum is None:
                    _LOGGER.warning(
                        "Could not verify corrected Recorder statistic %s; "
                        "keeping correction pending",
                        statistic_id,
                    )
                    continue

                if not math.isclose(
                    after_sum - before_sum,
                    adjustment,
                    rel_tol=1e-9,
                    abs_tol=1e-6,
                ):
                    _LOGGER.warning(
                        "Recorder statistic %s did not reflect expected "
                        "adjustment %.6f; keeping correction pending",
                        statistic_id,
                        adjustment,
                    )
                    continue

                acknowledgements.setdefault(day, set()).add(key)

        return acknowledgements

    def _statistic_id(self, account: str, suffix: str) -> str:
        """Return the Recorder statistic ID after any user entity-ID rename."""
        unique_id = f"{DOMAIN}.{account}.{suffix}"
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            raise ValueError(f"Entity registry has no entity for {unique_id}")
        return entity_id


async def _async_shadow_write(operation: Awaitable[Any]) -> None:
    """Keep a failed history write from interrupting the legacy data path."""
    try:
        await operation
    except Exception:
        _LOGGER.exception("HistoryStore shadow write failed; continuing legacy update")



def _merge_daily_days(usage_days: list[dict[str, Any]], cost_days: list[dict[str, Any]]) -> list[dict[str, float | str]]:
    """Merge bill responses by date without assuming equal response lengths."""
    days: dict[str, dict[str, float | str]] = {item[WF_ATTR_DATE]: dict(item) for item in usage_days}
    for item in cost_days:
        day = item[WF_ATTR_DATE]
        target = days.setdefault(day, {WF_ATTR_DATE: day})
        if WF_ATTR_KWH not in target and WF_ATTR_KWH in item:
            target[WF_ATTR_KWH] = item[WF_ATTR_KWH]
        if WF_ATTR_CHARGE in item:
            target[WF_ATTR_CHARGE] = item[WF_ATTR_CHARGE]
    return [days[day] for day in sorted(days)]


def _csg_today() -> dt.date:
    """Return the current calendar date used by the CSG API."""
    return dt_util.utcnow().astimezone(_CSG_TIME_ZONE).date()


def _ramp_fraction(ledger: dict[str, Any], day: str, local: dt.datetime) -> float:
    """Return how much of a day's reading has been smoothed out by `local`.

    A complete day total only exists once the API publishes it, so the ramp
    starts when the reading arrives instead of at midnight. Anchoring it at
    midnight would pay out the whole elapsed share of the day in the single
    update that delivers the reading.
    """
    start = dt.datetime.combine(local.date(), dt.time(0), tzinfo=_CSG_TIME_ZONE)
    end = start + dt.timedelta(days=1)
    started_at = ledger.get("counted_at", {}).get(day)
    if started_at:
        candidate = dt.datetime.fromisoformat(started_at).astimezone(_CSG_TIME_ZONE)
        if start <= candidate < end:
            start = candidate
    span = (end - start).total_seconds()
    if span <= 0:
        return 1.0
    return min(1.0, max(0.0, (local - start).total_seconds() / span))


def _guangzhou_residential_ladder(
    today: dt.date,
    usage_total: float,
    usage_days: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate Guangzhou residential ladder for a normal single household."""
    usage = float(usage_total)

    if 5 <= today.month <= 10:
        first_limit = 260.0
        second_limit = 600.0
    else:
        first_limit = 200.0
        second_limit = 400.0

    if usage <= first_limit:
        ladder = 1
        tariff = _GZ_BASE_TARIFF
        remaining: Any = round(max(0.0, first_limit - usage), 2)
        threshold = 0.0
    elif usage <= second_limit:
        ladder = 2
        tariff = _GZ_TIER2_TARIFF
        remaining = round(max(0.0, second_limit - usage), 2)
        threshold = first_limit
    else:
        ladder = 3
        tariff = _GZ_TIER3_TARIFF
        remaining = STATE_UNAVAILABLE
        threshold = second_limit

    # First tier begins on the first day of the month.
    # For higher tiers, derive the first published day on which the threshold
    # was exceeded from the daily usage data.
    start_date: str | None = today.replace(day=1).isoformat()

    if ladder > 1:
        cumulative = 0.0
        start_date = None

        for item in sorted(
            usage_days,
            key=lambda item: str(item.get(WF_ATTR_DATE, "")),
        ):
            day = item.get(WF_ATTR_DATE)
            day_usage = item.get(WF_ATTR_KWH)

            if day is None or day_usage is None:
                continue

            cumulative += float(day_usage)

            if cumulative > threshold:
                start_date = str(day)
                break

    return {
        WF_ATTR_LADDER: ladder,
        WF_ATTR_LADDER_REMAINING_KWH: remaining,
        WF_ATTR_LADDER_TARIFF: tariff,
        WF_ATTR_LADDER_START_DATE: start_date,
    }
def _ladder_data(ladder: dict[str, Any]) -> dict[str, Any]:
    return {
        SUFFIX_CURRENT_LADDER: ladder.get(WF_ATTR_LADDER, STATE_UNAVAILABLE),
        SUFFIX_CURRENT_LADDER_REMAINING_KWH: ladder.get(WF_ATTR_LADDER_REMAINING_KWH, STATE_UNAVAILABLE),
        SUFFIX_CURRENT_LADDER_TARIFF: ladder.get(WF_ATTR_LADDER_TARIFF, STATE_UNAVAILABLE),
        ATTR_KEY_CURRENT_LADDER_START_DATE: {ATTR_KEY_CURRENT_LADDER_START_DATE: ladder.get(WF_ATTR_LADDER_START_DATE)},
    }


def _set_latest_day(data: dict[str, Any], days: list[dict[str, float | str]]) -> None:
    if not days:
        data[SUFFIX_LATEST_DAY_KWH] = STATE_UNAVAILABLE
        data[SUFFIX_LATEST_DAY_COST] = STATE_UNAVAILABLE
        return
    latest = days[-1]
    data[SUFFIX_LATEST_DAY_KWH] = latest.get(WF_ATTR_KWH, STATE_UNAVAILABLE)
    data[SUFFIX_LATEST_DAY_COST] = latest.get(WF_ATTR_CHARGE, STATE_UNAVAILABLE)
    data[ATTR_KEY_SETTLEMENT_DATE] = {ATTR_KEY_SETTLEMENT_DATE: latest[WF_ATTR_DATE]}
