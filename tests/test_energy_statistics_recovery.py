"""A-3: optional materialization faults recover through real HA lifecycles."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import datetime as dt
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.components import sensor as ha_sensor
from homeassistant.const import CONF_USERNAME, STATE_UNAVAILABLE
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers import storage as ha_storage
from homeassistant.components.recorder.statistics import async_add_external_statistics

import custom_components.csg_plus as integration
from custom_components.csg_plus import energy_statistics as energy, sensor
from custom_components.csg_plus import history_coordinator as history
from custom_components.csg_plus.const import (
    CONF_AUTH_TOKEN, CONF_ENERGY_STATISTICS_ENABLED, CONF_HISTORY_START_MONTH, CONF_SETTINGS,
    CONF_UPDATE_INTERVAL, DOMAIN,
)
from test_energy_statistics_recorder import ACCOUNT, recorder_world
from test_energy_statistics_unload import Cloud, DAY, write
from test_energy_statistics_pending import wait_entered
from custom_components.csg_plus.energy_statistics import build_statistics, statistic_metadata
from custom_components.csg_plus.history_store import CSGHistoryStore


@pytest.fixture
def platform_world(recorder_world, monkeypatch):
    """No replacement Core setup/unload, platform forwarding or entity removal."""
    @asynccontextmanager
    async def world(*, before_setup=None, sync_on_setup=True):
        async with recorder_world() as base:
            await base.bridge.async_shutdown()
            hass, entry = base.hass, base.entry
            hass.config.skip_pip = True  # All tested requirements are in uv.lock.
            await ar.async_load(hass)
            dr.async_setup(hass)
            await dr.async_load(hass)
            await er.async_load(hass)
            hass.config_entries._entries[entry.entry_id] = entry
            hass.config_entries.async_update_entry(entry, data={
                **entry.data, CONF_AUTH_TOKEN: "synthetic", CONF_USERNAME: "synthetic",
                CONF_SETTINGS: {CONF_ENERGY_STATISTICS_ENABLED: True, CONF_UPDATE_INTERVAL: 3600},
            })
            cloud = Cloud(value=1)
            monkeypatch.setattr(integration.CSGClient, "load", lambda _: cloud)
            monkeypatch.setattr(sensor, "_csg_today", lambda: dt.date(2026, 9, 3))
            # Bootstrap the real EntityComponent without unrelated discovery /
            # HTTP dependencies. Core entry setup, forwarding and both sensor
            # setup/unload implementations remain entirely real.
            await loader.async_get_integration(hass, "sensor")
            assert await ha_sensor.async_setup(hass, {})
            hass.config.components.update({DOMAIN, "sensor"})
            if before_setup is not None:
                await before_setup(base)
            assert await hass.config_entries.async_setup(entry.entry_id)
            assert entry.state is ConfigEntryState.LOADED
            await hass.async_block_till_done()
            runtime = hass.data[DOMAIN][entry.entry_id]
            if sync_on_setup:
                await base.sync(runtime["energy_statistics_bridge"])
            entities = {entity.entity_id for entity in hass.data[ha_sensor.DATA_COMPONENT].entities}
            assert len(entities) == 16
            base.cloud = cloud
            base.entities = entities
            base.component = hass.data[ha_sensor.DATA_COMPONENT]
            assert len(tuple(base.component.entities)) == 16
            base.runtime = lambda: hass.data[DOMAIN][entry.entry_id]
            try:
                yield base
            finally:
                if entry.state is ConfigEntryState.LOADED:
                    assert await hass.config_entries.async_unload(entry.entry_id)
    return world


def test_real_platform_setup_unload_setup(platform_world):
    async def scenario():
        async with platform_world() as world:
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            assert world.entry.state is ConfigEntryState.NOT_LOADED
            assert not tuple(world.component.entities)
            # HA preserves registry placeholders; no active platform entity
            # remains, and any placeholder is unavailable with restored=True.
            for entity_id in world.entities:
                if state := world.hass.states.get(entity_id):
                    assert state.state == STATE_UNAVAILABLE
                    assert state.attributes["restored"] is True
            assert world.entry.entry_id not in world.hass.data[DOMAIN]
            assert await world.hass.config_entries.async_setup(world.entry.entry_id)
            await world.hass.async_block_till_done()
            assert world.entry.state is ConfigEntryState.LOADED
            assert set(world.hass.states.async_entity_ids("sensor")) == world.entities
    asyncio.run(scenario())


@asynccontextmanager
async def materialization_fault(world, monkeypatch, kind):
    runtime = world.runtime()
    bridge = runtime["energy_statistics_bridge"]
    metadata = statistic_metadata(ACCOUNT)
    rows = build_statistics({DAY: {"kwh": 1}})
    if kind == "metadata":
        # A real incompatible SQLite metadata row, changed/restored only through
        # the public import API; no synthetic query or private statistics write.
        async_add_external_statistics(world.hass, {**metadata, "unit_of_measurement": "Wh"}, rows)
        await world.drain()
    try:
        with monkeypatch.context() as fault:
            if kind == "absent":
                fault.setattr(energy, "get_instance", Mock(side_effect=KeyError("synthetic Recorder absent")))
            elif kind == "not-ready":
                fault.setattr(world.recorder, "async_db_ready", asyncio.get_running_loop().create_future())
            elif kind == "query":
                fault.setattr(energy, "statistics_during_period", Mock(side_effect=RuntimeError("synthetic query failure")))
            elif kind == "stopping":
                fault.setattr(world.recorder, "is_alive", lambda: False)
            elif kind in ("confirmation", "finalization"):
                async def unconfirmed(*args):
                    await asyncio.Event().wait()
                fault.setattr(bridge, "_async_read_back", unconfirmed)
                fault.setattr(energy, "_CONFIRMATION_TIMEOUT" if kind == "confirmation" else "_FINALIZATION_TIMEOUT", 0.05)
            elif kind == "durability":
                fault.setattr(runtime["history_store"], "async_ensure_persisted", AsyncMock(return_value=False))
            yield
    finally:
        if kind == "metadata":
            async_add_external_statistics(world.hass, metadata, rows)
            await world.drain()


async def assert_live(world):
    assert world.entry.state is ConfigEntryState.LOADED
    assert len(tuple(world.component.entities)) == 16
    assert set(entity.entity_id for entity in world.component.entities) == world.entities
    runtime = world.runtime()
    assert runtime["energy_statistics_bridge"].enabled
    assert not runtime["energy_statistics_bridge"]._shutdown
    return runtime


async def assert_unloaded(world):
    assert world.entry.state is ConfigEntryState.NOT_LOADED
    assert not tuple(world.component.entities)
    assert world.entry.entry_id not in world.hass.data[DOMAIN]
    assert not world.entry._on_unload and not world.entry._tasks
    for entity_id in world.entities:
        if state := world.hass.states.get(entity_id):
            assert state.state == STATE_UNAVAILABLE and state.attributes["restored"] is True


@pytest.mark.parametrize("operation", ["unload", "reload"])
@pytest.mark.parametrize("kind", ["absent", "not-ready", "query", "stopping", "confirmation", "finalization", "durability", "metadata"])
def test_eight_materialization_faults_recover_through_real_core(platform_world, monkeypatch, caplog, operation, kind):
    async def scenario():
        async with platform_world() as world:
            old = world.runtime()
            old_bridge, old_store = old["energy_statistics_bridge"], old["history_store"]
            assert [(r["state"], r["sum"]) for r in await world.query()] == [(1, 1)]
            await write(old_store, 3)
            facts = await old_store.async_daily_usage_snapshot(ACCOUNT)
            world.cloud.value = 3
            original_unload = integration.async_unload_entry
            handoff = asyncio.Event()

            async def unload(hass, entry):
                result = await original_unload(hass, entry)
                assert result and not tuple(world.component.entities)
                assert entry.entry_id not in hass.data[DOMAIN]
                assert (old_bridge._lanes[world.statistic_id].target is not None) == (
                    kind in ("stopping", "confirmation", "finalization")
                )
                handoff.set()
                return result

            async with materialization_fault(world, monkeypatch, kind):
                with monkeypatch.context() as observe:
                    # Delegate the complete real integration/platform unload;
                    # inspect ownership before Core can set up its new Bridge.
                    observe.setattr(integration, "async_unload_entry", unload)
                    method = getattr(world.hass.config_entries, f"async_{operation}")
                    assert await asyncio.wait_for(method(world.entry.entry_id), 5)
                assert handoff.is_set()
                assert world.entry.state is not ConfigEntryState.FAILED_UNLOAD
                assert "final materialization did not complete" in caplog.text
                assert "HistoryStore facts remain authoritative" in caplog.text
                assert old_bridge._shutdown and not old_bridge._accepting
                assert await old_store.async_daily_usage_snapshot(ACCOUNT) == facts
                restored = CSGHistoryStore(world.hass, world.entry.entry_id)
                await restored.async_load()
                assert await restored.async_daily_usage_snapshot(ACCOUNT) == facts
                if operation == "unload":
                    await assert_unloaded(world)
                    if kind in ("stopping", "confirmation", "finalization"):
                        assert old_bridge._lanes[world.statistic_id].target is not None
                else:
                    new = await assert_live(world)
                    assert new is not old and new["energy_statistics_bridge"] is not old_bridge
                    assert new["energy_statistics_bridge"]._lanes is old_bridge._lanes
            if operation == "unload":
                assert await world.hass.config_entries.async_setup(world.entry.entry_id)
            new = await assert_live(world)
            await world.sync(new["energy_statistics_bridge"])
            assert [(r["state"], r["sum"]) for r in await world.query()] == [(3, 3)]
            assert [r["change"] for r in await world.query(types={"change"})] == [3]
            assert new["energy_statistics_bridge"]._lanes[world.statistic_id].target is None
            assert (await new["history_store"].async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == 3
            count = len(world.imports)
            await world.sync(new["energy_statistics_bridge"])
            assert len(world.imports) == count
    asyncio.run(scenario())


def test_disable_failure_then_reenable_recovers_without_disabled_recorder_access(platform_world, monkeypatch, caplog):
    async def scenario():
        async with platform_world() as world:
            old = world.runtime()
            await write(old["history_store"], 3)
            world.cloud.value = 3
            world.hass.config_entries.async_update_entry(world.entry, data={
                **world.entry.data, CONF_SETTINGS: {**world.entry.data[CONF_SETTINGS], CONF_ENERGY_STATISTICS_ENABLED: False},
            })
            async with materialization_fault(world, monkeypatch, "query"):
                assert await world.hass.config_entries.async_reload(world.entry.entry_id)
            assert world.entry.state is ConfigEntryState.LOADED
            assert len(tuple(world.component.entities)) == 16
            disabled = world.runtime()["energy_statistics_bridge"]
            assert not disabled.enabled
            assert "final materialization did not complete" in caplog.text
            assert [(r["state"], r["sum"]) for r in await world.query()] == [(1, 1)]
            assert (await disabled.history_store.async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == 3
            with monkeypatch.context() as inactive:
                read = Mock(side_effect=AssertionError("Disabled Bridge Recorder access"))
                write_api = Mock(side_effect=AssertionError("Disabled Bridge Recorder write"))
                inactive.setattr(energy, "get_instance", read)
                inactive.setattr(energy, "async_add_external_statistics", write_api)
                disabled.request_sync()
                await disabled._async_sync()
                assert await world.hass.config_entries.async_unload(world.entry.entry_id)
                await assert_unloaded(world)
                assert await world.hass.config_entries.async_setup(world.entry.entry_id)
                read.assert_not_called()
                write_api.assert_not_called()
            world.hass.config_entries.async_update_entry(world.entry, data={
                **world.entry.data, CONF_SETTINGS: {**world.entry.data[CONF_SETTINGS], CONF_ENERGY_STATISTICS_ENABLED: True},
            })
            assert await world.hass.config_entries.async_reload(world.entry.entry_id)
            new = await assert_live(world)
            await world.sync(new["energy_statistics_bridge"])
            assert [(r["state"], r["sum"]) for r in await world.query()] == [(3, 3)]
    asyncio.run(scenario())


class ProducerCloud(Cloud):
    def __init__(self, error=False):
        super().__init__(value=3, blocked=True)
        self.error = error
        self.ended = threading.Event()

    def get_month_daily_usage_detail(self, account, month):
        try:
            result = super().get_month_daily_usage_detail(account, month)
            if self.error:
                raise RuntimeError("synthetic ordinary producer refresh failure")
            return result
        finally:
            self.ended.set()


async def start_producer(world, monkeypatch, kind, cloud):
    runtime = world.runtime()
    if kind == "history":
        world.hass.config_entries.async_update_entry(world.entry, data={
            **world.entry.data, CONF_SETTINGS: {**world.entry.data[CONF_SETTINGS], CONF_HISTORY_START_MONTH: "2026-09"},
        })
        monkeypatch.setattr(history, "historical_months", lambda *a: [(2026, 9)])
        first = True

        def client(_):
            nonlocal first
            if first:
                first = False
                return cloud
            return world.cloud

        monkeypatch.setattr(integration.CSGClient, "load", client)
        producer = runtime["history_coordinator"] = history.HistoryCoordinator(
            world.hass, world.entry, runtime["history_store"], runtime["energy_statistics_bridge"],
        )
        task = producer.start()
    else:
        producer = runtime[f"{kind}_coordinator"]
        monkeypatch.setattr(producer, "_client", AsyncMock(return_value=cloud))
        task = world.hass.async_create_task(producer.async_refresh(), f"A-3 real {kind} refresh", eager_start=False)
    await wait_entered(cloud.entered)
    return producer, task


@pytest.mark.parametrize("operation", ["unload", "reload"])
@pytest.mark.parametrize("kind", ["history", "billing", "realtime"])
@pytest.mark.parametrize("failure", ["timeout", "refresh-error"])
def test_each_producer_timeout_or_refresh_error_keeps_core_recoverable(platform_world, monkeypatch, caplog, operation, kind, failure):
    async def scenario():
        async with platform_world() as world:
            old = world.runtime()
            store = old["history_store"]
            before = await store.async_daily_usage_snapshot(ACCOUNT)
            upsert = AsyncMock(wraps=store.async_upsert_daily_usage)
            monkeypatch.setattr(store, "async_upsert_daily_usage", upsert)
            cloud = ProducerCloud(error=failure == "refresh-error")
            try:
                producer, task = await start_producer(world, monkeypatch, kind, cloud)
                if failure == "refresh-error":
                    cloud.release.set()
                    await asyncio.wait_for(task, 3)
                with monkeypatch.context() as timeout:
                    timeout.setattr(history if kind == "history" else sensor, "SETTING_UPDATE_TIMEOUT", 0.05)
                    method = getattr(world.hass.config_entries, f"async_{operation}")
                    assert await asyncio.wait_for(method(world.entry.entry_id), 5)
                assert task.done()
                if failure == "timeout":
                    assert task.cancelled() and not cloud.ended.is_set()
                    message = "timed out" if kind == "history" else "did not drain"
                    assert message in caplog.text
                assert (producer._shutdown if kind == "history" else producer._shutdown_requested)
                if kind != "history":
                    assert not producer._fact_updates
                if operation == "unload":
                    await assert_unloaded(world)
                else:
                    await assert_live(world)
                upsert.assert_not_awaited()
                cloud.release.set()
                await wait_entered(cloud.ended)
                assert await store.async_daily_usage_snapshot(ACCOUNT) == before
                upsert.assert_not_awaited()
                if operation == "unload":
                    assert await world.hass.config_entries.async_setup(world.entry.entry_id)
                new = await assert_live(world)
                await world.sync(new["energy_statistics_bridge"])
                assert [(r["state"], r["sum"]) for r in await world.query()] == [(1, 1)]
            finally:
                cloud.release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["history", "billing", "realtime"])
def test_shutdown_error_uses_actual_producer_abort_before_core_success(platform_world, monkeypatch, caplog, kind):
    async def scenario():
        async with platform_world() as world:
            store = world.runtime()["history_store"]
            upsert = AsyncMock(wraps=store.async_upsert_daily_usage)
            monkeypatch.setattr(store, "async_upsert_daily_usage", upsert)
            cloud = ProducerCloud()
            try:
                producer, task = await start_producer(world, monkeypatch, kind, cloud)
                with monkeypatch.context() as fault:
                    # Only the initial cleanup step fails; the real producer,
                    # admitted coroutine, executor and async_abort stay active.
                    fault.setattr(producer, "async_shutdown", AsyncMock(side_effect=RuntimeError("synthetic cleanup failure")))
                    assert await asyncio.wait_for(world.hass.config_entries.async_unload(world.entry.entry_id), 5)
                await assert_unloaded(world)
                assert task.cancelled() and not cloud.ended.is_set()
                assert "forcing coroutine shutdown" in caplog.text
                assert (producer._shutdown if kind == "history" else producer._shutdown_requested)
                cloud.release.set()
                await wait_entered(cloud.ended)
                upsert.assert_not_awaited()
                assert (await store.async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == 1
                assert await world.hass.config_entries.async_setup(world.entry.entry_id)
                await assert_live(world)
            finally:
                cloud.release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["history", "billing", "realtime"])
def test_cancel_after_real_store_write_started_drains_existing_m3_ownership(platform_world, monkeypatch, kind):
    async def scenario():
        async with platform_world() as world:
            old = world.runtime()
            entered, release, ended = threading.Event(), threading.Event(), threading.Event()
            original = ha_storage.write_utf8_file
            store_path = str(old["history_store"]._store.path)

            def physical_write(path, payload, *args, **kwargs):
                serialized = payload.decode() if isinstance(payload, bytes) else payload
                if str(path) == store_path and '"kwh":3.0' in serialized.replace(" ", ""):
                    entered.set()
                    assert release.wait(10), "test watchdog: release real Store disk worker"
                    result = original(path, payload, *args, **kwargs)
                    ended.set()
                    return result
                return original(path, payload, *args, **kwargs)

            monkeypatch.setattr(ha_storage, "write_utf8_file", physical_write)
            cloud = ProducerCloud()
            cloud.release.set()
            try:
                producer, task = await start_producer(world, monkeypatch, kind, cloud)
                await wait_entered(entered)
                aborting = asyncio.Event()
                original_abort = producer.async_abort

                async def abort():
                    aborting.set()
                    await original_abort()

                monkeypatch.setattr(producer, "async_abort", abort)
                final = asyncio.Event()
                bridge = old["energy_statistics_bridge"]
                original_sync = bridge._async_sync

                async def sync():
                    if bridge._finalizing:
                        final.set()
                    await original_sync()

                monkeypatch.setattr(bridge, "_async_sync", sync)
                with monkeypatch.context() as timeout:
                    timeout.setattr(history if kind == "history" else sensor, "SETTING_UPDATE_TIMEOUT", 0.05)
                    unloading = asyncio.create_task(world.hass.config_entries.async_unload(world.entry.entry_id))
                    await asyncio.wait_for(aborting.wait(), 2)
                    assert not task.done() and not unloading.done() and not final.is_set()
                    assert not ended.is_set()
                    release.set()
                    assert await asyncio.wait_for(unloading, 5)
                assert task.done() and task.cancelled()
                assert final.is_set() and ended.is_set()
                await assert_unloaded(world)
                restored = CSGHistoryStore(world.hass, world.entry.entry_id)
                await restored.async_load()
                assert (await restored.async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == 3
                assert [(r["state"], r["sum"]) for r in await world.query()] == [(3, 3)]
            finally:
                release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["history", "billing", "realtime"])
def test_global_stop_cancels_real_producer_without_waiting_for_late_cloud(platform_world, monkeypatch, kind):
    async def scenario():
        async with platform_world() as world:
            old = world.runtime()
            cloud = ProducerCloud()
            try:
                producer, task = await start_producer(world, monkeypatch, kind, cloud)
                stopping = asyncio.create_task(world.hass.async_stop(force=True))
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                assert not cloud.ended.is_set()
                assert old["energy_statistics_bridge"]._shutdown
                cloud.release.set()
                await asyncio.wait_for(stopping, 5)
                await wait_entered(cloud.ended)
                assert (await old["history_store"].async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == 1
            finally:
                cloud.release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_genuine_platform_failure_is_not_hidden_and_retains_runtime(platform_world, monkeypatch, failure):
    async def scenario():
        async with platform_world() as world:
            old = world.runtime()
            original = world.hass.config_entries.async_unload_platforms
            with monkeypatch.context() as fault:
                fault.setattr(world.hass.config_entries, "async_unload_platforms", AsyncMock(
                    return_value=False, side_effect=RuntimeError("synthetic platform unload failure") if failure == "exception" else None,
                ))
                assert not await world.hass.config_entries.async_unload(world.entry.entry_id)
                assert world.entry.state is ConfigEntryState.FAILED_UNLOAD
                assert world.runtime() is old
                assert len(tuple(world.component.entities)) == 16
            # Test-only teardown of a genuine unrecoverable platform fault;
            # recovery acceptance above exclusively uses supported Core APIs.
            assert await original(world.entry, integration.PLATFORMS)
    asyncio.run(scenario())
