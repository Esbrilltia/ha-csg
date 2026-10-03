"""Unit tests for CSG configuration-flow behavior."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import frame
from homeassistant.helpers.storage import Store

from custom_components.csg_plus import config_flow
from custom_components.csg_plus.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_LOGIN_TYPE,
    CONF_REFRESH_QR_CODE,
    CONF_SETTINGS,
    CONF_SMS_CODE,
    CONF_UPDATE_INTERVAL,
    CONF_UPDATED_AT,
    DOMAIN,
)
from custom_components.csg_plus.csg_client import CSGElectricityAccount, LoginType
from custom_components.csg_plus.config_flow import CSGOptionsFlowHandler


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


# All credentials and account/Store data below are synthetic. The real HA flow
# manager and domain indexes run; only cloud I/O and entry setup/reload are mocked.
SYNTHETIC_USERNAME = "00000000000"
OTHER_SYNTHETIC_USERNAME = "00000000001"
SMS_METHODS = ["sms_login", "sms_pwd_login"]
QR_METHODS = ["csg_qr_login", "wx_qr_login", "ali_qr_login"]


@pytest.fixture
def config_flow_world(tmp_path, monkeypatch):
    if not hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: None, raising=False)

    @asynccontextmanager
    async def world():
        hass = HomeAssistant(str(tmp_path))
        hass.config.skip_pip = True
        loader.async_setup(hass)
        frame.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        client = Mock(spec=config_flow.CSGClient)
        client.api_send_login_sms.return_value = None
        client.api_login_with_sms_code.return_value = "synthetic-renewed-token"
        client.api_login_with_password_and_sms_code.return_value = "synthetic-renewed-token"
        client.api_create_login_qr_code.return_value = (
            "synthetic-login-id", "https://example.invalid/synthetic-qr",
        )
        client.api_get_qr_login_status.return_value = (True, "synthetic-renewed-token")
        client.api_get_user_info.return_value = {"mobile": SYNTHETIC_USERNAME}
        monkeypatch.setattr(config_flow, "CSGClient", lambda: client)
        reload_entry = AsyncMock(return_value=True)
        monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
        monkeypatch.setattr(hass.config_entries, "async_setup", AsyncMock(return_value=True))
        stores = {}

        def add_entry(username=SYNTHETIC_USERNAME, *, domain=DOMAIN, unique_id=None):
            entry = ConfigEntry(
                version=1, minor_version=1, domain=domain, title="Synthetic CSG account",
                data={
                    CONF_USERNAME: username, CONF_AUTH_TOKEN: "synthetic-expired-token",
                    CONF_LOGIN_TYPE: LoginType.LOGIN_TYPE_CSG_QR, CONF_UPDATED_AT: "0",
                    CONF_ELE_ACCOUNTS: {"synthetic-payment-account": {"synthetic_fact": 0}},
                    CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 3600, "synthetic_setting": True},
                },
                options={"synthetic_option": True}, source=SOURCE_USER,
                unique_id=unique_id or f"{domain}-{username}",
                discovery_keys={}, subentries_data=None,
                entry_id=f"synthetic-{domain}-{username}",
            )
            hass.config_entries._entries[entry.entry_id] = entry
            path = Path(Store(hass, 1, f"{domain}.history_store.{entry.entry_id}").path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"synthetic_owner": entry.unique_id}), encoding="utf-8")
            stores[path] = (path.read_bytes(), path.stat().st_mtime_ns)
            return entry

        def assert_stores_unchanged():
            assert set(tmp_path.joinpath(".storage").glob("*.history_store.*")) == set(stores)
            for path, (content, mtime) in stores.items():
                assert path.read_bytes() == content
                assert path.stat().st_mtime_ns == mtime

        try:
            yield SimpleNamespace(
                hass=hass, client=client, reload=reload_entry, add_entry=add_entry,
                assert_stores_unchanged=assert_stores_unchanged,
            )
        finally:
            await hass.async_stop(force=True)

    return world


async def start_login(world, method, entry=None):
    """Follow the real reauth confirmation/menu or normal creation menu."""
    manager = world.hass.config_entries.flow
    context = {"source": SOURCE_USER}
    if entry is not None:
        context = {"source": SOURCE_REAUTH, "entry_id": entry.entry_id}
    result = await manager.async_init(DOMAIN, context=context)
    if entry is not None:
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    result = await manager.async_configure(result["flow_id"], {"next_step_id": method})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == ("qr_login" if method in QR_METHODS else method)
    return result


async def submit_sms_username(world, result, method, username):
    data = {CONF_USERNAME: username}
    if method == "sms_pwd_login":
        data[CONF_PASSWORD] = "synthetic-pwd"
    return await world.hass.config_entries.flow.async_configure(result["flow_id"], data)


def assert_reauth_success(world, entry, before, result, login_type):
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    world.reload.assert_awaited_once_with(entry.entry_id)
    assert world.hass.config_entries.async_entries(DOMAIN) == [entry]
    after = entry.as_dict()
    for key in before.keys() - {"data", "modified_at"}:
        assert after[key] == before[key], key
    assert entry.data[CONF_AUTH_TOKEN] == "synthetic-renewed-token"
    assert entry.data[CONF_LOGIN_TYPE] == login_type
    assert entry.data[CONF_USERNAME] == SYNTHETIC_USERNAME
    assert entry.data[CONF_UPDATED_AT] != "0"
    assert entry.data[CONF_ELE_ACCOUNTS] == before["data"][CONF_ELE_ACCOUNTS]
    assert entry.data[CONF_SETTINGS] == before["data"][CONF_SETTINGS]
    world.assert_stores_unchanged()


@pytest.mark.parametrize("method", SMS_METHODS)
def test_normal_sms_duplicate_is_rejected_before_sending(config_flow_world, method):
    async def scenario():
        async with config_flow_world() as world:
            entry = world.add_entry()
            before = deepcopy(entry.as_dict())
            result = await start_login(world, method)
            result = await submit_sms_username(world, result, method, SYNTHETIC_USERNAME)
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "already_configured"
            world.client.api_send_login_sms.assert_not_called()
            world.reload.assert_not_awaited()
            assert world.hass.config_entries.async_entries(DOMAIN) == [entry]
            assert entry.as_dict() == before
            world.assert_stores_unchanged()
    run(scenario())


@pytest.mark.parametrize("method", SMS_METHODS)
def test_sms_self_reauth_reaches_verification_and_updates_same_entry(config_flow_world, method):
    async def scenario():
        async with config_flow_world() as world:
            entry = world.add_entry()
            before = deepcopy(entry.as_dict())
            result = await start_login(world, method, entry)
            result = await submit_sms_username(world, result, method, SYNTHETIC_USERNAME)
            assert result["type"] is FlowResultType.FORM, (
                result, world.client.api_send_login_sms.call_count,
            )
            assert result["step_id"] == "validate_sms_code"
            world.client.api_send_login_sms.assert_called_once_with(SYNTHETIC_USERNAME)
            assert entry.as_dict() == before
            result = await world.hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_SMS_CODE: "000000"},
            )
            if method == "sms_login":
                login_type = LoginType.LOGIN_TYPE_SMS
                world.client.api_login_with_sms_code.assert_called_once_with(SYNTHETIC_USERNAME, "000000")
                world.client.api_login_with_password_and_sms_code.assert_not_called()
            else:
                login_type = LoginType.LOGIN_TYPE_PWD_AND_SMS
                world.client.api_login_with_password_and_sms_code.assert_called_once_with(
                    SYNTHETIC_USERNAME, "synthetic-pwd", "000000",
                )
                world.client.api_login_with_sms_code.assert_not_called()
            assert_reauth_success(world, entry, before, result, login_type)
    run(scenario())


@pytest.mark.parametrize("method", SMS_METHODS)
@pytest.mark.parametrize("other_entry_exists", [False, True])
def test_sms_reauth_rejects_wrong_account_and_other_entry(config_flow_world, method, other_entry_exists):
    async def scenario():
        async with config_flow_world() as world:
            entry = world.add_entry()
            if other_entry_exists:
                world.add_entry(OTHER_SYNTHETIC_USERNAME)
            before = [deepcopy(item.as_dict()) for item in world.hass.config_entries.async_entries()]
            result = await start_login(world, method, entry)
            result = await submit_sms_username(world, result, method, OTHER_SYNTHETIC_USERNAME)
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "unique_id_mismatch"
            world.client.api_send_login_sms.assert_not_called()
            world.client.api_login_with_sms_code.assert_not_called()
            world.client.api_login_with_password_and_sms_code.assert_not_called()
            world.reload.assert_not_awaited()
            assert [item.as_dict() for item in world.hass.config_entries.async_entries()] == before
            world.assert_stores_unchanged()
    run(scenario())


@pytest.mark.parametrize("legacy_unique_id", [f"csg-{SYNTHETIC_USERNAME}", f"csg_plus-{SYNTHETIC_USERNAME}"])
def test_sms_self_reauth_with_same_legacy_account_keeps_csg_untouched(config_flow_world, legacy_unique_id):
    async def scenario():
        async with config_flow_world() as world:
            old = world.add_entry(domain="csg", unique_id=legacy_unique_id)
            old_before = deepcopy(old.as_dict())
            entry = world.add_entry()
            before = deepcopy(entry.as_dict())
            result = await start_login(world, "sms_login", entry)
            result = await submit_sms_username(world, result, "sms_login", SYNTHETIC_USERNAME)
            assert result["step_id"] == "validate_sms_code"
            world.client.api_send_login_sms.assert_called_once_with(SYNTHETIC_USERNAME)
            result = await world.hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_SMS_CODE: "000000"},
            )
            assert_reauth_success(world, entry, before, result, LoginType.LOGIN_TYPE_SMS)
            assert world.hass.config_entries.async_entries("csg") == [old]
            assert old.as_dict() == old_before
    run(scenario())


@pytest.mark.parametrize("method", QR_METHODS)
@pytest.mark.parametrize("identity", ["self", "wrong-account", "other-entry"])
def test_qr_reauth_checks_authenticated_mobile(config_flow_world, method, identity):
    async def scenario():
        async with config_flow_world() as world:
            entry = world.add_entry()
            before = deepcopy(entry.as_dict())
            if identity == "other-entry":
                world.add_entry(OTHER_SYNTHETIC_USERNAME)
            entries_before = [deepcopy(item.as_dict()) for item in world.hass.config_entries.async_entries()]
            mobile = SYNTHETIC_USERNAME if identity == "self" else OTHER_SYNTHETIC_USERNAME
            world.client.api_get_user_info.return_value = {"mobile": mobile}
            result = await start_login(world, method, entry)
            result = await world.hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_REFRESH_QR_CODE: False},
            )
            world.client.api_get_qr_login_status.assert_called_once_with("synthetic-login-id")
            world.client.api_get_user_info.assert_called_once_with()
            world.client.api_send_login_sms.assert_not_called()
            if identity == "self":
                login_type = {
                    "csg_qr_login": LoginType.LOGIN_TYPE_CSG_QR,
                    "wx_qr_login": LoginType.LOGIN_TYPE_WX_QR,
                    "ali_qr_login": LoginType.LOGIN_TYPE_ALI_QR,
                }[method]
                assert_reauth_success(world, entry, before, result, login_type)
            else:
                assert result["type"] is FlowResultType.ABORT
                assert result["reason"] == "unique_id_mismatch"
                world.reload.assert_not_awaited()
                assert [item.as_dict() for item in world.hass.config_entries.async_entries()] == entries_before
                world.assert_stores_unchanged()
    run(scenario())


def test_real_sms_flow_creates_plus_entry_alongside_same_legacy_account(config_flow_world):
    async def scenario():
        async with config_flow_world() as world:
            old = world.add_entry(domain="csg", unique_id=f"csg_plus-{SYNTHETIC_USERNAME}")
            before = deepcopy(old.as_dict())
            result = await start_login(world, "sms_login")
            result = await submit_sms_username(world, result, "sms_login", SYNTHETIC_USERNAME)
            assert result["step_id"] == "validate_sms_code"
            world.client.api_send_login_sms.assert_called_once_with(SYNTHETIC_USERNAME)
            result = await world.hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_SMS_CODE: "000000"},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            entry = result["result"]
            assert entry.domain == DOMAIN
            assert entry.unique_id == f"csg_plus-{SYNTHETIC_USERNAME}"
            assert entry.data[CONF_AUTH_TOKEN] == "synthetic-renewed-token"
            assert world.hass.config_entries.async_entries(DOMAIN) == [entry]
            assert world.hass.config_entries.async_entries("csg") == [old]
            assert old.as_dict() == before
            world.reload.assert_not_awaited()
            world.assert_stores_unchanged()
    run(scenario())


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
