"""A-2: untrusted progress, account isolation, and unload/reload cleanup."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from requests import RequestException

import custom_components.csg_plus as integration
from custom_components.csg_plus.const import CONF_ELE_ACCOUNTS, DOMAIN
from custom_components.csg_plus.csg_client import CSGElectricityAccount
from test_history_coordinator import rig
from test_history_store import make_store

ACCOUNT = "fictional-a"
GOOD = "fictional-b"
MONTH = (2024, 2)
FACT_FIELDS = ("daily_usage", "monthly_bills", "daily_coverage", "monthly_reconciliation")


@pytest.mark.parametrize("sync", [
    {"history_backfill": None},
    {"history_backfill": {"completed_daily_months": None}},
    {"history_backfill": {"completed_bill_years": [2024], "bill_year_scopes": None}},
    {"history_backfill": {"completed_bill_years": [2024], "bill_year_scopes": []}},
    "absent",
    {"last_recent_sync": "old-recent"},
    {"history_backfill": {"last_completed_at": "old-history"}},
    {"history_backfill": {"completed_bill_years": [2024]}},
], ids=["null-backfill", "null-daily", "null-scopes", "list-scopes",
        "v1-no-sync", "partial-sync", "missing-optional-fields", "years-without-scopes"])
def test_untrusted_progress_preserves_facts_isolates_accounts_and_reloads(rig, sync):
    """Four audit inputs and four V1 controls must all remain retryable."""
    rig.entry.data[CONF_ELE_ACCOUNTS][GOOD] = CSGElectricityAccount(GOOD).dump()
    events = []

    async def forward(*args):
        events.append("forward")

    async def unload(*args):
        events.append("platforms")
        return True

    rig.hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=forward, async_unload_platforms=unload,
    )

    async def scenario():
        seed = (await rig.build()).history_store
        await seed.async_upsert_daily_usage(ACCOUNT, MONTH, [{"date": "2024-02-01", "kwh": 2}])
        await seed.async_upsert_monthly_bill(ACCOUNT, MONTH, usage_kwh=2, cost_cny=3)
        await seed.async_reconcile_month(ACCOUNT, MONTH)
        payload = next(iter(rig.persisted.values()))
        row = payload["accounts"][ACCOUNT]
        old = {field: deepcopy(row[field]) for field in FACT_FIELDS}
        if sync == "absent":
            row.pop("sync")
        else:
            row["sync"] = deepcopy(sync)
        rig.client.daily[ACCOUNT, MONTH] = RequestException("synthetic retryable failure")
        rig.client.bills[ACCOUNT, 2024] = RequestException("synthetic retryable failure")

        assert await integration.async_setup_entry(rig.hass, rig.entry)
        runtime = rig.hass.data[DOMAIN][rig.entry.entry_id]
        history = runtime["history_coordinator"]
        before = history.history_store.history_progress(ACCOUNT)
        assert not before.get("completed_daily_months")
        assert not before.get("bill_year_scopes")
        assert {field: history.history_store._data["accounts"][ACCOUNT][field] for field in FACT_FIELDS} == old
        await rig.tasks[-1]
        assert rig.tasks[-1].exception() is None
        assert rig.client.calls == [
            ("daily", ACCOUNT, MONTH), ("bill", ACCOUNT, 2024),
            ("daily", GOOD, MONTH), ("bill", GOOD, 2024),
        ]
        assert history.history_store.history_progress(GOOD)["completed_daily_months"] == ["2024-02"]
        assert not history.history_store.history_progress(ACCOUNT).get("bill_year_scopes")
        disk_row = next(iter(rig.persisted.values()))["accounts"][ACCOUNT]
        assert {field: disk_row[field] for field in FACT_FIELDS} == old

        async def billing_shutdown():
            assert history._shutdown
            events.append("billing")

        runtime["billing_coordinator"] = SimpleNamespace(async_shutdown=billing_shutdown)
        assert await integration.async_unload_entry(rig.hass, rig.entry)
        assert events == ["forward", "billing", "platforms"]
        assert rig.entry.entry_id not in rig.hass.data[DOMAIN]

        # Fresh runtime/store loads disk. Bad units retry; good checkpoints skip.
        rig.client.daily.clear()
        rig.client.bills.clear()
        rig.client.calls.clear()
        assert await integration.async_setup_entry(rig.hass, rig.entry)
        fresh = rig.hass.data[DOMAIN][rig.entry.entry_id]["history_coordinator"]
        assert fresh is not history and fresh.history_store is not history.history_store
        await rig.tasks[-1]
        assert rig.client.calls == [("daily", ACCOUNT, MONTH), ("bill", ACCOUNT, 2024)]
        progress = fresh.history_store.history_progress(ACCOUNT)
        assert progress["completed_daily_months"] == ["2024-02"]
        assert progress["bill_year_scopes"] == {"2024": ["2024-02"]}
        for field in FACT_FIELDS[:3]:
            assert fresh.history_store._data["accounts"][ACCOUNT][field] == old[field]
        reconciled = fresh.history_store.monthly_reconciliation(ACCOUNT, MONTH)
        assert {key: value for key, value in reconciled.items() if key != "checked_at"} == {
            key: value for key, value in old["monthly_reconciliation"]["2024-02"].items() if key != "checked_at"
        }
        assert await integration.async_unload_entry(rig.hass, rig.entry)
        assert all(task.done() for task in rig.tasks)

    asyncio.run(scenario())


@pytest.mark.parametrize("raw, daily, years, scopes", [
    (None, [], [], {}),
    ([], [], [], {}),
    ({"completed_daily_months": "2024-02"}, [], [], {}),
    ({"completed_daily_months": {"2024-02": True}}, [], [], {}),
    ({"completed_daily_months": ["2024-02", "202402", "2024-2", "0000-01", "2024-13",
                                None, False, 202402, [], "2024-01", "2024-02"]},
     ["2024-01", "2024-02"], [], {}),
    ({"completed_bill_years": None}, [], [], {}),
    ({"completed_bill_years": "2024", "bill_year_scopes": {"2024": ["2024-02"]}}, [], [], {}),
    ({"completed_bill_years": [True, 2024.0, "2024", 0, 10000, 2024, 2024],
      "bill_year_scopes": {"2024": ["2024-02", "2023-12", None, "202402", "2024-01"]}},
     [], [2024], {"2024": ["2024-01", "2024-02"]}),
    ({"bill_year_scopes": {"2024": ["2024-02"]}}, [], [], {}),
    ({"completed_bill_years": [2024], "bill_year_scopes": {
        "2024": None, "02024": ["2024-02"], "0": [], "10000": [], "9" * 5000: [],
        "２０２４": [], 2024: [], "2023": ["2023-12"]}}, [], [2024], {"2024": []}),
    ({"completed_bill_years": [2024], "bill_year_scopes": {"2024": "2024-02"}}, [], [2024], {"2024": []}),
    ({"completed_daily_months": ["9999-12", "0001-01"], "completed_bill_years": [9999, 1],
      "bill_year_scopes": {"1": ["0001-01"], "9999": ["9999-12"]}},
     ["0001-01", "9999-12"], [1, 9999], {"1": ["0001-01"], "9999": ["9999-12"]}),
])
def test_completion_members_are_strict_conservative_and_deterministic(raw, daily, years, scopes):
    store = make_store()
    row = store._account(ACCOUNT)
    row["daily_usage"] = {"2024-02-01": {"kwh": 2}}
    row["sync"] = {"history_backfill": raw}
    original = deepcopy(store._data)
    progress = store.history_progress(ACCOUNT)
    assert progress.get("completed_daily_months", []) == daily
    assert progress.get("completed_bill_years", []) == years
    assert progress.get("bill_year_scopes", {}) == scopes
    assert store.history_progress(ACCOUNT) == progress
    assert store._data == original
    progress.clear()
    assert store.daily_usage(ACCOUNT, "2024-02-01") == {"kwh": 2}


@pytest.mark.parametrize("sync", [None, [], "invalid"])
def test_invalid_sync_container_can_only_complete_after_new_verified_work(sync):
    async def scenario():
        store = make_store()
        await store.async_upsert_daily_usage(ACCOUNT, MONTH, [{"date": "2024-02-01", "kwh": 2}])
        old = store.daily_usage(ACCOUNT, "2024-02-01")
        store._account(ACCOUNT)["sync"] = sync
        assert store.history_progress(ACCOUNT) == {}
        assert await store.async_complete_history_unit(ACCOUNT, daily_month=MONTH)
        assert store.history_progress(ACCOUNT)["completed_daily_months"] == ["2024-02"]
        assert store.daily_usage(ACCOUNT, "2024-02-01") == old
    asyncio.run(scenario())


def test_unexpected_account_progress_read_failure_does_not_block_good_account(rig, monkeypatch):
    rig.entry.data[CONF_ELE_ACCOUNTS][GOOD] = CSGElectricityAccount(GOOD).dump()

    async def scenario():
        coordinator = await rig.build()
        original = coordinator.history_store.history_progress

        def read(account):
            if account == ACCOUNT:
                raise RuntimeError("synthetic account-local read failure")
            return original(account)

        monkeypatch.setattr(coordinator.history_store, "history_progress", read)
        await rig.sync(coordinator)
        assert original(ACCOUNT)["completed_daily_months"] == ["2024-02"]
        assert original(GOOD)["bill_year_scopes"] == {"2024": ["2024-02"]}
    asyncio.run(scenario())


def test_already_failed_history_task_is_harvested_and_full_unload_reloads(rig, caplog):
    events = []
    rig.hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(side_effect=lambda *args: events.append("platforms") or True),
    )

    async def scenario():
        history = await rig.build()

        async def fail():
            raise RuntimeError("synthetic unexpected historical task failure")

        history._async_sync = fail
        task = history.start()
        with pytest.raises(RuntimeError, match="unexpected historical"):
            await task
        rig.hass.data[DOMAIN] = {rig.entry.entry_id: {
            "history_coordinator": history,
            "billing_coordinator": SimpleNamespace(
                async_shutdown=AsyncMock(side_effect=lambda: events.append("billing")),
            ),
        }}
        assert await integration.async_unload_entry(rig.hass, rig.entry)
        assert events == ["billing", "platforms"]
        assert rig.entry.entry_id not in rig.hass.data[DOMAIN]
        assert await integration.async_setup_entry(rig.hass, rig.entry)
        await rig.tasks[-1]
        assert await integration.async_unload_entry(rig.hass, rig.entry)
    asyncio.run(scenario())
    assert "Historical task failed; continuing entry cleanup" in caplog.text


@pytest.mark.parametrize("phase", ["history", "billing", "platforms"])
def test_unproven_producer_or_platform_cleanup_failure_keeps_runtime(rig, phase):
    events = []
    failure = RuntimeError("synthetic integration cleanup failure")

    async def cleanup(name):
        events.append(name)
        if name == phase:
            raise failure
        return True

    async def scenario():
        rig.hass.data[DOMAIN] = {rig.entry.entry_id: {
            "history_coordinator": SimpleNamespace(async_shutdown=lambda: cleanup("history")),
            "billing_coordinator": SimpleNamespace(async_shutdown=lambda: cleanup("billing")),
        }}
        rig.hass.config_entries = SimpleNamespace(async_unload_platforms=lambda *args: cleanup("platforms"))
        with pytest.raises(RuntimeError) as raised:
            await integration.async_unload_entry(rig.hass, rig.entry)
        assert raised.value is failure
        # These unknown producers deliberately have no cancellation fallback.
        # An unproven barrier must prevent finalization/platform removal; a
        # genuine platform failure must also retain the runtime for diagnosis.
        assert events == (["history", "billing", "platforms"] if phase == "platforms" else ["history", "billing"])
        assert rig.entry.entry_id in rig.hass.data[DOMAIN]
    asyncio.run(scenario())


def test_shutdown_preserves_caller_cancellation_while_child_drains(rig):
    async def scenario():
        history = await rig.build()
        started, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def child():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                draining.set()
                await release.wait()
                raise

        history._task = asyncio.create_task(child())
        await started.wait()
        shutdown = asyncio.create_task(history.async_shutdown())
        await draining.wait()
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert not history._task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await history._task
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [SystemExit("synthetic"), KeyboardInterrupt("synthetic")])
def test_shutdown_does_not_swallow_system_exceptions(rig, error):
    async def scenario():
        history = await rig.build()
        history._task = asyncio.get_running_loop().create_future()
        history._task.set_exception(error)
        with pytest.raises(type(error)):
            await history.async_shutdown()
    asyncio.run(scenario())
