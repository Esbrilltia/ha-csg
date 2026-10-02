"""A-1: an executing old public import must not outlive final convergence."""

import asyncio
from contextlib import contextmanager
import threading
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.recorder import statistics as ha_statistics
from homeassistant.components.recorder.tasks import ImportStatisticsTask
from homeassistant.const import CONF_USERNAME

import custom_components.csg as integration
from custom_components.csg import sensor
from custom_components.csg.const import (
    CONF_AUTH_TOKEN, CONF_SETTINGS, CONF_UPDATE_INTERVAL, DOMAIN,
)
from custom_components.csg.energy_statistics import EnergyStatisticsBridge, build_statistics
from test_energy_statistics_recorder import ACCOUNT, recorder_world

DAY1 = "2026-09-01"
DAY2 = "2026-09-02"


@contextmanager
def blocked_import(monkeypatch, statistic_id):
    """Pause a real ImportStatisticsTask after Recorder dequeues it."""
    entered, release = threading.Event(), threading.Event()
    original = ImportStatisticsTask.run
    blocked = False

    def run(task, recorder):
        nonlocal blocked
        if task.metadata["statistic_id"] == statistic_id and not blocked:
            blocked = True
            entered.set()
            assert release.wait(10), "test watchdog: release real Recorder import"
        return original(task, recorder)

    monkeypatch.setattr(ImportStatisticsTask, "run", run)
    try:
        yield entered, release
    finally:
        release.set()


async def wait_entered(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 5), 6)


async def assert_converged(world, worker, expected):
    desired = build_statistics(await worker.history_store.async_daily_usage_snapshot(ACCOUNT))
    rows = await world.query()
    assert [(row["state"], row["sum"]) for row in rows] == expected
    assert [(row["start"], row["state"], row["sum"]) for row in rows] == [
        (row["start"].timestamp(), row["state"], row["sum"]) for row in desired
    ]
    changes = [row["change"] for row in await world.query(types={"change"})]
    assert changes == [row["state"] for row in desired]
    assert all(value >= 0 for value in changes)
    assert worker._task is None and not worker._pending
    assert all(lane.target is None and not lane.lock.locked() for lane in worker._lanes.values())
    count = len(world.imports)
    await world.sync(worker)
    assert len(world.imports) == count


@pytest.mark.parametrize("case", ["A", "B", "C", "D"])
def test_real_pending_import_four_audit_counterexamples(recorder_world, monkeypatch, case):
    async def scenario():
        async with recorder_world() as world:
            initial = {DAY1: 1} if case in ("A", "B") else {DAY1: 1, DAY2: 1}
            latest = {DAY1: 1}
            if case == "C":
                latest[DAY2] = 3
            elif case == "D":
                latest[DAY2] = 0
            expected = [(1, 1)] if case in ("A", "B") else [(1, 1), (latest[DAY2], 1 + latest[DAY2])]
            await world.upsert(initial)
            await world.sync()
            with blocked_import(monkeypatch, world.statistic_id) as (entered, release):
                await world.upsert({DAY1: 2})
                world.bridge.request_sync()
                old_task = world.bridge._task
                await wait_entered(entered)
                assert world.bridge._lanes[world.statistic_id].target is not None
                assert not old_task.done()
                await world.upsert(latest)
                worker = world.bridge
                if case == "B":
                    # Cancellation ends the waiter, never ownership of the import.
                    old_task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await old_task
                    worker = EnergyStatisticsBridge(world.hass, world.entry, world.store)
                for _ in range(20):
                    worker.request_sync()
                final_task = worker._task
                await asyncio.sleep(0)
                assert final_task is not None and not final_task.done()
                release.set()
                await asyncio.wait_for(final_task, 5)
                await assert_converged(world, worker, expected)
                await worker.async_shutdown()
    asyncio.run(scenario())


def test_real_integration_unload_reload_drains_before_new_bridge(recorder_world, monkeypatch):
    """Actual entry setup/unload, Store reload, coordinators and entry callbacks."""
    async def scenario():
        async with recorder_world() as world:
            await world.bridge.async_shutdown()
            # Register the actual ConfigEntry without triggering unrelated cloud
            # setup; the integration setup/unload below remains real.
            world.hass.config_entries._entries[world.entry.entry_id] = world.entry
            world.hass.config_entries.async_update_entry(world.entry, data={
                **world.entry.data, CONF_AUTH_TOKEN: "synthetic", CONF_USERNAME: "synthetic",
            })
            world.entry.data[CONF_SETTINGS][CONF_UPDATE_INTERVAL] = 3600
            monkeypatch.setattr(integration.CSGClient, "load", lambda _: SimpleNamespace(verify_login=lambda: True))
            monkeypatch.setattr(sensor.CSGCoordinator, "async_refresh", AsyncMock())
            timers = []
            monkeypatch.setattr(sensor, "async_track_time_change", lambda *a, **kw: timers.append(Mock()) or timers[-1])

            async def forward(entry, platforms):
                await sensor.async_setup_entry(world.hass, entry, lambda entities: None)

            monkeypatch.setattr(world.hass.config_entries, "async_forward_entry_setups", forward)
            unload_platforms = AsyncMock(return_value=True)
            monkeypatch.setattr(world.hass.config_entries, "async_unload_platforms", unload_platforms)
            assert await integration.async_setup_entry(world.hass, world.entry)
            runtime = world.hass.data[DOMAIN][world.entry.entry_id]
            old_bridge = runtime["energy_statistics_bridge"]
            old_store = runtime["history_store"]

            async def write(store, value):
                await store.async_upsert_daily_usage(ACCOUNT, (2026, 9), [{"date": DAY1, "kwh": value}])

            await write(old_store, 1)
            await world.sync(old_bridge)
            with blocked_import(monkeypatch, world.statistic_id) as (entered, release):
                await write(old_store, 2)
                old_bridge.request_sync()
                await wait_entered(entered)

                async def finish_history():
                    # A final producer write during shutdown must also converge.
                    assert not old_bridge._accepting
                    await write(old_store, 1)
                    old_bridge.request_sync()

                runtime["history_coordinator"] = SimpleNamespace(async_shutdown=finish_history)

                async def reload():
                    assert await integration.async_unload_entry(world.hass, world.entry)
                    await world.entry._async_process_on_unload(world.hass)
                    assert world.entry.entry_id not in world.hass.data[DOMAIN]
                    assert await integration.async_setup_entry(world.hass, world.entry)
                    return world.hass.data[DOMAIN][world.entry.entry_id]["energy_statistics_bridge"]

                reloading = asyncio.create_task(reload())
                await asyncio.sleep(0)
                assert not reloading.done()
                assert not old_bridge._accepting
                release.set()
                new_bridge = await asyncio.wait_for(reloading, 5)
                assert new_bridge is not old_bridge and new_bridge.history_store is not old_store
                await world.sync(new_bridge)
                await assert_converged(world, new_bridge, [(1, 1)])
                assert timers[0].call_count == 1
                assert unload_platforms.await_count == 1
                assert await integration.async_unload_entry(world.hass, world.entry)
                await world.entry._async_process_on_unload(world.hass)
                assert timers[1].call_count == 1
    asyncio.run(scenario())


def test_real_import_retry_cannot_be_confirmed_by_a_commit_hint(recorder_world, monkeypatch):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({DAY1: 1})
            await world.sync()
            original = ha_statistics.import_statistics
            attempts = 0
            entered, release = threading.Event(), threading.Event()

            def retry(recorder, metadata, rows, table):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    return False  # Real ImportStatisticsTask requeues its retry.
                if attempts == 2:
                    entered.set()
                    assert release.wait(10)
                return original(recorder, metadata, rows, table)

            monkeypatch.setattr(ha_statistics, "import_statistics", retry)
            try:
                await world.upsert({DAY1: 2})
                world.bridge.request_sync()
                task = world.bridge._task
                await wait_entered(entered)
                await world.upsert({DAY1: 1})
                world.bridge.request_sync()
                assert not task.done() and world.bridge._lanes[world.statistic_id].target is not None
                release.set()
                await asyncio.wait_for(task, 5)
                await assert_converged(world, world.bridge, [(1, 1)])
                assert attempts == 3
            finally:
                release.set()
    asyncio.run(scenario())


def test_real_global_stop_cancels_confirmation_without_persistent_ack(recorder_world, monkeypatch):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({DAY1: 1})
            await world.sync()
            with blocked_import(monkeypatch, world.statistic_id) as (entered, release):
                await world.upsert({DAY1: 2})
                world.bridge.request_sync()
                task = world.bridge._task
                await wait_entered(entered)
                # Exercise HA's actual global stop; let Recorder go only after
                # the Bridge waiter has been cancelled by the real stop event.
                stopping = asyncio.create_task(world.hass.async_stop(force=True))
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
                assert world.bridge._task is None and not world.bridge._pending
                assert world.bridge._lanes[world.statistic_id].target is not None
                assert not any("import" in key or "materialized" in key for key in world.store._data)
                release.set()
                await asyncio.wait_for(stopping, 5)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["query", "stopping", "timeout", "system"])
def test_real_unconfirmed_ownership_survives_failed_waiter(recorder_world, monkeypatch, failure):
    from custom_components.csg import energy_statistics as module

    class SystemFailure(BaseException):
        pass

    async def scenario():
        async with recorder_world() as world:
            await world.upsert({DAY1: 1})
            await world.sync()
            original = module.statistics_during_period
            failed = False

            def query(*args):
                nonlocal failed
                if not failed and world.bridge._lanes[world.statistic_id].target is not None:
                    failed = True
                    error = SystemFailure if failure == "system" else RuntimeError
                    raise error("Synthetic confirmation query failure")
                return original(*args)

            with blocked_import(monkeypatch, world.statistic_id) as (entered, release):
                # Deliberately return a completed synchronization hint even
                # while the real import is executing. Only read-back can confirm.
                monkeypatch.setattr(world.recorder, "async_block_till_done", AsyncMock())
                if failure in ("query", "system"):
                    monkeypatch.setattr(module, "statistics_during_period", query)
                elif failure == "stopping":
                    world.recorder.stop_requested = True
                else:
                    monkeypatch.setattr(module, "_CONFIRMATION_TIMEOUT", 0.1)
                await world.upsert({DAY1: 2})
                world.bridge.request_sync()
                old_task = world.bridge._task
                await wait_entered(entered)
                if failure == "system":
                    with pytest.raises(SystemFailure):
                        await asyncio.wait_for(old_task, 3)
                else:
                    await asyncio.wait_for(old_task, 3)
                lane = world.bridge._lanes[world.statistic_id]
                assert lane.target is not None
                await world.upsert({DAY1: 1})
                world.recorder.stop_requested = False
                monkeypatch.setattr(module, "_CONFIRMATION_TIMEOUT", 5)
                fresh = EnergyStatisticsBridge(world.hass, world.entry, world.store)
                fresh.request_sync()
                new_task = fresh._task
                await asyncio.sleep(0)
                assert not new_task.done() and lane.target is not None
                release.set()
                await asyncio.wait_for(new_task, 5)
                await assert_converged(world, fresh, [(1, 1)])
                await fresh.async_shutdown()
    asyncio.run(scenario())


def test_real_shutdown_caller_cancellation_preserves_old_import_ownership(recorder_world, monkeypatch):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({DAY1: 1})
            await world.sync()
            with blocked_import(monkeypatch, world.statistic_id) as (entered, release):
                await world.upsert({DAY1: 2})
                world.bridge.request_sync()
                worker_task = world.bridge._task
                await wait_entered(entered)
                shutting_down = asyncio.create_task(world.bridge.async_shutdown())
                await asyncio.sleep(0)
                shutting_down.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await shutting_down
                with pytest.raises(asyncio.CancelledError):
                    await worker_task
                assert world.bridge._task is None
                assert world.bridge._lanes[world.statistic_id].target is not None
                await world.upsert({DAY1: 1})
                fresh = EnergyStatisticsBridge(world.hass, world.entry, world.store)
                fresh.request_sync()
                task = fresh._task
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                await asyncio.wait_for(task, 5)
                await assert_converged(world, fresh, [(1, 1)])
                await fresh.async_shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["final_snapshot", "final_query", "finishing"])
def test_real_request_at_final_verification_or_worker_finish(recorder_world, monkeypatch, stage):
    from custom_components.csg import energy_statistics as module

    async def scenario():
        async with recorder_world() as world:
            await world.upsert({DAY1: 1})
            await world.sync()
            entered, release = asyncio.Event(), asyncio.Event()
            thread_entered, thread_release = threading.Event(), threading.Event()
            original_snapshot = world.store.async_daily_usage_snapshot
            original_query = module.statistics_during_period
            original_pass = world.bridge._async_sync
            snapshots = 0
            paused = False

            async def snapshot(account):
                nonlocal snapshots
                snapshots += 1
                if snapshots == 2:
                    entered.set()
                    await release.wait()
                return await original_snapshot(account)

            def query(*args):
                nonlocal paused
                result = original_query(*args)
                if len(world.imports) == 2 and not paused and world.bridge._lanes[world.statistic_id].target is None:
                    paused = True
                    thread_entered.set()
                    assert thread_release.wait(10)
                return result

            async def pass_then_pause():
                nonlocal paused
                await original_pass()
                if not paused:
                    paused = True
                    entered.set()
                    await release.wait()

            if stage == "final_snapshot":
                monkeypatch.setattr(world.store, "async_daily_usage_snapshot", snapshot)
            elif stage == "final_query":
                monkeypatch.setattr(module, "statistics_during_period", query)
            else:
                monkeypatch.setattr(world.bridge, "_async_sync", pass_then_pause)
            try:
                await world.upsert({DAY1: 2})
                world.bridge.request_sync()
                task = world.bridge._task
                if stage == "final_query":
                    await wait_entered(thread_entered)
                else:
                    await asyncio.wait_for(entered.wait(), 5)
                await world.upsert({DAY1: 3})
                for _ in range(20):
                    world.bridge.request_sync()
                    assert world.bridge._task is task
                release.set()
                thread_release.set()
                await asyncio.wait_for(task, 5)
                await assert_converged(world, world.bridge, [(3, 3)])
            finally:
                release.set()
                thread_release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["zero", "partial", "all", "repeated"])
def test_actual_process_exit_and_fresh_process_convergence(tmp_path, case):
    helper = Path(__file__).with_name("energy_statistics_restart_process.py")
    for mode in ("seed", "verify"):
        result = subprocess.run(
            [sys.executable, str(helper), mode, str(tmp_path), case],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["state_sum"] == [[1, 1], [0, 1]]
    assert evidence["change"] == [1, 0]
