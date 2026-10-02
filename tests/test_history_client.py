"""Exercise the actual daily/yearly client row conversion on synthetic raw data."""

from types import SimpleNamespace

import pytest

from custom_components.csg.csg_client import CSGClient, JSON_KEY_YEAR_MONTH
from custom_components.csg.history_helpers import collect_monthly_bill_candidates

ACCOUNT = SimpleNamespace(area_code="synthetic-area", ele_customer_id="synthetic-customer", metering_point_id="synthetic-meter")
INVALID = [None, True, False, "bad", "", "NaN", float("nan"), "inf", float("inf"), float("-inf"), -1]


@pytest.mark.parametrize("invalid", INVALID)
def test_daily_invalid_power_preserves_other_rows(invalid):
    client = CSGClient.__new__(CSGClient)
    client.api_query_day_electric_by_m_point = lambda *args: {
        "totalPower": "2",
        "result": [
            {"date": "2024-02-01", "power": "0"},
            {"date": "2024-02-02", "power": invalid},
            {"date": "2024-02-03", "power": "2"},
        ],
    }
    total, rows = client.get_month_daily_usage_detail(ACCOUNT, (2024, 2))
    assert total == 2
    assert rows == [
        {"date": "2024-02-01", "kwh": 0.0},
        {"date": "2024-02-02"},
        {"date": "2024-02-03", "kwh": 2.0},
    ]


@pytest.mark.parametrize("invalid_row", [None, "bad", [], {}, {"power": 3}, {"date": None, "power": 3}, {"date": 123, "power": 3}, {"date": "bad", "power": 3}, {"date": "2024-02-30", "power": 3}])
def test_daily_malformed_or_missing_date_does_not_poison_response(invalid_row):
    client = CSGClient.__new__(CSGClient)
    client.api_query_day_electric_by_m_point = lambda *args: {
        "totalPower": "2", "result": [invalid_row, {"date": "2024-02-29", "power": "2"}],
    }
    assert client.get_month_daily_usage_detail(ACCOUNT, (2024, 2))[1] == [{"date": "2024-02-29", "kwh": 2.0}]


@pytest.mark.parametrize("invalid", INVALID)
@pytest.mark.parametrize("field", ["billingElectricity", "actualTotalAmount"])
def test_yearly_fields_are_independently_validated(invalid, field):
    client = CSGClient.__new__(CSGClient)
    raw = {JSON_KEY_YEAR_MONTH: "202402", "billingElectricity": "0", "actualTotalAmount": "3"}
    raw[field] = invalid
    client.api_get_fee_analyze_details = lambda *args: {
        "totalBillingElectricity": "5", "totalActualAmount": "7",
        "electricAndChargeList": [raw, {JSON_KEY_YEAR_MONTH: "202401", "billingElectricity": "5", "actualTotalAmount": "4"}],
    }
    cost, usage, rows = client.get_year_month_stats(ACCOUNT, 2024)
    assert (cost, usage) == (7, 5)
    bad_key = "kwh" if field == "billingElectricity" else "charge"
    good_key = "charge" if field == "billingElectricity" else "kwh"
    assert bad_key not in rows[0]
    assert rows[0][good_key] == (3 if good_key == "charge" else 0)
    candidates = collect_monthly_bill_candidates(rows, "fictional-a", 2024)
    assert candidates[(2024, 1)] == (5, 4)
    assert candidates[(2024, 2)] == ((None, 3) if field == "billingElectricity" else (0, None))


@pytest.mark.parametrize("row", [None, "bad", [], {}, {"billingElectricity": 3}, {JSON_KEY_YEAR_MONTH: None, "billingElectricity": 3}, {JSON_KEY_YEAR_MONTH: "202413", "billingElectricity": 3}])
def test_yearly_malformed_row_preserves_valid_month(row):
    client = CSGClient.__new__(CSGClient)
    client.api_get_fee_analyze_details = lambda *args: {
        "totalBillingElectricity": "2", "totalActualAmount": "3",
        "electricAndChargeList": [row, {JSON_KEY_YEAR_MONTH: "2024-02", "billingElectricity": "2", "actualTotalAmount": "3"}],
    }
    candidates = collect_monthly_bill_candidates(client.get_year_month_stats(ACCOUNT, 2024)[2], "fictional-a", 2024)
    assert candidates == {(2024, 2): (2, 3)}
