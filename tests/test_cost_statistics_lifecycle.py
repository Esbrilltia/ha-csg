"""Cost lanes reuse real M4 A-1/A-2/A-3, currency and disabled lifecycles."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.recorder import statistics as ha_statistics
from homeassistant.config_entries import ConfigEntryState

import custom_components.csg as integration
from custom_components.csg import energy_statistics as module, sensor
from custom_components.csg.const import CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS, CONF_TARIFF_PROFILES
from custom_components.csg.cost_statistics import build_cost_statistics, cost_statistic_metadata
from custom_components.csg.energy_statistics import EnergyStatisticsBridge
from custom_components.csg.history_store import CSGHistoryStore
from test_cost_statistics_recorder import cost_world
from test_energy_statistics_pending import blocked_import, wait_entered
from test_energy_statistics_recorder import ACCOUNT, recorder_world
from test_energy_statistics_recovery import platform_world, assert_live, assert_unloaded
from test_tariff import CHOICES


async def write_bills(store, values):
    for key, cost in values.items():
        year, month = map(int, key.split("-"))
        await store.async_upsert_monthly_bill(ACCOUNT, (year, month), usage_kwh=None, cost_cny=cost)


@pytest.fixture
def cost_platform_world(cost_world, monkeypatch):
    return platform_world.__wrapped__(cost_world, monkeypatch)


async def assert_cost_converged(world, worker, expected):
    desired = build_cost_statistics(await worker.history_store.async_monthly_bills_snapshot(ACCOUNT), dt.date(2026, 10, 3))
    rows = await world.query_cost()
    assert [(row["state"], row["sum"]) for row in rows] == expected
    assert [(row["start"], row["state"], row["sum"]) for row in rows] == [(row["start"].timestamp(), row["state"], row["sum"]) for row in desired]
    assert [row["change"] for row in await world.query_cost(types={"change"})] == [row["state"] for row in desired]
    assert worker._task is None and not worker._pending
    assert all(lane.target is None and not lane.lock.locked() for lane in worker._lanes.values())
    count = len(world.imports)
    await world.sync(worker)
    assert len(world.imports) == count


@pytest.mark.parametrize("fresh", [False, True], ids=["same-worker", "fresh-bridge-durable-store"])
def test_cost_a1_queued_old_target_revision_hole_fill_and_early_queue_hint(cost_world, monkeypatch, fresh):
    async def scenario():
        async with cost_world() as world:
            await world.upsert_bills({"2026-01": 100, "2026-03": 150})
            await world.sync()
            with blocked_import(monkeypatch, world.cost_id) as (entered, release):
                # A queue pacing hint can return before the executing import.
                monkeypatch.setattr(world.recorder, "async_block_till_done", AsyncMock())
                await world.upsert_bills({"2026-01": 102})
                world.bridge.request_sync()
                old_task = world.bridge._task
                await wait_entered(entered)
                await world.upsert_bills({"2026-01": 98, "2026-02": 120})
                worker = world.bridge
                if fresh:
                    old_task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await old_task
                    restored = CSGHistoryStore(world.hass, world.entry.entry_id)
                    await restored.async_load()
                    worker = EnergyStatisticsBridge(world.hass, world.entry, restored)
                for _ in range(5):
                    worker.request_sync()
                task = worker._task
                await asyncio.sleep(0)
                assert task is not None and not task.done()
                assert worker._lanes[world.cost_id].target is not None
                release.set()
                await asyncio.wait_for(task, 5)
                await assert_cost_converged(world, worker, [(98, 98), (120, 218), (150, 368)])
                await worker.async_shutdown()
    asyncio.run(scenario())


def test_cost_a1_real_recorder_requeues_retry_then_rereads_latest_store(cost_world, monkeypatch):
    async def scenario():
        async with cost_world() as world:
            await world.upsert_bills({"2026-01": 100})
            await world.sync()
            original = ha_statistics.import_statistics
            attempts = 0
            entered, release = threading.Event(), threading.Event()

            def retry(recorder, metadata, rows, table):
                nonlocal attempts
                if metadata["statistic_id"] == world.cost_id:
                    attempts += 1
                    if attempts == 1:
                        return False
                    if attempts == 2:
                        entered.set()
                        assert release.wait(10)
                return original(recorder, metadata, rows, table)

            monkeypatch.setattr(ha_statistics, "import_statistics", retry)
            monkeypatch.setattr(world.recorder, "async_block_till_done", AsyncMock())
            try:
                await world.upsert_bills({"2026-01": 102})
                world.bridge.request_sync()
                task = world.bridge._task
                await wait_entered(entered)
                await world.upsert_bills({"2026-01": 98})
                assert not task.done() and world.bridge._lanes[world.cost_id].target is not None
                release.set()
                await asyncio.wait_for(task, 5)
                await assert_cost_converged(world, world.bridge, [(98, 98)])
                assert attempts == 3
            finally:
                release.set()
    asyncio.run(scenario())


def test_cost_a1_queued_import_drains_before_real_core_reload_handoff(cost_platform_world, monkeypatch):
    async def scenario():
        async with cost_platform_world() as world:
            old = world.runtime()
            store, bridge = old["history_store"], old["energy_statistics_bridge"]
            await write_bills(store, {"2026-01": 100, "2026-03": 150})
            await world.sync(bridge)
            with blocked_import(monkeypatch, world.cost_id) as (entered, release):
                await write_bills(store, {"2026-01": 102})
                bridge.request_sync()
                await wait_entered(entered)
                await write_bills(store, {"2026-01": 98, "2026-02": 120})
                closed = asyncio.Event()
                stop = bridge.stop_requests
                def close():
                    stop()
                    closed.set()
                monkeypatch.setattr(bridge, "stop_requests", close)
                reloading = asyncio.create_task(world.hass.config_entries.async_reload(world.entry.entry_id))
                await asyncio.wait_for(closed.wait(), 3)
                assert not reloading.done() and bridge._lanes[world.cost_id].target is not None
                release.set()
                assert await asyncio.wait_for(reloading, 5)
            new = await assert_live(world)
            fresh = new["energy_statistics_bridge"]
            assert fresh is not bridge and new["history_store"] is not store
            assert fresh._lanes is bridge._lanes
            await world.sync(fresh)
            await assert_cost_converged(world, fresh, [(98, 98), (120, 218), (150, 368)])
    asyncio.run(scenario())


def test_cost_global_stop_cancels_waiter_and_retains_unconfirmed_ownership(cost_world, monkeypatch):
    async def scenario():
        async with cost_world() as world:
            await world.upsert_bills({"2026-01": 100})
            await world.sync()
            with blocked_import(monkeypatch, world.cost_id) as (entered, release):
                await world.upsert_bills({"2026-01": 98})
                world.bridge.request_sync()
                task = world.bridge._task
                await wait_entered(entered)
                stopping = asyncio.create_task(world.hass.async_stop(force=True))
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
                assert world.bridge._task is None and not world.bridge._pending
                assert world.bridge._lanes[world.cost_id].target is not None
                assert not any("import" in key or "materialized" in key for key in world.store._data)
                release.set()
                await asyncio.wait_for(stopping, 5)
    asyncio.run(scenario())


@pytest.mark.parametrize("old_import", [False, True], ids=["idle", "old-confirmation-finishes-before-bill-tail"])
def test_cost_a2_billing_tail_after_gate_closes_converges_on_real_core_unload(cost_platform_world, monkeypatch, old_import):
    async def scenario():
        async with cost_platform_world() as world:
            runtime = world.runtime()
            store, bridge = runtime["history_store"], runtime["energy_statistics_bridge"]
            await write_bills(store, {"2026-01": 100})
            await world.sync(bridge)
            entered_bill, release_bill = threading.Event(), threading.Event()

            def year_stats(account, year):
                if year == 2026:
                    entered_bill.set()
                    assert release_bill.wait(10)
                    return 98, 20, [{"month": "202601", "charge": 98, "kwh": 20}]
                return 0, 0, []

            monkeypatch.setattr(world.cloud, "get_year_month_stats", year_stats)
            gate = asyncio.Event()
            stop = bridge.stop_requests

            def close():
                stop()
                gate.set()

            monkeypatch.setattr(bridge, "stop_requests", close)
            try:
                with blocked_import(monkeypatch, world.cost_id) if old_import else nullcontext((None, None)) as (entered, release):
                    old_task = None
                    if old_import:
                        await write_bills(store, {"2026-01": 102})
                        bridge.request_sync()
                        old_task = bridge._task
                        await wait_entered(entered)
                    refreshing = asyncio.create_task(runtime["billing_coordinator"]._handle_daily_refresh(dt.datetime.now(dt.UTC)))
                    await wait_entered(entered_bill)
                    unloading = asyncio.create_task(world.hass.config_entries.async_unload(world.entry.entry_id))
                    await asyncio.wait_for(gate.wait(), 3)
                    assert not unloading.done() and not bridge._accepting
                    if old_import:
                        release.set()
                        await asyncio.wait_for(old_task, 5)
                    elif bridge._task is not None:
                        await asyncio.wait_for(bridge._task, 5)
                    assert bridge._task is None
                    # Billing's final cost upsert and its refused ordinary
                    # request happen only after the old worker has completed.
                    release_bill.set()
                    await asyncio.wait_for(refreshing, 5)
                    assert await asyncio.wait_for(unloading, 5)
                    await assert_unloaded(world)
                    assert [(r["state"], r["sum"]) for r in await world.query_cost()] == [(98, 98)]
                    assert (await store.async_monthly_bills_snapshot(ACCOUNT))["2026-01"]["cost_cny"] == 98
                    restored = CSGHistoryStore(world.hass, world.entry.entry_id)
                    await restored.async_load()
                    assert await restored.async_monthly_bills_snapshot(ACCOUNT) == await store.async_monthly_bills_snapshot(ACCOUNT)
                    assert bridge._lanes[world.cost_id].target is None
            finally:
                release_bill.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["unload", "reload"])
@pytest.mark.parametrize("failure", ["query", "confirmation", "absent"])
def test_cost_a3_final_fault_is_recoverable_through_real_core(cost_platform_world, monkeypatch, caplog, operation, failure):
    async def scenario():
        async with cost_platform_world() as world:
            old = world.runtime()
            store, bridge = old["history_store"], old["energy_statistics_bridge"]
            await write_bills(store, {"2026-01": 100})
            await world.sync(bridge)
            await write_bills(store, {"2026-01": 98})
            facts = await store.async_monthly_bills_snapshot(ACCOUNT)
            with monkeypatch.context() as fault:
                if failure == "query":
                    original = module.statistics_during_period
                    def unavailable(*args, **kwargs):
                        if world.cost_id in args[3]:
                            raise RuntimeError("Synthetic cost query failure")
                        return original(*args, **kwargs)
                    fault.setattr(module, "statistics_during_period", unavailable)
                elif failure == "confirmation":
                    original = bridge._async_read_back
                    async def unconfirmed(recorder, lane):
                        if lane.target[0]["statistic_id"] == world.cost_id:
                            await asyncio.Event().wait()
                        else:
                            await original(recorder, lane)
                    fault.setattr(bridge, "_async_read_back", unconfirmed)
                    fault.setattr(module, "_CONFIRMATION_TIMEOUT", 0.05)
                else:
                    fault.setattr(module, "get_instance", Mock(side_effect=KeyError("Synthetic Recorder unavailable")))
                assert await asyncio.wait_for(getattr(world.hass.config_entries, f"async_{operation}")(world.entry.entry_id), 5)
                assert world.entry.state is not ConfigEntryState.FAILED_UNLOAD
                assert "final materialization did not complete" in caplog.text
                assert bridge._shutdown and not bridge._accepting
                assert await store.async_monthly_bills_snapshot(ACCOUNT) == facts
                restored = CSGHistoryStore(world.hass, world.entry.entry_id)
                await restored.async_load()
                assert await restored.async_monthly_bills_snapshot(ACCOUNT) == facts
                if failure == "confirmation" and operation == "unload":
                    assert bridge._lanes[world.cost_id].target is not None
                if operation == "unload":
                    await assert_unloaded(world)
                else:
                    new = await assert_live(world)
                    assert new["energy_statistics_bridge"]._lanes is bridge._lanes
            if operation == "unload":
                assert await world.hass.config_entries.async_setup(world.entry.entry_id)
            runtime = await assert_live(world)
            await world.sync(runtime["energy_statistics_bridge"])
            await assert_cost_converged(world, runtime["energy_statistics_bridge"], [(98, 98)])
    asyncio.run(scenario())


def test_currency_mismatch_has_zero_cost_reads_writes_and_restores_from_store(cost_platform_world, monkeypatch, caplog):
    async def scenario():
        async with cost_platform_world() as world:
            runtime = world.runtime()
            await write_bills(runtime["history_store"], {"2026-01": 100})
            await world.sync(runtime["energy_statistics_bridge"])
            before = await world.query_cost()
            world.hass.config.currency = "USD"
            cost_access = []
            originals = {name: getattr(module, name) for name in ("get_metadata", "statistics_during_period", "async_add_external_statistics")}
            def metadata(*args, **kwargs):
                if world.cost_id in kwargs.get("statistic_ids", set()):
                    cost_access.append("metadata")
                return originals["get_metadata"](*args, **kwargs)
            def query(*args, **kwargs):
                if world.cost_id in args[3]:
                    cost_access.append("query")
                return originals["statistics_during_period"](*args, **kwargs)
            def add(hass, meta, rows):
                if meta["statistic_id"] == world.cost_id:
                    cost_access.append("import")
                return originals["async_add_external_statistics"](hass, meta, rows)
            monkeypatch.setattr(module, "get_metadata", metadata)
            monkeypatch.setattr(module, "statistics_during_period", query)
            monkeypatch.setattr(module, "async_add_external_statistics", add)
            await write_bills(runtime["history_store"], {"2026-01": 98})
            world.cloud.value = 3
            caplog.clear()
            assert await world.hass.config_entries.async_reload(world.entry.entry_id)
            fresh = world.runtime()
            await world.sync(fresh["energy_statistics_bridge"])
            await world.sync(fresh["energy_statistics_bridge"])
            assert [(r["state"], r["sum"]) for r in await world.query()] == [(3, 3)]
            assert await world.query_cost() == before
            assert (await fresh["history_store"].async_monthly_bills_snapshot(ACCOUNT))["2026-01"]["cost_cny"] == 98
            assert cost_access == []
            # The closing old lifecycle and fresh lifecycle can each warn once.
            assert len([r for r in caplog.records if "external cost statistics require" in r.message]) == 2
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            assert cost_access == []
            world.hass.config.currency = "CNY"
            assert await world.hass.config_entries.async_setup(world.entry.entry_id)
            await world.sync(world.runtime()["energy_statistics_bridge"])
            await assert_cost_converged(world, world.runtime()["energy_statistics_bridge"], [(98, 98)])
    asyncio.run(scenario())


def test_explicit_false_has_zero_integration_recorder_access_while_tariff_works(cost_platform_world, monkeypatch):
    calls = []
    async def configure(base):
        base.hass.config_entries.async_update_entry(base.entry, data={**base.entry.data, CONF_SETTINGS: {
            **base.entry.data[CONF_SETTINGS], CONF_ENERGY_STATISTICS_ENABLED: False,
            CONF_TARIFF_PROFILES: {ACCOUNT: {"scheme": "combined", "multi_person": False, "tou": False}},
        }})
        # The synthetic account is explicitly marked as Guangzhou for tariff.
        from custom_components.csg.const import CONF_ELE_ACCOUNTS
        accounts = deepcopy(base.entry.data[CONF_ELE_ACCOUNTS])
        accounts[ACCOUNT]["area_code"] = "080000"
        base.hass.config_entries.async_update_entry(base.entry, data={**base.entry.data, CONF_ELE_ACCOUNTS: accounts})
        for name in ("get_instance", "get_metadata", "statistics_during_period", "async_add_external_statistics"):
            stub = Mock(side_effect=AssertionError("Explicit off accessed Recorder"))
            monkeypatch.setattr(module, name, stub)
            calls.append(stub)

    async def scenario():
        async with cost_platform_world(before_setup=configure) as world:
            runtime = world.runtime()
            assert not runtime["energy_statistics_bridge"].enabled
            await write_bills(runtime["history_store"], {"2026-01": 100})
            await world.sync(runtime["energy_statistics_bridge"])
            tariff = next(entity for entity in world.component.entities if entity.unique_id.endswith(".current_ladder_tariff"))
            assert tariff.available and tariff.native_value == 0.62586875
            assert tariff.native_unit_of_measurement == "CNY/kWh"
            assert await world.hass.config_entries.async_reload(world.entry.entry_id)
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            for stub in calls:
                stub.assert_not_called()
    asyncio.run(scenario())


def test_every_tariff_selection_leaves_committed_official_cost_identical(cost_world):
    async def scenario():
        async with cost_world() as world:
            await world.upsert_bills({"2026-01": 100, "2026-03": 150})
            await world.sync()
            before = await world.query_cost()
            facts = await world.store.async_monthly_bills_snapshot(ACCOUNT)
            count = len(world.imports)
            for choice in [None, {"scheme": "unconfigured"}, *CHOICES, {"scheme": "combined", "tou": True}]:
                world.entry.data[CONF_SETTINGS][CONF_TARIFF_PROFILES] = {ACCOUNT: choice}
                await world.sync()
                assert await world.query_cost() == before
                assert await world.store.async_monthly_bills_snapshot(ACCOUNT) == facts
                assert len(world.imports) == count
                assert not any("tariff" in key or "daily_cost" in key for key in world.store._data["accounts"][ACCOUNT])
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["zero", "partial", "all", "repeated"])
def test_cost_actual_process_exit_recovers_without_lane_registry(tmp_path, case):
    helper = Path(__file__).with_name("cost_statistics_restart_process.py")
    for mode in ("seed", "verify"):
        result = subprocess.run([sys.executable, str(helper), mode, str(tmp_path), case], capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["state_sum"] == [[98, 98], [120, 218], [150, 368]]
