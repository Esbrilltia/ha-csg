"""Daily semantics, convergence, anomalies and the entry-scoped worker lifecycle."""

from __future__ import annotations

import asyncio
import datetime as dt
from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.const import UnitOfEnergy
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import EnergyConverter
from homeassistant.util import dt as dt_util

from custom_components.csg import energy_statistics as module
from custom_components.csg.const import CONF_ELE_ACCOUNTS, CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS
from custom_components.csg.csg_client import CSGElectricityAccount
from custom_components.csg.energy_statistics import EnergyStatisticsBridge, build_statistics, statistic_metadata
from test_history_store import make_store

ACCOUNT = "fictional-energy-account"
OTHER = "fictional-other-account"


def facts(values):
    return {day: {"kwh": value} for day, value in values.items()}


def actual_rows(rows):
    return [{**row, "start": row["start"].timestamp()} for row in rows]


def test_stable_identity_and_exact_metadata():
    import hashlib

    metadata = statistic_metadata(ACCOUNT)
    digest = hashlib.sha256(ACCOUNT.encode("utf-8")).hexdigest()
    assert metadata == {
        "source": "csg", "statistic_id": f"csg:energy_{digest}",
        "name": f"CSG energy {digest[:8]}", "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "unit_class": EnergyConverter.UNIT_CLASS, "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
    }
    assert statistic_metadata(ACCOUNT) == metadata
    assert statistic_metadata(OTHER)["statistic_id"] != metadata["statistic_id"]
    assert ACCOUNT not in metadata["statistic_id"] and ACCOUNT not in metadata["name"]
    assert valid_statistic_id(metadata["statistic_id"])
    assert "has_mean" not in metadata


def test_builder_sorts_zero_holes_years_and_decimal_without_hourly_distribution():
    rows = build_statistics(facts({
        "2026-01-04": 0.2, "2025-12-31": 10, "2026-01-02": 0,
        "2026-01-01": 0.1,
    }))
    assert [row["start"].date().isoformat() for row in rows] == [
        "2025-12-31", "2026-01-01", "2026-01-02", "2026-01-04",
    ]
    assert [row["state"] for row in rows] == [10, 0.1, 0, 0.2]
    assert [row["sum"] for row in rows] == [10, 10.1, 10.1, 10.3]
    assert all(row["start"].tzinfo == ZoneInfo("Asia/Shanghai") for row in rows)
    assert all(row["start"].time() == dt.time() for row in rows)
    assert rows[1]["start"].astimezone(dt.UTC) == dt.datetime(2025, 12, 31, 16, tzinfo=dt.UTC)
    assert build_statistics(facts({"2026-09-01": 0.1, "2026-09-02": 0.2}))[-1]["sum"] == 0.3
    assert build_statistics({}) == []


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf")])
def test_builder_rejects_invalid_facts(value):
    with pytest.raises((ValueError, ArithmeticError)):
        build_statistics(facts({"2026-09-01": value}))


def test_business_midnight_is_independent_of_home_assistant_timezone():
    old_zone = dt_util.get_default_time_zone()
    try:
        dt_util.set_default_time_zone(ZoneInfo("America/Los_Angeles"))
        row = build_statistics(facts({"2026-09-01": 12}))[0]
        assert row["start"] == dt.datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert row["start"].astimezone(dt.UTC) == dt.datetime(2026, 8, 31, 16, tzinfo=dt.UTC)
        assert row["state"] == row["sum"] == 12
    finally:
        dt_util.set_default_time_zone(old_zone)


def test_snapshot_is_locked_detached_and_does_not_create_an_unknown_account():
    async def scenario():
        store = make_store()
        await store.async_upsert_daily_usage(ACCOUNT, (2026, 9), [{"date": "2026-09-01", "kwh": 2}])
        before = deepcopy(store._data)
        await store._lock.acquire()
        reading = asyncio.create_task(store.async_daily_usage_snapshot(ACCOUNT))
        await asyncio.sleep(0)
        assert not reading.done()
        store._lock.release()
        snapshot = await reading
        snapshot["2026-09-01"]["kwh"] = 99
        assert await store.async_daily_usage_snapshot(OTHER) == {}
        assert store._data == before
        assert store.daily_usage(ACCOUNT, "2026-09-01")["kwh"] == 2
        store._persistence_pending = True
        with pytest.raises(HomeAssistantError, match="durable"):
            await store.async_daily_usage_snapshot(ACCOUNT)
    asyncio.run(scenario())


@pytest.fixture
def rig(monkeypatch):
    class Recorder:
        def __init__(self):
            self.rows = {}
            self.metadata = {}
            self.reads = []
            self.imports = []
            self.fail_ids = set()
            self.read_started = None
            self.read_release = None
            self.stop_requested = False

        async def async_add_executor_job(self, function, *args):
            if isinstance(function, partial):
                assert function.func is module.get_metadata
                statistic_id = next(iter(function.keywords["statistic_ids"]))
                self.reads.append(("metadata", statistic_id))
                return {statistic_id: (1, deepcopy(self.metadata[statistic_id]))} if statistic_id in self.metadata else {}
            assert function is module.statistics_during_period
            _, start, end, ids, period, units, types = args
            assert start.tzinfo is dt.UTC and end is None
            assert period == "hour" and types == {"state", "sum"}
            assert units == {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR}
            statistic_id = next(iter(ids))
            self.reads.append(("statistics", statistic_id))
            if statistic_id in self.fail_ids:
                raise RuntimeError("Synthetic account query failure")
            if self.read_started is not None:
                self.read_started.set()
                await self.read_release.wait()
            return {statistic_id: deepcopy(self.rows.get(statistic_id, []))}

        def add(self, hass, metadata, rows):
            self.imports.append((deepcopy(metadata), deepcopy(rows)))

        async def async_block_till_done(self):
            self.commit(clear=False)

        def commit(self, clear=True):
            for metadata, rows in self.imports:
                statistic_id = metadata["statistic_id"]
                self.metadata[statistic_id] = deepcopy(metadata)
                existing = {row["start"]: row for row in self.rows.get(statistic_id, [])}
                existing.update({row["start"]: row for row in actual_rows(rows)})
                self.rows[statistic_id] = list(existing.values())
            if clear:
                self.imports.clear()

    recorder = Recorder()
    tasks = []
    store = make_store()

    def create_task(hass, coroutine, name, eager_start):
        assert eager_start is False
        task = asyncio.create_task(coroutine, name=name)
        tasks.append(task)
        return task

    entry = SimpleNamespace(data={
        CONF_SETTINGS: {CONF_ENERGY_STATISTICS_ENABLED: True},
        CONF_ELE_ACCOUNTS: {"display-label": CSGElectricityAccount(ACCOUNT).dump()},
    }, async_create_background_task=create_task, async_on_unload=Mock())
    hass = SimpleNamespace(data={}, bus=SimpleNamespace(async_listen_once=Mock(return_value=Mock())), is_stopping=False)
    bridge = EnergyStatisticsBridge(hass, entry, store)
    monkeypatch.setattr(module, "get_instance", lambda hass: recorder)
    monkeypatch.setattr(module, "async_add_external_statistics", recorder.add)

    async def sync(worker=bridge):
        recorder.async_db_ready = asyncio.get_running_loop().create_future()
        recorder.async_db_ready.set_result(True)
        worker.request_sync()
        task = worker._task
        if task is not None:
            await task

    async def upsert(values, account=ACCOUNT):
        for day, value in values.items():
            date = dt.date.fromisoformat(day)
            await store.async_upsert_daily_usage(account, (date.year, date.month), [{"date": day, "kwh": value}])

    return SimpleNamespace(
        recorder=recorder, bridge=bridge, entry=entry, hass=hass, store=store,
        tasks=tasks, sync=sync, upsert=upsert, statistic_id=statistic_metadata(ACCOUNT)["statistic_id"],
    )


def test_initial_repeat_append_revision_and_hole_fill_share_convergence(rig):
    async def scenario():
        await rig.upsert({"2026-09-01": 10, "2026-09-02": 0, "2026-09-04": 5})
        await rig.sync()
        assert [row["state"] for row in rig.recorder.imports[-1][1]] == [10, 0, 5]
        rig.recorder.commit()
        await rig.sync()
        assert rig.recorder.imports == []
        await rig.upsert({"2026-09-05": 8})
        await rig.sync()
        assert [row["state"] for row in rig.recorder.imports[-1][1]] == [8]
        rig.recorder.commit()
        await rig.upsert({"2026-09-04": 4})
        await rig.sync()
        assert [(row["state"], row["sum"]) for row in rig.recorder.imports[-1][1]] == [(4, 14), (8, 22)]
        rig.recorder.commit()
        await rig.upsert({"2026-09-03": 7})
        await rig.sync()
        assert [(row["state"], row["sum"]) for row in rig.recorder.imports[-1][1]] == [(7, 17), (4, 21), (8, 29)]
        rig.recorder.commit()
        await rig.sync()
        assert rig.recorder.imports == []
    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["state", "sum"])
def test_state_or_sum_mismatch_and_explicit_tolerance(rig, field):
    async def scenario():
        await rig.upsert({"2026-09-01": 10, "2026-09-02": 8, "2026-09-03": 5})
        await rig.sync()
        rig.recorder.commit()
        actual = rig.recorder.rows[rig.statistic_id]
        actual[0][field] += 5e-10
        await rig.sync()
        assert rig.recorder.imports == []
        actual[1][field] -= 2
        await rig.sync()
        assert [row["state"] for row in rig.recorder.imports[-1][1]] == [8, 5]
    asyncio.run(scenario())


def test_restart_converges_partial_suffix_import_with_existing_metadata(rig):
    async def scenario():
        old = {"2026-09-01": 10, "2026-09-02": 8, "2026-09-03": 5, "2026-09-04": 3}
        await rig.upsert(old)
        await rig.sync()
        rig.recorder.commit()
        await rig.upsert({"2026-09-02": 7})
        new_rows = actual_rows(build_statistics(await rig.store.async_daily_usage_snapshot(ACCOUNT)))
        rig.recorder.rows[rig.statistic_id][1] = new_rows[1]  # Only the first revised day committed.
        recreated = EnergyStatisticsBridge(rig.hass, rig.entry, rig.store)
        await rig.sync(recreated)
        assert [row["state"] for row in rig.recorder.imports[-1][1]] == [5, 3]
        rig.recorder.commit()
        await rig.sync(recreated)
        assert rig.recorder.imports == []
        assert rig.recorder.rows[rig.statistic_id] == new_rows
        assert not any("checkpoint" in key or "materialized" in key for key in vars(recreated))
    asyncio.run(scenario())


def test_monthly_reconciliation_mismatch_does_not_change_daily_statistics(rig):
    async def scenario():
        await rig.upsert({f"2026-08-{day:02}": 1 for day in range(1, 32)})
        await rig.store.async_upsert_monthly_bill(ACCOUNT, (2026, 8), usage_kwh=99, cost_cny=77)
        result = await rig.store.async_reconcile_month(ACCOUNT, (2026, 8), today=dt.date(2026, 9, 1))
        assert result["usage_state"] == "mismatch"
        before = deepcopy(rig.store._data)
        await rig.sync()
        rows = rig.recorder.imports[-1][1]
        assert len(rows) == 31 and rows[-1]["sum"] == 31
        assert all(row["state"] == 1 for row in rows)
        assert rig.store._data == before
    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"source": "other"}, {"has_sum": False}, {"mean_type": StatisticMeanType.ARITHMETIC},
    {"unit_class": "power"}, {"unit_of_measurement": "Wh"},
])
def test_incompatible_metadata_is_never_overwritten(rig, change, caplog):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        metadata = statistic_metadata(ACCOUNT) | change
        rig.recorder.metadata[rig.statistic_id] = deepcopy(metadata)
        await rig.sync()
        assert rig.recorder.imports == []
        assert rig.recorder.metadata[rig.statistic_id] == metadata
        assert rig.recorder.reads == [("metadata", rig.statistic_id)]
    asyncio.run(scenario())
    assert "Incompatible" in caplog.text


def test_compatible_name_can_be_updated_without_rewriting_rows(rig):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        await rig.sync()
        rig.recorder.commit()
        rig.recorder.metadata[rig.statistic_id]["name"] = "Old display name"
        await rig.sync()
        assert rig.recorder.imports == [(statistic_metadata(ACCOUNT), [])]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["before", "hole", "after", "none_sum", "none_state", "nan", "bad_start", "duplicate"])
def test_extra_or_malformed_recorder_rows_stop_one_account_without_cleanup(rig, kind, caplog):
    async def scenario():
        await rig.upsert({"2026-09-01": 2, "2026-09-03": 3})
        await rig.sync()
        rig.recorder.commit()
        rows = rig.recorder.rows[rig.statistic_id]
        if kind in ("before", "hole", "after"):
            day = {"before": "2026-08-31", "hole": "2026-09-02", "after": "2026-09-04"}[kind]
            rows.extend(actual_rows(build_statistics(facts({day: 0}))))
        elif kind == "duplicate":
            rows.append(deepcopy(rows[0]))
        else:
            field, value = {"none_sum": ("sum", None), "none_state": ("state", None),
                            "nan": ("sum", float("nan")), "bad_start": ("start", "invalid")}[kind]
            rows[0][field] = value
        before = deepcopy(rows)
        await rig.sync()
        assert rig.recorder.imports == []
        assert repr(rig.recorder.rows[rig.statistic_id]) == repr(before)
    asyncio.run(scenario())
    assert "unsafe or unavailable" in caplog.text


@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_or_missing_setting_performs_no_recorder_access(rig, enabled):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        if enabled is None:
            rig.entry.data[CONF_SETTINGS].clear()
        else:
            rig.entry.data[CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] = enabled
        worker = EnergyStatisticsBridge(rig.hass, rig.entry, rig.store)
        rig.store.async_ensure_persisted = AsyncMock(side_effect=AssertionError("Disabled Store read"))
        worker.request_sync()
        await worker._async_sync()
        assert worker._task is None and not rig.tasks
        assert not rig.recorder.reads and not rig.recorder.imports
    asyncio.run(scenario())


def test_unconfirmed_persistence_prevents_all_recorder_access(rig):
    rig.store.async_ensure_persisted = AsyncMock(return_value=False)
    asyncio.run(rig.sync())
    assert not rig.recorder.reads and not rig.recorder.imports


def test_snapshot_rejects_a_new_unconfirmed_write_after_persistence_gate(rig):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        async def ensure():
            rig.store._persistence_pending = True  # A concurrent cancelled/failed writer staged new facts.
            return True
        rig.store.async_ensure_persisted = ensure
        await rig.sync()
        assert not rig.recorder.reads and not rig.recorder.imports
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["absent", "not_ready", "ready_false", "import"])
def test_recorder_unavailable_defers_without_discarding_facts(rig, monkeypatch, failure, caplog):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        before = deepcopy(rig.store._data)
        if failure == "absent":
            monkeypatch.setattr(module, "get_instance", lambda _: (_ for _ in ()).throw(KeyError("recorder")))
        elif failure == "import":
            monkeypatch.setattr(module, "async_add_external_statistics", lambda *args: (_ for _ in ()).throw(RuntimeError("Synthetic import failure")))
        else:
            ready = asyncio.get_running_loop().create_future()
            if failure == "ready_false":
                ready.set_result(False)
            rig.recorder.async_db_ready = ready
            rig.bridge.request_sync()
            await rig.bridge._task
            assert not rig.recorder.reads
            assert rig.store._data == before
            return
        await rig.sync()
        assert rig.store._data == before and not rig.recorder.imports
        monkeypatch.setattr(module, "get_instance", lambda _: rig.recorder)
        monkeypatch.setattr(module, "async_add_external_statistics", rig.recorder.add)
        await rig.sync()
        assert rig.recorder.imports[-1][1][0]["state"] == 2
    asyncio.run(scenario())
    assert "unavailable" in caplog.text or "not ready" in caplog.text


@pytest.mark.parametrize("failure", ["query", "extra"])
def test_multiple_accounts_continue_after_one_account_is_unsafe(rig, failure):
    async def scenario():
        rig.entry.data[CONF_ELE_ACCOUNTS][OTHER] = CSGElectricityAccount(OTHER).dump()
        await rig.upsert({"2026-09-01": 2})
        await rig.upsert({"2026-09-01": 7}, OTHER)
        if failure == "query":
            rig.recorder.fail_ids.add(rig.statistic_id)
        else:
            rig.recorder.rows[rig.statistic_id] = actual_rows(build_statistics(facts({"2026-09-02": 0})))
        await rig.sync()
        assert len(rig.recorder.imports) == 1
        metadata, rows = rig.recorder.imports[0]
        assert metadata == statistic_metadata(OTHER) and rows[0]["sum"] == 7
    asyncio.run(scenario())


def test_requests_coalesce_and_one_dirty_pass_observes_latest_snapshot(rig):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        original = rig.bridge._async_sync
        entered, release = asyncio.Event(), asyncio.Event()
        passes = 0
        active = maximum = 0
        async def sync():
            nonlocal passes, active, maximum
            passes += 1
            active += 1
            maximum = max(maximum, active)
            if passes == 1:
                entered.set()
                await release.wait()
            await original()
            active -= 1
        rig.bridge._async_sync = sync
        rig.recorder.async_db_ready = asyncio.get_running_loop().create_future()
        rig.recorder.async_db_ready.set_result(True)
        for _ in range(20):
            rig.bridge.request_sync()
        task = rig.bridge._task
        await entered.wait()
        await rig.upsert({"2026-09-02": 3})
        for _ in range(20):
            rig.bridge.request_sync()
            assert rig.bridge._task is task
        release.set()
        await task
        assert passes == 2 and maximum == 1 and len(rig.tasks) == 1
        assert rig.bridge._task is None
        assert rig.recorder.imports[-1][1][-1]["sum"] == 5
        assert all(task.done() for task in rig.tasks)
    asyncio.run(scenario())


def test_shutdown_cancels_active_read_is_repeatable_and_blocks_future_requests(rig):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        rig.recorder.read_started = asyncio.Event()
        rig.recorder.read_release = asyncio.Event()
        syncing = asyncio.create_task(rig.sync())
        await rig.recorder.read_started.wait()
        task = rig.bridge._task
        rig.bridge.request_sync()
        await rig.bridge.async_shutdown()
        await asyncio.gather(syncing, return_exceptions=True)
        await rig.bridge.async_shutdown()
        rig.bridge.request_sync()
        assert task.cancelled() and rig.bridge._task is None
        assert not rig.recorder.imports and all(task.done() for task in rig.tasks)
    asyncio.run(scenario())


def test_shutdown_retains_imports_already_owned_by_recorder(rig):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        await rig.sync()
        await rig.bridge.async_shutdown()
        assert len(rig.recorder.imports) == 1
        rig.recorder.commit()
        assert rig.recorder.rows[rig.statistic_id][0]["sum"] == 2
    asyncio.run(scenario())
