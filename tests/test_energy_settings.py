"""Opt-in Boolean settings and translations for external energy statistics."""

import asyncio
import json
from pathlib import Path

import pytest
import voluptuous as vol

from custom_components.csg.const import CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS, CONF_UPDATE_INTERVAL
from test_history_settings import make_flow


def test_energy_setting_defaults_off_and_accepts_only_boolean():
    flow, _ = make_flow()
    schema = asyncio.run(flow.async_step_settings())["data_schema"]
    assert schema({CONF_UPDATE_INTERVAL: 3600})[CONF_ENERGY_STATISTICS_ENABLED] is False
    assert schema({CONF_UPDATE_INTERVAL: 3600, CONF_ENERGY_STATISTICS_ENABLED: True})[CONF_ENERGY_STATISTICS_ENABLED] is True
    with pytest.raises(vol.Invalid):
        schema({CONF_UPDATE_INTERVAL: 3600, CONF_ENERGY_STATISTICS_ENABLED: "true"})


@pytest.mark.parametrize("enabled", [True, False])
def test_energy_setting_saves_and_reloads_without_losing_history_bound(enabled):
    flow, entry = make_flow("2024-02")
    result = asyncio.run(flow.async_step_settings({
        CONF_UPDATE_INTERVAL: 3600, "history_start_month": "2024-02",
        CONF_ENERGY_STATISTICS_ENABLED: enabled,
    }))
    assert result == {"title": "", "data": {}}
    settings = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"][CONF_SETTINGS]
    assert settings[CONF_ENERGY_STATISTICS_ENABLED] is enabled
    assert settings["history_start_month"] == "2024-02"
    flow.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


def test_other_settings_edit_preserves_existing_energy_opt_in():
    flow, entry = make_flow()
    entry.data[CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] = True
    schema = asyncio.run(flow.async_step_settings())["data_schema"]
    assert schema({CONF_UPDATE_INTERVAL: 3600})[CONF_ENERGY_STATISTICS_ENABLED] is True
    asyncio.run(flow.async_step_settings({CONF_UPDATE_INTERVAL: 3600}))
    assert flow.hass.config_entries.async_update_entry.call_args.kwargs["data"][CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] is True


def test_energy_setting_translations_include_retention_and_manual_selection():
    root = Path(__file__).parents[1] / "custom_components" / "csg"
    for name in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
        setting = json.loads((root / name).read_text(encoding="utf-8"))["options"]["step"]["settings"]
        assert setting["data"][CONF_ENERGY_STATISTICS_ENABLED]
        description = setting["data_description"][CONF_ENERGY_STATISTICS_ENABLED]
        assert ("retains" in description and "manually" in description) or ("保留" in description and "手动" in description)
