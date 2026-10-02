"""Unit tests for CSG configuration-flow behavior."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace

from custom_components.csg.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    CONF_UPDATED_AT,
)
from custom_components.csg.csg_client import CSGElectricityAccount
from custom_components.csg.config_flow import CSGOptionsFlowHandler


def run(coroutine):
    """Run an async unit under pytest without pytest-asyncio."""
    return asyncio.run(coroutine)


def test_options_flow_shows_translated_menu() -> None:
    """The options entry point routes through Home Assistant menu translations."""
    flow = CSGOptionsFlowHandler()
    flow.async_show_menu = lambda **kwargs: kwargs

    result = run(flow.async_step_init())

    assert result == {
        "step_id": "init",
        "menu_options": ["add_account", "settings", "tariff_account"],
    }


def test_settings_update_reloads_entry() -> None:
    """Changing the interval reloads coordinators so the value takes effect."""
    entry = SimpleNamespace(
        entry_id="entry-id",
        data={CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 14_400}, "updated_at": "0"},
    )

    class FakeConfigEntries:
        def __init__(self) -> None:
            self.updated_data = None
            self.reloaded_entry_id = None

        def async_update_entry(self, config_entry, *, data) -> None:
            self.updated_data = data

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded_entry_id = entry_id

    config_entries = FakeConfigEntries()
    config_entries.async_get_known_entry = lambda entry_id: entry
    flow = CSGOptionsFlowHandler()
    flow.hass = SimpleNamespace(config_entries=config_entries)
    flow.handler = "entry-id"
    flow.async_create_entry = lambda **kwargs: kwargs

    result = run(flow.async_step_settings({CONF_UPDATE_INTERVAL: 3_600}))

    assert config_entries.updated_data[CONF_SETTINGS][CONF_UPDATE_INTERVAL] == 3_600
    assert config_entries.reloaded_entry_id == "entry-id"
    assert result == {"title": "", "data": {}}


def test_add_account_updates_mappingproxy_entry_data() -> None:
    """Adding an account handles immutable ConfigEntry data."""
    entry = SimpleNamespace(
        entry_id="entry-id",
        data=MappingProxyType(
            {
                "username": "13800138000",
                CONF_AUTH_TOKEN: "token",
                CONF_ELE_ACCOUNTS: MappingProxyType({"existing": {"id": "existing"}}),
                CONF_SETTINGS: MappingProxyType({CONF_UPDATE_INTERVAL: 14_400}),
                CONF_UPDATED_AT: "0",
            }
        ),
    )

    class FakeConfigEntries:
        def __init__(self) -> None:
            self.updated_data = None
            self.reloaded_entry_id = None

        def async_entries(self, domain: str):
            return [entry]

        def async_update_entry(self, config_entry, *, data) -> None:
            self.updated_data = data

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded_entry_id = entry_id

    config_entries = FakeConfigEntries()
    flow = CSGOptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(config_entries=config_entries)
    flow.async_create_entry = lambda **kwargs: kwargs
    flow.all_electricity_accounts = [
        CSGElectricityAccount(account_number="new-account")
    ]

    result = run(flow.async_step_add_account({CONF_ACCOUNT_NUMBER: "new-account"}))

    assert config_entries.updated_data[CONF_ELE_ACCOUNTS] == {
        "existing": {"id": "existing"},
        "new-account": CSGElectricityAccount(account_number="new-account").dump(),
    }
    assert config_entries.reloaded_entry_id == "entry-id"
    assert result == {"title": "", "data": {}}
