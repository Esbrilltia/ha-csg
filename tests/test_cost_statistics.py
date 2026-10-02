"""Official costs have monthly intervals, independent of tariff and daily usage."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from copy import deepcopy
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.exceptions import HomeAssistantError

from custom_components.csg.cost_statistics import build_cost_statistics, cost_statistic_metadata
from custom_components.csg.energy_statistics import _different_suffix, statistic_metadata
from test_history_store import make_store

ACCOUNT = "fictional-cost-account"
TODAY = dt.date(2026, 10, 3)


def bills(values):
    return {month: {"cost_cny": value} for month, value in values.items()}


def pairs(rows):
    return [(row["state"], row["sum"]) for row in rows]


def test_cost_identity_and_metadata_match_core_opower_without_sensitive_labels():
    digest = hashlib.sha256(ACCOUNT.encode()).hexdigest()
    metadata = cost_statistic_metadata(ACCOUNT)
    assert metadata == {
        "source": "csg", "statistic_id": f"csg:cost_{digest}",
        "name": f"CSG cost {digest[:8]}", "unit_of_measurement": None,
        "unit_class": None, "mean_type": StatisticMeanType.NONE, "has_sum": True,
    }
    assert statistic_metadata(ACCOUNT)["statistic_id"] == f"csg:energy_{digest}"
    assert valid_statistic_id(metadata["statistic_id"])
    assert ACCOUNT not in str(metadata)


@pytest.mark.parametrize("values,expected", [
    ({}, []),
    ({"2026-01": 100}, [(100, 100)]),
    ({"2026-01": 100, "2026-03": 150}, [(100, 100), (150, 250)]),
    ({"2026-03": 150, "2026-01": 100, "2026-02": 120}, [(100, 100), (120, 220), (150, 370)]),
    ({"2026-01": 0, "2025-12": 0.1, "2026-02": Decimal("0.2")}, [(0.1, 0.1), (0, 0.1), (0.2, 0.3)]),
])
def test_builder_single_multiple_holes_zero_cross_year_decimal(values, expected):
    source = bills(values)
    before = deepcopy(source)
    rows = build_cost_statistics(source, TODAY)
    assert pairs(rows) == expected
    assert source == before
    assert [row["start"].date().isoformat()[:7] for row in rows] == sorted(values)
    assert all(row["start"].tzinfo == ZoneInfo("Asia/Shanghai") for row in rows)
    assert all(row["start"].day == 15 and row["start"].time() == dt.time(12) for row in rows)


@pytest.mark.parametrize("month", [
    "2026-1", "202601", "2026-00", "2026-13", "2026-01-01", " 2026-01", "2026-01 ",
    "0000-01", "garbage", "", None, 202601, "2026-10", "2026-11", "2027-01",
])
def test_malformed_current_future_months_are_skipped(month):
    assert build_cost_statistics(bills({month: 100}), TODAY) == []


@pytest.mark.parametrize("value", [None, True, False, -1, "-0.01", "", "invalid", float("nan"), float("inf"), float("-inf"), Decimal("NaN")])
def test_missing_negative_nonfinite_and_invalid_costs_produce_no_row(value):
    assert build_cost_statistics(bills({"2026-01": value}), TODAY) == []
    assert build_cost_statistics({"2026-01": {"usage_kwh": 100}}, TODAY) == []


def test_interval_uses_month_anchor_not_fetch_time_and_current_month_is_open():
    rows = build_cost_statistics(bills({"2026-09": 100, "2026-10": 120}), dt.date(2026, 10, 1))
    assert len(rows) == 1
    assert rows[0]["start"].astimezone(dt.UTC) == dt.datetime(2026, 9, 15, 4, tzinfo=dt.UTC)


@pytest.mark.parametrize("revision", [98, 103])
def test_hole_fill_and_revisions_replace_earliest_changed_suffix(revision):
    initial = build_cost_statistics(bills({"2026-01": 100, "2026-03": 150}), TODAY)
    actual = [{**row, "start": row["start"].timestamp()} for row in initial]
    filled = build_cost_statistics(bills({"2026-01": 100, "2026-02": 120, "2026-03": 150}), TODAY)
    assert pairs(_different_suffix(filled, actual)) == [(120, 220), (150, 370)]
    actual = [{**row, "start": row["start"].timestamp()} for row in filled]
    revised = build_cost_statistics(bills({"2026-01": revision, "2026-02": 120, "2026-03": 150}), TODAY)
    assert pairs(_different_suffix(revised, actual)) == [(revision, revision), (120, revision + 120), (150, revision + 270)]


def test_monthly_snapshot_is_locked_detached_noncreating_and_durability_gated():
    async def scenario():
        store = make_store()
        await store.async_upsert_monthly_bill(ACCOUNT, (2026, 1), usage_kwh=200, cost_cny=100)
        before = deepcopy(store._data)
        await store._lock.acquire()
        task = asyncio.create_task(store.async_monthly_bills_snapshot(ACCOUNT))
        await asyncio.sleep(0)
        assert not task.done()
        store._lock.release()
        snapshot = await task
        snapshot["2026-01"]["cost_cny"] = 999
        assert await store.async_monthly_bills_snapshot("fictional-absent") == {}
        assert store._data == before
        store._persistence_pending = True
        with pytest.raises(HomeAssistantError, match="durable"):
            await store.async_monthly_bills_snapshot(ACCOUNT)
        with pytest.raises(HomeAssistantError, match="durable"):
            await store.async_monthly_bills_snapshot("fictional-absent")
    asyncio.run(scenario())
