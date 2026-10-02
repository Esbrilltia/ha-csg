# -*- coding: utf-8 -*-
"""The China Southern Power Grid Statistics integration."""
from __future__ import annotations

import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from requests import RequestException
from homeassistant.helpers import entity_registry
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_HISTORY_START_MONTH,
    CONF_LOGIN_TYPE,
    CONF_SETTINGS,
    CONF_UPDATED_AT,
    DOMAIN,
)
from .csg_client import (
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
    InvalidCredentials,
    NotLoggedIn,
)
from .history_coordinator import HistoryCoordinator
from .history_store import CSGHistoryStore
from .energy_statistics import EnergyStatisticsBridge

PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up China Southern Power Grid Statistics from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # validate session, re-authenticate if needed
    client = CSGClient.load(
        {
            CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
        }
    )
    try:
        logged_in = await hass.async_add_executor_job(client.verify_login)
    except (CSGAPIError, RequestException) as err:
        raise ConfigEntryNotReady(f"Unable to contact China Southern Power Grid: {type(err).__name__}") from err
    if not logged_in:
        raise ConfigEntryAuthFailed("Login expired")

    history_store = CSGHistoryStore(hass, entry.entry_id)
    await history_store.async_load()
    bridge = EnergyStatisticsBridge(hass, entry, history_store)
    hass.data[DOMAIN][entry.entry_id] = {
        "history_store": history_store, "energy_statistics_bridge": bridge,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.data.get(CONF_SETTINGS, {}).get(CONF_HISTORY_START_MONTH):
        history = HistoryCoordinator(hass, entry, history_store, bridge)
        hass.data[DOMAIN][entry.entry_id]["history_coordinator"] = history
        history.start()

    bridge.request_sync()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading CSG entry %s", entry.entry_id)
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    bridge = runtime.get("energy_statistics_bridge")
    if bridge is not None:
        bridge.stop_requests()
    cleanup_errors: list[Exception] = []
    for key in ("history_coordinator", "billing_coordinator", "realtime_coordinator"):
        producer = runtime.get(key)
        if producer is None:
            continue
        try:
            await producer.async_shutdown()
        except Exception as err:
            _LOGGER.warning("CSG %s cleanup failed; forcing coroutine shutdown", key, exc_info=True)
            if abort := getattr(producer, "async_abort", None):
                await abort()
            else:
                cleanup_errors.append(err)
    if cleanup_errors:
        # Unknown lifecycle failures cannot be treated as proven quiescence.
        raise cleanup_errors[0]
    if bridge is not None:
        try:
            await bridge.async_shutdown()
        except Exception:
            _LOGGER.warning(
                "CSG external statistics final materialization did not complete; "
                "HistoryStore facts remain authoritative and a future enabled Bridge sync can retry",
                exc_info=True,
            )
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    _LOGGER.debug("Unload CSG platforms for entry %s, success: %s", entry.entry_id, unload_ok)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove device"""
    _LOGGER.info("Removing CSG account device")
    account_num = list(device_entry.identifiers)[0][1]

    # remove entities
    entity_reg = entity_registry.async_get(hass)
    entities = {
        ent.unique_id: ent.entity_id
        for ent in entity_registry.async_entries_for_config_entry(
            entity_reg, config_entry.entry_id
        )
        if account_num in ent.unique_id
    }
    for entity_id in entities.values():
        entity_reg.async_remove(entity_id)

    # update config entry
    new_data = {
        **config_entry.data,
        CONF_ELE_ACCOUNTS: {
            account_number: account_data
            for account_number, account_data in config_entry.data[
                CONF_ELE_ACCOUNTS
            ].items()
            if account_number != account_num
        },
    }
    new_data[CONF_UPDATED_AT] = str(int(time.time() * 1000))
    hass.config_entries.async_update_entry(
        config_entry,
        data=new_data,
    )
    _LOGGER.info("Removed a linked electricity account")
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    _LOGGER.info("Removing CSG entry %s", entry.entry_id)

    # logout
    def client_logout():
        client = CSGClient.load(
            {
                CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
            }
        )
        if client.verify_login():
            client.logout(entry.data[CONF_LOGIN_TYPE])
            _LOGGER.info("CSG account logged out")

    await hass.async_add_executor_job(client_logout)
