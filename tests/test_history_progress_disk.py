"""A-2 audit schema cases through real V1 files, Stores, and entry tasks."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from requests import RequestException

import custom_components.csg_plus as integration
from custom_components.csg_plus import history_coordinator as coordinator_module
from custom_components.csg_plus.const import CONF_ELE_ACCOUNTS, DOMAIN
from custom_components.csg_plus.csg_client import CSGElectricityAccount
from test_history_lifecycle import worlds

ACCOUNT = "fictional-lifecycle"
GOOD = "fictional-good"
MONTH = (2024, 2)
FACT_FIELDS = ("daily_usage", "monthly_bills", "daily_coverage", "monthly_reconciliation")


class SchemaCloud:
    def __init__(self):
        self.fail_bad = True
        self.calls = []

    def verify_login(self):
        return True

    def initialize(self):
        pass

    def request(self, lane, account):
        self.calls.append((lane, account.account_number))
        if self.fail_bad and account.account_number == ACCOUNT:
            raise RequestException("synthetic account-local retryable failure")

    def get_month_daily_usage_detail(self, account, month):
        self.request("daily", account)
        return 0, []

    def get_year_month_stats(self, account, year):
        self.request("bill", account)
        return 0, 0, []


@pytest.mark.parametrize("sync", [
    {"history_backfill": None},
    {"history_backfill": {"completed_daily_months": None}},
    {"history_backfill": {"completed_bill_years": [2024], "bill_year_scopes": None}},
    {"history_backfill": {"completed_bill_years": [2024], "bill_year_scopes": []}},
    "absent",
    {"last_recent_sync": "old-recent"},
    {"history_backfill": {"last_completed_at": "old-history"}},
    {"history_backfill": {"completed_bill_years": [2024]}},
], ids=["null-backfill", "null-daily", "null-scopes", "list-scopes",
        "v1-no-sync", "partial-sync", "missing-optional-fields", "years-without-scopes"])
def test_real_v1_schema_inputs_preserve_facts_and_unload_reload(worlds, monkeypatch, sync):
    async def scenario():
        instances = []
        try:
            seed_hass, seed, _, _ = await worlds()
            instances.append(seed_hass)
            await seed.async_upsert_daily_usage(ACCOUNT, MONTH, [{"date": "2024-02-01", "kwh": 2}])
            await seed.async_upsert_monthly_bill(ACCOUNT, MONTH, usage_kwh=2, cost_cny=3)
            await seed.async_reconcile_month(ACCOUNT, MONTH)
            path = Path(seed._store.path)
            envelope = json.loads(path.read_text(encoding="utf-8"))
            assert envelope["version"] == 1
            row = envelope["data"]["accounts"][ACCOUNT]
            old = {field: deepcopy(row[field]) for field in FACT_FIELDS}
            if sync == "absent":
                row.pop("sync")
            else:
                row["sync"] = deepcopy(sync)
            # Only the test's temporary JSON file is edited, before fresh load.
            path.write_text(json.dumps(envelope), encoding="utf-8")
            await seed_hass.async_stop(force=True)

            cloud = SchemaCloud()
            events = []

            async def forward(*args):
                events.append("forward")

            async def platforms(*args):
                events.append("platforms")
                return True

            async def load_runtime():
                hass, _, owner, _ = await worlds()
                instances.append(hass)
                entry = owner.entry
                entry.data[CONF_ELE_ACCOUNTS][GOOD] = CSGElectricityAccount(GOOD).dump()
                hass.config_entries = SimpleNamespace(
                    async_forward_entry_setups=forward, async_unload_platforms=platforms,
                )
                monkeypatch.setattr(coordinator_module.CSGClient, "load", lambda _: cloud)
                assert await integration.async_setup_entry(hass, entry)
                return hass, entry, hass.data[DOMAIN][entry.entry_id]

            hass, entry, runtime = await load_runtime()
            history = runtime["history_coordinator"]
            progress = history.history_store.history_progress(ACCOUNT)
            assert not progress.get("completed_daily_months")
            assert not progress.get("bill_year_scopes")
            await history._task
            assert history._task.exception() is None
            assert cloud.calls == [("daily", ACCOUNT), ("bill", ACCOUNT), ("daily", GOOD), ("bill", GOOD)]
            disk_row = json.loads(path.read_text(encoding="utf-8"))["data"]["accounts"][ACCOUNT]
            assert {field: disk_row[field] for field in FACT_FIELDS} == old
            assert not history.history_store.history_progress(ACCOUNT).get("bill_year_scopes")
            assert history.history_store.history_progress(GOOD)["completed_daily_months"] == ["2024-02"]

            async def billing():
                assert history._shutdown
                events.append("billing")

            runtime["billing_coordinator"] = SimpleNamespace(async_shutdown=billing)
            assert await integration.async_unload_entry(hass, entry)
            assert events == ["forward", "billing", "platforms"]
            assert entry.entry_id not in hass.data[DOMAIN]

            # New HA manager and real disk load; only the unconfirmed account retries.
            cloud.fail_bad = False
            cloud.calls.clear()
            fresh_hass, fresh_entry, fresh_runtime = await load_runtime()
            restored = fresh_runtime["history_coordinator"]
            await restored._task
            assert cloud.calls == [("daily", ACCOUNT), ("bill", ACCOUNT)]
            assert restored.history_store.daily_usage(ACCOUNT, "2024-02-01") == old["daily_usage"]["2024-02-01"]
            assert restored.history_store.monthly_bill(ACCOUNT, MONTH) == old["monthly_bills"]["2024-02"]
            assert restored.history_store.daily_coverage(ACCOUNT, MONTH) == old["daily_coverage"]["2024-02"]
            assert restored.history_store.monthly_reconciliation(ACCOUNT, MONTH)["usage_state"] == "not_comparable"
            assert restored.history_store.history_progress(ACCOUNT)["bill_year_scopes"] == {"2024": ["2024-02"]}
            assert await integration.async_unload_entry(fresh_hass, fresh_entry)
        finally:
            for hass in instances:
                await hass.async_stop(force=True)
    asyncio.run(scenario())
