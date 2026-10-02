"""A-2: all real daily fact producers drain before ordinary final convergence."""

import asyncio
from contextlib import nullcontext
import datetime as dt
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr

import custom_components.csg as integration
from custom_components.csg import energy_statistics as energy, sensor
from custom_components.csg.const import (
    CONF_AUTH_TOKEN, CONF_ENERGY_STATISTICS_ENABLED, CONF_HISTORY_START_MONTH,
    CONF_SETTINGS, CONF_UPDATE_INTERVAL, DOMAIN,
)
from custom_components.csg.energy_statistics import EnergyStatisticsBridge
from custom_components.csg.history_coordinator import HistoryCoordinator
from custom_components.csg.history_store import CSGHistoryStore
from test_energy_statistics_pending import blocked_import, wait_entered
from test_energy_statistics_recorder import ACCOUNT, recorder_world

DAY = "2026-09-01"


class Cloud:
    """Read-only cloud worker; production refresh, storage and shutdown stay real."""

    def __init__(self, value=3, blocked=False):
        self.value = value
        self.entered, self.release = threading.Event(), threading.Event()
        if not blocked:
            self.release.set()

    def verify_login(self):
        return True

    def initialize(self):
        pass

    def get_balance_and_arrears(self, account):
        return 50, 0

    def get_month_daily_usage_detail(self, account, month):
        if month != (2026, 9):
            return 0, []
        self.entered.set()
        assert self.release.wait(10), "test watchdog: release read-only cloud worker"
        return self.value, [{"date": DAY, "kwh": self.value}]

    def get_year_month_stats(self, account, year):
        return 0, 0, []


async def setup(world, monkeypatch):
    """Use actual integration/sensor wiring over real Store and temporary SQLite."""
    await world.bridge.async_shutdown()
    dr.async_setup(world.hass)
    await dr.async_load(world.hass, load_empty=True)
    await sensor.er.async_load(world.hass, load_empty=True)
    world.hass.config_entries._entries[world.entry.entry_id] = world.entry
    world.hass.config_entries.async_update_entry(world.entry, data={
        **world.entry.data, CONF_AUTH_TOKEN: "synthetic", CONF_USERNAME: "synthetic",
        CONF_SETTINGS: {CONF_ENERGY_STATISTICS_ENABLED: True, CONF_UPDATE_INTERVAL: 3600},
    })
    monkeypatch.setattr(integration.CSGClient, "load", lambda _: Cloud())
    monkeypatch.setattr(sensor, "_csg_today", lambda: dt.date(2026, 9, 3))
    monkeypatch.setattr(sensor.CSGCoordinator, "_notify_failure", Mock())
    monkeypatch.setattr(sensor.CSGCoordinator, "_clear_failure", Mock())
    timers = []
    monkeypatch.setattr(sensor, "async_track_time_change", lambda *a, **kw: timers.append(Mock()) or timers[-1])

    async def forward(entry, platforms):
        # Initial refresh is irrelevant to tail timing. Subsequent explicit /
        # daily callback refreshes below use the actual HA refresh implementation.
        with monkeypatch.context() as initial:
            initial.setattr(sensor.CSGCoordinator, "async_refresh", AsyncMock())
            await sensor.async_setup_entry(world.hass, entry, lambda entities: None)

    monkeypatch.setattr(world.hass.config_entries, "async_forward_entry_setups", forward)
    platforms = AsyncMock(return_value=True)
    monkeypatch.setattr(world.hass.config_entries, "async_unload_platforms", platforms)
    assert await integration.async_setup_entry(world.hass, world.entry)
    runtime = world.hass.data[DOMAIN][world.entry.entry_id]
    bridge = runtime["energy_statistics_bridge"]
    if bridge._task is not None:
        await bridge._task
    await write(runtime["history_store"], 1)
    await world.sync(bridge)
    assert [(r["state"], r["sum"]) for r in await world.query()] == [(1, 1)]
    gate = asyncio.Event()
    close = bridge.stop_requests

    def close_requests():
        close()
        gate.set()

    monkeypatch.setattr(bridge, "stop_requests", close_requests)
    final = asyncio.Event()
    sync = bridge._async_sync

    async def sync_after_producers():
        if bridge._finalizing:
            assert not bridge._accepting and not bridge._pending
            for key in ("realtime_coordinator", "billing_coordinator"):
                producer = runtime[key]
                assert producer._shutdown_requested and not producer._fact_updates
            if history := runtime.get("history_coordinator"):
                assert history._shutdown and history._task.done()
            final.set()
        await sync()

    monkeypatch.setattr(bridge, "_async_sync", sync_after_producers)
    return runtime, bridge, gate, final, platforms, timers


async def write(store, value):
    await store.async_upsert_daily_usage(ACCOUNT, (2026, 9), [{"date": DAY, "kwh": value}])


def core_reload(world, monkeypatch, before_setup=None):
    """Run Core's actual reload/unload/error handling, with local module lookup."""
    world.hass.config.components.add(DOMAIN)
    world.entry._async_set_state(world.hass, ConfigEntryState.LOADED, None)
    monkeypatch.setattr(world.entry, "_integration_for_domain", SimpleNamespace(
        domain=DOMAIN, logger=integration._LOGGER,
        async_get_component=AsyncMock(return_value=integration),
    ))

    async def setup_entry(entry_id, _lock=False):
        assert entry_id == world.entry.entry_id
        if before_setup is not None:
            await before_setup()
        return await integration.async_setup_entry(world.hass, world.entry)

    setup_call = AsyncMock(side_effect=setup_entry)
    monkeypatch.setattr(world.hass.config_entries, "async_setup", setup_call)
    return setup_call


async def assert_final(world, runtime, expected):
    store = runtime["history_store"]
    assert (await store.async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == expected
    restored = CSGHistoryStore(world.hass, world.entry.entry_id)
    await restored.async_load()
    assert (await restored.async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == expected
    assert [(r["state"], r["sum"]) for r in await world.query()] == [(expected, expected)]
    assert [r["change"] for r in await world.query(types={"change"})] == [expected]
    bridge = runtime["energy_statistics_bridge"]
    assert bridge._task is None and not bridge._pending and not bridge._accepting
    assert all(lane.target is None and not lane.lock.locked() for lane in bridge._lanes.values())
    assert world.entry.entry_id not in world.hass.data[DOMAIN]


@pytest.mark.parametrize("history_drain,old_import", [
    pytest.param(False, False, id="case1-idle"),
    pytest.param(False, True, id="case2-old-confirmation-ends-first"),
    pytest.param(True, False, id="case3-real-history-and-billing-idle"),
    pytest.param(True, True, id="case4-real-history-and-billing-old-ends-first"),
])
def test_four_audit_cases(recorder_world, monkeypatch, history_drain, old_import):
    async def scenario():
        async with recorder_world() as world:
            runtime, bridge, gate, final, platforms, timers = await setup(world, monkeypatch)
            billing_cloud = Cloud(blocked=True)
            billing = runtime["billing_coordinator"]
            monkeypatch.setattr(billing, "_client", AsyncMock(return_value=billing_cloud))
            history_cloud = Cloud(blocked=True)
            try:
                imports = blocked_import(monkeypatch, world.statistic_id) if old_import else nullcontext((None, None))
                with imports as (entered, release):
                    old_task = None
                    if old_import:
                        await write(runtime["history_store"], 2)
                        bridge.request_sync()
                        old_task = bridge._task
                        await wait_entered(entered)
                        assert bridge._lanes[world.statistic_id].target is not None
                    if history_drain:
                        world.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2026-09"
                        monkeypatch.setattr(integration.CSGClient, "load", lambda _: history_cloud)
                        # Freeze only range planning; the real History fetch,
                        # cancellation and physical request drain are exercised.
                        monkeypatch.setattr("custom_components.csg.history_coordinator.historical_months", lambda *a: [(2026, 9)])
                        history = runtime["history_coordinator"] = HistoryCoordinator(
                            world.hass, world.entry, runtime["history_store"], bridge,
                        )
                        history.start()
                        await wait_entered(history_cloud.entered)
                    refreshing = asyncio.create_task(billing._handle_daily_refresh(dt.datetime.now(dt.UTC)))
                    await wait_entered(billing_cloud.entered)
                    unloading = asyncio.create_task(integration.async_unload_entry(world.hass, world.entry))
                    await gate.wait()
                    assert not final.is_set() and not unloading.done()
                    if old_import:
                        release.set()
                        await asyncio.wait_for(old_task, 5)
                        assert bridge._task is None and bridge._lanes[world.statistic_id].target is None
                        assert [(r["state"], r["sum"]) for r in await world.query()] == [(2, 2)]
                    else:
                        assert bridge._task is None and bridge._lanes[world.statistic_id].target is None
                    # All late ordinary request_sync calls are refused. A final
                    # pass must happen even after the old worker ended above.
                    billing_cloud.release.set()
                    await asyncio.wait_for(refreshing, 5)
                    assert billing.last_update_success
                    assert (await runtime["history_store"].async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == 3
                    assert not bridge._pending
                    if history_drain:
                        assert bridge._task is None
                        assert not history._task.done() and not final.is_set()
                        history_cloud.release.set()
                    assert await asyncio.wait_for(unloading, 5)
                    assert final.is_set()
                    await assert_final(world, runtime, 3)
                    platforms.assert_awaited_once()
                    await world.entry._async_process_on_unload(world.hass)
                    assert timers[0].call_count == 1
            finally:
                billing_cloud.release.set()
                history_cloud.release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("multiple", [False, True], ids=["realtime-tail", "all-producers-tail-order"])
def test_realtime_tail_and_multiple_producers(recorder_world, monkeypatch, multiple):
    async def scenario():
        async with recorder_world() as world:
            runtime, bridge, gate, final, _, _ = await setup(world, monkeypatch)
            realtime_cloud = Cloud(value=4 if multiple else 3, blocked=True)
            billing_cloud, history_cloud = Cloud(blocked=True), Cloud(blocked=True)
            realtime = runtime["realtime_coordinator"]
            monkeypatch.setattr(realtime, "_client", AsyncMock(return_value=realtime_cloud))
            try:
                if multiple:
                    world.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2026-09"
                    monkeypatch.setattr(integration.CSGClient, "load", lambda _: history_cloud)
                    monkeypatch.setattr("custom_components.csg.history_coordinator.historical_months", lambda *a: [(2026, 9)])
                    history = runtime["history_coordinator"] = HistoryCoordinator(world.hass, world.entry, runtime["history_store"], bridge)
                    history.start()
                    await wait_entered(history_cloud.entered)
                    monkeypatch.setattr(runtime["billing_coordinator"], "_client", AsyncMock(return_value=billing_cloud))
                    billing_task = asyncio.create_task(runtime["billing_coordinator"].async_refresh())
                    await wait_entered(billing_cloud.entered)
                refreshing = asyncio.create_task(realtime.async_refresh())
                await wait_entered(realtime_cloud.entered)
                unloading = asyncio.create_task(integration.async_unload_entry(world.hass, world.entry))
                await gate.wait()
                if multiple:
                    billing_cloud.release.set()
                    await asyncio.wait_for(billing_task, 5)
                    assert runtime["billing_coordinator"].last_update_success
                    assert (await runtime["history_store"].async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == 3
                    assert not history._task.done() and not final.is_set()
                realtime_cloud.release.set()
                await asyncio.wait_for(refreshing, 5)
                assert realtime.last_update_success
                assert (await runtime["history_store"].async_daily_usage_snapshot(ACCOUNT))[DAY]["kwh"] == realtime_cloud.value
                if multiple:
                    assert not final.is_set()
                    history_cloud.release.set()
                assert await asyncio.wait_for(unloading, 5)
                assert final.is_set()
                await assert_final(world, runtime, realtime_cloud.value)
                # Core shutdown and later refresh callbacks cannot admit writers.
                count = len(world.imports)
                await world.entry._async_process_on_unload(world.hass)
                await realtime.async_refresh()
                assert len(world.imports) == count
                assert not realtime._fact_updates
            finally:
                for cloud in (realtime_cloud, billing_cloud, history_cloud):
                    cloud.release.set()
    asyncio.run(scenario())


def test_disable_reload_finalizes_old_enabled_lifecycle(recorder_world, monkeypatch):
    async def scenario():
        async with recorder_world() as world:
            runtime, bridge, gate, final, _, _ = await setup(world, monkeypatch)
            cloud = Cloud(blocked=True)
            monkeypatch.setattr(runtime["billing_coordinator"], "_client", AsyncMock(return_value=cloud))
            try:
                async def before_disabled_setup():
                    assert final.is_set()
                    await assert_final(world, runtime, 3)

                setup_call = core_reload(world, monkeypatch, before_disabled_setup)
                refreshing = asyncio.create_task(runtime["billing_coordinator"].async_refresh())
                await wait_entered(cloud.entered)
                # Options writes settings before calling reload: the old Bridge
                # must use its captured enabled lifecycle, not the updated flag.
                world.hass.config_entries.async_update_entry(world.entry, data={
                    **world.entry.data, CONF_SETTINGS: {**world.entry.data[CONF_SETTINGS], CONF_ENERGY_STATISTICS_ENABLED: False},
                })
                assert bridge.enabled
                reloading = asyncio.create_task(world.hass.config_entries.async_reload(world.entry.entry_id))
                await gate.wait()
                cloud.release.set()
                await asyncio.wait_for(refreshing, 5)
                assert runtime["billing_coordinator"].last_update_success
                assert await asyncio.wait_for(reloading, 5)
                assert final.is_set()
                setup_call.assert_awaited_once()
                count = len(world.imports)
                disabled = world.hass.data[DOMAIN][world.entry.entry_id]["energy_statistics_bridge"]
                assert not disabled.enabled and disabled._task is None
                disabled.request_sync()
                await disabled._async_sync()
                assert len(world.imports) == count
                assert await integration.async_unload_entry(world.hass, world.entry)
                await world.entry._async_process_on_unload(world.hass)
            finally:
                cloud.release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["absent", "not-ready", "query", "stopping", "timeout", "final-budget", "persistence"])
def test_final_failure_is_not_unload_success_and_can_recover(recorder_world, monkeypatch, failure):
    async def scenario():
        async with recorder_world() as world:
            runtime, bridge, _, _, platforms, _ = await setup(world, monkeypatch)
            setup_call = core_reload(world, monkeypatch)
            await write(runtime["history_store"], 3)
            with monkeypatch.context() as fault:
                if failure == "absent":
                    fault.setattr(energy, "get_instance", Mock(side_effect=KeyError("Recorder absent")))
                elif failure == "not-ready":
                    fault.setattr(world.recorder, "async_db_ready", asyncio.get_running_loop().create_future())
                elif failure == "query":
                    fault.setattr(energy, "statistics_during_period", Mock(side_effect=RuntimeError("query unavailable")))
                elif failure == "stopping":
                    fault.setattr(world.recorder, "is_alive", lambda: False)
                elif failure in ("timeout", "final-budget"):
                    async def never_confirm(*args):
                        await asyncio.Event().wait()
                    fault.setattr(bridge, "_async_read_back", never_confirm)
                    fault.setattr(energy, "_CONFIRMATION_TIMEOUT" if failure == "timeout" else "_FINALIZATION_TIMEOUT", 0.05)
                else:
                    fault.setattr(runtime["history_store"], "async_ensure_persisted", AsyncMock(return_value=False))
                assert not await world.hass.config_entries.async_reload(world.entry.entry_id)
                assert world.entry.state is ConfigEntryState.FAILED_UNLOAD
                setup_call.assert_not_awaited()
                if failure in ("stopping", "timeout", "final-budget"):
                    assert bridge._lanes[world.statistic_id].target is not None
                assert not bridge._accepting and bridge._task is None
                platforms.assert_awaited_once()
            # Finalization failed, so Core does not proceed to the next setup.
            # A later rebuilt Bridge confirms retained ownership before comparing.
            recovered = EnergyStatisticsBridge(world.hass, world.entry, runtime["history_store"])
            await world.sync(recovered)
            assert [(r["state"], r["sum"]) for r in await world.query()] == [(3, 3)]
            assert bridge._lanes[world.statistic_id].target is None
            await recovered.async_shutdown()
    asyncio.run(scenario())


def test_producer_timeout_retains_runtime_and_refuses_final_pass(recorder_world, monkeypatch):
    async def scenario():
        async with recorder_world() as world:
            runtime, bridge, gate, final, platforms, _ = await setup(world, monkeypatch)
            cloud = Cloud(blocked=True)
            billing = runtime["billing_coordinator"]
            monkeypatch.setattr(billing, "_client", AsyncMock(return_value=cloud))
            try:
                refreshing = asyncio.create_task(billing.async_refresh())
                await wait_entered(cloud.entered)
                with monkeypatch.context() as fault:
                    fault.setattr(sensor, "SETTING_UPDATE_TIMEOUT", 0.05)
                    with pytest.raises(TimeoutError):
                        await integration.async_unload_entry(world.hass, world.entry)
                assert gate.is_set() and not final.is_set()
                assert world.hass.data[DOMAIN][world.entry.entry_id] is runtime
                platforms.assert_not_awaited()
                assert not refreshing.done() and not bridge._accepting
                cloud.release.set()
                await asyncio.wait_for(refreshing, 5)
                assert await integration.async_unload_entry(world.hass, world.entry)
                assert final.is_set()
                await assert_final(world, runtime, 3)
            finally:
                cloud.release.set()
    asyncio.run(scenario())


def test_actual_global_stop_cancels_internal_final_waiter(recorder_world, monkeypatch):
    async def scenario():
        async with recorder_world() as world:
            runtime, bridge, _, final, _, _ = await setup(world, monkeypatch)
            await write(runtime["history_store"], 3)
            with blocked_import(monkeypatch, world.statistic_id) as (entered, release):
                unloading = asyncio.create_task(integration.async_unload_entry(world.hass, world.entry))
                await wait_entered(entered)
                assert final.is_set() and bridge._task is not None
                stopping = asyncio.create_task(world.hass.async_stop(force=True))
                assert await asyncio.wait_for(unloading, 3)
                assert bridge._task is None and not bridge._pending
                assert bridge._lanes[world.statistic_id].target is not None
                assert not any("import" in key or "materialized" in key for key in runtime["history_store"]._data)
                release.set()
                await asyncio.wait_for(stopping, 5)
    asyncio.run(scenario())


def test_final_timeout_does_not_join_a_cancellation_drain_forever(recorder_world, monkeypatch):
    async def scenario():
        async with recorder_world() as world:
            runtime, bridge, _, _, _, _ = await setup(world, monkeypatch)
            await write(runtime["history_store"], 3)
            draining, release = asyncio.Event(), asyncio.Event()

            async def drain_after_cancel(*args):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    draining.set()
                    await release.wait()
                    raise

            with monkeypatch.context() as fault:
                fault.setattr(bridge, "_async_read_back", drain_after_cancel)
                fault.setattr(energy, "_FINALIZATION_TIMEOUT", 0.05)
                try:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(integration.async_unload_entry(world.hass, world.entry), 1)
                    assert draining.is_set()
                    waiter = bridge._task
                    assert waiter is not None and not waiter.done()
                    assert bridge._lanes[world.statistic_id].target is not None
                    assert bridge._lanes[world.statistic_id].lock.locked()
                finally:
                    release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(waiter, 1)
                assert bridge._task is None
            recovered = EnergyStatisticsBridge(world.hass, world.entry, runtime["history_store"])
            await world.sync(recovered)
            assert [(r["state"], r["sum"]) for r in await world.query()] == [(3, 3)]
            assert bridge._lanes[world.statistic_id].target is None
            await recovered.async_shutdown()
    asyncio.run(scenario())
