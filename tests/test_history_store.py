"""Unit tests for the persistent CSG history fact store."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import custom_components.csg.history_store as history_store_module
from custom_components.csg.history_store import CSGHistoryStore


class MemoryStore:
    """Minimal Home Assistant Store replacement."""

    def __init__(self) -> None:
        self.saved_data: dict | None = None
        self.save_count = 0

    async def async_save(self, data: dict) -> None:
        self.saved_data = data
        self.save_count += 1


def make_store() -> CSGHistoryStore:
    """Create a HistoryStore without a Home Assistant instance."""
    store = CSGHistoryStore.__new__(CSGHistoryStore)
    store._data = {"accounts": {}}
    store._lock = asyncio.Lock()
    store._store = MemoryStore()
    return store


def run(coroutine):
    """Run an async unit without pytest-asyncio."""
    return asyncio.run(coroutine)


def test_daily_usage_insert_and_coverage(monkeypatch) -> None:
    store = make_store()
    monkeypatch.setattr(
        history_store_module,
        "_utcnow_iso",
        lambda: "2026-09-27T00:00:00+00:00",
    )

    result = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-01", "kwh": 12.5}],
        )
    )

    assert result.inserted_dates == ("2026-08-01",)
    assert result.updated_dates == ()
    assert result.unchanged_dates == ()
    assert result.earliest_changed_date == "2026-08-01"
    assert result.coverage_changed
    assert len(result.missing_dates) == 30
    assert store.daily_usage("test-account", "2026-08-01") == {
        "kwh": 12.5,
        "source": "daily_usage_api",
        "updated_at": "2026-09-27T00:00:00+00:00",
    }

    coverage = store.daily_coverage("test-account", (2026, 8))
    assert coverage["state"] == "partial"
    assert coverage["valid_days"] == 1
    assert coverage["first_valid_date"] == "2026-08-01"
    assert coverage["last_valid_date"] == "2026-08-01"


def test_same_value_refetch_is_idempotent_and_keeps_updated_at(
    monkeypatch,
) -> None:
    store = make_store()
    stamps = iter(("first", "second"))
    monkeypatch.setattr(
        history_store_module, "_utcnow_iso", lambda: next(stamps)
    )

    run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-01", "kwh": 5}],
        )
    )
    result = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-01", "kwh": 5.0}],
        )
    )

    assert result.inserted_dates == ()
    assert result.updated_dates == ()
    assert result.unchanged_dates == ("2026-08-01",)
    assert result.earliest_changed_date is None
    assert not result.coverage_changed
    assert (
        store.daily_usage("test-account", "2026-08-01")["updated_at"]
        == "first"
    )
    assert store._store.save_count == 1


def test_daily_usage_revision_overwrites_fact(monkeypatch) -> None:
    store = make_store()
    stamps = iter(("first", "second"))
    monkeypatch.setattr(
        history_store_module, "_utcnow_iso", lambda: next(stamps)
    )

    run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-02", "kwh": 8}],
        )
    )
    result = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-02", "kwh": 7.75}],
        )
    )

    assert result.updated_dates == ("2026-08-02",)
    assert result.earliest_changed_date == "2026-08-02"
    assert store.daily_usage("test-account", "2026-08-02") == {
        "kwh": 7.75,
        "source": "daily_usage_api",
        "updated_at": "second",
    }


def test_nan_does_not_create_or_delete_daily_fact(monkeypatch) -> None:
    store = make_store()
    monkeypatch.setattr(
        history_store_module, "_utcnow_iso", lambda: "stamp"
    )

    result = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-03", "kwh": float("nan")}],
        )
    )
    assert result.inserted_dates == ()
    assert store.daily_usage("test-account", "2026-08-03") is None

    run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-03", "kwh": 3.5}],
        )
    )
    result = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-03", "kwh": float("nan")}],
        )
    )

    assert result.updated_dates == ()
    assert store.daily_usage("test-account", "2026-08-03")["kwh"] == 3.5


def test_zero_is_a_valid_daily_fact(monkeypatch) -> None:
    store = make_store()
    monkeypatch.setattr(
        history_store_module, "_utcnow_iso", lambda: "stamp"
    )

    result = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [{"date": "2026-08-04", "kwh": 0}],
        )
    )

    assert result.inserted_dates == ("2026-08-04",)
    assert store.daily_usage("test-account", "2026-08-04")["kwh"] == 0.0


def test_duplicate_same_value_dedupes_and_conflict_is_skipped(
    monkeypatch, caplog
) -> None:
    store = make_store()
    monkeypatch.setattr(
        history_store_module, "_utcnow_iso", lambda: "stamp"
    )

    same = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [
                {"date": "2026-08-05", "kwh": 2.25},
                {"date": "2026-08-05", "kwh": 2.25},
            ],
        )
    )
    assert same.inserted_dates == ("2026-08-05",)

    caplog.set_level(logging.WARNING)
    conflict = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 8),
            [
                {"date": "2026-08-06", "kwh": 1.0},
                {"date": "2026-08-06", "kwh": 2.0},
            ],
        )
    )
    assert conflict.inserted_dates == ()
    assert store.daily_usage("test-account", "2026-08-06") is None
    assert "Conflicting daily usage values" in caplog.text


def test_coverage_transitions_from_partial_to_complete(monkeypatch) -> None:
    store = make_store()
    monkeypatch.setattr(
        history_store_module, "_utcnow_iso", lambda: "stamp"
    )
    first_27 = [
        {"date": f"2026-02-{day:02d}", "kwh": 1}
        for day in range(1, 28)
    ]

    partial = run(
        store.async_upsert_daily_usage(
            "test-account", (2026, 2), first_27
        )
    )
    assert partial.missing_dates == ("2026-02-28",)
    assert (
        store.daily_coverage("test-account", (2026, 2))["state"]
        == "partial"
    )

    complete = run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 2),
            [{"date": "2026-02-28", "kwh": 1}],
        )
    )
    assert complete.missing_dates == ()
    coverage = store.daily_coverage("test-account", (2026, 2))
    assert coverage["state"] == "complete"
    assert coverage["valid_days"] == 28


def test_monthly_bill_upsert_is_revision_aware(monkeypatch) -> None:
    store = make_store()
    stamps = iter(("first", "second"))
    monkeypatch.setattr(
        history_store_module, "_utcnow_iso", lambda: next(stamps)
    )

    assert run(
        store.async_upsert_monthly_bill(
            "test-account",
            (2026, 2),
            usage_kwh=28,
            cost_cny=16,
        )
    )
    assert not run(
        store.async_upsert_monthly_bill(
            "test-account",
            (2026, 2),
            usage_kwh=28.0,
            cost_cny=16.0,
        )
    )
    assert (
        store.monthly_bill("test-account", (2026, 2))["updated_at"]
        == "first"
    )

    assert run(
        store.async_upsert_monthly_bill(
            "test-account",
            (2026, 2),
            usage_kwh=29,
            cost_cny=None,
        )
    )
    assert store.monthly_bill("test-account", (2026, 2)) == {
        "usage_kwh": 29.0,
        "cost_cny": 16.0,
        "source": "year_month_stats",
        "updated_at": "second",
    }


def test_reconciliation_states(monkeypatch) -> None:
    store = make_store()
    monkeypatch.setattr(
        history_store_module, "_utcnow_iso", lambda: "stamp"
    )

    pending = run(
        store.async_reconcile_month(
            "test-account",
            (2026, 2),
            today=dt.date(2026, 9, 27),
        )
    )
    assert pending["usage_state"] == "pending"
    assert pending["difference_kwh"] is None

    run(
        store.async_upsert_monthly_bill(
            "test-account",
            (2026, 2),
            usage_kwh=28,
            cost_cny=16,
        )
    )
    run(
        store.async_upsert_daily_usage(
            "test-account",
            (2026, 2),
            [{"date": "2026-02-01", "kwh": 1}],
        )
    )
    not_comparable = run(
        store.async_reconcile_month(
            "test-account",
            (2026, 2),
            today=dt.date(2026, 9, 27),
        )
    )
    assert not_comparable["usage_state"] == "not_comparable"
    assert not_comparable["difference_kwh"] is None

    remaining = [
        {"date": f"2026-02-{day:02d}", "kwh": 1}
        for day in range(2, 29)
    ]
    run(
        store.async_upsert_daily_usage(
            "test-account", (2026, 2), remaining
        )
    )
    matched = run(
        store.async_reconcile_month(
            "test-account",
            (2026, 2),
            today=dt.date(2026, 9, 27),
        )
    )
    assert matched["usage_state"] == "matched"
    assert matched["daily_sum_kwh"] == 28.0
    assert matched["billed_usage_kwh"] == 28.0
    assert matched["difference_kwh"] == 0.0

    run(
        store.async_upsert_monthly_bill(
            "test-account",
            (2026, 2),
            usage_kwh=30,
            cost_cny=16,
        )
    )
    mismatch = run(
        store.async_reconcile_month(
            "test-account",
            (2026, 2),
            today=dt.date(2026, 9, 27),
        )
    )
    assert mismatch["usage_state"] == "mismatch"
    assert mismatch["difference_kwh"] == -2.0

    current = run(
        store.async_reconcile_month(
            "test-account",
            (2026, 9),
            today=dt.date(2026, 9, 27),
        )
    )
    assert current["usage_state"] == "pending"
