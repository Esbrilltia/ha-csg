"""Unit tests for the persistent CSG history fact store."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from copy import deepcopy
from itertools import permutations

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import storage as ha_storage
from homeassistant.util import json as json_util
from homeassistant.util.file import WriteError

import custom_components.csg_plus.history_store as history_store_module
from custom_components.csg_plus.history_store import CSGHistoryStore


class MemoryStore:
    """Minimal Home Assistant Store replacement."""

    def __init__(self) -> None:
        self.saved_data: dict | None = None
        self.save_count = 0

    async def async_save(self, data: dict) -> None:
        self.saved_data = deepcopy(data)
        self.save_count += 1

    async def async_load(self) -> dict | None:
        return deepcopy(self.saved_data)


def make_store() -> CSGHistoryStore:
    """Create a HistoryStore without a Home Assistant instance."""
    store = CSGHistoryStore.__new__(CSGHistoryStore)
    store._data = {"accounts": {}}
    store._lock = asyncio.Lock()
    store._store = MemoryStore()
    store._verification_store = store._store
    store._persistence_pending = False
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


@pytest.fixture
def real_storage_io(monkeypatch):
    """Inject errors below real HA Store, leaving its save/load contract intact."""
    # HA's POSIX permission operation is unavailable on Windows. File creation,
    # serialization and replacement still use the real HA implementation.
    if not hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: None, raising=False)

    class StorageIO:
        writes = 0
        reads = 0
        failed_writes = 0
        fail_next_read = False

        def write(self, *args, **kwargs):
            self.writes += 1
            if self.failed_writes:
                self.failed_writes -= 1
                raise WriteError("Synthetic disk write failure")
            return original_write(*args, **kwargs)

        def read(self, *args, **kwargs):
            self.reads += 1
            if self.fail_next_read:
                self.fail_next_read = False
                raise HomeAssistantError("Synthetic verification read failure")
            return original_read(*args, **kwargs)

    # Store._write_prepared_data resolves this physical writer in HA
    # 2026.9.3; keep its executor and HistoryStorageHass queue intact.
    original_write = ha_storage.write_utf8_file
    original_read = json_util.load_json
    io = StorageIO()
    monkeypatch.setattr(ha_storage, "write_utf8_file", io.write)
    monkeypatch.setattr(json_util, "load_json", io.read)
    return io


@pytest.mark.parametrize("domain", ["daily", "monthly"])
@pytest.mark.parametrize("initial", [None, 5], ids=["insert", "revision"])
def test_real_ha_store_recovers_swallowed_write_failure(
    tmp_path, monkeypatch, caplog, real_storage_io, domain, initial
):
    """A1: all four recovery cases use real Store and a fresh HA/Store reader."""
    stamp = ["initial"]
    monkeypatch.setattr(history_store_module, "_utcnow_iso", lambda: stamp[0])

    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        restarted_hass = None
        store = CSGHistoryStore(hass, "test-entry")

        async def upsert(value):
            if domain == "daily":
                return await store.async_upsert_daily_usage(
                    "test-account", (2026, 8), [{"date": "2026-08-01", "kwh": value}]
                )
            return await store.async_upsert_monthly_bill(
                "test-account", (2026, 8), usage_kwh=value, cost_cny=value * 2
            )

        def fact(target):
            if domain == "daily":
                return target.daily_usage("test-account", "2026-08-01")
            return target.monthly_bill("test-account", (2026, 8))

        try:
            await store.async_load()
            if initial is not None:
                await upsert(initial)
            stamp[0] = "failed-write"
            writes_before = real_storage_io.writes
            real_storage_io.failed_writes = 1
            # Store logs/swallow WriteError: this call returns normally.
            changed = await upsert(8)
            assert real_storage_io.writes == writes_before + 1
            assert "Error writing config" in caplog.text
            assert store._persistence_pending
            expected_fact = fact(store)
            assert expected_fact["updated_at"] == "failed-write"
            if domain == "daily":
                assert changed.earliest_changed_date == "2026-08-01"
            else:
                assert changed is True

            before_recovery = CSGHistoryStore(hass, "test-entry")
            await before_recovery.async_load()
            old_fact = fact(before_recovery)
            key = "kwh" if domain == "daily" else "usage_kwh"
            assert (old_fact[key] if old_fact else None) == initial

            stamp[0] = "retry"
            retried = await upsert(8)
            assert real_storage_io.writes == writes_before + 2
            assert not store._persistence_pending
            assert fact(store) == expected_fact
            if domain == "daily":
                assert retried.inserted_dates == retried.updated_dates == ()
                assert retried.unchanged_dates == ("2026-08-01",)
                assert retried.earliest_changed_date is None
            else:
                assert retried is False  # Persistence retry is not a source revision.

            reads, writes = real_storage_io.reads, real_storage_io.writes
            for _ in range(3):
                await upsert(8)
            assert (real_storage_io.reads, real_storage_io.writes) == (reads, writes)

            # A fresh HomeAssistant has no shared preload or pending-write data.
            restarted_hass = HomeAssistant(str(tmp_path))
            restored = CSGHistoryStore(restarted_hass, "test-entry")
            await restored.async_load()
            assert fact(restored) == expected_fact
            assert fact(restored)[key] == 8
        finally:
            if restarted_hass is not None:
                await restarted_hass.async_stop(force=True)
            await hass.async_stop(force=True)

    run(scenario())


def test_real_ha_store_retries_until_disk_recovers(tmp_path, real_storage_io):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        try:
            store = CSGHistoryStore(hass, "test-entry")
            await store.async_load()
            real_storage_io.failed_writes = 2
            row = [{"date": "2026-08-01", "kwh": 5}]
            await store.async_upsert_daily_usage("test-account", (2026, 8), row)
            fact = store.daily_usage("test-account", "2026-08-01")
            for attempt in range(2):
                result = await store.async_upsert_daily_usage("test-account", (2026, 8), row)
                assert result.earliest_changed_date is None
                assert store._persistence_pending is (attempt == 0)
            assert real_storage_io.writes == 3
            restored = CSGHistoryStore(hass, "test-entry")
            await restored.async_load()
            assert restored.daily_usage("test-account", "2026-08-01") == fact
        finally:
            await hass.async_stop(force=True)

    run(scenario())


def test_real_ha_store_read_failure_keeps_persistence_pending(tmp_path, real_storage_io):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        try:
            store = CSGHistoryStore(hass, "test-entry")
            await store.async_load()
            real_storage_io.fail_next_read = True
            row = [{"date": "2026-08-01", "kwh": 5}]
            await store.async_upsert_daily_usage("test-account", (2026, 8), row)
            assert store._persistence_pending
            fact = store.daily_usage("test-account", "2026-08-01")
            result = await store.async_upsert_daily_usage("test-account", (2026, 8), row)
            assert result.earliest_changed_date is None
            assert not store._persistence_pending
            assert real_storage_io.writes == 2
            restored = CSGHistoryStore(hass, "test-entry")
            await restored.async_load()
            assert restored.daily_usage("test-account", "2026-08-01") == fact
        finally:
            await hass.async_stop(force=True)

    run(scenario())


def test_real_ha_store_pending_memory_is_not_disk_confirmation(tmp_path, real_storage_io):
    """The public writer load can return unsaved data; verification must not."""
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        try:
            store = CSGHistoryStore(hass, "test-entry")
            await store.async_load()
            hass.set_state(CoreState.stopping)
            row = [{"date": "2026-08-01", "kwh": 5}]
            await store.async_upsert_daily_usage("test-account", (2026, 8), row)
            assert await store._store.async_load() == store._data
            assert real_storage_io.writes == 0
            assert store._persistence_pending
            hass.set_state(CoreState.running)
            await store.async_upsert_daily_usage("test-account", (2026, 8), row)
            assert real_storage_io.writes == 1
            assert not store._persistence_pending
            restored = CSGHistoryStore(hass, "test-entry")
            await restored.async_load()
            assert restored.daily_usage("test-account", "2026-08-01")["kwh"] == 5
        finally:
            await hass.async_stop(force=True)

    run(scenario())


@pytest.mark.parametrize("values", list(permutations((5.0, 5.00000000075, 5.0000000015))))
def test_duplicate_tolerance_chain_is_always_conflicting(monkeypatch, caplog, values):
    """A2: all six orders retain the old fact and identical change metadata."""
    monkeypatch.setattr(history_store_module, "_utcnow_iso", lambda: "stamp")
    store = make_store()
    run(store.async_upsert_daily_usage(
        "test-account", (2026, 8), [{"date": "2026-08-01", "kwh": 4}]
    ))
    before = deepcopy(store._data)
    previous = run(store.async_upsert_daily_usage("test-account", (2026, 8), []))
    result = run(store.async_upsert_daily_usage(
        "test-account", (2026, 8),
        [{"date": "2026-08-01", "kwh": value} for value in values],
    ))
    assert result == previous
    assert result.inserted_dates == result.updated_dates == result.unchanged_dates == ()
    assert result.earliest_changed_date is None
    assert store._data == before
    assert store._store.save_count == 1
    assert "Conflicting daily usage values" in caplog.text


@pytest.mark.parametrize("values", list(permutations((5.0, 5.00000000025, 5.00000000075))))
def test_duplicate_tolerance_set_uses_stable_source_representative(monkeypatch, values):
    monkeypatch.setattr(history_store_module, "_utcnow_iso", lambda: "stamp")
    store = make_store()
    result = run(store.async_upsert_daily_usage(
        "test-account", (2026, 8),
        [{"date": "2026-08-01", "kwh": value} for value in values],
    ))
    assert store.daily_usage("test-account", "2026-08-01") == {
        "kwh": 5.0, "source": "daily_usage_api", "updated_at": "stamp"
    }
    assert result.inserted_dates == ("2026-08-01",)
    assert result.updated_dates == result.unchanged_dates == ()
    assert result.earliest_changed_date == "2026-08-01"


@pytest.mark.parametrize("values,conflict", [([5, 5], False), ([5, 7], True), ([7, 5], True)])
def test_exact_and_conflicting_duplicates_preserve_existing_fact(values, conflict):
    store = make_store()
    run(store.async_upsert_daily_usage(
        "test-account", (2026, 8), [{"date": "2026-08-01", "kwh": 4}]
    ))
    result = run(store.async_upsert_daily_usage(
        "test-account", (2026, 8),
        [{"date": "2026-08-01", "kwh": value} for value in values],
    ))
    assert store.daily_usage("test-account", "2026-08-01")["kwh"] == (4 if conflict else 5)
    assert result.inserted_dates == ()
    assert result.updated_dates == (() if conflict else ("2026-08-01",))


@pytest.mark.parametrize("billed,expected_difference,expected_state", [
    (27.991, 0.009, "matched"),
    (28.009, -0.009, "matched"),
    (27.99, 0.01, "matched"),
    (28.01, -0.01, "matched"),
    (27.9899999999, 0.0100000001, "mismatch"),
    (28.0100000001, -0.0100000001, "mismatch"),
    (27.98, 0.02, "mismatch"),
    (28.02, -0.02, "mismatch"),
])
def test_reconciliation_decimal_boundary(billed, expected_difference, expected_state):
    """B1: inclusive 0.01 without a widened epsilon or rewritten facts."""
    store = make_store()
    run(store.async_upsert_daily_usage(
        "test-account", (2026, 2),
        [{"date": f"2026-02-{day:02d}", "kwh": 1} for day in range(1, 29)],
    ))
    run(store.async_upsert_monthly_bill(
        "test-account", (2026, 2), usage_kwh=billed, cost_cny=16
    ))
    before = deepcopy(store._data["accounts"]["test-account"])
    result = run(store.async_reconcile_month("test-account", (2026, 2), today=dt.date(2026, 9, 27)))
    assert result["usage_state"] == expected_state
    assert result["difference_kwh"] == pytest.approx(expected_difference, abs=1e-14, rel=0)
    assert result["daily_sum_kwh"] == 28.0
    assert result["billed_usage_kwh"] == billed
    after = store._data["accounts"]["test-account"]
    assert after["daily_usage"] == before["daily_usage"]
    assert after["monthly_bills"] == before["monthly_bills"]


def test_reconciliation_sums_decimal_daily_values_before_boundary_comparison():
    store = make_store()
    run(store.async_upsert_daily_usage(
        "test-account", (2026, 2),
        [{"date": f"2026-02-{day:02d}", "kwh": 0.1} for day in range(1, 29)],
    ))
    run(store.async_upsert_monthly_bill(
        "test-account", (2026, 2), usage_kwh=2.79, cost_cny=None
    ))
    result = run(store.async_reconcile_month("test-account", (2026, 2), today=dt.date(2026, 9, 27)))
    assert result["usage_state"] == "matched"


@pytest.mark.parametrize("row", [
    {"date": "2026-08-01", "kwh": None},
    {"date": "2026-08-01", "kwh": float("nan")},
    {"date": "2026-08-01", "kwh": float("inf")},
    {"date": "2026-08-01", "kwh": float("-inf")},
    {"date": "2026-08-01", "kwh": -1},
    {"date": "2026-08-01"},
    {"date": "2026-07-31", "kwh": 5},
])
def test_invalid_and_missing_daily_refetch_preserves_facts(row):
    store = make_store()
    empty = run(store.async_upsert_daily_usage("test-account", (2026, 8), [row]))
    assert empty.inserted_dates == empty.updated_dates == ()
    assert store._data["accounts"]["test-account"]["daily_usage"] == {}
    run(store.async_upsert_daily_usage(
        "test-account", (2026, 8), [{"date": "2026-08-01", "kwh": 5}]
    ))
    before = deepcopy(store._data)
    for rows in ([row], []):
        result = run(store.async_upsert_daily_usage("test-account", (2026, 8), rows))
        assert result.inserted_dates == result.updated_dates == ()
        assert result.earliest_changed_date is None
        assert store._data == before


def test_monthly_and_daily_facts_stay_independent():
    store = make_store()
    run(store.async_upsert_monthly_bill(
        "test-account", (2026, 8), usage_kwh=100, cost_cny=60
    ))
    assert store._data["accounts"]["test-account"]["daily_usage"] == {}
    bill = store.monthly_bill("test-account", (2026, 8))
    run(store.async_upsert_daily_usage(
        "test-account", (2026, 8), [{"date": "2026-08-01", "kwh": 0}]
    ))
    assert store.monthly_bill("test-account", (2026, 8)) == bill
    assert store.daily_usage("test-account", "2026-08-01")["kwh"] == 0
    assert not run(store.async_upsert_monthly_bill(
        "test-account", (2026, 8), usage_kwh=None, cost_cny=None
    ))
    assert store.monthly_bill("test-account", (2026, 8)) == bill
    assert store.daily_usage("test-account", "2026-08-02") is None


def test_earliest_changed_date_ignores_unchanged_and_invalid_rows():
    store = make_store()
    run(store.async_upsert_daily_usage("test-account", (2026, 8), [
        {"date": "2026-08-01", "kwh": 1},
        {"date": "2026-08-03", "kwh": 3},
    ]))
    result = run(store.async_upsert_daily_usage("test-account", (2026, 8), [
        {"date": "2026-08-01", "kwh": 1},
        {"date": "2026-08-02", "kwh": float("nan")},
        {"date": "2026-08-03", "kwh": 4},
        {"date": "2026-08-04", "kwh": 5},
    ]))
    assert result.unchanged_dates == ("2026-08-01",)
    assert result.updated_dates == ("2026-08-03",)
    assert result.inserted_dates == ("2026-08-04",)
    assert result.earliest_changed_date == "2026-08-03"
