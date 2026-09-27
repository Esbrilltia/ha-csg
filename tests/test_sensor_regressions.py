"""A1–A6 pre-V2 audit regressions and narrowly scoped known-defect reproducers.

Strict xfails assert the desired behavior. Recorder tests use the contract
adapter in conftest, not a live HA database or a fresh Core API audit.
"""

from copy import deepcopy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.csg.const import (
    SUFFIX_BAL,
    SUFFIX_ENERGY_TOTAL,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from custom_components.csg.csg_client import CSGAPIError
from custom_components.csg.sensor import (
    ENERGY_TOTAL,
    REALTIME_DESCRIPTIONS,
    BillingCoordinator,
    CSGSensor,
    _KEY_YESTERDAY_DATE,
)
from test_sensor import (
    FakeBillingCoordinator,
    FakeRealtimeCoordinator,
    FakeUsageClient,
    freeze_utcnow,
    make_ledger,
    run,
    yesterdays_kwh_description,
)


CST = ZoneInfo("Asia/Shanghai")
DAY = "2026-08-01"
START = dt.datetime(2026, 8, 2, 12, tzinfo=CST)
ENERGY_ID = "sensor.energy_total"


def billing_coordinator(ledger):
    coordinator = BillingCoordinator.__new__(BillingCoordinator)
    coordinator.hass = object()
    coordinator.ledger = ledger
    return coordinator


def revised_ledger(monkeypatch):
    freeze_utcnow(monkeypatch, START)
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", DAY, 10))
    run(ledger.async_record_billing("account", [{"date": DAY, "kwh": 8}]))
    return ledger


def apply_pending(ledger):
    """Use the real correction and acknowledgement path without cloud calls."""
    async def apply():
        _, pending = await ledger.async_record_billing("account", [])
        ack = await billing_coordinator(ledger)._async_correct_statistics("account", pending)
        if ack:
            await ledger.async_acknowledge_corrections("account", ack)
        return ack

    return run(apply())


def test_a1_billing_helpers_are_class_methods():
    for name in ("_add_year_data", "_async_statistic_sum_at", "_async_correct_statistics"):
        method = BillingCoordinator.__dict__[name]
        assert method.__qualname__ == f"BillingCoordinator.{name}"


@pytest.mark.parametrize("month_key", ["202612", "2026-12"])
@pytest.mark.parametrize("current_year_fails", [False, True])
def test_a1_year_billing_recovers_previous_december(
    monkeypatch, month_key, current_year_fails
):
    freeze_utcnow(monkeypatch, dt.datetime(2027, 1, 15, 4, tzinfo=dt.UTC))
    calls = []

    class Client:
        def get_year_month_stats(self, account, year):
            calls.append(year)
            if year == 2027:
                if current_year_fails:
                    raise CSGAPIError("synthetic failure")
                return 0.0, 0.0, []
            return 100.0, 200.0, [{"month": month_key, "charge": 6.5}]

    data = {SUFFIX_LAST_MONTH_COST: STATE_UNAVAILABLE}
    run(BillingCoordinator._add_year_data(
        FakeBillingCoordinator(), Client(), SimpleNamespace(account_number="account"), data
    ))
    assert calls == [2027, 2026]
    assert data[SUFFIX_LAST_MONTH_COST] == 6.5
    assert data[SUFFIX_LAST_YEAR_COST] == 100.0
    assert data[SUFFIX_LAST_YEAR_KWH] == 200.0
    expected = STATE_UNAVAILABLE if current_year_fails else 0.0
    assert data[SUFFIX_THIS_YEAR_COST] == expected
    assert data[SUFFIX_THIS_YEAR_KWH] == expected


def test_a2_downward_realtime_revision_uses_counted_billing_baseline(monkeypatch):
    freeze_utcnow(monkeypatch, START)
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", DAY, 10))
    run(ledger.async_record_realtime("account", DAY, 8))
    _, pending = run(ledger.async_record_billing("account", [{"date": DAY, "kwh": 8}]))
    assert ledger.energy_total("account") == 10
    assert ledger._data["accounts"]["account"]["realtime"][DAY] == 8
    assert ledger._data["accounts"]["account"]["counted_realtime"][DAY] == 10
    assert pending == {DAY: ({"kwh": 10.0}, {"kwh": 8.0})}


@pytest.mark.parametrize("realtime", [8.0, 10.0])
def test_a3_empty_ledger_billing_first_establishes_baseline(
    monkeypatch, recorder_harness, realtime
):
    freeze_utcnow(monkeypatch, START)
    ledger = make_ledger()
    run(ledger.async_record_billing("account", [{"date": DAY, "kwh": 8}]))
    assert ledger.energy_total("account") is None
    run(ledger.async_record_realtime("account", DAY, realtime))
    assert ledger.energy_total("account") == realtime
    _, pending = run(ledger.async_record_billing("account", []))
    assert pending == {DAY: ({"kwh": realtime}, {"kwh": 8.0})}
    recorder_harness.add(ENERGY_ID, "5minute", START, 100)
    assert apply_pending(ledger) == {DAY: {"kwh"}}
    assert recorder_harness.sums(ENERGY_ID, "5minute") == [100 + 8 - realtime]
    assert run(ledger.async_record_billing("account", []))[1] == {}


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="A3: Billing marks a new day reported before an existing ledger counts realtime",
)
@pytest.mark.parametrize("realtime", [8.0, 10.0])
def test_a3_existing_ledger_counts_billing_first_new_day(monkeypatch, realtime):
    freeze_utcnow(monkeypatch, START)
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-07-31", 4))
    run(ledger.async_record_billing("account", [{"date": DAY, "kwh": 8}]))
    run(ledger.async_record_realtime("account", DAY, realtime))
    assert ledger.energy_total("account") == 4 + realtime
    assert ledger._data["accounts"]["account"]["counted_realtime"][DAY] == realtime


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="A3: initial valid zero leaves no counted marker and locks a later upward revision",
)
def test_a3_initial_zero_can_recover_to_positive_usage(monkeypatch):
    freeze_utcnow(monkeypatch, START)
    ledger = make_ledger()
    run(ledger.async_record_billing("account", [{"date": DAY, "kwh": 0}]))
    run(ledger.async_record_realtime("account", DAY, 0))
    run(ledger.async_record_billing("account", [{"date": DAY, "kwh": 0}]))
    run(ledger.async_record_billing("account", [{"date": DAY, "kwh": 8}]))
    run(ledger.async_record_realtime("account", DAY, 8))
    assert ledger.energy_total("account") == 8
    assert ledger._data["accounts"]["account"]["counted_realtime"][DAY] == 8


def test_a3_entity_unavailable_if_final_interpolated_value_is_missing():
    coordinator = SimpleNamespace(
        data={"account": {SUFFIX_ENERGY_TOTAL: 0.0}},
        last_update_success=True, ledger=make_ledger(),
    )
    sensor = CSGSensor(coordinator, "account", ENERGY_TOTAL)
    assert not sensor.available
    assert sensor.native_value is None


@pytest.mark.parametrize("period", ["5minute", "hour"])
def test_a4_counted_at_uses_shanghai_floor_and_hour_fallback(
    monkeypatch, recorder_harness, period
):
    arrival = START.replace(minute=17, second=43)
    freeze_utcnow(monkeypatch, arrival.astimezone(dt.UTC))
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", DAY, 10))
    run(ledger.async_record_billing("account", [{"date": DAY, "kwh": 8}]))
    point = START.replace(minute=15) if period == "5minute" else START
    recorder_harness.add(ENERGY_ID, period, point, 100)
    assert apply_pending(ledger) == {DAY: {"kwh"}}
    assert recorder_harness.adjustments == [
        (ENERGY_ID, START.replace(minute=15), -2.0, "kWh")
    ]
    assert recorder_harness.sums(ENERGY_ID, period) == [98]
    queries = recorder_harness.queries
    assert [query[3] for query in queries] == (
        ["5minute", "5minute"] if period == "5minute" else ["5minute", "hour", "hour"]
    )
    assert queries[-1][0] == point.astimezone(dt.UTC)
    assert run(ledger.async_record_billing("account", []))[1] == {}


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="A4: full delta at ramp start creates negative interval usage instead of correcting the ramp",
)
@pytest.mark.parametrize("period,minutes", [("5minute", 5), ("hour", 60)])
def test_a4_correction_preserves_nonnegative_ramp_intervals(
    monkeypatch, recorder_harness, period, minutes
):
    ledger = revised_ledger(monkeypatch)
    step = dt.timedelta(minutes=minutes)
    # Statistics at each bucket start represent the sum at that bucket's end.
    recorder_harness.add(ENERGY_ID, period, START - step, 100)
    for offset in (0, 1):
        value = ledger.energy_total_at("account", START + step * (offset + 1))
        recorder_harness.add(ENERGY_ID, period, START + step * offset, 100 + value)
    midnight = START + dt.timedelta(hours=12)
    recorder_harness.add(ENERGY_ID, period, midnight - step, 110)
    apply_pending(ledger)
    sums = recorder_harness.sums(ENERGY_ID, period)
    assert sums[-1] == 108
    contributions = [later - earlier for earlier, later in zip(sums, sums[1:])]
    assert all(value >= 0 for value in contributions)
    assert contributions[0] == pytest.approx(8 * minutes / (12 * 60))


def test_a5_uncommitted_adjustment_is_not_acknowledged(monkeypatch, recorder_harness):
    ledger = revised_ledger(monkeypatch)
    recorder_harness.add(ENERGY_ID, "5minute", START, 100)
    recorder_harness.async_block_till_done = AsyncMock()
    assert apply_pending(ledger) == {}
    assert recorder_harness.sums(ENERGY_ID, "5minute") == [100]
    assert len(recorder_harness.pending) == 1
    assert run(ledger.async_record_billing("account", []))[1] == {
        DAY: ({"kwh": 10.0}, {"kwh": 8.0})
    }


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="A5: committed correction is applied again after failed verification or restart before acknowledgement",
)
@pytest.mark.parametrize("interruption", ["after-read-failure", "restart-before-ack"])
def test_a5_committed_correction_is_not_applied_twice(
    monkeypatch, recorder_harness, interruption
):
    ledger = revised_ledger(monkeypatch)
    recorder_harness.add(ENERGY_ID, "5minute", START, 100)
    _, pending = run(ledger.async_record_billing("account", []))
    recorder_harness.fail_read_after_write = interruption == "after-read-failure"
    ack = run(billing_coordinator(ledger)._async_correct_statistics("account", pending))
    assert recorder_harness.sums(ENERGY_ID, "5minute") == [98]
    assert ack == ({} if interruption == "after-read-failure" else {DAY: {"kwh"}})
    if interruption == "restart-before-ack":
        restored = make_ledger()
        restored._store = ledger._store
        run(restored.async_load())
        ledger = restored
    apply_pending(ledger)
    assert recorder_harness.sums(ENERGY_ID, "5minute") == [98]
    assert len(recorder_harness.adjustments) == 1
    assert run(ledger.async_record_billing("account", []))[1] == {}


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="A5: missing exact 5-minute/hour buckets leave correction pending despite later statistics",
)
def test_a5_missing_fixed_points_can_use_later_statistics(monkeypatch, recorder_harness):
    ledger = revised_ledger(monkeypatch)
    later = START + dt.timedelta(hours=1)
    freeze_utcnow(monkeypatch, START + dt.timedelta(hours=3))
    for period in ("5minute", "hour"):
        recorder_harness.add(ENERGY_ID, period, later, 100)
    for _ in range(3):
        apply_pending(ledger)
    assert recorder_harness.sums(ENERGY_ID, "5minute") == [98]
    assert recorder_harness.sums(ENERGY_ID, "hour") == [98]
    assert run(ledger.async_record_billing("account", []))[1] == {}


@pytest.mark.parametrize("today,expected", [(1, 5.0), (3, STATE_UNAVAILABLE)])
def test_a6_last_month_fallback_does_not_substitute_for_yesterday(monkeypatch, today, expected):
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, today, 4, tzinfo=dt.UTC))
    client = FakeUsageClient(5.0, day="2026-07-31")
    coordinator = FakeRealtimeCoordinator(client)
    data = run(coordinator._async_update_data())
    assert client.calls == [(2026, 8), (2026, 7)]
    assert data["account"][SUFFIX_YESTERDAY_KWH] == expected
    assert data["account"][SUFFIX_ENERGY_TOTAL] == 5.0
    assert data["account"][_KEY_YESTERDAY_DATE] == ("2026-07-31" if today == 1 else None)


def test_a6_midnight_guard_invalidates_locally_and_cleans_up_timers(monkeypatch):
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 2, 15, 59, tzinfo=dt.UTC))
    client = FakeUsageClient(5.0, day=DAY)
    source = FakeRealtimeCoordinator(client)
    data = run(source._async_update_data())
    before = deepcopy(data)
    coordinator = SimpleNamespace(data=data, last_update_success=True)
    yesterday = CSGSensor(coordinator, "account", yesterdays_kwh_description())
    balance = CSGSensor(coordinator, "account", next(
        item for item in REALTIME_DESCRIPTIONS if item.suffix == SUFFIX_BAL
    ))
    active = {}
    registrations = []
    writes = []

    def track(hass, callback, interval):
        token = object()
        active[token] = callback
        registrations.append(interval)
        return lambda: active.pop(token)

    # Isolate HA's entity lifecycle; exercise CSG's real timer registration,
    # callback, state calculation and cancellation methods.
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())
    monkeypatch.setattr(CoordinatorEntity, "async_will_remove_from_hass", AsyncMock())
    monkeypatch.setattr("custom_components.csg.sensor.async_track_time_interval", track)
    yesterday.hass = balance.hass = object()
    yesterday.async_write_ha_state = lambda: writes.append(yesterday.native_value)
    assert yesterday.available and yesterday.native_value == 5
    run(yesterday.async_added_to_hass())
    run(balance.async_added_to_hass())
    assert registrations == [dt.timedelta(minutes=1)]
    assert len(active) == 1

    midnight = dt.datetime(2026, 8, 2, 16, tzinfo=dt.UTC)
    freeze_utcnow(monkeypatch, midnight)
    next(iter(active.values()))(midnight)
    assert not yesterday.available
    assert yesterday.native_value is None
    assert balance.available and balance.native_value == 1.0
    assert writes == [None]
    assert data == before
    assert client.calls == [(2026, 8)]

    run(yesterday.async_will_remove_from_hass())
    assert not active
    assert yesterday._unsub_yesterday_guard is None
    run(yesterday.async_added_to_hass())
    assert len(active) == 1
    assert registrations == [dt.timedelta(minutes=1)] * 2
    run(yesterday.async_will_remove_from_hass())
    run(balance.async_will_remove_from_hass())
    assert not active
