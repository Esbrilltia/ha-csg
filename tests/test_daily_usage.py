"""B-2: finite nonnegative facts and nonfact coverage at the client boundary.

Synthetic cloud responses exercise the real client and all three consumers.
HistoryStore replaces the retired ledger as the sole daily fact authority.
"""

from copy import deepcopy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE

from custom_components.csg_plus.const import (
    ATTR_KEY_CURRENT_LADDER_START_DATE,
    ATTR_KEY_SETTLEMENT_DATE,
    CONF_SETTINGS,
    CONF_TARIFF_PROFILES,
    SUFFIX_CURRENT_LADDER,
    SUFFIX_CURRENT_LADDER_REMAINING_KWH,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from custom_components.csg_plus.csg_client import CSGClient
from custom_components.csg_plus.sensor import (
    BILLING_DESCRIPTIONS, CURRENT_DESCRIPTIONS, BillingCoordinator, CSGSensor, CurrentCoordinator,
    RealtimeCoordinator,
)
from test_history_store import make_store
from test_sensor import freeze_utcnow, run, yesterdays_kwh_description

INVALID = [
    pytest.param("NaN", id="nan-string"),
    pytest.param(float("nan"), id="nan-float"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(-1.0, id="negative"),
    pytest.param(None, id="none"),
    pytest.param("", id="empty-string"),
    pytest.param("not-a-number", id="malformed-number"),
    pytest.param(True, id="boolean"),
]
ACCOUNT = SimpleNamespace(
    account_number="synthetic-account", area_code="080000",
    ele_customer_id="synthetic-customer", metering_point_id="synthetic-meter",
)


def make_client(rows, total="0", year_month=(2026, 8)):
    client = CSGClient.__new__(CSGClient)
    client.api_query_day_electric_by_m_point = lambda year, month, *args: {
        "totalPower": total,
        "result": deepcopy(rows) if (year, month) == year_month else [],
    }
    client.get_balance_and_arrears = lambda account: (1.0, 0.0)
    return client


def make_coordinator(kind, client, history=None):
    coordinator = kind.__new__(kind)
    coordinator._client = AsyncMock(return_value=client)
    coordinator._accounts = lambda: [ACCOUNT]

    async def fetch(function, *args):
        return function(*args)

    coordinator._fetch = fetch
    coordinator._clear_failure = lambda *args: None
    coordinator._notify_failure = lambda *args: pytest.fail("Unexpected API failure")
    if kind is CurrentCoordinator:
        coordinator.entry = SimpleNamespace(data={CONF_SETTINGS: {CONF_TARIFF_PROFILES: {
            ACCOUNT.account_number: {"scheme": "ladder", "multi_person": False, "tou": False},
        }}})
    if kind in (RealtimeCoordinator, BillingCoordinator):
        coordinator.history_store = history or make_store()
        coordinator.energy_statistics_bridge = None
    if kind is BillingCoordinator:
        coordinator._add_year_data = AsyncMock()
    return coordinator


def entity(data, description):
    return CSGSensor(SimpleNamespace(data=data, last_update_success=True),
                     ACCOUNT.account_number, description)


def bill(client, history):
    coordinator = make_coordinator(BillingCoordinator, client, history)
    return run(coordinator._update_account(client, ACCOUNT, [(2026, 8), (2026, 7)]))


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC))


@pytest.mark.parametrize("power", INVALID)
def test_invalid_daily_usage_never_becomes_a_fact(power):
    client = make_client([{"date": "2026-08-02", "power": power}])
    assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 8)) == (0.0, [{"date": "2026-08-02"}])
    history = make_store()
    data = run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    yesterday = entity(data, yesterdays_kwh_description())
    assert not yesterday.available and yesterday.native_value is None
    billing = bill(client, history)
    assert billing[SUFFIX_LATEST_DAY_KWH] == STATE_UNAVAILABLE
    latest = entity({ACCOUNT.account_number: billing}, next(
        item for item in BILLING_DESCRIPTIONS if item.suffix == SUFFIX_LATEST_DAY_KWH
    ))
    assert not latest.available and latest.native_value is None
    assert not run(history.async_daily_usage_snapshot(ACCOUNT.account_number))


def test_missing_daily_power_is_not_zero():
    client = make_client([{"date": "2026-08-02"}])
    assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 8)) == (0.0, [{"date": "2026-08-02"}])
    history = make_store()
    data = run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    assert not entity(data, yesterdays_kwh_description()).available
    assert history.daily_usage(ACCOUNT.account_number, "2026-08-02") is None


@pytest.mark.parametrize("power", [0, 0.0, "0", "0.0", "-0.0", 2.5, "2.5"])
def test_valid_daily_usage_including_zero_remains_available(power):
    value = float(power)
    client = make_client([{"date": "2026-08-02", "power": power}], total=str(value))
    assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 8)) == (
        value, [{"date": "2026-08-02", "kwh": value}]
    )
    history = make_store()
    data = run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    yesterday = entity(data, yesterdays_kwh_description())
    assert yesterday.available and yesterday.native_value == value
    assert history.daily_usage(ACCOUNT.account_number, "2026-08-02")["kwh"] == value
    assert bill(client, history)[SUFFIX_LATEST_DAY_KWH] == value
    assert history.daily_usage(ACCOUNT.account_number, "2026-08-02")["kwh"] == value


@pytest.mark.parametrize("power", INVALID)
def test_invalid_refetch_preserves_existing_history_facts(power):
    history = make_store()
    client = make_client([{"date": "2026-08-02", "power": "5"}], total="5")
    run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    bill(client, history)
    before = run(history.async_daily_usage_snapshot(ACCOUNT.account_number))
    invalid = make_client([{"date": "2026-08-02", "power": power}])
    data = run(make_coordinator(RealtimeCoordinator, invalid, history)._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    bill(invalid, history)
    assert run(history.async_daily_usage_snapshot(ACCOUNT.account_number)) == before


@pytest.mark.parametrize("rows", [[], [{"date": "2026-08-02"}]])
def test_missing_refetch_preserves_existing_history_facts(rows):
    history = make_store()
    client = make_client([{"date": "2026-08-02", "power": "5"}], total="5")
    run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    bill(client, history)
    before = run(history.async_daily_usage_snapshot(ACCOUNT.account_number))
    missing = make_client(rows)
    data = run(make_coordinator(RealtimeCoordinator, missing, history)._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    bill(missing, history)
    assert run(history.async_daily_usage_snapshot(ACCOUNT.account_number)) == before


@pytest.mark.parametrize("power", INVALID)
def test_invalid_yesterday_does_not_hide_an_older_valid_daily_fact(power):
    client = make_client([
        {"date": "2026-08-01", "power": 3}, {"date": "2026-08-02", "power": power},
    ], total="3")
    history = make_store()
    data = run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    assert not entity(data, yesterdays_kwh_description()).available
    assert history.daily_usage(ACCOUNT.account_number, "2026-08-01")["kwh"] == 3
    billing = bill(client, history)
    assert billing[SUFFIX_LATEST_DAY_KWH] == 3
    assert billing[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-08-01"}


def test_mixed_daily_response_keeps_valid_rows_for_all_consumers():
    client = make_client([
        {"date": "2026-08-01", "power": "270"}, {"date": "2026-08-02", "power": 0.0},
        {"date": "2026-08-03", "power": "NaN"}, {"date": "2026-08-04", "power": float("inf")},
        {"date": "2026-08-05", "power": float("-inf")}, {"date": "2026-08-06", "power": -100},
        {"date": "2026-08-07", "power": None}, {"date": "2026-08-08"},
    ], total="270")
    history = make_store()
    data = run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    assert data[ACCOUNT.account_number][SUFFIX_YESTERDAY_KWH] == 0.0
    billing = bill(client, history)
    assert billing[SUFFIX_LATEST_DAY_KWH] == 0.0
    assert billing[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-08-02"}
    facts = run(history.async_daily_usage_snapshot(ACCOUNT.account_number))
    assert {day: fact["kwh"] for day, fact in facts.items()} == {
        "2026-08-01": 270.0, "2026-08-02": 0.0,
    }


@pytest.mark.parametrize("power", INVALID)
def test_invalid_daily_values_do_not_enter_ladder_accumulation(power):
    client = make_client([
        {"date": "2026-08-01", "power": power}, {"date": "2026-08-02", "power": 270},
    ], total="270")
    current = make_coordinator(CurrentCoordinator, client)
    data = run(current._async_update_data())
    assert data[ACCOUNT.account_number][ATTR_KEY_CURRENT_LADDER_START_DATE] == {
        ATTR_KEY_CURRENT_LADDER_START_DATE: None
    }
    assert data[ACCOUNT.account_number][SUFFIX_CURRENT_LADDER] == 2
    assert data[ACCOUNT.account_number][SUFFIX_CURRENT_LADDER_REMAINING_KWH] == 330


@pytest.mark.parametrize("power", INVALID)
def test_invalid_tail_keeps_coverage_through_real_client_coordinator_tariff_and_entities(monkeypatch, power):
    freeze_utcnow(monkeypatch, dt.datetime(2026, 9, 4, 4, tzinfo=dt.UTC))
    client = make_client([
        {"date": "2026-09-01", "power": 250},
        {"date": "2026-09-02", "power": 20},
        {"date": "2026-09-03", "power": power},
    ], total="270", year_month=(2026, 9))
    assert client.get_month_daily_usage_detail(ACCOUNT, (2026, 9)) == (270, [
        {"date": "2026-09-01", "kwh": 250},
        {"date": "2026-09-02", "kwh": 20},
        {"date": "2026-09-03"},
    ])
    current = make_coordinator(CurrentCoordinator, client)
    current.data = run(current._async_update_data())
    current.last_update_success = True
    sensors = {description.suffix: CSGSensor(current, ACCOUNT.account_number, description)
               for description in CURRENT_DESCRIPTIONS}
    tier = sensors[SUFFIX_CURRENT_LADDER]
    remaining = sensors[SUFFIX_CURRENT_LADDER_REMAINING_KWH]
    assert tier.available and tier.native_value == 2
    assert remaining.available and remaining.native_value == 330
    assert tier.extra_state_attributes[ATTR_KEY_CURRENT_LADDER_START_DATE] is None

    history = make_store()
    realtime = run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    yesterday = entity(realtime, yesterdays_kwh_description())
    assert not yesterday.available and yesterday.native_value is None
    billing = run(make_coordinator(BillingCoordinator, client, history)._update_account(
        client, ACCOUNT, [(2026, 9), (2026, 8)],
    ))
    assert billing[SUFFIX_LATEST_DAY_KWH] == 20
    assert billing[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-09-02"}
    facts = run(history.async_daily_usage_snapshot(ACCOUNT.account_number))
    assert {day: fact["kwh"] for day, fact in facts.items()} == {
        "2026-09-01": 250, "2026-09-02": 20,
    }


def test_real_zero_tail_is_a_fact_and_completes_tariff_coverage(monkeypatch):
    freeze_utcnow(monkeypatch, dt.datetime(2026, 9, 4, 4, tzinfo=dt.UTC))
    client = make_client([
        {"date": "2026-09-01", "power": 250},
        {"date": "2026-09-02", "power": 20},
        {"date": "2026-09-03", "power": 0},
    ], total="270", year_month=(2026, 9))
    current = make_coordinator(CurrentCoordinator, client)
    current.data = run(current._async_update_data())
    current.last_update_success = True
    tier = CSGSensor(current, ACCOUNT.account_number, next(
        description for description in CURRENT_DESCRIPTIONS if description.suffix == SUFFIX_CURRENT_LADDER
    ))
    assert tier.available and tier.native_value == 2
    assert tier.extra_state_attributes[ATTR_KEY_CURRENT_LADDER_START_DATE] == "2026-09-02"
    history = make_store()
    realtime = run(make_coordinator(RealtimeCoordinator, client, history)._async_update_data())
    yesterday = entity(realtime, yesterdays_kwh_description())
    assert yesterday.available and yesterday.native_value == 0
    billing = run(make_coordinator(BillingCoordinator, client, history)._update_account(
        client, ACCOUNT, [(2026, 9), (2026, 8)],
    ))
    assert billing[SUFFIX_LATEST_DAY_KWH] == 0
    assert billing[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-09-03"}
    assert history.daily_usage(ACCOUNT.account_number, "2026-09-03")["kwh"] == 0
