"""Coordinators request the entry's bridge after durable fact writes/passes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import custom_components.csg as integration
from custom_components.csg import energy_statistics
from custom_components.csg.const import CONF_ENERGY_STATISTICS_ENABLED, CONF_HISTORY_START_MONTH, CONF_SETTINGS, DOMAIN
from test_history_coordinator import rig as history_rig
from test_history_store_integration import rig as recent_rig


@pytest.mark.parametrize("kind", ["realtime", "billing"])
def test_recent_daily_shadow_writes_request_bridge_after_store_write(recent_rig, kind):
    async def scenario():
        objects = await recent_rig.build()
        upsert = AsyncMock(wraps=objects.history.async_upsert_daily_usage)
        objects.history.async_upsert_daily_usage = upsert
        counts = []
        bridge = SimpleNamespace(request_sync=Mock(side_effect=lambda: counts.append(upsert.await_count)))
        getattr(objects, kind).energy_statistics_bridge = bridge
        await getattr(objects, kind)._async_update_data()
        assert counts == list(range(1, upsert.await_count + 1))
        assert upsert.await_count > 0
    asyncio.run(scenario())


def test_history_requests_once_per_completed_pass_not_per_month(history_rig):
    history_rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2024-01"
    async def scenario():
        history = await history_rig.build()
        bridge = SimpleNamespace(request_sync=Mock())
        history.energy_statistics_bridge = bridge
        await history_rig.sync(history)
        assert len([call for call in history_rig.client.calls if call[0] == "daily"]) == 2
        bridge.request_sync.assert_called_once_with()
        await history.async_shutdown()
    asyncio.run(scenario())


def test_entry_initial_request_and_all_producer_shutdown_order(recent_rig, monkeypatch):
    events = []
    bridge = SimpleNamespace(
        request_sync=Mock(side_effect=lambda: events.append("initial")),
        stop_requests=Mock(side_effect=lambda: events.append("close requests")),
        async_shutdown=AsyncMock(side_effect=lambda: events.append("bridge")),
    )
    monkeypatch.setattr(integration, "EnergyStatisticsBridge", Mock(return_value=bridge))
    monkeypatch.setattr(integration.CSGClient, "load", lambda _: recent_rig.client)
    recent_rig.hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(side_effect=lambda *args: events.append("forward")),
        async_unload_platforms=AsyncMock(side_effect=lambda *args: events.append("platforms") or True),
    )
    async def scenario():
        assert await integration.async_setup_entry(recent_rig.hass, recent_rig.entry)
        runtime = recent_rig.hass.data[DOMAIN][recent_rig.entry.entry_id]
        assert runtime["energy_statistics_bridge"] is bridge
        integration.EnergyStatisticsBridge.assert_called_once_with(recent_rig.hass, recent_rig.entry, runtime["history_store"])
        assert events == ["forward", "initial"]
        runtime["history_coordinator"] = SimpleNamespace(async_shutdown=AsyncMock(side_effect=lambda: events.append("history")))
        runtime["billing_coordinator"] = SimpleNamespace(async_shutdown=AsyncMock(side_effect=lambda: events.append("billing")))
        runtime["realtime_coordinator"] = SimpleNamespace(async_shutdown=AsyncMock(side_effect=lambda: events.append("realtime")))
        assert await integration.async_unload_entry(recent_rig.hass, recent_rig.entry)
        assert events == ["forward", "initial", "close requests", "history", "billing", "realtime", "bridge", "platforms"]
        assert await integration.async_unload_entry(recent_rig.hass, recent_rig.entry)
        bridge.async_shutdown.assert_awaited_once()
    asyncio.run(scenario())


def test_optional_recorder_failure_does_not_fail_entry_setup_or_unload(recent_rig, monkeypatch, caplog):
    tasks = []
    def create_task(hass, coroutine, name, eager_start):
        task = asyncio.create_task(coroutine, name=name)
        tasks.append(task)
        return task
    recent_rig.entry.async_create_background_task = create_task
    recent_rig.entry.data[CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] = True
    recent_rig.hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(), async_unload_platforms=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(integration.CSGClient, "load", lambda _: recent_rig.client)
    monkeypatch.setattr(energy_statistics, "get_instance", Mock(side_effect=KeyError("Recorder absent")))
    async def scenario():
        assert await integration.async_setup_entry(recent_rig.hass, recent_rig.entry)
        await tasks[0]
        assert "history_store" in recent_rig.hass.data[DOMAIN][recent_rig.entry.entry_id]
        recent_rig.hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        assert await integration.async_unload_entry(recent_rig.hass, recent_rig.entry)
        assert recent_rig.entry.entry_id not in recent_rig.hass.data[DOMAIN]
        recent_rig.hass.config_entries.async_unload_platforms.assert_awaited_once()
        assert all(task.done() for task in tasks)
    asyncio.run(scenario())
    assert "pass unavailable" in caplog.text
    assert "final materialization did not complete" in caplog.text
    assert "HistoryStore facts remain authoritative" in caplog.text
