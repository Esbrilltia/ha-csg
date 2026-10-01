"""Coordinator shadow writes with real HistoryStore and EnergyLedger logic.

Only HA storage I/O, scheduling, notifications and the cloud are replaced.
Fact validation, coverage, persistence verification and reconciliation stay real.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from copy import deepcopy
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import custom_components.csg as integration
from custom_components.csg import history_store as history_module, sensor
from custom_components.csg.const import (
    ATTR_KEY_MONTH_BILLING_DELAY,
    ATTR_KEY_SETTLEMENT_DATE,
    ATTR_KEY_YEAR_BILLING_DELAY,
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    SUFFIX_ARR,
    SUFFIX_BAL,
    SUFFIX_ENERGY_TOTAL,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_MONTH_KWH,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_SETTLED_COST_TOTAL,
    SUFFIX_THIS_MONTH_COST,
    SUFFIX_THIS_MONTH_KWH,
    SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from custom_components.csg.csg_client import CSGAPIError, CSGElectricityAccount
from custom_components.csg.history_store import CSGHistoryStore
from homeassistant.const import CONF_USERNAME, STATE_UNAVAILABLE


class Client:
    """Record exact API requests and return configurable published facts."""

    def __init__(self):
        self.daily = {}
        self.years = {}
        self.calls = []

    def verify_login(self):
        return True

    def get_balance_and_arrears(self, account):
        self.calls.append(("balance", account.account_number))
        return 50, 0

    def get_month_daily_usage_detail(self, account, month):
        self.calls.append(("daily", account.account_number, month))
        result = self.daily.get((account.account_number, month), (0, []))
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def get_year_month_stats(self, account, year):
        self.calls.append(("year", account.account_number, year))
        result = self.years.get((account.account_number, year), (0, 0, []))
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


@pytest.fixture
def rig(monkeypatch):
    """Construct real business objects over a keyed in-memory HA Store adapter."""
    persisted = {}
    history_io = {"save": 0, "readback": 0}

    class Storage:
        def __init__(self, hass, version, key, **kwargs):
            self.key = key
            self.read_only = kwargs.get("read_only", False)

        async def async_load(self):
            if self.read_only:
                history_io["readback"] += 1
            return deepcopy(persisted.get(self.key))

        async def async_save(self, data):
            if self.key.startswith(history_module.HISTORY_STORAGE_KEY):
                history_io["save"] += 1
            persisted[self.key] = deepcopy(data)

    async def execute(function, *args):
        return function(*args)

    client = Client()
    entry = SimpleNamespace(
        entry_id="integration-test",
        title="CSG",
        data={
            CONF_AUTH_TOKEN: "test",
            CONF_USERNAME: "test-user",
            CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 3600},
            CONF_ELE_ACCOUNTS: {
                name: CSGElectricityAccount(name, area_code="080000").dump()
                for name in ("account", "other")
            },
        },
    )
    hass = SimpleNamespace(data={}, async_add_executor_job=execute)
    monkeypatch.setattr(history_module, "Store", Storage)
    monkeypatch.setattr(sensor, "Store", Storage)
    monkeypatch.setattr(sensor.CSGCoordinator, "_client", AsyncMock(return_value=client))
    monkeypatch.setattr(sensor.CSGCoordinator, "_fetch", staticmethod(execute))
    monkeypatch.setattr(sensor.CSGCoordinator, "_notify_failure", Mock())
    monkeypatch.setattr(sensor.CSGCoordinator, "_clear_failure", Mock())
    monkeypatch.setattr(sensor.BillingCoordinator, "_async_correct_statistics", AsyncMock(return_value={}))
    monkeypatch.setattr(sensor, "_csg_today", lambda: dt.date(2026, 9, 3))
    monkeypatch.setattr(history_module, "_csg_today", lambda: dt.date(2026, 9, 3))
    monkeypatch.setattr(history_module, "_utcnow_iso", lambda: "first")
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: dt.datetime(2026, 9, 3, tzinfo=dt.UTC))

    async def build():
        history = CSGHistoryStore(hass, entry.entry_id)
        await history.async_load()
        ledger = sensor.EnergyLedger(hass, entry.entry_id)
        await ledger.async_load()
        return SimpleNamespace(
            history=history,
            ledger=ledger,
            realtime=sensor.RealtimeCoordinator(hass, entry, ledger, history),
            billing=sensor.BillingCoordinator(hass, entry, ledger, history),
            current=sensor.CurrentCoordinator(hass, entry, ledger),
        )

    return SimpleNamespace(
        build=build, client=client, entry=entry, hass=hass, persisted=persisted,
        history_io=history_io,
    )


def test_entry_loads_one_shared_store_before_platform_and_removes_only_runtime(rig, monkeypatch):
    created = []
    entities = []
    loaded = set()
    cancel_timer = Mock()
    monkeypatch.setattr(sensor, "async_track_time_change", Mock(return_value=cancel_timer))
    monkeypatch.setattr(integration.CSGClient, "load", Mock(return_value=rig.client))

    def create(hass, entry_id):
        history = CSGHistoryStore(hass, entry_id)
        created.append(history)
        original_load = history.async_load

        async def load():
            await original_load()
            loaded.add(history)

        history.async_load = load
        return history

    async def refresh(coordinator):
        if isinstance(coordinator, (sensor.RealtimeCoordinator, sensor.BillingCoordinator)):
            assert coordinator.history_store in loaded
            assert coordinator.history_store is created[-1]
        coordinator.data = await coordinator._async_update_data()

    async def forward(entry, platforms):
        assert rig.hass.data[DOMAIN][entry.entry_id]["history_store"] in loaded
        await sensor.async_setup_entry(rig.hass, entry, entities.extend)

    rig.hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(side_effect=forward),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(integration, "CSGHistoryStore", create)
    monkeypatch.setattr(sensor.CSGCoordinator, "async_refresh", refresh)
    rig.client.daily["account", (2026, 9)] = (2, [{"date": "2026-09-02", "kwh": 2}])

    async def exercise():
        assert await integration.async_setup_entry(rig.hass, rig.entry)
        assert len(created) == 1
        first = created[0]
        coordinators = {entity.coordinator for entity in entities}
        assert len(coordinators) == 3
        assert all(c.history_store is first for c in coordinators if hasattr(c, "history_store"))
        current = next(c for c in coordinators if isinstance(c, sensor.CurrentCoordinator))
        assert not hasattr(current, "history_store")
        billing = rig.hass.data[DOMAIN][rig.entry.entry_id]["billing_coordinator"]
        assert billing.update_interval is None
        assert current.update_interval == dt.timedelta(hours=1)
        assert all(entity.unique_id.startswith("csg.") for entity in entities)
        stored_before = deepcopy(rig.persisted)
        assert await integration.async_unload_entry(rig.hass, rig.entry)
        assert rig.entry.entry_id not in rig.hass.data[DOMAIN]
        cancel_timer.assert_called_once()
        assert billing._shutdown_requested
        assert rig.persisted == stored_before
        # A new entry lifecycle reloads the same facts from persistent storage.
        reloaded = CSGHistoryStore(rig.hass, rig.entry.entry_id)
        await reloaded.async_load()
        assert reloaded.daily_usage("account", "2026-09-02") == first.daily_usage("account", "2026-09-02")

    asyncio.run(exercise())


def test_entry_load_failure_does_not_forward_or_replace_history(rig, monkeypatch):
    history = Mock(async_load=AsyncMock(side_effect=OSError("load failed")))
    factory = Mock(return_value=history)
    monkeypatch.setattr(integration, "CSGHistoryStore", factory)
    monkeypatch.setattr(integration.CSGClient, "load", Mock(return_value=rig.client))
    rig.hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
    with pytest.raises(OSError, match="load failed"):
        asyncio.run(integration.async_setup_entry(rig.hass, rig.entry))
    factory.assert_called_once_with(rig.hass, rig.entry.entry_id)
    history.async_load.assert_awaited_once()
    rig.hass.config_entries.async_forward_entry_setups.assert_not_awaited()
    assert rig.entry.entry_id not in rig.hass.data[DOMAIN]


def test_realtime_full_rows_zero_account_isolation_and_legacy_latest_only(rig):
    rows = [{"date": "2026-09-01", "kwh": 0}, {"date": "2026-09-02", "kwh": 4}]
    rig.client.daily["account", (2026, 9)] = (99, rows)
    rig.client.daily["other", (2026, 9)] = (7, [{"date": "2026-09-02", "kwh": 7}])

    async def exercise():
        objects = await rig.build()
        data = await objects.realtime._async_update_data()
        assert data["account"] == {
            SUFFIX_BAL: 50, SUFFIX_ARR: 0, SUFFIX_ENERGY_TOTAL: 4,
            SUFFIX_YESTERDAY_KWH: 4, sensor._KEY_YESTERDAY_DATE: "2026-09-02",
        }
        assert objects.history.daily_usage("account", "2026-09-01")["kwh"] == 0
        assert objects.history.daily_usage("account", "2026-09-02")["kwh"] == 4
        assert objects.history.daily_usage("other", "2026-09-02")["kwh"] == 7
        assert objects.history.daily_usage("other", "2026-09-01") is None
        assert objects.ledger._data["accounts"]["account"]["realtime"] == {"2026-09-02": 4}
        assert objects.history.monthly_bill("account", (2026, 9)) is None
        assert objects.history.monthly_reconciliation("account", (2026, 9)) is None
        assert rig.client.calls == [
            ("balance", "account"), ("daily", "account", (2026, 9)),
            ("balance", "other"), ("daily", "other", (2026, 9)),
        ]

    asyncio.run(exercise())


@pytest.mark.parametrize("previous_rows", [[], [{"date": "2026-08-31", "kwh": 0}]])
def test_realtime_empty_success_records_coverage_and_preserves_fallback(rig, previous_rows):
    rig.client.daily["account", (2026, 8)] = (0, previous_rows)

    async def exercise():
        objects = await rig.build()
        data = (await objects.realtime._async_update_data())["account"]
        assert objects.history.daily_coverage("account", (2026, 9))["state"] == "empty"
        assert objects.history.daily_usage("account", "2026-09-02") is None
        assert data[SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
        assert data[SUFFIX_ENERGY_TOTAL] == (0 if previous_rows else STATE_UNAVAILABLE)
        assert rig.client.calls[:3] == [
            ("balance", "account"), ("daily", "account", (2026, 9)),
            ("daily", "account", (2026, 8)),
        ]

    asyncio.run(exercise())


def test_realtime_published_older_day_still_stops_at_current_month(rig):
    rig.client.daily["account", (2026, 9)] = (3, [{"date": "2026-09-01", "kwh": 3}])

    async def exercise():
        objects = await rig.build()
        data = (await objects.realtime._async_update_data())["account"]
        assert data[SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
        assert data[SUFFIX_ENERGY_TOTAL] == 3
        assert ("daily", "account", (2026, 8)) not in rig.client.calls

    asyncio.run(exercise())


def test_billing_ingests_independent_facts_then_reconciles_without_display_changes(rig, monkeypatch):
    current = [{"date": "2026-09-01", "kwh": 0}, {"date": "2026-09-02", "kwh": 4}]
    previous = [{"date": "2026-08-31", "kwh": 6}]
    rig.client.daily["account", (2026, 9)] = (99, current)
    rig.client.daily["account", (2026, 8)] = (88, previous)
    rig.client.years["account", 2026] = (18, 30, [
        {"month": "202608", "kwh": 20, "charge": 12},
        {"month": "2026-07", "kwh": 10, "charge": 6},
    ])

    async def exercise():
        objects = await rig.build()
        await objects.realtime._async_update_data()
        original = objects.history.daily_usage("account", "2026-09-02")
        monkeypatch.setattr(history_module, "_utcnow_iso", lambda: "second")
        reconcile = AsyncMock(wraps=objects.history.async_reconcile_month)
        objects.history.async_reconcile_month = reconcile
        rig.client.calls.clear()
        data = (await objects.billing._async_update_data())["account"]
        assert data == {
            SUFFIX_THIS_MONTH_KWH: 99, SUFFIX_THIS_MONTH_COST: STATE_UNAVAILABLE,
            ATTR_KEY_MONTH_BILLING_DELAY: {ATTR_KEY_MONTH_BILLING_DELAY: 2},
            SUFFIX_LATEST_DAY_KWH: 4, SUFFIX_LATEST_DAY_COST: STATE_UNAVAILABLE,
            ATTR_KEY_SETTLEMENT_DATE: {ATTR_KEY_SETTLEMENT_DATE: "2026-09-02"},
            SUFFIX_LAST_MONTH_KWH: 88, SUFFIX_LAST_MONTH_COST: 12,
            SUFFIX_SETTLED_COST_TOTAL: STATE_UNAVAILABLE,
            SUFFIX_THIS_YEAR_KWH: 30, SUFFIX_THIS_YEAR_COST: 18,
            SUFFIX_LAST_YEAR_KWH: 0, SUFFIX_LAST_YEAR_COST: 0,
            ATTR_KEY_YEAR_BILLING_DELAY: {ATTR_KEY_YEAR_BILLING_DELAY: "2026-08"},
        }
        assert objects.history.daily_usage("account", "2026-09-02") == original
        assert objects.history.daily_usage("account", "2026-09-01")["kwh"] == 0
        assert objects.history.daily_usage("account", "2026-08-31")["kwh"] == 6
        assert objects.history.daily_usage("account", "2026-08-01") is None
        assert objects.history.monthly_bill("account", (2026, 8))["usage_kwh"] == 20
        assert objects.history.monthly_bill("account", (2026, 9)) is None
        assert objects.history.monthly_bill("other", (2026, 8)) is None
        assert objects.history.monthly_reconciliation("account", (2026, 9))["usage_state"] == "pending"
        assert objects.history.monthly_reconciliation("account", (2026, 8))["usage_state"] == "not_comparable"
        assert objects.history.monthly_reconciliation("account", (2026, 7)) is None
        assert reconcile.await_args_list == [
            call("account", (2026, 9)), call("account", (2026, 8)),
            call("other", (2026, 9)), call("other", (2026, 8)),
        ]
        assert objects.ledger.energy_total("account") == 4
        assert objects.ledger.billing_days("account") == {
            "2026-09-01": {"kwh": 0}, "2026-09-02": {"kwh": 4}, "2026-08-31": {"kwh": 6},
        }
        objects.billing._async_correct_statistics.assert_any_await("account", {
            "2026-09-01": ({}, {"kwh": 0}),
            "2026-08-31": ({}, {"kwh": 6}),
        })
        assert rig.client.calls == [
            (kind, account, period)
            for account in ("account", "other")
            for kind, period in (("daily", (2026, 9)), ("daily", (2026, 8)), ("year", 2026), ("year", 2025))
        ]

    asyncio.run(exercise())


@pytest.mark.parametrize("order", list(permutations(("202608", "2026-07", "202609"))))
def test_all_monthly_rows_ingested_in_any_order_despite_display_break(rig, order):
    rig.client.years["account", 2026] = (18, 30, [
        {"month": month, "kwh": 10, "charge": 6} for month in order
    ])

    async def exercise():
        objects = await rig.build()
        data = (await objects.billing._async_update_data())["account"]
        assert data[SUFFIX_LAST_MONTH_COST] == 6
        for month in (7, 8, 9):
            bill = objects.history.monthly_bill("account", (2026, month))
            assert bill == {"usage_kwh": 10, "cost_cny": 6, "source": "year_month_stats", "updated_at": "first"}
        assert objects.history.daily_usage("account", "2026-08-01") is None

    asyncio.run(exercise())


@pytest.mark.parametrize("bad", [None, {}, {"month": "202613"}, {"month": "000001"}, {"month": "2026-1"}, {"month": "2026--08"}, {"month": "nonsense"}])
def test_malformed_month_row_does_not_poison_year_or_valid_months(rig, bad):
    rig.client.years["account", 2026] = (18, 30, [
        bad, {"month": "202608", "kwh": 10, "charge": 6},
        {"month": "202607", "kwh": 20, "charge": 12},
    ])

    async def exercise():
        objects = await rig.build()
        data = (await objects.billing._async_update_data())["account"]
        assert data[SUFFIX_THIS_YEAR_KWH] == 30
        assert data[SUFFIX_THIS_YEAR_COST] == 18
        # Preserve the legacy display parser, even for its double-hyphen match.
        expected_cost = STATE_UNAVAILABLE if bad == {"month": "2026--08"} else 6
        assert data[SUFFIX_LAST_MONTH_COST] == expected_cost
        assert set(objects.history._data["accounts"]["account"]["monthly_bills"]) == {"2026-07", "2026-08"}

    asyncio.run(exercise())


def test_raw_daily_rows_reach_store_validation_before_legacy_merge(rig):
    rows = [
        {"date": "2026-09-01", "kwh": 0},
        {"date": "2026-09-02", "kwh": 2},
        {"date": "2026-09-02", "kwh": 3},
    ]
    rig.client.daily["account", (2026, 9)] = (5, rows)

    async def exercise():
        objects = await rig.build()
        ingestion = AsyncMock(wraps=objects.history.async_upsert_daily_usage)
        objects.history.async_upsert_daily_usage = ingestion
        await objects.realtime._async_update_data()
        await objects.billing._async_update_data()
        assert ingestion.await_args_list.count(call("account", (2026, 9), rows)) == 2
        assert objects.history.daily_usage("account", "2026-09-01")["kwh"] == 0
        assert objects.history.daily_usage("account", "2026-09-02") is None
        assert objects.history.daily_coverage("account", (2026, 9))["valid_days"] == 1
        # Legacy merge keeps its existing last-row behavior independently.
        assert objects.ledger.billing_days("account")["2026-09-02"]["kwh"] == 3

    asyncio.run(exercise())


@pytest.mark.parametrize("billed,expected", [(31, "matched"), (32, "mismatch")])
def test_reconciliation_observes_daily_and_monthly_ingestion_from_same_refresh(rig, billed, expected):
    rig.client.daily["account", (2026, 8)] = (999, [
        {"date": f"2026-08-{day:02d}", "kwh": 1} for day in range(1, 32)
    ])
    rig.client.years["account", 2026] = (20, billed, [{"month": "202608", "kwh": billed, "charge": 20}])

    async def exercise():
        objects = await rig.build()
        await objects.billing._async_update_data()
        reconciliation = objects.history.monthly_reconciliation("account", (2026, 8))
        assert reconciliation["usage_state"] == expected
        assert reconciliation["daily_sum_kwh"] == 31
        assert reconciliation["billed_usage_kwh"] == billed

    asyncio.run(exercise())


def test_billing_january_ingests_previous_december_from_previous_year(rig, monkeypatch):
    monkeypatch.setattr(sensor, "_csg_today", lambda: dt.date(2027, 1, 3))
    monkeypatch.setattr(history_module, "_csg_today", lambda: dt.date(2027, 1, 3))
    rig.client.years["account", 2026] = (6, 10, [{"month": "2026-12", "kwh": 10, "charge": 6}])

    async def exercise():
        objects = await rig.build()
        data = (await objects.billing._async_update_data())["account"]
        assert objects.history.monthly_bill("account", (2026, 12))["usage_kwh"] == 10
        assert objects.history.monthly_reconciliation("account", (2027, 1))["usage_state"] == "pending"
        assert data[SUFFIX_LAST_MONTH_COST] == 6
        assert rig.client.calls[:4] == [
            ("daily", "account", (2027, 1)), ("daily", "account", (2026, 12)),
            ("year", "account", 2027), ("year", "account", 2026),
        ]

    asyncio.run(exercise())


@pytest.mark.parametrize("coordinator_name", ["realtime", "billing"])
def test_failed_daily_api_does_not_ingest_a_successful_empty_response(rig, coordinator_name):
    rig.client.daily["account", (2026, 9)] = CSGAPIError("offline")

    async def exercise():
        objects = await rig.build()
        ingestion = AsyncMock(wraps=objects.history.async_upsert_daily_usage)
        objects.history.async_upsert_daily_usage = ingestion
        await getattr(objects, coordinator_name)._async_update_data()
        assert call("account", (2026, 9), []) not in ingestion.await_args_list
        assert call("account", (2026, 8), []) in ingestion.await_args_list
        assert objects.history.daily_usage("account", "2026-09-02") is None

    asyncio.run(exercise())


def test_current_coordinator_keeps_ladder_without_history_writes(rig):
    rig.client.daily["account", (2026, 9)] = (20, [{"date": "2026-09-02", "kwh": 20}])

    async def exercise():
        objects = await rig.build()
        before = deepcopy(objects.history._data)
        data = await objects.current._async_update_data()
        assert data["account"] == sensor._ladder_data(sensor._guangzhou_residential_ladder(
            dt.date(2026, 9, 3), 20, [{"date": "2026-09-02", "kwh": 20}]
        ))
        assert not hasattr(objects.current, "history_store")
        assert objects.history._data == before
        assert rig.client.calls == [("daily", "account", (2026, 9)), ("daily", "other", (2026, 9))]

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["async_upsert_daily_usage", "async_upsert_monthly_bill", "async_reconcile_month"])
def test_shadow_write_failure_preserves_legacy_data_and_reports_error(rig, monkeypatch, caplog, operation):
    rig.client.daily["account", (2026, 9)] = (4, [{"date": "2026-09-02", "kwh": 4}])
    rig.client.years["account", 2026] = (6, 10, [{"month": "202608", "kwh": 10, "charge": 6}])

    async def exercise():
        objects = await rig.build()
        monkeypatch.setattr(objects.history, operation, AsyncMock(side_effect=OSError("disk failure")))
        realtime = (await objects.realtime._async_update_data())["account"]
        billing = (await objects.billing._async_update_data())["account"]
        assert realtime[SUFFIX_YESTERDAY_KWH] == 4
        assert billing[SUFFIX_THIS_MONTH_KWH] == 4
        assert billing[SUFFIX_LAST_MONTH_COST] == 6
        assert billing[SUFFIX_THIS_YEAR_KWH] == 10
        assert objects.ledger.energy_total("account") == 4
        assert objects.ledger.billing_days("account")["2026-09-02"] == {"kwh": 4}
        assert "HistoryStore shadow write failed" in caplog.text

    asyncio.run(exercise())


@pytest.mark.parametrize("order", list(permutations(((10, 5), (20, 10), (30, 15)))))
def test_monthly_conflict_permutations_and_repeated_refetch_preserve_old_fact(
    rig, monkeypatch, caplog, order
):
    """A conflicting batch never revisions a month, including after refetch."""
    rows = [{"month": "202608", "kwh": usage, "charge": cost} for usage, cost in order]
    rows.insert(1, {"month": "2026-07", "kwh": 7, "charge": 3})
    rig.client.years["account", 2026] = (33, 60, rows)

    async def exercise():
        objects = await rig.build()
        await objects.history.async_upsert_monthly_bill(
            "account", (2026, 8), usage_kwh=4, cost_cny=2
        )
        old = objects.history.monthly_bill("account", (2026, 8))
        upsert = AsyncMock(wraps=objects.history.async_upsert_monthly_bill)
        objects.history.async_upsert_monthly_bill = upsert
        rig.history_io.update(save=0, readback=0)

        for refresh in range(2):
            monkeypatch.setattr(history_module, "_utcnow_iso", lambda: f"refresh-{refresh}")
            data = {}
            await objects.billing._add_year_data(
                rig.client, CSGElectricityAccount("account"), data
            )
            assert objects.history.monthly_bill("account", (2026, 8)) == old
            assert rig.persisted[objects.history._store.key]["accounts"]["account"]["monthly_bills"]["2026-08"] == old
            assert objects.history.monthly_bill("account", (2026, 7))["usage_kwh"] == 7
            assert objects.history.monthly_bill("account", (2026, 7))["cost_cny"] == 3
            assert upsert.await_args_list == [
                call("account", (2026, 7), usage_kwh=7, cost_cny=3)
            ] * (refresh + 1)
            # Only July's first insertion saves; conflicts and the refetch add no I/O.
            assert rig.history_io == {"save": 1, "readback": 1}
            assert data[SUFFIX_LAST_MONTH_COST] == order[0][1]
            assert data[SUFFIX_THIS_YEAR_KWH] == 60
            assert data[SUFFIX_THIS_YEAR_COST] == 33

        assert rig.client.calls == [
            ("year", "account", 2026), ("year", "account", 2025)
        ] * 2
        assert any(
            "account" in record.getMessage()
            and "2026-08" in record.getMessage()
            and "conflict" in record.getMessage().lower()
            for record in caplog.records
        )

    asyncio.run(exercise())


@pytest.mark.parametrize("values", [((10, 5), (20, 5)), ((10, 5), (10, 6))], ids=["usage-only", "cost-only"])
def test_monthly_conflict_in_either_field_skips_entire_month(rig, values):
    rig.client.years["account", 2026] = (0, 0, [
        {"month": "202608", "kwh": usage, "charge": cost}
        for usage, cost in values
    ])

    async def exercise():
        objects = await rig.build()
        await objects.history.async_upsert_monthly_bill(
            "account", (2026, 8), usage_kwh=4, cost_cny=2
        )
        old = objects.history.monthly_bill("account", (2026, 8))
        before_io = dict(rig.history_io)
        upsert = AsyncMock(wraps=objects.history.async_upsert_monthly_bill)
        objects.history.async_upsert_monthly_bill = upsert
        await objects.billing._add_year_data(rig.client, CSGElectricityAccount("account"), {})
        assert objects.history.monthly_bill("account", (2026, 8)) == old
        upsert.assert_not_awaited()
        assert rig.history_io == before_io

    asyncio.run(exercise())


@pytest.mark.parametrize("months", [("202608",) * 3, ("202608", "2026-08", "202608")], ids=["identical-rows", "canonical-aliases"])
def test_monthly_candidates_deduplicate_identical_rows_once_and_allow_later_revision(
    rig, monkeypatch, months
):
    rig.client.years["account", 2026] = (5, 10, [
        {"month": month, "kwh": 10, "charge": 5} for month in months
    ])

    async def exercise():
        objects = await rig.build()
        upsert = AsyncMock(wraps=objects.history.async_upsert_monthly_bill)
        objects.history.async_upsert_monthly_bill = upsert
        await objects.billing._add_year_data(rig.client, CSGElectricityAccount("account"), {})
        upsert.assert_awaited_once_with("account", (2026, 8), usage_kwh=10, cost_cny=5)
        assert rig.history_io == {"save": 1, "readback": 1}
        old = objects.history.monthly_bill("account", (2026, 8))
        assert old == {"usage_kwh": 10, "cost_cny": 5, "source": "year_month_stats", "updated_at": "first"}

        # A new API response still has the audited right to revise official facts.
        monkeypatch.setattr(history_module, "_utcnow_iso", lambda: "later-refetch")
        rig.client.years["account", 2026] = (6, 12, [
            {"month": "2026-08", "kwh": 12, "charge": 6}
        ])
        await objects.billing._add_year_data(rig.client, CSGElectricityAccount("account"), {})
        assert objects.history.monthly_bill("account", (2026, 8)) == {
            "usage_kwh": 12, "cost_cny": 6, "source": "year_month_stats", "updated_at": "later-refetch",
        }
        assert rig.history_io == {"save": 2, "readback": 2}

    asyncio.run(exercise())


@pytest.mark.parametrize("existing", [False, True], ids=["no-old-fact", "existing-fact"])
def test_monthly_conflict_canonical_aliases_do_not_create_or_modify_fact(rig, existing):
    rig.client.years["account", 2026] = (0, 0, [
        {"month": "202608", "kwh": 10, "charge": 5},
        {"month": "2026-08", "kwh": 20, "charge": 10},
    ])

    async def exercise():
        objects = await rig.build()
        if existing:
            await objects.history.async_upsert_monthly_bill(
                "account", (2026, 8), usage_kwh=4, cost_cny=2
            )
        old = objects.history.monthly_bill("account", (2026, 8))
        before_io = dict(rig.history_io)
        for _ in range(2):
            await objects.billing._add_year_data(rig.client, CSGElectricityAccount("account"), {})
            assert objects.history.monthly_bill("account", (2026, 8)) == old
            assert rig.history_io == before_io

    asyncio.run(exercise())


@pytest.mark.parametrize("order", [False, True], ids=["forward", "reverse"])
def test_monthly_candidates_combine_only_nonconflicting_valid_partial_fields(rig, order):
    rows = [
        {"month": "202608", "kwh": "0", "charge": None},
        {"month": "2026-08", "charge": "5"},
        {"month": "202608", "kwh": float("nan"), "charge": True},
        {"month": "202608", "kwh": -1, "charge": float("inf")},
        {"month": "202608", "kwh": "invalid", "charge": -1},
    ]
    rig.client.years["account", 2026] = (5, 0, rows[::-1] if order else rows)

    async def exercise():
        objects = await rig.build()
        upsert = AsyncMock(wraps=objects.history.async_upsert_monthly_bill)
        objects.history.async_upsert_monthly_bill = upsert
        await objects.billing._add_year_data(rig.client, CSGElectricityAccount("account"), {})
        upsert.assert_awaited_once_with("account", (2026, 8), usage_kwh=0, cost_cny=5)
        assert objects.history.monthly_bill("account", (2026, 8))["usage_kwh"] == 0
        assert objects.history.monthly_bill("account", (2026, 8))["cost_cny"] == 5
        assert rig.history_io == {"save": 1, "readback": 1}

    asyncio.run(exercise())


def test_monthly_candidates_distinct_months_keep_billing_io_and_api_counts(rig):
    """One account's 12+12 bills retain the audited first/repeat I/O counts."""
    rig.entry.data[CONF_ELE_ACCOUNTS].pop("other")
    for year in (2026, 2025):
        rig.client.years["account", year] = (60, 120, [
            {"month": f"{year}{month:02d}", "kwh": 10, "charge": 5}
            for month in range(1, 13)
        ])

    async def exercise():
        objects = await rig.build()
        await objects.billing._async_update_data()
        assert rig.history_io == {"save": 28, "readback": 28}
        assert len(objects.history._data["accounts"]["account"]["monthly_bills"]) == 24
        rig.history_io.update(save=0, readback=0)
        await objects.billing._async_update_data()
        assert rig.history_io == {"save": 2, "readback": 2}
        assert rig.client.calls == [
            ("daily", "account", (2026, 9)), ("daily", "account", (2026, 8)),
            ("year", "account", 2026), ("year", "account", 2025),
        ] * 2

    asyncio.run(exercise())
