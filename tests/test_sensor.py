"""Unit tests for CSG sensor data handling."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE, UnitOfEnergy

from custom_components.csg.const import (
    ATTR_KEY_SETTLEMENT_DATE,
    SUFFIX_ENERGY_TOTAL,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_SETTLED_COST_TOTAL,
    SUFFIX_YESTERDAY_KWH,
)
from custom_components.csg.csg_client import CSGAPIError
from custom_components.csg.sensor import (
    BILLING_DESCRIPTIONS,
    CURRENT_DESCRIPTIONS,
    ENERGY_TOTAL,
    REALTIME_DESCRIPTIONS,
    SETTLED_COST_TOTAL,
    BillingCoordinator,
    CSGSensor,
    EnergyLedger,
    RealtimeCoordinator,
    _KEY_YESTERDAY_DATE,
    _csg_today,
    _ladder_data,
    _merge_daily_days,
    _set_latest_day,
)


class MemoryStore:
    """Minimal Store replacement for unit-testing the ledger."""

    def __init__(self) -> None:
        self.saved_data: dict | None = None

    async def async_save(self, data: dict) -> None:
        self.saved_data = deepcopy(data)

    async def async_load(self) -> dict | None:
        return deepcopy(self.saved_data)


def make_ledger() -> EnergyLedger:
    """Create an EnergyLedger without a Home Assistant instance."""
    ledger = EnergyLedger.__new__(EnergyLedger)
    ledger._data = {"accounts": {}}
    ledger._lock = asyncio.Lock()
    ledger._store = MemoryStore()
    return ledger


def run(coroutine):
    """Run an async unit under pytest without pytest-asyncio."""
    return asyncio.run(coroutine)


@pytest.fixture(autouse=True)
def stable_default_clock(monkeypatch):
    """Keep ramp fixtures independent of the calendar day CI happens to run."""
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    )


def test_energy_sensor_descriptions_have_correct_statistics_semantics() -> None:
    """Only Energy dashboard counters are total-increasing sensors."""
    assert ENERGY_TOTAL.device_class is SensorDeviceClass.ENERGY
    assert ENERGY_TOTAL.unit is UnitOfEnergy.KILO_WATT_HOUR
    assert ENERGY_TOTAL.state_class is SensorStateClass.TOTAL_INCREASING
    assert SETTLED_COST_TOTAL.device_class is SensorDeviceClass.MONETARY
    assert SETTLED_COST_TOTAL.unit == "CNY"
    assert SETTLED_COST_TOTAL.state_class is SensorStateClass.TOTAL_INCREASING

    snapshots = (*REALTIME_DESCRIPTIONS, *CURRENT_DESCRIPTIONS, *BILLING_DESCRIPTIONS)
    assert all(
        description.state_class is not SensorStateClass.TOTAL_INCREASING
        for description in snapshots
    )


def test_ledger_records_realtime_usage_once_per_day() -> None:
    """Realtime values only increase a day's contribution."""
    ledger = make_ledger()

    assert run(ledger.async_record_realtime("account", "2026-08-01", 10)) == 10
    assert run(ledger.async_record_realtime("account", "2026-08-01", 10)) == 10
    assert run(ledger.async_record_realtime("account", "2026-08-01", 12)) == 12
    assert run(ledger.async_record_realtime("account", "2026-08-01", 11)) == 12
    assert run(ledger.async_record_realtime("account", "2026-08-02", 4)) == 16


def test_ledger_initializes_zero_totals() -> None:
    """Valid zero readings must not leave the running totals undefined."""
    ledger = make_ledger()

    assert run(ledger.async_record_realtime("account", "2026-08-01", 0)) == 0
    assert run(
        ledger.async_record_billing("account", [{"date": "2026-08-01", "kwh": 0}])
    )[0] == 0


def test_energy_total_advances_linearly_through_the_day() -> None:
    """The displayed cumulative value does not jump when the API is polled."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 24))

    at_midnight = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    at_noon = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert at_midnight == 0
    assert at_noon == 12


def test_energy_total_does_not_interpolate_billing_only_day() -> None:
    """A billing import without realtime contribution cannot lower the meter."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-07-31", 10))
    run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-01", "kwh": 24, "charge": 5}]
        )
    )
    # This is strictly billing-only. Billing followed by realtime on an
    # existing ledger is the separate A3 strict-xfail regression.
    assert ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    ) == 10


def test_energy_total_uses_safe_legacy_realtime_marker() -> None:
    """Pre-upgrade ledgers smooth unbilled days without trusting billing rows."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 24))
    del ledger._data["accounts"]["account"]["counted_realtime"]

    assert ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    ) == 12


def freeze_utcnow(monkeypatch, moment: dt.datetime) -> None:
    """Pin the ledger's clock so a recorded reading has a known arrival time."""
    monkeypatch.setattr("custom_components.csg.sensor.dt_util.utcnow", lambda: moment)


def test_energy_ramp_starts_when_the_reading_arrives(monkeypatch) -> None:
    """The exposed total does not jump at the moment a reading arrives.

    The ramp used to start at midnight, so a day total published at 11:00 paid
    out eleven hours of usage in the single update that delivered it: the flat
    line, wall, slow rise shape on the energy dashboard.
    """
    ledger = make_ledger()
    # 2026-08-02 11:00 in China Standard Time.
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 2, 3, tzinfo=dt.UTC))
    run(ledger.async_record_realtime("account", "2026-08-01", 24))

    cst = ZoneInfo("Asia/Shanghai")
    before_arrival = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 10, 59, tzinfo=cst)
    )
    at_arrival = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 11, tzinfo=cst)
    )
    at_eighteen = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 18, tzinfo=cst)
    )
    at_next_midnight = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 3, tzinfo=cst)
    )

    # Nothing is paid out for the eleven hours elapsed before the reading.
    assert at_arrival == before_arrival == 0
    # 18:00 is seven of the thirteen hours between the arrival and 24:00.
    assert at_eighteen == pytest.approx(before_arrival + 24 * 7 / 13)
    # The ledger's cumulative total is still reached exactly by 24:00.
    assert at_next_midnight == 24


def test_energy_ramp_keeps_its_anchor_when_a_reading_is_revised(monkeypatch) -> None:
    """A revision must not move the ramp start and step the total backwards.

    Moving the anchor to the revision time would restart the ramp from a
    shorter window with a larger day total, so the exposed value could drop.
    Home Assistant books a drop on a total increasing sensor as a meter reset
    and records the whole new value as consumption.
    """
    ledger = make_ledger()
    cst = ZoneInfo("Asia/Shanghai")
    # 2026-08-02 06:00 in China Standard Time.
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 1, 22, tzinfo=dt.UTC))
    run(ledger.async_record_realtime("account", "2026-08-01", 20))
    anchor = ledger._data["accounts"]["account"]["counted_at"]["2026-08-01"]
    assert anchor == "2026-08-01T22:00:00+00:00"

    # A revision is booked as a meter reset if the exposed value steps down,
    # so the reading at the revision moment must rise or hold, never fall.
    before_revision = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 15, tzinfo=cst)
    )

    # 2026-08-02 15:00 in China Standard Time.
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 2, 7, tzinfo=dt.UTC))
    run(ledger.async_record_realtime("account", "2026-08-01", 24))

    assert ledger._data["accounts"]["account"]["counted_at"]["2026-08-01"] == anchor
    # The ramp still runs from 06:00 to 24:00, so 15:00 is its midpoint.
    at_fourteen_fiftyfive = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 14, 55, tzinfo=cst)
    )
    at_fifteen = ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 15, tzinfo=cst)
    )
    assert at_fifteen == pytest.approx(12)
    assert at_fifteen >= at_fourteen_fiftyfive
    # 10 kWh at the old reading, 12 kWh at the revision: up, never down.
    assert before_revision == pytest.approx(10)
    assert at_fifteen >= before_revision
    # The ledger's cumulative total is still reached exactly by 24:00.
    assert (
        ledger.energy_total_at("account", dt.datetime(2026, 8, 3, tzinfo=cst)) == 24
    )


def test_energy_ramp_samples_a_day_without_a_wall(monkeypatch) -> None:
    """A five minute sample of the ramp day never steps by a day's share.

    The day before is already counted, so the exposed sensor holds the ledger
    total until yesterday's reading arrives at 11:00. The old midnight anchor
    paid out the eleven hours that had already elapsed the moment it arrived -
    an 11 kWh step - and then crawled. Anchoring the ramp at the arrival
    spreads the same 24 kWh over the thirteen hours that are left, so a five
    minute step is about 0.15 kWh.
    """
    ledger = make_ledger()
    cst = ZoneInfo("Asia/Shanghai")
    # 2026-07-31's reading arrived on 2026-08-01 at 11:00 CST.
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 1, 3, tzinfo=dt.UTC))
    run(ledger.async_record_realtime("account", "2026-07-31", 24))

    step = dt.timedelta(minutes=5)
    start = dt.datetime(2026, 8, 2, tzinfo=cst)
    arrival = dt.datetime(2026, 8, 2, 11, tzinfo=cst)
    arrival_index = int((arrival - start) / step)
    samples: list[float] = []
    arrived = False
    for index in range(int(dt.timedelta(days=1) / step)):
        moment = start + step * index
        if not arrived and moment >= arrival:
            # 2026-08-02 11:00 in China Standard Time.
            freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 2, 3, tzinfo=dt.UTC))
            run(ledger.async_record_realtime("account", "2026-08-01", 24))
            arrived = True
        samples.append(ledger.energy_total_at("account", moment))

    steps = [later - earlier for earlier, later in zip(samples, samples[1:])]

    # The reading pays out nothing for the hours elapsed before it arrived.
    assert samples[arrival_index] == samples[arrival_index - 1] == 24
    assert all(value >= 0 for value in steps)
    assert max(steps) < 24 / 24
    # The ramp never overshoots the ledger's cumulative total.
    assert samples[-1] <= 48
    assert (
        ledger.energy_total_at("account", dt.datetime(2026, 8, 3, tzinfo=cst)) == 48
    )


def test_energy_ramp_ignores_an_anchor_outside_the_ramp_day(monkeypatch) -> None:
    """An arrival timestamp from another day cannot shorten the ramp."""
    ledger = make_ledger()
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 1, 2, tzinfo=dt.UTC))
    run(ledger.async_record_realtime("account", "2026-08-01", 24))

    assert ledger.energy_total_at(
        "account", dt.datetime(2026, 8, 2, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    ) == 12


def test_ledger_billing_correction_and_settlement_lock() -> None:
    """Billing changes are reported for Recorder and never double-count usage."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 10))

    cost_total, changes = run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-01", "kwh": 12, "charge": 6}]
        )
    )
    assert cost_total == 6
    assert changes == {"2026-08-01": ({"kwh": 10.0}, {"kwh": 12.0, "charge": 6.0})}
    run(ledger.async_acknowledge_corrections("account", {"2026-08-01": {"kwh"}}))
    assert run(ledger.async_record_realtime("account", "2026-08-01", 13)) == 10

    cost_total, changes = run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-01", "kwh": 12, "charge": 7}]
        )
    )
    assert cost_total == 6
    assert changes == {
        "2026-08-01": (
            {"kwh": 12.0, "charge": 6.0},
            {"kwh": 12.0, "charge": 7.0},
        )
    }


def test_ledger_imports_billing_usage_missed_after_installation() -> None:
    """Settled usage fills a post-installation realtime polling gap."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 1))

    _, changes = run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-02", "kwh": 4, "charge": 2}]
        )
    )

    assert ledger.energy_total("account") == 1
    assert changes["2026-08-02"] == ({"kwh": 0.0}, {"kwh": 4.0, "charge": 2.0})


def test_ledger_retries_pending_corrections_until_acknowledged() -> None:
    """Corrections survive a failed Recorder update and are retried later."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 1))
    _, corrections = run(
        ledger.async_record_billing("account", [{"date": "2026-08-01", "kwh": 2}])
    )

    _, retry = run(ledger.async_record_billing("account", []))
    assert retry == corrections
    run(ledger.async_acknowledge_corrections("account", {"2026-08-01": {"kwh"}}))
    assert run(ledger.async_record_billing("account", []))[1] == {}


def test_ledger_keeps_unacknowledged_statistics_corrections() -> None:
    """A failed cost correction must not retry an acknowledged usage adjustment."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 1))
    _, corrections = run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-01", "kwh": 2, "charge": 1}]
        )
    )
    run(ledger.async_acknowledge_corrections("account", {"2026-08-01": {"kwh"}}))
    _, corrections = run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-01", "kwh": 3, "charge": 2}]
        )
    )

    run(ledger.async_acknowledge_corrections("account", {"2026-08-01": {"kwh"}}))
    _, retry = run(ledger.async_record_billing("account", []))

    assert corrections["2026-08-01"] == (
        {"kwh": 2.0, "charge": 1.0},
        {"kwh": 3.0, "charge": 2.0},
    )
    assert retry["2026-08-01"] == (
        {"kwh": 3.0, "charge": 1.0},
        {"kwh": 3.0, "charge": 2.0},
    )


def test_sensor_uses_initial_coordinator_data_and_clears_missing_values() -> None:
    """Sensors expose the first refresh and never retain a failed value."""
    coordinator = SimpleNamespace(
        data={"account": {SUFFIX_ENERGY_TOTAL: 3.5}}, last_update_success=True
    )
    sensor = CSGSensor(coordinator, "account", ENERGY_TOTAL)

    assert sensor.native_value == 3.5
    assert sensor.available
    coordinator.data = {"account": {SUFFIX_ENERGY_TOTAL: STATE_UNAVAILABLE}}
    sensor._update_from_coordinator()
    assert sensor.native_value is None
    assert not sensor.available


def test_energy_sensor_interpolation_tick_writes_a_new_state(monkeypatch) -> None:
    """The energy entity refreshes its estimated value between cloud polls."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 24))
    now = dt.datetime(2026, 8, 2, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("custom_components.csg.sensor.dt_util.utcnow", lambda: now)
    coordinator = SimpleNamespace(
        data={"account": {SUFFIX_ENERGY_TOTAL: 24}},
        last_update_success=True,
        ledger=ledger,
    )
    sensor = CSGSensor(coordinator, "account", ENERGY_TOTAL)
    writes: list[bool] = []
    sensor.async_write_ha_state = lambda: writes.append(True)

    sensor._handle_interpolation_tick(now)

    assert sensor.native_value == 12
    assert writes == [True]


def test_csg_today_uses_china_standard_time(monkeypatch) -> None:
    """CSG API dates must not depend on Home Assistant's configured timezone."""
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 3, 16, tzinfo=dt.UTC),
    )

    assert _csg_today() == dt.date(2026, 8, 4)


def test_ledger_keeps_accounts_and_cost_days_independent() -> None:
    """Separate payment accounts must not share a ledger or cost baseline."""
    ledger = make_ledger()

    assert run(ledger.async_record_realtime("first", "2026-08-01", 1)) == 1
    assert run(ledger.async_record_realtime("second", "2026-08-01", 2)) == 2
    assert run(
        ledger.async_record_billing(
            "first", [{"date": "2026-08-01", "kwh": 1, "charge": 1.5}]
        )
    )[0] == 1.5
    assert run(
        ledger.async_record_billing(
            "second", [{"date": "2026-08-01", "kwh": 2, "charge": 3.0}]
        )
    )[0] == 3.0


def test_merge_daily_days_prefers_usage_endpoint_for_kwh() -> None:
    """Usage and charge endpoints have separate authoritative fields."""
    merged = _merge_daily_days(
        [
            {"date": "2026-08-01", "kwh": 1.2},
            {"date": "2026-08-02", "kwh": 2.3},
        ],
        [
            {"date": "2026-08-01", "kwh": 1.1, "charge": 0.6},
            {"date": "2026-08-03", "kwh": 3.4, "charge": 1.2},
        ],
    )

    assert merged == [
        {"date": "2026-08-01", "kwh": 1.2, "charge": 0.6},
        {"date": "2026-08-02", "kwh": 2.3},
        {"date": "2026-08-03", "kwh": 3.4, "charge": 1.2},
    ]


def test_set_latest_day_marks_missing_data_unavailable() -> None:
    """Latest settlement sensors are unavailable when no daily bill exists."""
    data: dict = {}
    _set_latest_day(data, [])
    assert data == {
        SUFFIX_LATEST_DAY_KWH: STATE_UNAVAILABLE,
        SUFFIX_LATEST_DAY_COST: STATE_UNAVAILABLE,
    }

    _set_latest_day(data, [{"date": "2026-08-03", "kwh": 4.5, "charge": 2.0}])
    assert data[SUFFIX_LATEST_DAY_KWH] == 4.5
    assert data[SUFFIX_LATEST_DAY_COST] == 2.0
    assert data[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-08-03"}


def test_ladder_data_handles_missing_values() -> None:
    """Null ladder fields are exposed as unavailable rather than invalid values."""
    data = _ladder_data({})
    assert all(value == STATE_UNAVAILABLE for key, value in data.items() if key != "current_ladder_start_date")


class FakeLedger:
    """Capture billing rows passed from the billing coordinator."""

    def __init__(self) -> None:
        self.days: list[dict] = []
        self.acknowledgements: dict = {}

    async def async_record_billing(self, account: str, days: list[dict]):
        self.days = days
        return 0.0, {}

    async def async_acknowledge_corrections(self, account, acknowledgements):
        self.acknowledgements = acknowledgements


class FakeBillingCoordinator:
    """Small collaborator for testing BillingCoordinator._update_account."""

    _fetch = staticmethod(lambda function, *args: _call(function, *args))

    def __init__(self) -> None:
        self.ledger = FakeLedger()
        self.history_store = AsyncMock()
        self.energy_statistics_bridge = None
        self.corrected: dict | None = None
        self.verified: dict = {}

    async def _async_correct_statistics(self, account: str, changed: dict) -> dict:
        self.corrected = changed
        return self.verified

    async def _add_year_data(self, client, account, data: dict) -> None:
        return None

    def _notify_failure(self, account: str, kind: str, err: Exception) -> None:
        return None

    def _clear_failure(self, account: str, kind: str) -> None:
        return None


async def _call(function, *args):
    return function(*args)


class FakeClient:
    """Provide deterministic daily responses without network I/O."""

    def get_month_daily_usage_detail(self, account, year_month):
        if year_month == (2026, 8):
            return 0.0, []
        return 4.5, [{"date": "2026-07-31", "kwh": 4.5}]


def test_billing_coordinator_falls_back_to_last_month_settlement(monkeypatch) -> None:
    """The latest settlement day uses last month when current month is empty."""
    coordinator = FakeBillingCoordinator()
    account = SimpleNamespace(account_number="account")
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC))

    data = run(
        BillingCoordinator._update_account(
            coordinator, FakeClient(), account, [(2026, 8), (2026, 7)]
        )
    )

    assert data[SUFFIX_LATEST_DAY_KWH] == 4.5
    assert data[SUFFIX_LATEST_DAY_COST] == STATE_UNAVAILABLE
    assert data[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-07-31"}
    assert data[SUFFIX_SETTLED_COST_TOTAL] == STATE_UNAVAILABLE
    assert coordinator.ledger.days == [{"date": "2026-07-31", "kwh": 4.5}]
    assert coordinator.corrected == {}


def test_billing_coordinator_applies_pending_corrections_with_daily_rows(monkeypatch) -> None:
    """Normal bill refreshes submit corrections instead of dropping them."""
    class CorrectingLedger(FakeLedger):
        async def async_record_billing(self, account: str, days: list[dict]):
            self.days = days
            return 5.0, {"2026-07-31": ({"kwh": 1.0}, {"kwh": 2.0})}

    coordinator = FakeBillingCoordinator()
    coordinator.ledger = CorrectingLedger()
    coordinator.verified = {"2026-07-31": {"kwh"}}
    account = SimpleNamespace(account_number="account")
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC))

    run(
        BillingCoordinator._update_account(
            coordinator, FakeClient(), account, [(2026, 8), (2026, 7)]
        )
    )

    assert coordinator.corrected == {
        "2026-07-31": ({"kwh": 1.0}, {"kwh": 2.0})
    }
    assert coordinator.ledger.acknowledgements == coordinator.verified


def test_billing_coordinator_marks_failed_month_unavailable(monkeypatch) -> None:
    """A failed month request leaves its snapshots explicitly unavailable."""
    class FailingClient:
        def get_month_daily_usage_detail(self, account, year_month):
            raise CSGAPIError("failure")

    coordinator = FakeBillingCoordinator()
    account = SimpleNamespace(account_number="account")
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC))

    data = run(
        BillingCoordinator._update_account(
            coordinator, FailingClient(), account, [(2026, 8), (2026, 7)]
        )
    )

    assert data[SUFFIX_LATEST_DAY_KWH] == STATE_UNAVAILABLE
    assert data[SUFFIX_LATEST_DAY_COST] == STATE_UNAVAILABLE
    assert coordinator.ledger.days == []


def test_billing_correction_adjusts_existing_energy_and_cost_statistics(
    recorder_harness,
) -> None:
    """Recorder adjustments use the registered entity IDs and daily delta."""
    start = dt.datetime(2026, 8, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    recorder_harness.add("sensor.energy_total", "5minute", start, 100)
    recorder_harness.add("sensor.settled_cost_total", "5minute", start, 40)
    coordinator = BillingCoordinator.__new__(BillingCoordinator)
    coordinator.hass = object()
    coordinator.ledger = make_ledger()

    acknowledgements = run(
        BillingCoordinator._async_correct_statistics(
            coordinator,
            "account",
            {
                "2026-08-01": (
                    {"kwh": 10.0, "charge": 4.0},
                    {"kwh": 12.0, "charge": 5.5},
                ),
                "2026-08-02": ({}, {"kwh": 3.0, "charge": 1.0}),
            },
        )
    )

    assert [(statistic_id, adjustment, unit) for statistic_id, _, adjustment, unit in recorder_harness.adjustments] == [
        ("sensor.energy_total", 2.0, "kWh"),
        ("sensor.settled_cost_total", 1.5, "CNY"),
    ]
    assert all(point == start for _, point, _, _ in recorder_harness.adjustments)
    assert recorder_harness.sums("sensor.energy_total", "5minute") == [102]
    assert recorder_harness.sums("sensor.settled_cost_total", "5minute") == [41.5]
    assert acknowledgements == {"2026-08-01": {"kwh", "charge"}}


class FakeUsageClient:
    """Serve dated monthly usage rows through the current API only."""

    def __init__(self, usage: float | None | Exception, day="2026-08-01") -> None:
        self.usage = usage
        self.day = day
        self.calls = []

    def get_balance_and_arrears(self, account):
        return 1.0, 0.0

    def get_month_daily_usage_detail(self, account, year_month):
        self.calls.append(year_month)
        if isinstance(self.usage, Exception):
            raise self.usage
        if self.usage is None or self.day[:7] != f"{year_month[0]}-{year_month[1]:02d}":
            return 0.0, []
        return self.usage, [{"date": self.day, "kwh": self.usage}]


def make_realtime_coordinator(ledger: EnergyLedger, client: FakeUsageClient):
    """Drive RealtimeCoordinator._async_update_data without Home Assistant."""
    coordinator = RealtimeCoordinator.__new__(RealtimeCoordinator)
    coordinator.hass = object()
    coordinator.entry = SimpleNamespace(entry_id="entry")
    coordinator.ledger = ledger
    coordinator.history_store = AsyncMock()
    coordinator.energy_statistics_bridge = None

    async def _client():
        return client

    async def _fetch(function, *args):
        return function(*args)

    coordinator._client = _client
    coordinator._fetch = _fetch
    coordinator._accounts = lambda: (SimpleNamespace(account_number="account"),)
    return coordinator


def capture_notifications(monkeypatch) -> tuple[list[str], list[str]]:
    """Record every persistent notification the coordinator tries to touch."""
    created: list[str] = []
    dismissed: list[str] = []
    monkeypatch.setattr(
        "custom_components.csg.sensor.persistent_notification.async_create",
        lambda hass, message, title=None, notification_id=None: created.append(notification_id),
    )
    monkeypatch.setattr(
        "custom_components.csg.sensor.persistent_notification.async_dismiss",
        lambda hass, notification_id: dismissed.append(notification_id),
    )
    return created, dismissed


def notification_ids(kind: str, ids: list[str]) -> list[str]:
    return [value for value in ids if f"_{kind}_" in value]


def warning_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def yesterdays_kwh_description():
    return next(
        description
        for description in REALTIME_DESCRIPTIONS
        if description.suffix == SUFFIX_YESTERDAY_KWH
    )


def test_realtime_coordinator_treats_missing_yesterday_usage_as_a_gap(monkeypatch, caplog) -> None:
    """An unpublished yesterday reading must not be reported as a failure."""
    caplog.set_level(logging.DEBUG)
    created, dismissed = capture_notifications(monkeypatch)
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 20))
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    coordinator = make_realtime_coordinator(ledger, FakeUsageClient(None))

    data = run(coordinator._async_update_data())

    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data["account"][SUFFIX_ENERGY_TOTAL] == 20.0
    assert created == []
    assert warning_messages(caplog) == []
    assert data["account"][_KEY_YESTERDAY_DATE] is None
    # A successful request clears any earlier usage failure.
    assert notification_ids("usage", dismissed) == ["csg_entry_usage_account"]

    yesterday = CSGSensor(
        SimpleNamespace(data=data, last_update_success=True),
        "account",
        yesterdays_kwh_description(),
    )
    energy = CSGSensor(
        SimpleNamespace(data=data, last_update_success=True, ledger=ledger),
        "account",
        ENERGY_TOTAL,
    )
    assert not yesterday.available
    assert energy.available
    assert energy.native_value == 20.0


def test_realtime_coordinator_keeps_energy_unavailable_without_a_ledger_total(monkeypatch, caplog) -> None:
    """An empty reading before any reading was recorded stays quiet too."""
    caplog.set_level(logging.DEBUG)
    created, _ = capture_notifications(monkeypatch)
    ledger = make_ledger()
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    coordinator = make_realtime_coordinator(ledger, FakeUsageClient(None))

    data = run(coordinator._async_update_data())

    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data["account"][SUFFIX_ENERGY_TOTAL] == STATE_UNAVAILABLE
    assert created == []
    assert warning_messages(caplog) == []


def test_realtime_coordinator_notifies_a_failed_yesterday_request(monkeypatch, caplog) -> None:
    """A real request failure still warns and raises a notification."""
    caplog.set_level(logging.DEBUG)
    created, dismissed = capture_notifications(monkeypatch)
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 20))
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    coordinator = make_realtime_coordinator(ledger, FakeUsageClient(CSGAPIError("boom")))

    data = run(coordinator._async_update_data())

    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data["account"][SUFFIX_ENERGY_TOTAL] == 20.0
    assert notification_ids("usage", created) == ["csg_entry_usage_account"] * 2
    assert notification_ids("usage", dismissed) == []
    assert all("Could not update daily usage" in message for message in warning_messages(caplog))
    assert len(warning_messages(caplog)) == 2


def test_realtime_coordinator_records_a_published_yesterday_reading(monkeypatch, caplog) -> None:
    """A published reading is still recorded and clears earlier failures."""
    caplog.set_level(logging.DEBUG)
    created, dismissed = capture_notifications(monkeypatch)
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 20))
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 2, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    coordinator = make_realtime_coordinator(ledger, FakeUsageClient(25))

    data = run(coordinator._async_update_data())

    assert data["account"][SUFFIX_YESTERDAY_KWH] == 25
    assert data["account"][SUFFIX_ENERGY_TOTAL] == 25.0
    assert created == []
    assert warning_messages(caplog) == []
    assert notification_ids("usage", dismissed) == ["csg_entry_usage_account"]


def test_realtime_coordinator_leaves_energy_unavailable_when_a_failed_request_has_no_total(
    monkeypatch, caplog
) -> None:
    """A failed request with nothing recorded yet has no total to expose."""
    caplog.set_level(logging.DEBUG)
    created, dismissed = capture_notifications(monkeypatch)
    ledger = make_ledger()
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    coordinator = make_realtime_coordinator(ledger, FakeUsageClient(CSGAPIError("boom")))

    data = run(coordinator._async_update_data())

    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data["account"][SUFFIX_ENERGY_TOTAL] == STATE_UNAVAILABLE
    assert notification_ids("usage", created) == ["csg_entry_usage_account"] * 2
    assert notification_ids("usage", dismissed) == []


class FakeRealtimeClient(FakeUsageClient):
    """Monthly daily-usage fake with separate value/error constructor inputs."""

    def __init__(self, usage: float | None = None, error: Exception | None = None) -> None:
        super().__init__(error if error is not None else usage, day="2026-08-02")


class FakeRealtimeCoordinator(RealtimeCoordinator):
    """Small collaborator for testing RealtimeCoordinator._async_update_data.

    Only the collaborators are replaced, so the helpers under test stay real.
    """

    def __init__(self, client: FakeRealtimeClient) -> None:
        self.client = client
        self.ledger = make_ledger()
        self.history_store = AsyncMock()
        self.energy_statistics_bridge = None
        self.notifications: list[tuple[str, str, Exception]] = []
        self.dismissed: list[tuple[str, str]] = []

    async def _client(self):
        return self.client

    def _accounts(self):
        return [SimpleNamespace(account_number="account")]

    async def _fetch(self, function, *args):
        return function(*args)

    def _notify_failure(self, account: str, kind: str, err: Exception) -> None:
        self.notifications.append((account, kind, err))

    def _clear_failure(self, account: str, kind: str) -> None:
        self.dismissed.append((account, kind))


def _patch_utcnow(monkeypatch) -> None:
    """Pin the coordinator's clock so "yesterday" is a fixed day."""
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC),
    )


def test_empty_yesterday_usage_is_not_a_failed_request(monkeypatch, caplog) -> None:
    """An unpublished yesterday total is a data gap, not a broken account."""
    caplog.set_level(logging.DEBUG)
    _patch_utcnow(monkeypatch)
    coordinator = FakeRealtimeCoordinator(FakeRealtimeClient(usage=None))
    run(coordinator.ledger.async_record_realtime("account", "2026-08-01", 12))

    data = run(RealtimeCoordinator._async_update_data(coordinator))

    assert coordinator.notifications == []
    assert ("account", "usage") in coordinator.dismissed
    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data["account"][SUFFIX_ENERGY_TOTAL] == 12.0
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert coordinator.client.calls == [(2026, 8), (2026, 7)]


def test_failed_yesterday_usage_request_still_notifies(monkeypatch) -> None:
    """A real yesterday request failure still reports the account as failing."""
    _patch_utcnow(monkeypatch)
    error = CSGAPIError("boom")
    coordinator = FakeRealtimeCoordinator(FakeRealtimeClient(error=error))

    data = run(RealtimeCoordinator._async_update_data(coordinator))

    assert coordinator.notifications == [("account", "usage", error)] * 2
    assert coordinator.client.calls == [(2026, 8), (2026, 7)]
    assert ("account", "usage") not in coordinator.dismissed
    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data["account"][SUFFIX_ENERGY_TOTAL] == STATE_UNAVAILABLE
