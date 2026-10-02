"""Bounded historical collection, durability, resume, and lifecycle regressions.

The real HistoryStore and client parsers run over synthetic cloud responses and
keyed in-memory storage. No user data or Recorder database is used.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from copy import deepcopy
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError
from requests import RequestException

import custom_components.csg as integration
from custom_components.csg import history_coordinator as module, history_store as store_module, sensor
from custom_components.csg.const import (
    CONF_AUTH_TOKEN, CONF_ELE_ACCOUNTS, CONF_ENERGY_STATISTICS_ENABLED, CONF_HISTORY_START_MONTH, CONF_SETTINGS,
    CONF_UPDATE_INTERVAL, DOMAIN, SUFFIX_LAST_MONTH_COST,
)
from custom_components.csg.csg_client import CSGAPIError, CSGElectricityAccount
from custom_components.csg.history_coordinator import HistoryCoordinator, historical_months
from custom_components.csg.history_helpers import month_key, parse_history_start_month
from custom_components.csg.history_store import CSGHistoryStore
from test_history_store_integration import rig as recent_rig


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def rig(monkeypatch):
    persisted = {}
    writes = []
    control = SimpleNamespace(fail_phase=None, failure="swallow", last_phase=None)

    def checkpoints(payload):
        return {
            account: data.get("sync", {}).get("history_backfill", {})
            for account, data in (payload or {}).get("accounts", {}).items()
            if data.get("sync", {}).get("history_backfill")
        }

    class Storage:
        def __init__(self, hass, version, key, **kwargs):
            self.key = key
            self.read_only = kwargs.get("read_only", False)

        async def async_save(self, payload):
            phase = "checkpoint" if checkpoints(payload) != checkpoints(persisted.get(self.key)) else "facts"
            control.last_phase = phase
            writes.append((phase, deepcopy(payload)))
            if phase == control.fail_phase and control.failure in ("swallow", "raise"):
                if control.failure == "raise":
                    raise OSError("Synthetic save failure")
                return
            persisted[self.key] = deepcopy(payload)

        async def async_load(self):
            if self.read_only and control.last_phase == control.fail_phase:
                if control.failure == "verify_raise":
                    raise HomeAssistantError("Synthetic verification failure")
                if control.failure == "verify_mismatch":
                    return None
            return deepcopy(persisted.get(self.key))

    class Client:
        def __init__(self):
            self.daily = {}
            self.bills = {}
            self.calls = []

        def verify_login(self):
            return True

        def initialize(self):
            pass

        def get_month_daily_usage_detail(self, account, month):
            self.calls.append(("daily", account.account_number, month))
            result = self.daily.get((account.account_number, month), (0, []))
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)

        def get_year_month_stats(self, account, year):
            self.calls.append(("bill", account.account_number, year))
            result = self.bills.get((account.account_number, year), (0, 0, []))
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)

    async def execute(function, *args):
        return function(*args)

    client = Client()
    for name in (
        "get_yesterday_kwh", "get_month_daily_cost_detail",
        "api_query_day_electric_charge_by_m_point",
    ):
        setattr(client, name, Mock(side_effect=AssertionError("Forbidden historical API")))
    tasks = []

    def create_task(hass, target, name, eager_start=True):
        assert not eager_start
        task = asyncio.create_task(target, name=name)
        tasks.append(task)
        return task

    entry = SimpleNamespace(
        entry_id="synthetic-history", title="CSG synthetic",
        data={
            CONF_AUTH_TOKEN: "synthetic-auth", "username": "synthetic-user",
            CONF_SETTINGS: {CONF_HISTORY_START_MONTH: "2024-02", CONF_UPDATE_INTERVAL: 3600, CONF_ENERGY_STATISTICS_ENABLED: False},
            CONF_ELE_ACCOUNTS: {"fictional-a": CSGElectricityAccount("fictional-a").dump()},
        },
        async_create_background_task=create_task,
    )
    hass = SimpleNamespace(data={}, async_add_executor_job=execute)
    clock = SimpleNamespace(now=dt.datetime(2024, 3, 1, tzinfo=dt.UTC))
    monkeypatch.setattr(store_module, "Store", Storage)
    monkeypatch.setattr(module.CSGClient, "load", Mock(return_value=client))
    monkeypatch.setattr(module.dt_util, "utcnow", lambda: clock.now)
    monkeypatch.setattr(store_module, "_csg_today", lambda: clock.now.astimezone(module._CSG_TIME_ZONE).date())
    # Any architecture violation fails the test where it occurs.
    forbidden = []
    import homeassistant.components.recorder as recorder
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.components.recorder import statistics
    for owner, name in (
        (recorder, "get_instance"),
        (Recorder, "async_adjust_statistics"),
        (statistics, "async_add_external_statistics"),
    ):
        stub = Mock(side_effect=AssertionError("Historical collection touched Recorder"))
        monkeypatch.setattr(owner, name, stub)
        forbidden.append(stub)

    async def build():
        history = CSGHistoryStore(hass, entry.entry_id)
        await history.async_load()
        return HistoryCoordinator(hass, entry, history)

    async def sync(coordinator=None):
        coordinator = coordinator or await build()
        task = coordinator.start()
        if task is not None:
            await task
        return coordinator

    result = SimpleNamespace(
        build=build, sync=sync, entry=entry, hass=hass, client=client, clock=clock,
        persisted=persisted, writes=writes, control=control, tasks=tasks, forbidden=forbidden,
    )
    yield result
    for name in (
        "get_yesterday_kwh", "get_month_daily_cost_detail",
        "api_query_day_electric_charge_by_m_point",
    ):
        getattr(client, name).assert_not_called()
    for stub in forbidden:
        stub.assert_not_called()


@pytest.mark.parametrize("value", ["2024-02", "0001-01", "9999-12"])
def test_canonical_calendar_month(value):
    assert month_key(parse_history_start_month(value)) == value


@pytest.mark.parametrize("value", ["2024-2", "202402", "2024-00", "2024-13", "0000-01", "10000-01", " 2024-02", "2024-02\n", "２０２４-０２", "2024-02-01", "", None, True, 202402])
def test_invalid_calendar_month(value):
    with pytest.raises(ValueError):
        parse_history_start_month(value)


@pytest.mark.parametrize("start,now,expected", [
    ("2023-12", "2024-03-01T00:00:00+00:00", [(2024, 2), (2024, 1), (2023, 12)]),
    ("2024-02", "2024-03-01T00:00:00+00:00", [(2024, 2)]),
    ("2024-03", "2024-03-01T00:00:00+00:00", []),
    ("2024-04", "2024-03-01T00:00:00+00:00", []),
    ("2024-01", "2024-02-29T15:59:59+00:00", [(2024, 1)]),
    ("2024-01", "2024-02-29T16:00:00+00:00", [(2024, 2), (2024, 1)]),
])
def test_range_excludes_current_shanghai_month(start, now, expected):
    assert historical_months(start, dt.datetime.fromisoformat(now)) == expected


@pytest.mark.parametrize("start", [None, "", "2024-03", "2024-04", "broken"])
def test_disabled_or_empty_scope_makes_no_historical_requests(rig, start):
    if start is None:
        rig.entry.data[CONF_SETTINGS].pop(CONF_HISTORY_START_MONTH)
    else:
        rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = start
    coordinator = run(rig.sync())
    assert rig.client.calls == []
    module.CSGClient.load.assert_not_called()
    assert rig.persisted == {}
    if not start:
        assert coordinator.start() is None
        assert rig.tasks == []


def test_disabled_preserves_recent_request_counts(recent_rig):
    async def scenario():
        objects = await recent_rig.build()
        history = HistoryCoordinator(recent_rig.hass, recent_rig.entry, objects.history)
        assert history.start() is None
        await objects.realtime._async_update_data()
        await objects.billing._async_update_data()
        for account in ("account", "other"):
            assert recent_rig.client.calls.count(("balance", account)) == 1
            assert recent_rig.client.calls.count(("daily", account, (2026, 9))) == 2
            assert recent_rig.client.calls.count(("daily", account, (2026, 8))) == 2
            assert recent_rig.client.calls.count(("year", account, 2026)) == 1
            assert recent_rig.client.calls.count(("year", account, 2025)) == 1
        assert len(recent_rig.client.calls) == 14
    run(scenario())


@pytest.mark.parametrize("kind,state,days", [("complete", "complete", 29), ("partial", "partial", 1), ("empty", "empty", 0)])
def test_daily_complete_partial_and_empty_are_successful(rig, kind, state, days):
    rows = [{"date": f"2024-02-{day:02d}", "kwh": 0 if day == 1 else 1} for day in range(1, days + 1)]
    rig.client.daily["fictional-a", (2024, 2)] = (days, rows)
    rig.client.bills["fictional-a", 2024] = (0, 0, [{"month": "202402", "kwh": max(0, days - 1), "charge": 8}])
    history = run(rig.sync()).history_store
    assert history.daily_coverage("fictional-a", (2024, 2))["state"] == state
    assert history.history_progress("fictional-a")["completed_daily_months"] == ["2024-02"]
    assert history.monthly_reconciliation("fictional-a", (2024, 2))["usage_state"] == ("matched" if state == "complete" else "not_comparable")
    if days:
        assert history.daily_usage("fictional-a", "2024-02-01")["kwh"] == 0
    else:
        assert history.daily_usage("fictional-a", "2024-02-01") is None


def test_daily_response_validation_conflicts_and_refetch_revision(rig):
    rows = [
        {"date": "2024-02-01", "kwh": 0},
        {"date": "2024-02-02", "kwh": 3}, {"date": "2024-02-02", "kwh": 3.0},
        {"date": "2024-02-03", "kwh": 4}, {"date": "2024-02-03", "kwh": 5},
        {"date": "2024-02-04", "kwh": None}, {"date": "2024-02-05"},
        {"date": "2024-02-06", "kwh": float("nan")},
        {"date": "2024-02-07", "kwh": float("inf")},
        {"date": "2024-02-08", "kwh": float("-inf")},
        {"date": "2024-02-09", "kwh": -1},
        {"date": "malformed", "kwh": 2}, {"kwh": 2},
        {"date": "2024-03-01", "kwh": 2},
    ]
    rig.client.daily["fictional-a", (2024, 2)] = (123, rows)
    async def scenario():
        coordinator = await rig.sync()
        store = coordinator.history_store
        assert store.daily_coverage("fictional-a", (2024, 2))["valid_days"] == 2
        zero = store.daily_usage("fictional-a", "2024-02-01")
        await store.async_upsert_daily_usage("fictional-a", (2024, 2), [{"date": "2024-02-02", "kwh": 2}])
        assert store.daily_usage("fictional-a", "2024-02-02")["kwh"] == 2
        assert store.daily_usage("fictional-a", "2024-02-01") == zero
        await store.async_upsert_daily_usage("fictional-a", (2024, 2), [])
        assert store.daily_coverage("fictional-a", (2024, 2))["valid_days"] == 2
    run(scenario())


@pytest.mark.parametrize("rows", list(permutations([
    {"month": "202402", "kwh": 3, "charge": 4},
    {"month": "2024-02", "kwh": 3.0, "charge": 4.0},
    {"month": "202402", "kwh": None, "charge": float("nan")},
])))
def test_bill_alias_and_identical_duplicates(rig, rows):
    rig.client.bills["fictional-a", 2024] = (0, 0, rows)
    history = run(rig.sync()).history_store
    assert history.monthly_bill("fictional-a", (2024, 2))["usage_kwh"] == 3
    assert history.monthly_bill("fictional-a", (2024, 2))["cost_cny"] == 4
    assert history.history_progress("fictional-a")["completed_bill_years"] == [2024]
    assert rig.client.calls.count(("bill", "fictional-a", 2024)) == 1


@pytest.mark.parametrize("field", ["kwh", "charge"])
@pytest.mark.parametrize("reverse", [False, True])
def test_bill_conflict_preserves_existing_fact_and_allows_other_months(rig, field, reverse):
    rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2024-01"
    rows = [{"month": "202402", "kwh": 3, "charge": 4}, {"month": "2024-02", "kwh": 3, "charge": 4}]
    rows[1][field] += 1
    if reverse:
        rows.reverse()
    rows.append({"month": "202401", "kwh": 7, "charge": 8})
    rig.client.bills["fictional-a", 2024] = (0, 0, rows)
    async def scenario():
        coordinator = await rig.build()
        await coordinator.history_store.async_upsert_monthly_bill("fictional-a", (2024, 2), usage_kwh=1, cost_cny=2)
        before = coordinator.history_store.monthly_bill("fictional-a", (2024, 2))
        await rig.sync(coordinator)
        assert coordinator.history_store.monthly_bill("fictional-a", (2024, 2)) == before
        assert coordinator.history_store.monthly_bill("fictional-a", (2024, 1))["usage_kwh"] == 7
    run(scenario())


def test_bill_scope_requested_year_and_partial_fields(rig, caplog):
    rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2024-01"
    rig.client.bills["fictional-a", 2024] = (0, 0, [
        None, "bad", {}, {"month": "202413", "kwh": 1},
        {"month": "202312", "kwh": 99, "charge": 88},
        {"month": "202403", "kwh": 66},
        {"month": "202401", "kwh": None, "charge": 5},
        {"month": "202402", "kwh": 0, "charge": False},
    ])
    async def scenario():
        coordinator = await rig.build()
        store = coordinator.history_store
        await store.async_upsert_monthly_bill("fictional-a", (2023, 12), usage_kwh=1, cost_cny=2)
        old = store.monthly_bill("fictional-a", (2023, 12))
        await rig.sync(coordinator)
        assert store.monthly_bill("fictional-a", (2023, 12)) == old
        assert store.monthly_bill("fictional-a", (2024, 3)) is None
        assert "usage_kwh" not in store.monthly_bill("fictional-a", (2024, 1))
        assert "cost_cny" not in store.monthly_bill("fictional-a", (2024, 2))
        assert store.monthly_bill("fictional-a", (2024, 2))["usage_kwh"] == 0
        assert store.history_progress("fictional-a")["completed_bill_years"] == [2024]
    run(scenario())
    assert "outside requested year" in caplog.text


@pytest.mark.parametrize("lane", ["daily", "bill"])
@pytest.mark.parametrize("error", [CSGAPIError("synthetic"), RequestException("synthetic"), TimeoutError("synthetic")])
def test_api_lane_failures_do_not_block_other_lane(rig, lane, error):
    if lane == "daily":
        rig.client.daily["fictional-a", (2024, 2)] = error
    else:
        rig.client.bills["fictional-a", 2024] = error
    async def scenario():
        first = await rig.sync()
        progress = first.history_store.history_progress("fictional-a")
        assert progress["completed_daily_months"] == ([] if lane == "daily" else ["2024-02"])
        assert progress["completed_bill_years"] == ([] if lane == "bill" else [2024])
        rig.client.daily.clear()
        rig.client.bills.clear()
        rig.client.calls.clear()
        resumed = await rig.sync()
        assert rig.client.calls == [(lane, "fictional-a", (2024, 2) if lane == "daily" else 2024)]
        assert resumed.history_store.history_progress("fictional-a")["completed_bill_years"] == [2024]
    run(scenario())


def test_empty_old_units_do_not_stop_at_empty_month_or_year(rig):
    rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2022-12"
    coordinator = run(rig.sync())
    assert len([call for call in rig.client.calls if call[0] == "daily"]) == 15
    assert [call[2] for call in rig.client.calls if call[0] == "bill"] == [2024, 2023, 2022]
    assert len(coordinator.history_store.history_progress("fictional-a")["completed_daily_months"]) == 15


@pytest.mark.parametrize("failure", ["swallow", "raise", "verify_raise", "verify_mismatch"])
def test_fact_save_or_verification_failure_never_advances_checkpoint(rig, failure):
    rig.control.fail_phase = "facts"
    rig.control.failure = failure
    rig.client.daily["fictional-a", (2024, 2)] = (2, [{"date": "2024-02-01", "kwh": 2}])
    rig.client.bills["fictional-a", 2024] = (3, 2, [{"month": "202402", "kwh": 2, "charge": 3}])
    coordinator = run(rig.sync())
    assert coordinator.history_store.history_progress("fictional-a") == {}
    assert all(phase == "facts" for phase, _ in rig.writes)
    assert all(not data["accounts"]["fictional-a"]["sync"].get("history_backfill") for data in rig.persisted.values())
    rig.control.fail_phase = None
    resumed = run(rig.sync())
    assert resumed.history_store.history_progress("fictional-a")["completed_daily_months"] == ["2024-02"]


@pytest.mark.parametrize("lane", ["daily", "bill"])
@pytest.mark.parametrize("failure", ["swallow", "raise"])
def test_facts_durable_checkpoint_failure_restart_is_idempotent(rig, lane, failure):
    rig.control.fail_phase = "checkpoint"
    rig.control.failure = failure
    rig.client.daily["fictional-a", (2024, 2)] = (2, [{"date": "2024-02-01", "kwh": 2}])
    rig.client.bills["fictional-a", 2024] = (3, 2, [{"month": "202402", "kwh": 2, "charge": 3}])
    async def scenario():
        first = await rig.sync()
        store = first.history_store
        fact = store.daily_usage("fictional-a", "2024-02-01") if lane == "daily" else store.monthly_bill("fictional-a", (2024, 2))
        assert store.history_progress("fictional-a") == {}
        disk = next(iter(rig.persisted.values()))["accounts"]["fictional-a"]
        assert disk["daily_usage"]["2024-02-01"]["kwh"] == 2
        assert disk["monthly_bills"]["2024-02"]["usage_kwh"] == 2
        rig.control.fail_phase = None
        rig.client.calls.clear()
        second = await rig.sync()
        assert rig.client.calls == [("daily", "fictional-a", (2024, 2)), ("bill", "fictional-a", 2024)]
        restored = second.history_store
        assert (restored.daily_usage("fictional-a", "2024-02-01") if lane == "daily" else restored.monthly_bill("fictional-a", (2024, 2))) == fact
        assert restored.history_progress("fictional-a")["completed_daily_months"] == ["2024-02"]
        rig.client.calls.clear()
        writes = len(rig.writes)
        await rig.sync()
        assert rig.client.calls == []
        assert len(rig.writes) == writes
    run(scenario())


def test_two_phase_order_and_detached_confirmed_progress(rig):
    rig.client.daily["fictional-a", (2024, 2)] = (2, [{"date": "2024-02-01", "kwh": 2}])
    coordinator = run(rig.sync())
    for index, (phase, payload) in enumerate(rig.writes):
        if phase == "checkpoint":
            assert index > 0
            assert payload["accounts"]["fictional-a"]["daily_usage"]["2024-02-01"]["kwh"] == 2
            assert payload["accounts"]["fictional-a"]["monthly_reconciliation"]["2024-02"]
    progress = coordinator.history_store.history_progress("fictional-a")
    progress["completed_daily_months"].clear()
    assert coordinator.history_store.history_progress("fictional-a")["completed_daily_months"] == ["2024-02"]


def test_range_growth_earlier_later_and_disabled_preserve_facts(rig):
    rig.client.daily["fictional-a", (2024, 2)] = (2, [{"date": "2024-02-01", "kwh": 2}])
    rig.client.bills["fictional-a", 2024] = (0, 0, [
        {"month": "202401", "kwh": 1}, {"month": "202402", "kwh": 2}, {"month": "202403", "kwh": 3},
    ])
    async def scenario():
        first = await rig.sync()
        assert first.history_store.monthly_bill("fictional-a", (2024, 1)) is None
        rig.client.calls.clear()
        rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2024-01"
        earlier = await rig.sync()
        assert rig.client.calls == [("daily", "fictional-a", (2024, 1)), ("bill", "fictional-a", 2024)]
        assert earlier.history_store.monthly_bill("fictional-a", (2024, 1))["usage_kwh"] == 1
        rig.client.calls.clear()
        rig.clock.now = dt.datetime(2024, 4, 1, tzinfo=dt.UTC)
        newer = await rig.sync()
        assert rig.client.calls == [("daily", "fictional-a", (2024, 3)), ("bill", "fictional-a", 2024)]
        assert newer.history_store.monthly_bill("fictional-a", (2024, 3))["usage_kwh"] == 3
        before = deepcopy(rig.persisted)
        rig.client.calls.clear()
        rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2024-03"
        await rig.sync()
        rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = ""
        await rig.sync()
        assert rig.persisted == before
        assert rig.client.calls == []
    run(scenario())


def test_multiple_accounts_and_years_fail_independently(rig):
    rig.entry.data[CONF_ELE_ACCOUNTS]["fictional-b"] = CSGElectricityAccount("fictional-b").dump()
    rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2023-12"
    rig.client.daily["fictional-a", (2024, 1)] = RequestException("synthetic")
    rig.client.bills["fictional-a", 2024] = RequestException("synthetic")
    coordinator = run(rig.sync())
    a = coordinator.history_store.history_progress("fictional-a")
    b = coordinator.history_store.history_progress("fictional-b")
    assert a["completed_daily_months"] == ["2023-12", "2024-02"]
    assert a["completed_bill_years"] == [2023]
    assert b["completed_daily_months"] == ["2023-12", "2024-01", "2024-02"]
    assert b["completed_bill_years"] == [2023, 2024]
    assert [call[1] for call in rig.client.calls] == ["fictional-a"] * 5 + ["fictional-b"] * 5


def test_one_task_per_entry_and_reload_after_nonblocking_setup(rig, monkeypatch):
    stages = []
    async def forward(entry, platforms):
        assert rig.tasks == [] or all(task.done() for task in rig.tasks)
        stages.append("entities_ready")
    rig.hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(side_effect=forward),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    async def scenario():
        assert await integration.async_setup_entry(rig.hass, rig.entry)
        assert stages == ["entities_ready"]
        assert rig.client.calls == []
        coordinator = rig.hass.data[DOMAIN][rig.entry.entry_id]["history_coordinator"]
        assert coordinator.start() is rig.tasks[0]
        await rig.tasks[0]
        assert coordinator.start() is rig.tasks[0]
        assert len(rig.tasks) == 1
        billing = SimpleNamespace(async_shutdown=AsyncMock())
        rig.hass.data[DOMAIN][rig.entry.entry_id]["billing_coordinator"] = billing
        assert await integration.async_unload_entry(rig.hass, rig.entry)
        billing.async_shutdown.assert_awaited_once()
        assert coordinator.start() is None
        assert await integration.async_setup_entry(rig.hass, rig.entry)
        assert len(rig.tasks) == 2
        await rig.tasks[1]
        assert rig.tasks[0] is not rig.tasks[1]
        assert await integration.async_unload_entry(rig.hass, rig.entry)
        assert all(task.done() for task in rig.tasks)
    run(scenario())


def test_unload_cancels_executor_and_drains_before_reload(rig):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def execute(function, *args):
            if getattr(function, "__name__", None) == "get_month_daily_usage_detail":
                entered.set()
                await release.wait()
            return function(*args)
        rig.hass.async_add_executor_job = execute
        coordinator = await rig.build()
        coordinator.start()
        await entered.wait()
        shutdown = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert coordinator.start() is None
        release.set()
        await shutdown
        assert coordinator.history_store.history_progress("fictional-a") == {}
        assert rig.persisted == {}
        assert all(task.done() for task in rig.tasks)
        rig.hass.async_add_executor_job = lambda function, *args: asyncio.to_thread(function, *args)
        await rig.sync()
        assert rig.client.calls.count(("daily", "fictional-a", (2024, 2))) == 2
    run(scenario())


@pytest.mark.parametrize("stage", ["fact_save", "fact_verify", "checkpoint_save", "checkpoint_verify"])
def test_cancel_during_persistence_is_safe_to_repeat_or_confirmed(rig, stage):
    async def scenario():
        coordinator = await rig.build()
        store = coordinator.history_store
        entered = asyncio.Event()
        release = asyncio.Event()
        original = store._store.async_save if stage.endswith("save") else store._verification_store.async_load
        async def pause(*args):
            payload = args[0] if args else next(iter(rig.persisted.values()), {})
            is_checkpoint = bool(payload.get("accounts", {}).get("fictional-a", {}).get("sync", {}).get("history_backfill"))
            if is_checkpoint == stage.startswith("checkpoint"):
                entered.set()
                await release.wait()
            return await original(*args)
        if stage.endswith("save"):
            store._store.async_save = pause
        else:
            store._verification_store.async_load = pause
        rig.client.daily["fictional-a", (2024, 2)] = (2, [{"date": "2024-02-01", "kwh": 2}])
        coordinator.start()
        await entered.wait()
        shutdown = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await shutdown
        assert not store.history_progress("fictional-a")
        restored = await rig.build()
        progress = restored.history_store.history_progress("fictional-a")
        if progress.get("completed_daily_months"):
            assert restored.history_store.daily_usage("fictional-a", "2024-02-01")["kwh"] == 2
            assert restored.history_store.monthly_reconciliation("fictional-a", (2024, 2))
        rig.client.calls.clear()
        await rig.sync(restored)
        assert rig.client.calls.count(("daily", "fictional-a", (2024, 2))) == (0 if progress.get("completed_daily_months") else 1)
        assert all(task.done() for task in rig.tasks)
    run(scenario())


@pytest.mark.parametrize("kind", ["save", "verify"])
def test_cancel_preserved_when_drained_storage_operation_fails(rig, kind):
    """An I/O error during cancellation must not restart the unit loop."""
    async def scenario():
        coordinator = await rig.build()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def fail(*args):
            entered.set()
            await release.wait()
            raise OSError("Synthetic disk failure after cancellation")
        if kind == "save":
            coordinator.history_store._store.async_save = fail
        else:
            coordinator.history_store._verification_store.async_load = fail
        coordinator.start()
        await entered.wait()
        shutdown = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        # A repeated cancel must also leave the in-flight operation owned.
        rig.tasks[0].cancel()
        await asyncio.sleep(0)
        release.set()
        await shutdown
        assert rig.client.calls == [("daily", "fictional-a", (2024, 2))]
        assert coordinator.history_store.history_progress("fictional-a") == {}
        assert all(task.done() for task in rig.tasks)
    run(scenario())


@pytest.mark.parametrize("failure", ["verify_raise", "verify_mismatch"])
def test_checkpoint_verification_failure_never_installs_memory_progress(rig, failure):
    rig.control.fail_phase = "checkpoint"
    rig.control.failure = failure
    async def scenario():
        first = await rig.sync()
        assert first.history_store.history_progress("fictional-a") == {}
        rig.control.fail_phase = None
        resumed = await rig.sync()
        progress = resumed.history_store.history_progress("fictional-a")
        assert progress["completed_daily_months"] == ["2024-02"]
        assert progress["completed_bill_years"] == [2024]
    run(scenario())


def test_request_timeout_drains_executor_before_next_lane(rig, monkeypatch):
    async def scenario():
        active = 0
        maximum = 0
        events = []
        async def execute(function, *args):
            nonlocal active, maximum
            name = getattr(function, "__name__", None)
            active += 1
            maximum = max(maximum, active)
            if name == "get_month_daily_usage_detail":
                events.append("daily_start")
                await asyncio.sleep(0.2)
                events.append("daily_end")
            if name == "get_year_month_stats":
                events.append("bill_start")
            try:
                return function(*args)
            finally:
                active -= 1
        rig.hass.async_add_executor_job = execute
        monkeypatch.setattr(module, "SETTING_UPDATE_TIMEOUT", 0.1)
        coordinator = await rig.sync()
        assert maximum == 1
        assert events == ["daily_start", "daily_end", "bill_start"]
        assert coordinator.history_store.history_progress("fictional-a")["completed_daily_months"] == []
        assert coordinator.history_store.history_progress("fictional-a")["completed_bill_years"] == [2024]
    run(scenario())


def test_unload_while_draining_timeout_does_not_start_another_request(rig, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def execute(function, *args):
            if getattr(function, "__name__", None) == "get_month_daily_usage_detail":
                entered.set()
                await release.wait()
            return function(*args)
        rig.hass.async_add_executor_job = execute
        monkeypatch.setattr(module, "SETTING_UPDATE_TIMEOUT", 0.1)
        coordinator = await rig.build()
        coordinator.start()
        await entered.wait()
        await asyncio.sleep(0.2)
        shutdown = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        release.set()
        await shutdown
        assert rig.client.calls == [("daily", "fictional-a", (2024, 2))]
        assert coordinator.history_store.history_progress("fictional-a") == {}
        assert rig.persisted == {}
    run(scenario())


@pytest.mark.parametrize("kind", ["login", "initialize"])
def test_auth_or_initialization_failure_leaves_units_retryable(rig, kind):
    if kind == "login":
        rig.client.verify_login = Mock(return_value=False)
    else:
        rig.client.initialize = Mock(side_effect=RequestException("synthetic"))
    coordinator = run(rig.sync())
    assert rig.client.calls == []
    assert coordinator.history_store.history_progress("fictional-a") == {}
    assert rig.persisted == {}


def test_bill_revision_from_separate_refetch_and_incomplete_reconciliation(rig):
    rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2024-02"
    rig.client.bills["fictional-a", 2024] = (0, 0, [{"month": "202402", "kwh": 4, "charge": 5}])
    async def scenario():
        first = await rig.sync()
        assert first.history_store.monthly_reconciliation("fictional-a", (2024, 2))["usage_state"] == "not_comparable"
        rig.entry.data[CONF_SETTINGS][CONF_HISTORY_START_MONTH] = "2024-01"
        rig.client.bills["fictional-a", 2024] = (0, 0, [{"month": "2024-02", "kwh": 3, "charge": 4}])
        resumed = await rig.sync()
        assert resumed.history_store.monthly_bill("fictional-a", (2024, 2))["usage_kwh"] == 3
        assert resumed.history_store.monthly_bill("fictional-a", (2024, 2))["cost_cny"] == 4
        assert resumed.history_store.daily_usage("fictional-a", "2024-02-01") is None
    run(scenario())


def test_billing_uses_shared_requested_year_guard_without_changing_display(recent_rig, caplog):
    recent_rig.client.years["account", 2026] = (1, 2, [
        {"month": "202508", "kwh": 99, "charge": 88},
        {"month": "202608", "kwh": 3, "charge": 4},
        {"month": "2026-08", "kwh": 5, "charge": 6},
    ])
    async def scenario():
        objects = await recent_rig.build()
        await objects.history.async_upsert_monthly_bill("account", (2025, 8), usage_kwh=1, cost_cny=2)
        old = objects.history.monthly_bill("account", (2025, 8))
        data = {}
        account = CSGElectricityAccount.load(recent_rig.entry.data[CONF_ELE_ACCOUNTS]["account"])
        await objects.billing._add_year_data(recent_rig.client, account, data)
        assert objects.history.monthly_bill("account", (2025, 8)) == old
        assert objects.history.monthly_bill("account", (2026, 8)) is None
        assert data[SUFFIX_LAST_MONTH_COST] == 4
    run(scenario())
    assert "outside requested year" in caplog.text
