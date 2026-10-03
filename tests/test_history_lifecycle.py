"""A-1: real HA global stop, Store, disk workers, and fresh-instance recovery."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage as ha_storage
from homeassistant.util import json as json_util

from custom_components.csg_plus import history_coordinator as coordinator_module
from custom_components.csg_plus import history_store as store_module
from custom_components.csg_plus.const import (
    CONF_AUTH_TOKEN, CONF_ELE_ACCOUNTS, CONF_HISTORY_START_MONTH, CONF_SETTINGS,
    CONF_UPDATE_INTERVAL, DOMAIN,
)
from custom_components.csg_plus.csg_client import CSGClient, CSGElectricityAccount
from custom_components.csg_plus.history_coordinator import HistoryCoordinator
from custom_components.csg_plus.history_io import HistoryStorageHass, _StorageLane
from custom_components.csg_plus.history_store import CSGHistoryStore

ACCOUNT = "fictional-lifecycle"
MONTH = (2024, 2)
DAY = "2024-02-01"


class Cloud(CSGClient):
    """Synthetic raw responses; the actual client parsers remain in use."""

    def __init__(self):
        self.value = 2
        self.calls = []
        self.block = None

    def verify_login(self):
        return True

    def initialize(self):
        pass

    def api_query_day_electric_by_m_point(self, year, month, *args):
        self.calls.append(("daily", (year, month)))
        if self.block is not None:
            self.block()
        return {"totalPower": self.value, "result": [{"date": DAY, "power": self.value}]}

    def api_get_fee_analyze_details(self, year, *args):
        self.calls.append(("bill", year))
        return {"totalBillingElectricity": 0, "totalActualAmount": 0, "electricAndChargeList": []}


@pytest.fixture
def worlds(monkeypatch, tmp_path):
    # HA's POSIX chmod is absent on Windows; retain real serialization and files.
    if not hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: None, raising=False)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: dt.datetime(2024, 3, 1, tzinfo=dt.UTC))
    monkeypatch.setattr(store_module, "_csg_today", lambda: dt.date(2024, 3, 1))

    async def build(path=tmp_path):
        hass = HomeAssistant(str(path))
        cloud = Cloud()
        entry = ConfigEntry(
            version=1, minor_version=1, domain="csg_plus", title="Synthetic lifecycle",
            data={
                CONF_AUTH_TOKEN: "synthetic", CONF_SETTINGS: {CONF_HISTORY_START_MONTH: "2024-02"},
                CONF_USERNAME: "synthetic-user",
                CONF_ELE_ACCOUNTS: {ACCOUNT: CSGElectricityAccount(ACCOUNT).dump()},
            },
            options={}, source="user", unique_id=None, discovery_keys={}, entry_id="synthetic-lifecycle",
            subentries_data=None,
        )
        entry.data[CONF_SETTINGS][CONF_UPDATE_INTERVAL] = 3600
        store = CSGHistoryStore(hass, entry.entry_id)
        await store.async_load()
        coordinator = HistoryCoordinator(hass, entry, store)
        monkeypatch.setattr(coordinator_module.CSGClient, "load", lambda _: cloud)
        return hass, store, coordinator, cloud

    return build


def test_config_entry_cleanup_repeats_manual_billing_shutdown_safely(worlds, monkeypatch):
    """Manual unload followed by HA's real entry callbacks cancels each timer once."""
    import custom_components.csg_plus as integration
    from custom_components.csg_plus import sensor

    async def scenario():
        hass, store, history, _ = await worlds()
        entry = history.entry
        events = []
        active = set()
        coordinators = []
        monkeypatch.setattr(sensor.CSGCoordinator, "async_refresh", AsyncMock())

        def track(_hass, _callback, **kwargs):
            token = object()
            active.add(token)

            def cancel():
                active.remove(token)  # A duplicate unregister is an error.
                events.append("timer")

            return cancel

        async def forward(*args):
            await sensor.async_setup_entry(hass, entry, lambda entities: coordinators.extend(
                {entity.coordinator for entity in entities}
            ))

        async def unload(*args):
            events.append("platforms")
            return True

        monkeypatch.setattr(sensor, "async_track_time_change", track)
        hass.config_entries = SimpleNamespace(
            async_forward_entry_setups=forward, async_unload_platforms=unload,
        )
        try:
            # Use the actual sensor setup over the already loaded shared Store.
            hass.data[DOMAIN] = {entry.entry_id: {"history_store": store}}
            await forward()
            assert len(coordinators) == 3
            assert all(coordinator.config_entry is entry for coordinator in coordinators)
            assert len(entry._on_unload) == 3
            assert len(active) == 1
            assert await integration.async_unload_entry(hass, entry)
            assert events == ["timer", "platforms"]
            # Execute HA 2026.9.3's real callback/task cleanup chain after the
            # integration's manual Billing shutdown, just as entry unload does.
            await entry._async_process_on_unload(hass)
            await entry._async_process_on_unload(hass)
            await next(c for c in coordinators if isinstance(c, sensor.BillingCoordinator)).async_shutdown()
            assert events == ["timer", "platforms"]
            assert not active
            assert all(coordinator._shutdown_requested for coordinator in coordinators)
            assert not entry._on_unload and not entry._tasks
            assert entry.entry_id not in hass.data[DOMAIN]
        finally:
            await history.async_shutdown()
            await hass.async_stop(force=True)

    asyncio.run(scenario())


async def thread_event(event):
    # Test watchdog only, never a production ownership timeout.
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 3), 4)


def disk(store):
    return json.loads(Path(store._store.path).read_text(encoding="utf-8"))["data"]


@pytest.mark.parametrize("stage", ["fact_write", "fact_verify", "checkpoint_write", "checkpoint_verify"])
def test_global_stop_retains_worker_order_and_newer_fact(worlds, monkeypatch, stage):
    """The audit's late 2 snapshot must never roll a confirmed newer 3 back."""
    async def scenario():
        hass, store, coordinator, cloud = await worlds()
        await hass.async_start()
        await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{"date": DAY, "kwh": 1}])
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        newer_staged = asyncio.Event()
        newer_write_queued = asyncio.Event()
        events = []
        blocked = False
        last_checkpoint = False
        original_write, original_read = ha_storage.write_utf8_file, json_util.load_json
        historical = recent = stopping = None
        fresh_hass = None

        def selected(payload):
            row = payload.get("accounts", {}).get(ACCOUNT, {})
            progress = row.get("sync", {}).get("history_backfill", {})
            checkpoint = bool(progress.get("completed_daily_months"))
            value = row.get("daily_usage", {}).get(DAY, {}).get("kwh")
            return value, checkpoint

        def pause():
            entered.set()
            assert release.wait(5), "test failed to release the real worker"

        def write(path, text, *args, **kwargs):
            nonlocal blocked, last_checkpoint
            if path != store._store.path:
                return original_write(path, text, *args, **kwargs)
            value, checkpoint = selected(json.loads(text)["data"])
            last_checkpoint = checkpoint
            should_block = stage.endswith("write") and value == 2 and checkpoint == stage.startswith("checkpoint")
            if should_block and not blocked:
                blocked = True
                events.append("old_start")
                pause()
                try:
                    return original_write(path, text, *args, **kwargs)
                finally:
                    events.append("old_end")
                    ended.set()
            if value == 3:
                events.append("new_write")
            return original_write(path, text, *args, **kwargs)

        def read(path, *args, **kwargs):
            nonlocal blocked
            should_block = stage.endswith("verify") and last_checkpoint == stage.startswith("checkpoint")
            if path == store._store.path and should_block and not blocked:
                blocked = True
                events.append("old_start")
                pause()
                try:
                    return original_read(path, *args, **kwargs)
                finally:
                    events.append("old_end")
                    ended.set()
            return original_read(path, *args, **kwargs)

        original_save = store._store.async_save
        async def save(payload):
            if selected(payload)[0] == 3:
                newer_staged.set()
            return await original_save(payload)

        delegate = store._store.hass
        original_submit = delegate.async_add_executor_job
        def submit(function, *args):
            future = original_submit(function, *args)
            # Observe submission, rather than waiting for this newer disk worker
            # to enter: it must remain queued behind the blocked older worker.
            # HA 2026.9.3 serializes in the event loop, then queues this
            # physical write through the unchanged HistoryStorageHass lane.
            if getattr(function, "__name__", None) == "_write_prepared_data":
                if selected(json.loads(args[-1])["data"])[0] == 3:
                    newer_write_queued.set()
            return future

        monkeypatch.setattr(ha_storage, "write_utf8_file", write)
        monkeypatch.setattr(json_util, "load_json", read)
        monkeypatch.setattr(store._store, "async_save", save)
        monkeypatch.setattr(delegate, "async_add_executor_job", submit)

        async def stop_listener(event):
            # Fix the same feasible scheduling window as the audit, using an
            # event from the actual newer save instead of a scheduling sleep.
            await newer_staged.wait()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_listener)
        try:
            historical = coordinator.start()
            await thread_event(entered)
            recent = hass.async_create_task(
                store.async_upsert_daily_usage(ACCOUNT, MONTH, [{"date": DAY, "kwh": 3}]),
                "Synthetic newer history fact", eager_start=False,
            )
            stopping = asyncio.create_task(hass.async_stop())
            await asyncio.wait_for(newer_write_queued.wait(), 3)
            assert historical.done() and historical.cancelled()
            assert not ended.is_set()
            assert "new_write" not in events
            lane = delegate._lane
            with lane._condition:
                assert lane._issued > lane._finished  # worker ownership survives task cancellation
            release.set()
            await asyncio.wait_for(stopping, 4)
            await asyncio.gather(recent, return_exceptions=True)
            assert ended.is_set()
            assert events.index("old_end") < events.index("new_write")
            assert disk(store)["accounts"][ACCOUNT]["daily_usage"][DAY]["kwh"] == 3

            # New HA, new manager, new Stores, same actual disk, no cached writer.
            fresh_hass, restored, resumed, fresh_cloud = await worlds()
            assert restored.daily_usage(ACCOUNT, DAY)["kwh"] == 3
            assert not restored.history_progress(ACCOUNT).get("completed_daily_months")
            fresh_cloud.value = 3
            await resumed.start()
            assert ("daily", MONTH) in fresh_cloud.calls
            assert restored.daily_usage(ACCOUNT, DAY)["kwh"] == 3
            assert restored.history_progress(ACCOUNT)["completed_daily_months"] == ["2024-02"]
            await resumed.async_shutdown()
        finally:
            release.set()
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
            if fresh_hass is not None:
                await fresh_hass.async_stop(force=True)
    asyncio.run(scenario())


def test_global_stop_returns_while_worker_owns_disk_then_new_instance_waits(worlds, monkeypatch):
    """No unlimited Core shutdown wait; the next lifecycle cannot pass the worker."""
    async def scenario():
        hass, store, coordinator, _ = await worlds()
        await hass.async_start()
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        original = ha_storage.write_utf8_file
        fresh_hass = None
        loading = None
        def write(path, text, *args, **kwargs):
            if path == store._store.path and not entered.is_set():
                entered.set()
                assert release.wait(5)
                try:
                    return original(path, text, *args, **kwargs)
                finally:
                    ended.set()
            return original(path, text, *args, **kwargs)
        monkeypatch.setattr(ha_storage, "write_utf8_file", write)
        try:
            task = coordinator.start()
            await thread_event(entered)
            await asyncio.wait_for(hass.async_stop(), 3)
            assert task.cancelled() and not ended.is_set()
            loading = asyncio.create_task(worlds())
            # The fresh reader is physically queued on the existing path lane.
            # Reserve one more same-path operation to prove it cannot bypass.
            delegate = store._store.hass
            queued = delegate.async_add_executor_job(lambda: ended.is_set())
            assert not queued.done()
            release.set()
            assert await queued
            fresh_hass, restored, resumed, cloud = await loading
            assert ended.is_set()
            assert restored.daily_usage(ACCOUNT, DAY)["kwh"] == 2
            assert not restored.history_progress(ACCOUNT).get("completed_daily_months")
            await resumed.async_shutdown()
        finally:
            release.set()
            if loading is not None:
                await asyncio.gather(loading, return_exceptions=True)
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
            if fresh_hass is not None:
                await fresh_hass.async_stop(force=True)
    asyncio.run(scenario())


def test_closed_old_event_loop_cannot_release_disk_order_to_new_loop(worlds, monkeypatch):
    """Physical ordering survives the old HA loop and executor lifecycle too."""
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    original = ha_storage.write_utf8_file

    def write(path, text, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
            try:
                return original(path, text, *args, **kwargs)
            finally:
                ended.set()
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(ha_storage, "write_utf8_file", write)

    async def old_lifecycle():
        hass, _, coordinator, _ = await worlds()
        await hass.async_start()
        task = coordinator.start()
        await thread_event(entered)
        await asyncio.wait_for(hass.async_stop(), 3)
        assert task.cancelled() and not ended.is_set()
        await coordinator.async_shutdown()

    async def new_lifecycle():
        submitted = asyncio.Event()
        original_submit = HistoryStorageHass.async_add_executor_job

        def submit(delegate, function, *args):
            completion = original_submit(delegate, function, *args)
            if function is json_util.load_json:
                submitted.set()
            return completion

        monkeypatch.setattr(HistoryStorageHass, "async_add_executor_job", submit)
        loading = asyncio.create_task(worlds())
        await asyncio.wait_for(submitted.wait(), 3)
        assert not loading.done() and not ended.is_set()
        release.set()
        hass, store, coordinator, _ = await asyncio.wait_for(loading, 3)
        try:
            assert ended.is_set()
            assert store.daily_usage(ACCOUNT, DAY)["kwh"] == 2
            assert not store.history_progress(ACCOUNT).get("completed_daily_months")
            await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{"date": DAY, "kwh": 3}])
            assert disk(store)["accounts"][ACCOUNT]["daily_usage"][DAY]["kwh"] == 3
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)

    try:
        asyncio.run(old_lifecycle())  # Closes loop and shuts down its default executor.
        assert not ended.is_set()
        asyncio.run(new_lifecycle())
    finally:
        release.set()


def test_global_stop_discards_late_read_only_api_response(worlds):
    async def scenario():
        hass, store, coordinator, cloud = await worlds()
        await hass.async_start()
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        def block():
            entered.set()
            try:
                assert release.wait(5)
            finally:
                ended.set()
        cloud.block = block
        try:
            task = coordinator.start()
            await thread_event(entered)
            await asyncio.wait_for(hass.async_stop(), 3)
            assert task.cancelled() and not ended.is_set()
            release.set()
            await thread_event(ended)
            assert store.daily_usage(ACCOUNT, DAY) is None
            assert store.history_progress(ACCOUNT) == {}
            assert cloud.calls == [("daily", MONTH)]
        finally:
            release.set()
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [False, True])
def test_cancelled_queued_completion_still_retires_its_worker_ticket(tmp_path, failed):
    """A cancelled queued waiter or worker error cannot strand later Store I/O."""
    async def scenario():
        loop = asyncio.get_running_loop()
        hass = SimpleNamespace(loop=loop, config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))))
        delegate = HistoryStorageHass(hass, "synthetic-ticket")
        entered, release = threading.Event(), threading.Event()
        events = []

        def first():
            entered.set()
            assert release.wait(5)
            events.append("first")

        def second():
            events.append("second")
            if failed:
                raise OSError("synthetic cancelled worker failure")

        def third():
            events.append("third")
            return events.copy()

        first_completion = delegate.async_add_executor_job(first)
        try:
            await thread_event(entered)
            cancelled = delegate.async_add_executor_job(second)
            cancelled.cancel()
            next_completion = delegate.async_add_executor_job(third)
            assert events == []
            release.set()
            await first_completion
            assert await asyncio.wait_for(next_completion, 3) == ["first", "second", "third"]
            assert cancelled.cancelled()
        finally:
            release.set()
    asyncio.run(scenario())


def test_failed_worker_start_does_not_leave_an_unserviceable_ticket(monkeypatch):
    async def scenario():
        lane = _StorageLane()
        original = threading.Thread.start
        def reject(*args):
            raise RuntimeError("synthetic submission failure")

        monkeypatch.setattr(threading.Thread, "start", reject)
        with pytest.raises(RuntimeError, match="submission failure"):
            lane.submit(lambda: None, ())
        monkeypatch.setattr(threading.Thread, "start", original)
        assert await asyncio.wrap_future(lane.submit(lambda: "next I/O", ())) == "next I/O"
        assert lane._issued == lane._finished == 1
    asyncio.run(scenario())
