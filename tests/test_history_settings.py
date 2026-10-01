"""History start setting validation, clearing, translations, and reload."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.csg.config_flow import CSGOptionsFlowHandler
from custom_components.csg.const import (
    CONF_BILLING_UPDATE_TIME, CONF_HISTORY_START_MONTH, CONF_SETTINGS, CONF_UPDATE_INTERVAL,
)


def make_flow(start=""):
    entry = SimpleNamespace(entry_id="synthetic-entry", data={CONF_SETTINGS: {
        CONF_UPDATE_INTERVAL: 3600, CONF_BILLING_UPDATE_TIME: "12:15:00", CONF_HISTORY_START_MONTH: start,
    }})
    flow = CSGOptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock(), async_reload=AsyncMock()))
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_create_entry = lambda **kwargs: kwargs
    return flow, entry


@pytest.mark.parametrize("value", ["2024-02", "0001-01", "", None, False, 0, "bad", "2024-2", "202402", "2024-13", "2024-00", "0000-01", " 2024-02", "2024-02\n"])
def test_history_setting_validates_and_reloads_only_on_valid_input(value):
    flow, entry = make_flow("2023-01")
    result = asyncio.run(flow.async_step_settings({CONF_UPDATE_INTERVAL: 3600, CONF_HISTORY_START_MONTH: value}))
    config = flow.hass.config_entries
    if value in ("2024-02", "0001-01", ""):
        assert result == {"title": "", "data": {}}
        settings = config.async_update_entry.call_args.kwargs["data"][CONF_SETTINGS]
        assert settings[CONF_HISTORY_START_MONTH] == value
        assert settings[CONF_UPDATE_INTERVAL] == 3600
        assert settings[CONF_BILLING_UPDATE_TIME] == "12:15:00"
        config.async_reload.assert_awaited_once_with(entry.entry_id)
    else:
        assert result["errors"] == {CONF_HISTORY_START_MONTH: "invalid_history_start_month"}
        config.async_update_entry.assert_not_called()
        config.async_reload.assert_not_awaited()


def test_optional_history_schema_allows_empty_or_omitted_to_disable():
    flow, _ = make_flow("2023-01")
    result = asyncio.run(flow.async_step_settings())
    schema = result["data_schema"]
    parsed = schema({CONF_UPDATE_INTERVAL: 3600, CONF_BILLING_UPDATE_TIME: "12:15:00", CONF_HISTORY_START_MONTH: ""})
    assert parsed[CONF_HISTORY_START_MONTH] == ""
    omitted = schema({CONF_UPDATE_INTERVAL: 3600, CONF_BILLING_UPDATE_TIME: "12:15:00"})
    assert omitted[CONF_HISTORY_START_MONTH] == ""
    marker = next(key for key in schema.schema if str(key) == CONF_HISTORY_START_MONTH)
    assert marker.description["suggested_value"] == "2023-01"


def test_history_translations_explain_permission_bound_and_empty_setting():
    root = Path(__file__).parents[1] / "custom_components" / "csg"
    for name in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))["options"]
        setting = data["step"]["settings"]
        assert "YYYY-MM" in setting["data"][CONF_HISTORY_START_MONTH]
        assert setting["data_description"][CONF_HISTORY_START_MONTH]
        assert data["error"]["invalid_history_start_month"]
