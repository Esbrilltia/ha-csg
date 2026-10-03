"""Default-on Boolean settings and translations for external energy statistics."""

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import voluptuous as vol

from custom_components.csg_plus.const import CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS, CONF_UPDATE_INTERVAL
from test_history_settings import make_flow
from custom_components.csg_plus.config_flow import CSGConfigFlow


def test_energy_setting_defaults_on_and_accepts_only_boolean():
    flow, _ = make_flow()
    schema = asyncio.run(flow.async_step_settings())["data_schema"]
    assert schema({CONF_UPDATE_INTERVAL: 3600})[CONF_ENERGY_STATISTICS_ENABLED] is True
    assert schema({CONF_UPDATE_INTERVAL: 3600, CONF_ENERGY_STATISTICS_ENABLED: True})[CONF_ENERGY_STATISTICS_ENABLED] is True
    with pytest.raises(vol.Invalid):
        schema({CONF_UPDATE_INTERVAL: 3600, CONF_ENERGY_STATISTICS_ENABLED: "true"})


@pytest.mark.parametrize("before,enabled", [(True, False), (False, True)])
def test_energy_setting_transitions_reload_without_losing_history_bound(before, enabled):
    flow, entry = make_flow("2024-02")
    entry.data[CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] = before
    result = asyncio.run(flow.async_step_settings({
        CONF_UPDATE_INTERVAL: 3600, "history_start_month": "2024-02",
        CONF_ENERGY_STATISTICS_ENABLED: enabled,
    }))
    assert result == {"title": "", "data": {}}
    settings = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"][CONF_SETTINGS]
    assert settings[CONF_ENERGY_STATISTICS_ENABLED] is enabled
    assert settings["history_start_month"] == "2024-02"
    flow.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("enabled", [True, False])
def test_other_settings_edit_preserves_explicit_energy_preference(enabled):
    flow, entry = make_flow()
    entry.data[CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] = enabled
    schema = asyncio.run(flow.async_step_settings())["data_schema"]
    assert schema({CONF_UPDATE_INTERVAL: 3600})[CONF_ENERGY_STATISTICS_ENABLED] is enabled
    asyncio.run(flow.async_step_settings({CONF_UPDATE_INTERVAL: 3600}))
    assert flow.hass.config_entries.async_update_entry.call_args.kwargs["data"][CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] is enabled


def test_new_entry_explicitly_defaults_energy_statistics_on():
    flow = CSGConfigFlow()
    flow._reauth_entry = None
    flow.async_create_entry = Mock(side_effect=lambda **kwargs: kwargs)
    result = asyncio.run(flow.create_or_update_config_entry("synthetic", "sms", "", "fictional-user"))
    assert result["data"][CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] is True


def test_missing_field_is_saved_enabled_when_editing_other_settings():
    flow, _ = make_flow()
    asyncio.run(flow.async_step_settings({CONF_UPDATE_INTERVAL: 3600}))
    settings = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"][CONF_SETTINGS]
    assert settings[CONF_ENERGY_STATISTICS_ENABLED] is True


def test_energy_setting_translations_include_retention_and_manual_selection():
    root = Path(__file__).parents[1] / "custom_components" / "csg_plus"
    for name in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
        setting = json.loads((root / name).read_text(encoding="utf-8"))["options"]["step"]["settings"]
        assert setting["data"][CONF_ENERGY_STATISTICS_ENABLED]
        description = setting["data_description"][CONF_ENERGY_STATISTICS_ENABLED]
        assert ("retains" in description and "manually" in description) or ("保留" in description and "手动" in description)
