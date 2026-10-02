"""Sensors for the China Southern Power Grid integration."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval, async_track_utc_time_change
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
    CONF_TARIFF_PROFILES,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    SETTING_UPDATE_TIMEOUT,
    DEFAULT_BILLING_UPDATE_TIME,
    SUFFIX_ARR,
    SUFFIX_BAL,
    SUFFIX_CURRENT_LADDER,
    SUFFIX_CURRENT_LADDER_REMAINING_KWH,
    SUFFIX_CURRENT_LADDER_TARIFF,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_MONTH_KWH,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
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
from .tariff import TariffProfile, current_ladder, resolve_tariff_profile
from .utils import account_log_id

_LOGGER = logging.getLogger(__name__)
_BILLING_DELAY = 2
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_KEY_YESTERDAY_DATE = "_yesterday_usage_date"
_KEY_TARIFF_MONTH = "_tariff_usage_month"
_KEY_TARIFF_ATTRIBUTES = "_tariff_attributes"
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


REALTIME_DESCRIPTIONS = (
    SensorDescription(SUFFIX_YESTERDAY_KWH, "yesterday_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-arrow-left"),
    SensorDescription(SUFFIX_BAL, "balance", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:wallet"),
    SensorDescription(SUFFIX_ARR, "arrears", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:cash-remove"),
)
CURRENT_DESCRIPTIONS = (
    SensorDescription(SUFFIX_CURRENT_LADDER, "current_ladder", icon="mdi:stairs", attributes_key=ATTR_KEY_CURRENT_LADDER_START_DATE),
    SensorDescription(SUFFIX_CURRENT_LADDER_REMAINING_KWH, "current_ladder_remaining", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:lightning-bolt-circle"),
    SensorDescription(SUFFIX_CURRENT_LADDER_TARIFF, "current_ladder_tariff", unit="CNY/kWh", state_class=SensorStateClass.MEASUREMENT, icon="mdi:currency-cny", attributes_key=_KEY_TARIFF_ATTRIBUTES),
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up CSG sensors."""
    if not entry.data[CONF_ELE_ACCOUNTS]:
        return
    history_store = hass.data[DOMAIN][entry.entry_id]["history_store"]
    bridge = hass.data[DOMAIN][entry.entry_id].get("energy_statistics_bridge")
    realtime = RealtimeCoordinator(hass, entry, history_store, bridge)
    current = CurrentCoordinator(hass, entry)
    billing = BillingCoordinator(hass, entry, history_store, bridge)
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
        self._unsub_yesterday_guard = None
        self._unsub_tariff_boundary = None
        self._update_from_coordinator()

    async def async_added_to_hass(self) -> None:
        """Register local state refresh timers."""
        await super().async_added_to_hass()

        if self._description.suffix == SUFFIX_YESTERDAY_KWH:
            self._unsub_yesterday_guard = async_track_time_interval(
                self.hass,
                self._handle_yesterday_guard_tick,
                timedelta(minutes=1),
            )
        if self._description.suffix == SUFFIX_CURRENT_LADDER_TARIFF and isinstance(self.coordinator, CurrentCoordinator):
            profile = self.coordinator.tariff_profile(self._account)
            if profile is not None and profile.tou_enabled:
                # Shanghai has no DST. These UTC hours are precisely local
                # 08, 10, 12, 14, 19 and 00, regardless of HA's display zone.
                self._unsub_tariff_boundary = async_track_utc_time_change(
                    self.hass, self._handle_tariff_boundary,
                    hour=[0, 2, 4, 6, 11, 16], minute=0, second=0,
                )

    async def async_will_remove_from_hass(self) -> None:
        """Stop local refresh timers when the entity is removed."""
        if self._unsub_yesterday_guard:
            self._unsub_yesterday_guard()
            self._unsub_yesterday_guard = None
        if self._unsub_tariff_boundary:
            self._unsub_tariff_boundary()
            self._unsub_tariff_boundary = None

        await super().async_will_remove_from_hass()


    @callback
    def _handle_yesterday_guard_tick(
        self,
        _now: dt.datetime,
    ) -> None:
        """Invalidate yesterday usage after the CSG calendar day changes."""
        self._update_from_coordinator()
        self.async_write_ha_state()

    @callback
    def _handle_tariff_boundary(self, _now: dt.datetime) -> None:
        """Refresh the current unit price from cached usage, without cloud I/O."""
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
        if self._description.suffix == SUFFIX_CURRENT_LADDER_TARIFF and isinstance(self.coordinator, CurrentCoordinator):
            value = self.coordinator.current_tariff(self._account)

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

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, name: str) -> None:
        self.entry = entry
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
            raise UpdateFailed(f"Unable to initialize CSG client: {type(err).__name__}") from err
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
        history_store: CSGHistoryStore,
        bridge: EnergyStatisticsBridge | None = None,
    ) -> None:
        super().__init__(
            hass,
            entry,
            f"CSG realtime {entry.entry_id}",
        )
        self.history_store = history_store
        self.energy_statistics_bridge = bridge

    def _mark_yesterday_unavailable(
        self,
        account_data: dict[str, Any],
    ) -> None:
        """Hide unpublished yesterday usage without inventing a zero."""
        account_data[SUFFIX_YESTERDAY_KWH] = STATE_UNAVAILABLE
        account_data[_KEY_YESTERDAY_DATE] = None

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
                    account_log_id(account.account_number),
                    type(err).__name__,
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
                        account_log_id(account.account_number),
                        year,
                        month,
                        type(err).__name__,
                    )
                    usage_failed = True
                    self._notify_failure(
                        account.account_number,
                        "usage",
                        err,
                    )
                    continue

                await _async_write_history(
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

                for item in valid_days:
                    if str(item[WF_ATTR_DATE]) == yesterday:
                        yesterday_usage = float(item[WF_ATTR_KWH])
                        break

                # Months are checked newest first. Once one contains published
                # daily data, an older month cannot contain a newer reading.
                break

            if yesterday_usage is None:
                # Requests may have succeeded even though the latest daily
                # reading has not been published yet.
                self._mark_yesterday_unavailable(
                    account_data,
                )
            else:
                account_data[SUFFIX_YESTERDAY_KWH] = yesterday_usage
                account_data[_KEY_YESTERDAY_DATE] = yesterday

            if not usage_failed:
                self._clear_failure(
                    account.account_number,
                    "usage",
                )

            data[account.account_number] = account_data

        return data


class CurrentCoordinator(CSGCoordinator):
    """Show current tariff capability only for an explicitly selected policy."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            entry,
            f"CSG current {entry.entry_id}",
        )

    def tariff_profile(self, account_number: str) -> TariffProfile | None:
        selections = self.entry.data.get(CONF_SETTINGS, {}).get(CONF_TARIFF_PROFILES, {})
        for account in self._accounts():
            if account.account_number == account_number:
                selection = selections.get(account_number) if isinstance(selections, Mapping) else None
                return resolve_tariff_profile(account.area_code, selection)
        return None

    def current_tariff(self, account_number: str) -> float | str:
        """Re-evaluate TOU time locally; a previous month's tier is never reused."""
        profile = self.tariff_profile(account_number)
        if profile is None:
            return STATE_UNAVAILABLE
        now = _csg_now()
        account_data = (self.data or {}).get(account_number, {})
        if profile.ladder_enabled and account_data.get(_KEY_TARIFF_MONTH) != now.date().isoformat()[:7]:
            return STATE_UNAVAILABLE
        tier = account_data.get(SUFFIX_CURRENT_LADDER)
        rate = profile.current_rate(tier, now)
        return float(rate) if rate is not None else STATE_UNAVAILABLE

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        client = None
        today = _csg_today()
        data: dict[str, dict[str, Any]] = {}

        for account in self._accounts():
            profile = self.tariff_profile(account.account_number)
            account_data = data[account.account_number] = _ladder_data({})
            if profile is None:
                continue
            account_data[_KEY_TARIFF_ATTRIBUTES] = {
                "tariff_profile": profile.profile_id,
                "billing_period": profile.billing_period,
                "multi_person_allowance_kwh": float(profile.multi_person_allowance),
                "tou_enabled": profile.tou_enabled,
                "effective_from": profile.effective_from.isoformat(),
                "policy_sources": list(profile.source),
            }
            if not profile.ladder_enabled:
                account_data[SUFFIX_CURRENT_LADDER_TARIFF] = float(profile.current_rate(None, _csg_now()))
                continue

            try:
                if client is None:
                    client = await self._client()
                usage_total, usage_days = await self._fetch(
                    client.get_month_daily_usage_detail,
                    account,
                    (today.year, today.month),
                )

                ladder = current_ladder(profile, today, usage_total, usage_days)
                account_data.update(_ladder_data({
                    WF_ATTR_LADDER: ladder.tier,
                    WF_ATTR_LADDER_REMAINING_KWH: float(ladder.remaining_kwh) if ladder.remaining_kwh is not None else STATE_UNAVAILABLE,
                    WF_ATTR_LADDER_TARIFF: float(profile.current_rate(ladder.tier, _csg_now())),
                    WF_ATTR_LADDER_START_DATE: ladder.start_date,
                }))
                account_data[_KEY_TARIFF_MONTH] = today.isoformat()[:7]

                self._clear_failure(account.account_number, "ladder")

            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning(
                    "Could not calculate ladder for tariff profile %s: %s",
                    profile.profile_id,
                    type(err).__name__,
                )

                self._notify_failure(
                    account.account_number,
                    "ladder",
                    err,
                )

        return data


class BillingCoordinator(CSGFactCoordinator):
    """Fetch official billing snapshots and persist daily and monthly facts."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        history_store: CSGHistoryStore,
        bridge: EnergyStatisticsBridge | None = None,
    ) -> None:
        super().__init__(hass, entry, f"CSG billing {entry.entry_id}")
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
        data: dict[str, Any] = {
            SUFFIX_LAST_MONTH_KWH: STATE_UNAVAILABLE,
            SUFFIX_LAST_MONTH_COST: STATE_UNAVAILABLE,
        }
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
                    account_log_id(account.account_number),
                    year,
                    month,
                    type(err).__name__,
                )
                usage_failed = True
                self._notify_failure(
                    account.account_number,
                    "billing",
                    err,
                )

            if usage_ok:
                await _async_write_history(
                    self.history_store.async_upsert_daily_usage(
                        account.account_number, (year, month), usage_days
                    )
                )
                if self.energy_statistics_bridge is not None:
                    self.energy_statistics_bridge.request_sync()
                # The client also returns date-only coverage markers. Settlement
                # selection uses valid facts; the markers are only for tariff.
                daily_days = sorted(
                    (item for item in usage_days if item.get(WF_ATTR_KWH) is not None),
                    key=lambda item: str(item[WF_ATTR_DATE]),
                )

                values = (
                    usage_total,
                    None,
                    daily_days,
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
            usage_total, cost_total, current_days = current_month

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
            if not has_current_settlement_day:
                _set_latest_day(data, last_month[2])

        # Closed-month snapshots come only from the official billing response;
        # a daily usage total must not stand in for a missing official bill.
        await self._add_year_data(client, account, data)
        for month in months:
            await _async_write_history(
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
                bill_candidates = _collect_monthly_bill_candidates(
                    by_month, account.account_number, year
                )
                for month, values in bill_candidates.items():
                    await _async_write_history(
                        self.history_store.async_upsert_monthly_bill(
                            account.account_number,
                            month,
                            usage_kwh=values[0],
                            cost_cny=values[1],
                        )
                    )
                # Fact changes and view convergence are separate: an unchanged
                # refetch can finish a pending save, and a failed save can be
                # retried by the Bridge's durable gate. Request once per batch.
                if bill_candidates and self.energy_statistics_bridge is not None:
                    self.energy_statistics_bridge.request_sync()

                for month_data in by_month:
                    if not isinstance(month_data, Mapping):
                        continue
                    month_key = str(
                        month_data.get(WF_ATTR_MONTH, "")
                    ).replace("-", "")

                    if month_key == previous_month_key:
                        data[SUFFIX_LAST_MONTH_KWH] = month_data.get(
                            WF_ATTR_KWH,
                            STATE_UNAVAILABLE,
                        )
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
                    account_log_id(account.account_number),
                    year,
                    type(err).__name__,
                )
                data[usage_suffix] = STATE_UNAVAILABLE
                data[cost_suffix] = STATE_UNAVAILABLE


async def _async_write_history(operation: Awaitable[Any]) -> Any:
    """Keep official snapshots available when history persistence fails."""
    try:
        return await operation
    except Exception:
        _LOGGER.exception("HistoryStore write failed; continuing snapshot update")


def _csg_today() -> dt.date:
    """Return the current calendar date used by the CSG API."""
    return _csg_now().date()


def _csg_now() -> dt.datetime:
    return dt_util.utcnow().astimezone(_CSG_TIME_ZONE)


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
