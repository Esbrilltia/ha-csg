"""B2 regression: only finite nonnegative daily facts cross the client boundary.

All API responses and account identifiers are synthetic. Exercise the real
client conversion and all three consumers without network or Recorder access.
"""

from copy import deepcopy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE

from custom_components.csg.const import (
    ATTR_KEY_CURRENT_LADDER_START_DATE,
    ATTR_KEY_SETTLEMENT_DATE,
    SUFFIX_ENERGY_TOTAL,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from custom_components.csg.csg_client import CSGClient
from custom_components.csg.sensor import (
    BILLING_DESCRIPTIONS,
    BillingCoordinator,
    CSGSensor,
    CurrentCoordinator,
    RealtimeCoordinator,
)
from test_sensor import freeze_utcnow, make_ledger, run, yesterdays_kwh_description


INVALID = [
    pytest.param("NaN", id="nan-string"),
    pytest.param(float("nan"), id="nan-float"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(-1.0, id="negative"),
    pytest.param(None, id="none"),
    pytest.param("", id="empty-string"),
]
ACCOUNT = SimpleNamespace(
    account_number="synthetic-account",
    area_code="080000",
    ele_customer_id="synthetic-customer",
    metering_point_id="synthetic-meter",
)


def make_client(rows, total="0"):
    client = CSGClient.__new__(CSGClient)
    client.api_query_day_electric_by_m_point = lambda year, month, *args: {
        "totalPower": total,
        "result": deepcopy(rows) if (year, month) == (2026, 8) else [],
    }
    client.get_balance_and_arrears = lambda account: (1.0, 0.0)
    return client


def make_coordinator(kind, client, ledger):
    coordinator = kind.__new__(kind)
    coordinator.ledger = ledger
    coordinator._client = AsyncMock(return_value=client)
    coordinator._accounts = lambda: [ACCOUNT]

    async def fetch(function, *args):
        return function(*args)

    coordinator._fetch = fetch
    coordinator._clear_failure = lambda *args: None
    coordinator._notify_failure = lambda *args: pytest.fail("Unexpected API failure")
    if kind is BillingCoordinator:
        coordinator._async_correct_statistics = AsyncMock(return_value={})
        coordinator._add_year_data = AsyncMock()
    return coordinator


def entity(data, description):
    return CSGSensor(
        SimpleNamespace(data=data, last_update_success=True),
        ACCOUNT.account_number,
        description,
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC))


@pytest.mark.parametrize("power", INVALID)
def test_invalid_daily_usage_never_becomes_a_fact(power):
    client = make_client([{"date": "2026-08-02", "power": power}])
    assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 8)) == (0.0, [])
    ledger = make_ledger()
    ledger.async_record_realtime = AsyncMock(wraps=ledger.async_record_realtime)
    realtime = make_coordinator(RealtimeCoordinator, client, ledger)
    data = run(realtime._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data[ACCOUNT.account_number][SUFFIX_ENERGY_TOTAL] == STATE_UNAVAILABLE
    yesterday = entity(data, yesterdays_kwh_description())
    assert not yesterday.available
    assert yesterday.native_value is None
    ledger.async_record_realtime.assert_not_awaited()
    assert ledger.energy_total(ACCOUNT.account_number) is None

    billing = make_coordinator(BillingCoordinator, client, ledger)
    ledger.async_record_billing = AsyncMock(wraps=ledger.async_record_billing)
    bill = run(billing._update_account(client, ACCOUNT, [(2026, 8), (2026, 7)]))
    ledger.async_record_billing.assert_awaited_once_with(ACCOUNT.account_number, [])
    assert bill[SUFFIX_LATEST_DAY_KWH] == STATE_UNAVAILABLE
    latest = entity({ACCOUNT.account_number: bill}, next(
        item for item in BILLING_DESCRIPTIONS if item.suffix == SUFFIX_LATEST_DAY_KWH
    ))
    assert not latest.available
    assert latest.native_value is None
    account_state = ledger._data["accounts"][ACCOUNT.account_number]
    for key in ("realtime", "counted_realtime", "reported_realtime", "billing"):
        assert not account_state.get(key)


def test_missing_daily_power_is_not_zero():
    client = make_client([{"date": "2026-08-02"}])
    assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 8)) == (0.0, [])
    ledger = make_ledger()
    data = run(make_coordinator(RealtimeCoordinator, client, ledger)._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_ENERGY_TOTAL] == STATE_UNAVAILABLE
    assert not entity(data, yesterdays_kwh_description()).available
    assert ledger.energy_total(ACCOUNT.account_number) is None


@pytest.mark.parametrize("power", [0, 0.0, "0", "0.0", "-0.0", 2.5, "2.5"])
def test_valid_daily_usage_including_zero_remains_available(power):
    value = float(power)
    client = make_client([{"date": "2026-08-02", "power": power}], total=str(value))
    assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 8)) == (
        value, [{"date": "2026-08-02", "kwh": value}]
    )
    ledger = make_ledger()
    data = run(make_coordinator(RealtimeCoordinator, client, ledger)._async_update_data())
    yesterday = entity(data, yesterdays_kwh_description())
    assert yesterday.available
    assert yesterday.native_value == value
    assert ledger.energy_total(ACCOUNT.account_number) == value
    assert ledger._data["accounts"][ACCOUNT.account_number]["realtime"] == {
        "2026-08-02": value
    }
    billing = make_coordinator(BillingCoordinator, client, ledger)
    bill = run(billing._update_account(client, ACCOUNT, [(2026, 8), (2026, 7)]))
    assert bill[SUFFIX_LATEST_DAY_KWH] == value
    assert ledger.billing_days(ACCOUNT.account_number) == {"2026-08-02": {"kwh": value}}


@pytest.mark.parametrize("power", INVALID)
def test_invalid_refetch_preserves_existing_ledger_facts(power):
    ledger = make_ledger()
    client = make_client([{"date": "2026-08-02", "power": "5"}], total="5")
    realtime = make_coordinator(RealtimeCoordinator, client, ledger)
    billing = make_coordinator(BillingCoordinator, client, ledger)
    run(realtime._async_update_data())
    run(billing._update_account(client, ACCOUNT, [(2026, 8), (2026, 7)]))
    before = deepcopy(ledger._data)

    invalid_client = make_client([{"date": "2026-08-02", "power": power}])
    ledger.async_record_realtime = AsyncMock(wraps=ledger.async_record_realtime)
    realtime = make_coordinator(RealtimeCoordinator, invalid_client, ledger)
    data = run(realtime._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data[ACCOUNT.account_number][SUFFIX_ENERGY_TOTAL] == 5.0
    ledger.async_record_realtime.assert_not_awaited()
    run(billing._update_account(invalid_client, ACCOUNT, [(2026, 8), (2026, 7)]))
    assert ledger._data == before


@pytest.mark.parametrize("rows", [[], [{"date": "2026-08-02"}]])
def test_missing_refetch_preserves_existing_ledger_facts(rows):
    ledger = make_ledger()
    client = make_client([{"date": "2026-08-02", "power": "5"}], total="5")
    run(make_coordinator(RealtimeCoordinator, client, ledger)._async_update_data())
    billing = make_coordinator(BillingCoordinator, client, ledger)
    run(billing._update_account(client, ACCOUNT, [(2026, 8), (2026, 7)]))
    before = deepcopy(ledger._data)
    missing_client = make_client(rows)
    data = run(make_coordinator(RealtimeCoordinator, missing_client, ledger)._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert data[ACCOUNT.account_number][SUFFIX_ENERGY_TOTAL] == 5.0
    run(billing._update_account(missing_client, ACCOUNT, [(2026, 8), (2026, 7)]))
    assert ledger._data == before


@pytest.mark.parametrize("power", INVALID)
def test_invalid_yesterday_does_not_hide_an_older_valid_daily_fact(power):
    client = make_client([
        {"date": "2026-08-01", "power": 3},
        {"date": "2026-08-02", "power": power},
    ], total="3")
    ledger = make_ledger()
    ledger.async_record_realtime = AsyncMock(wraps=ledger.async_record_realtime)
    data = run(make_coordinator(RealtimeCoordinator, client, ledger)._async_update_data())
    assert not entity(data, yesterdays_kwh_description()).available
    assert data[ACCOUNT.account_number][SUFFIX_ENERGY_TOTAL] == 3.0
    ledger.async_record_realtime.assert_awaited_once_with(
        ACCOUNT.account_number, "2026-08-01", 3.0
    )
    billing = make_coordinator(BillingCoordinator, client, ledger)
    bill = run(billing._update_account(client, ACCOUNT, [(2026, 8), (2026, 7)]))
    assert bill[SUFFIX_LATEST_DAY_KWH] == 3.0
    assert bill[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-08-01"}


def test_mixed_daily_response_keeps_valid_rows_for_all_consumers():
    rows = [
        {"date": "2026-08-01", "power": "270"},
        {"date": "2026-08-02", "power": 0.0},
        {"date": "2026-08-03", "power": "NaN"},
        {"date": "2026-08-04", "power": float("inf")},
        {"date": "2026-08-05", "power": float("-inf")},
        {"date": "2026-08-06", "power": -100},
        {"date": "2026-08-07", "power": None},
        {"date": "2026-08-08"},
    ]
    client = make_client(rows, total="270")
    ledger = make_ledger()
    ledger.async_record_realtime = AsyncMock(wraps=ledger.async_record_realtime)
    data = run(make_coordinator(RealtimeCoordinator, client, ledger)._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == 0.0
    ledger.async_record_realtime.assert_awaited_once_with(
        ACCOUNT.account_number, "2026-08-02", 0.0
    )
    billing = make_coordinator(BillingCoordinator, client, ledger)
    bill = run(billing._update_account(client, ACCOUNT, [(2026, 8), (2026, 7)]))
    assert bill[SUFFIX_LATEST_DAY_KWH] == 0.0
    assert bill[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-08-02"}
    assert ledger.billing_days(ACCOUNT.account_number) == {
        "2026-08-01": {"kwh": 270.0}, "2026-08-02": {"kwh": 0.0}
    }


@pytest.mark.parametrize("power", INVALID)
def test_invalid_daily_values_do_not_enter_ladder_accumulation(power):
    client = make_client([
        {"date": "2026-08-01", "power": power},
        {"date": "2026-08-02", "power": 270},
    ], total="270")
    current = make_coordinator(CurrentCoordinator, client, make_ledger())
    data = run(current._async_update_data())
    assert data[ACCOUNT.account_number][ATTR_KEY_CURRENT_LADDER_START_DATE] == {
        ATTR_KEY_CURRENT_LADDER_START_DATE: "2026-08-02"
    }
