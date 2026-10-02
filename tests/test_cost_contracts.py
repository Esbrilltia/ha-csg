"""Lock the official monthly source, production call graph and privacy boundary."""

import ast
import asyncio
from copy import deepcopy
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.csg import sensor
from custom_components.csg.csg_client import CSGClient, CSGElectricityAccount, CSGAPIError
from custom_components.csg.csg_client.const import JSON_KEY_YEAR_MONTH, JSON_KEY_STA
from custom_components.csg.history_helpers import collect_monthly_bill_candidates
from custom_components.csg.history_store import MONTHLY_BILL_SOURCE
from test_history_store_integration import rig as recent_rig


def test_only_actual_total_amount_becomes_monthly_cost_fact():
    client = CSGClient.__new__(CSGClient)
    client.api_get_fee_analyze_details = lambda *args: {
        "totalActualAmount": "100", "totalBillingElectricity": "200",
        "electricAndChargeList": [{JSON_KEY_YEAR_MONTH: "202601", "actualTotalAmount": "100", "billingElectricity": "200", "estimatedAmount": "500", "totalAmount": "400", "charge": "300"}],
    }
    account = CSGElectricityAccount("fictional-source-account")
    cost, usage, rows = client.get_year_month_stats(account, 2026)
    assert (cost, usage) == (100, 200)
    assert collect_monthly_bill_candidates(rows, account.account_number, 2026) == {(2026, 1): (200, 100)}


def test_bill_batches_request_bridge_after_all_upserts_independently_of_changes(recent_rig):
    months = [{"month": "202601", "charge": 100, "kwh": 200},
              {"month": "202602", "charge": 120, "kwh": 200}]
    recent_rig.client.years["account", 2026] = (220, 400, months)
    async def scenario():
        objects = await recent_rig.build()
        notifications = []
        bridge = SimpleNamespace(request_sync=Mock(side_effect=lambda: notifications.append({
            month: deepcopy(objects.history.monthly_bill("account", (2026, month))) for month in (1, 2)
        })))
        objects.billing.energy_statistics_bridge = bridge
        account = CSGElectricityAccount("account", area_code="080000")
        await objects.billing._add_year_data(recent_rig.client, account, {})
        bridge.request_sync.assert_called_once_with()
        assert notifications[0][1]["cost_cny"] == 100 and notifications[0][1]["source"] == MONTHLY_BILL_SOURCE
        assert notifications[0][2]["cost_cny"] == 120
        assert not any(call[0] == "daily" for call in recent_rig.client.calls)
        await objects.billing._add_year_data(recent_rig.client, account, {})
        assert bridge.request_sync.call_count == 2
        assert notifications[1] == notifications[0]
        recent_rig.client.years["account", 2026] = (218, 400, [{**months[0], "charge": 98}, months[1]])
        await objects.billing._add_year_data(recent_rig.client, account, {})
        assert bridge.request_sync.call_count == 3
        assert notifications[-1][1]["cost_cny"] == 98
        assert notifications[-1][2]["cost_cny"] == 120
    asyncio.run(scenario())


def test_production_never_reaches_retired_charge_apis_or_energy_preferences():
    root = Path(__file__).parents[1] / "custom_components" / "csg"
    forbidden = {"get_month_daily_cost_detail", "get_yesterday_kwh", "api_query_day_electric_charge_by_m_point", "async_adjust_statistics", "async_clear_statistics"}
    for path in root.rglob("*.py"):
        if "csg_client" in path.parts or path.name == "csg_client_demo.py":
            continue  # Compatibility wrappers remain uncalled in the client.
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert not any(isinstance(node, ast.Attribute) and node.attr in forbidden for node in ast.walk(tree)), path
        assert "queryDayElectricChargeByMPoint" not in source
        assert "homeassistant.components.energy" not in source
        assert ".storage/energy" not in source
    facts_source = (root / "history_store.py").read_text(encoding="utf-8")
    assert "daily_cost" not in facts_source and "tariff_profiles" not in facts_source
    assert "tariff" not in (root / "cost_statistics.py").read_text(encoding="utf-8").split("from .const import DOMAIN")[-1]


def test_client_debug_logs_never_dump_account_payload_auth_or_response(caplog):
    client = CSGClient.__new__(CSGClient)
    number, token, address = "fictional-sensitive-account", "fictional-sensitive-token", "fictional-sensitive-address"
    client.auth_token = token
    client.customer_number = number
    client._common_headers = {}
    client._session = SimpleNamespace(post=Mock(return_value=SimpleNamespace(status_code=200, content=json.dumps({"account": number, "address": address, "token": token}).encode(), headers={})))
    with caplog.at_level(logging.DEBUG):
        client._make_request("charge/getAnalyzeFeeDetails", {"account": number, "token": token})
        with pytest.raises(CSGAPIError):
            client._handle_unsuccessful_response("charge/getAnalyzeFeeDetails", {JSON_KEY_STA: "fictional-error", "message": number})
    assert number not in caplog.text and token not in caplog.text and address not in caplog.text
    assert "getAnalyzeFeeDetails" in caplog.text


def test_monthly_conflict_diagnostics_do_not_log_utility_account_number(caplog):
    account = "fictional-private-account-number"
    rows = [{"month": "202601", "charge": 100}, {"month": "202601", "charge": 98}]
    assert collect_monthly_bill_candidates(rows, account, 2026) == {}
    assert account not in caplog.text and "conflict" in caplog.text
