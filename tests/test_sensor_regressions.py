"""A1/A2/A6 retained regressions; A3/A4/A5 are retired by design.

The eight former strict xfails tested the removed ledger/interpolation/adjust
path. Its closure is covered by test_energy_retirement, not algorithm patches.
"""

from copy import deepcopy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.csg.const import (
    SUFFIX_BAL,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from custom_components.csg.energy_statistics import build_statistics
from custom_components.csg.csg_client import CSGAPIError
from custom_components.csg.sensor import (
    REALTIME_DESCRIPTIONS,
    BillingCoordinator,
    CSGSensor,
    _KEY_YESTERDAY_DATE,
)
from test_history_store_integration import rig
from test_sensor import (
    FakeBillingCoordinator,
    FakeRealtimeCoordinator,
    FakeUsageClient,
    freeze_utcnow,
    run,
    yesterdays_kwh_description,
)


DAY = "2026-08-01"


def test_a1_billing_helpers_are_class_methods():
    for name in ("_add_year_data",):
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


@pytest.mark.parametrize("today,expected", [(1, 5.0), (3, STATE_UNAVAILABLE)])
def test_a6_last_month_fallback_does_not_substitute_for_yesterday(monkeypatch, today, expected):
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, today, 4, tzinfo=dt.UTC))
    client = FakeUsageClient(5.0, day="2026-07-31")
    coordinator = FakeRealtimeCoordinator(client)
    data = run(coordinator._async_update_data())
    assert client.calls == [(2026, 8), (2026, 7)]
    assert data["account"][SUFFIX_YESTERDAY_KWH] == expected
    assert "energy_total" not in data["account"]
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


def test_a2_downward_realtime_revision_converges_to_daily_billing_fact(rig):
    """A downward cloud revision is a fact revision, never a meter reset."""
    async def scenario():
        objects = await rig.build()
        rig.client.daily["account", (2026, 9)] = (10, [{"date": "2026-09-02", "kwh": 10}])
        await objects.realtime._async_update_data()
        assert objects.history.daily_usage("account", "2026-09-02")["kwh"] == 10
        rig.client.daily["account", (2026, 9)] = (8, [{"date": "2026-09-02", "kwh": 8}])
        await objects.realtime._async_update_data()
        await objects.billing._async_update_data()
        facts = await objects.history.async_daily_usage_snapshot("account")
        assert facts["2026-09-02"]["kwh"] == 8
        rows = build_statistics(facts)
        assert [(row["state"], row["sum"]) for row in rows] == [(8, 8)]
    run(scenario())
