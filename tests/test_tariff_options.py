"""Options explicitly persist user choices, preserving auth and other settings."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.csg_plus.config_flow import CSGConfigFlow, CSGOptionsFlowHandler
from custom_components.csg_plus.const import (
    CONF_ACCOUNT_NUMBER, CONF_AUTH_TOKEN, CONF_ELE_ACCOUNTS, CONF_SETTINGS,
    CONF_TARIFF_PROFILES, CONF_UPDATE_INTERVAL,
)
from custom_components.csg_plus.csg_client import CSGElectricityAccount
from test_tariff import CHOICES

ACCOUNT = "fictional-options-account"
OTHER = "fictional-other-options-account"


def flow_rig(area="080000", selections=None):
    entry = SimpleNamespace(entry_id="fictional-options-entry", data={
        CONF_AUTH_TOKEN: "fictional-token",
        CONF_ELE_ACCOUNTS: {ACCOUNT: CSGElectricityAccount(ACCOUNT, area_code=area).dump(), OTHER: CSGElectricityAccount(OTHER, area_code="030000").dump()},
        CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 3600},
    })
    if selections is not None:
        entry.data[CONF_SETTINGS][CONF_TARIFF_PROFILES] = deepcopy(selections)
    entries = SimpleNamespace(async_update_entry=Mock(), async_reload=AsyncMock(), async_entries=lambda _: [entry])
    flow = CSGOptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(config_entries=entries)
    flow.async_show_form = lambda **kw: kw
    flow.async_abort = lambda **kw: kw
    flow.async_create_entry = lambda **kw: kw
    return entry, entries, flow


@pytest.mark.parametrize("choice", CHOICES + [{"scheme": "unconfigured", "multi_person": False, "tou": False}])
def test_each_choice_saves_only_after_confirmation_and_reloads(choice, caplog):
    other = {"scheme": "ladder", "multi_person": True, "tou": False}
    entry, entries, flow = flow_rig(selections={OTHER: other})
    before = deepcopy(entry.data)
    result = asyncio.run(flow.async_step_tariff_account())
    assert result["step_id"] == "tariff_account"
    schema = result["data_schema"]
    assert schema({CONF_ACCOUNT_NUMBER: ACCOUNT}) == {CONF_ACCOUNT_NUMBER: ACCOUNT}
    with pytest.raises(Exception):
        schema({CONF_ACCOUNT_NUMBER: OTHER})
    form = asyncio.run(flow.async_step_tariff_account({CONF_ACCOUNT_NUMBER: ACCOUNT}))
    assert form["step_id"] == "tariff_profile"
    assert form["data_schema"]({}) == {"scheme": "unconfigured", "multi_person": False, "tou": False}
    entries.async_update_entry.assert_not_called()
    assert entry.data == before
    assert asyncio.run(flow.async_step_tariff_profile(choice)) == {"title": "", "data": {}}
    data = entries.async_update_entry.call_args.kwargs["data"]
    assert data[CONF_SETTINGS][CONF_TARIFF_PROFILES] == {OTHER: other, ACCOUNT: choice}
    assert data[CONF_SETTINGS][CONF_UPDATE_INTERVAL] == 3600
    assert data[CONF_AUTH_TOKEN] == "fictional-token" and data[CONF_ELE_ACCOUNTS] == before[CONF_ELE_ACCOUNTS]
    assert entry.data == before
    entries.async_reload.assert_awaited_once_with(entry.entry_id)
    assert ACCOUNT not in caplog.text


@pytest.mark.parametrize("choice", [
    {"scheme": "combined", "tou": True}, {"scheme": "combined", "multi_person": True},
    {"scheme": "ladder", "tou": "yes"}, {"scheme": "unconfigured", "multi_person": True},
])
def test_invalid_options_are_rejected_without_writes(choice):
    entry, entries, flow = flow_rig()
    asyncio.run(flow.async_step_tariff_account({CONF_ACCOUNT_NUMBER: ACCOUNT}))
    result = asyncio.run(flow.async_step_tariff_profile(choice))
    assert result["errors"] == {"base": "invalid_tariff_profile"}
    entries.async_update_entry.assert_not_called()
    entries.async_reload.assert_not_called()
    assert CONF_TARIFF_PROFILES not in entry.data[CONF_SETTINGS]


def test_other_areas_cannot_be_configured():
    _, entries, flow = flow_rig(area="030000")
    assert asyncio.run(flow.async_step_tariff_account()) == {"reason": "no_supported_tariff_accounts"}
    entries.async_update_entry.assert_not_called()


def test_new_entry_unconfigured_and_reauth_preserves_tariff_settings():
    entry, entries, _ = flow_rig(selections={ACCOUNT: CHOICES[3]})
    flow = CSGConfigFlow()
    flow.hass = SimpleNamespace(config_entries=entries)
    flow.async_create_entry = lambda **kw: kw
    flow.async_abort = lambda **kw: kw
    result = asyncio.run(flow.create_or_update_config_entry("fictional-new-token", "sms", "", "fictional-user"))
    assert CONF_TARIFF_PROFILES not in result["data"][CONF_SETTINGS]
    before = deepcopy(entry.data)
    flow._reauth_entry = entry
    assert asyncio.run(flow.create_or_update_config_entry("fictional-new-token", "sms", "", "fictional-user")) == {"reason": "reauth_successful"}
    saved = entries.async_update_entry.call_args.kwargs["data"]
    assert saved[CONF_SETTINGS] == before[CONF_SETTINGS]
    assert saved[CONF_AUTH_TOKEN] == "fictional-new-token"


def test_added_account_starts_unconfigured_and_preserves_other_selections(caplog):
    entry, entries, flow = flow_rig(selections={ACCOUNT: CHOICES[0], "fictional-new-account": CHOICES[3]})
    account = CSGElectricityAccount("fictional-new-account", area_code="080000")
    flow.all_electricity_accounts = [account]
    result = asyncio.run(flow.async_step_add_account({CONF_ACCOUNT_NUMBER: account.account_number}))
    assert result == {"title": "", "data": {}}
    settings = entries.async_update_entry.call_args.kwargs["data"][CONF_SETTINGS]
    assert settings[CONF_TARIFF_PROFILES] == {ACCOUNT: CHOICES[0]}
    assert account.account_number not in caplog.text


def test_general_settings_updates_keep_tariff_selections():
    _, entries, flow = flow_rig(selections={ACCOUNT: CHOICES[3]})
    asyncio.run(flow.async_step_settings({CONF_UPDATE_INTERVAL: 7200}))
    saved = entries.async_update_entry.call_args.kwargs["data"][CONF_SETTINGS]
    assert saved[CONF_TARIFF_PROFILES] == {ACCOUNT: CHOICES[3]}
