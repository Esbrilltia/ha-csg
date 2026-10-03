"""M7 permanent identity and domain-scoped configuration, using fictional data."""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import HANDLERS, ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.data_entry_flow import AbortFlow

import custom_components.csg_plus as integration
from custom_components.csg_plus import config_flow
from custom_components.csg_plus.config_flow import CSGConfigFlow, CSGOptionsFlowHandler
from custom_components.csg_plus.const import (
    CONF_ACCOUNT_NUMBER, CONF_AUTH_TOKEN, CONF_ELE_ACCOUNTS, CONF_SETTINGS, CONF_TARIFF_PROFILES, DOMAIN,
)
from custom_components.csg_plus.csg_client import CSGElectricityAccount
from test_energy_statistics_recorder import ACCOUNT, recorder_world

USERNAME = "fictional-standalone-user"


def old_entry(unique_id=None):
    """Represent upstream identity without loading another implementation/client."""
    return ConfigEntry(
        version=1, minor_version=1, domain="csg", title="Fictional upstream CSG",
        data={
            CONF_USERNAME: USERNAME, CONF_AUTH_TOKEN: "fictional-old-token",
            CONF_ELE_ACCOUNTS: {ACCOUNT: CSGElectricityAccount(ACCOUNT).dump()},
            CONF_SETTINGS: {"fictional_old_setting": True},
        },
        options={}, source="user", unique_id=unique_id or f"CSG-{USERNAME}",
        discovery_keys={}, subentries_data=None, entry_id="fictional-upstream-entry",
    )


def test_permanent_package_and_config_flow_identity():
    assert DOMAIN == "csg_plus"
    assert Path(integration.__file__).parent.name == "csg_plus"
    assert HANDLERS["csg_plus"] is CSGConfigFlow
    assert not hasattr(integration, "async_migrate_entry")


@pytest.mark.parametrize("legacy_unique_id", [f"CSG-{USERNAME}", f"csg_plus-{USERNAME}"])
def test_same_user_can_create_plus_entry_without_migrating_csg(recorder_world, legacy_unique_id):
    async def scenario():
        async with recorder_world() as world:
            old = old_entry(legacy_unique_id)
            world.hass.config_entries._entries[old.entry_id] = old
            original = deepcopy(old.as_dict())
            flow = CSGConfigFlow()
            flow.hass = world.hass
            flow.handler = DOMAIN
            flow.context = {"source": "user"}
            await flow.check_and_set_unique_id(USERNAME)
            assert flow.unique_id == f"csg_plus-{USERNAME}"
            assert flow._async_current_entries() == []
            result = await flow.create_or_update_config_entry(
                "fictional-new-token", "sms", "", USERNAME,
            )
            assert result["title"] == f"CSG Plus-{USERNAME}"
            assert result["data"][CONF_ELE_ACCOUNTS] == {}
            assert CONF_TARIFF_PROFILES not in result["data"][CONF_SETTINGS]
            assert "fictional_old_setting" not in result["data"][CONF_SETTINGS]
            new = ConfigEntry(
                version=1, minor_version=1, domain=DOMAIN, title=result["title"],
                data=result["data"], options={}, source="user", unique_id=flow.unique_id,
                discovery_keys={}, subentries_data=None, entry_id="fictional-plus-entry",
            )
            world.hass.config_entries._entries[new.entry_id] = new
            assert world.hass.config_entries.async_entries("csg") == [old]
            assert world.hass.config_entries.async_entries(DOMAIN) == [new]
            assert flow._async_current_entries() == [new]
            with pytest.raises(AbortFlow, match="already_configured"):
                await flow.check_and_set_unique_id(USERNAME)
            assert old.as_dict() == original
    asyncio.run(scenario())


def test_plus_options_offer_payment_account_already_present_in_csg(recorder_world, monkeypatch):
    async def scenario():
        async with recorder_world() as world:
            old = old_entry()
            world.hass.config_entries._entries[old.entry_id] = old
            world.hass.config_entries._entries[world.entry.entry_id] = world.entry
            world.hass.config_entries.async_update_entry(world.entry, data={
                **world.entry.data, CONF_AUTH_TOKEN: "fictional-new-token", CONF_ELE_ACCOUNTS: {},
            })
            before = deepcopy(old.as_dict())
            account = CSGElectricityAccount(ACCOUNT, user_name="Fictional user", address="Fictional address")
            client = SimpleNamespace(
                verify_login=lambda: True, initialize=lambda: None,
                get_all_electricity_accounts=lambda: [account],
            )
            monkeypatch.setattr(config_flow.CSGClient, "load", lambda _: client)
            flow = CSGOptionsFlowHandler(world.entry)
            flow.hass = world.hass
            form = await flow.async_step_add_account()
            assert form["step_id"] == "add_account"
            assert form["data_schema"]({CONF_ACCOUNT_NUMBER: ACCOUNT}) == {CONF_ACCOUNT_NUMBER: ACCOUNT}
            assert old.as_dict() == before
            assert world.entry.data[CONF_ELE_ACCOUNTS] == {}
    asyncio.run(scenario())
