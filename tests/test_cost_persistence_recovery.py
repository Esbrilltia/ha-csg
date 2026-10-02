"""M6-A1: normal Billing refresh owns convergence after bill save recovery."""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant

import custom_components.csg_plus as integration
from custom_components.csg_plus import energy_statistics as module
from custom_components.csg_plus.const import CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS
from custom_components.csg_plus.csg_client import CSGClient, JSON_KEY_YEAR_MONTH
from custom_components.csg_plus.history_store import CSGHistoryStore
from test_cost_statistics_lifecycle import cost_platform_world
from test_cost_statistics_recorder import cost_world
from test_energy_statistics_recorder import ACCOUNT, recorder_world


class OfficialBillsClient(CSGClient):
    """Real yearly parser over synthetic raw bills; daily requests always fail."""

    def __init__(self):
        self.cost = 100
        self.daily_calls = []

    def verify_login(self):
        return True

    def initialize(self):
        pass

    def get_balance_and_arrears(self, account):
        return 50, 0

    def api_query_day_electric_by_m_point(self, year, month, *args):
        self.daily_calls.append((year, month))
        raise ValueError("Synthetic persistent daily API failure")

    def api_get_fee_analyze_details(self, year, *args):
        rows = [{JSON_KEY_YEAR_MONTH: "202601", "actualTotalAmount": self.cost,
                 "billingElectricity": 200}] if year == 2026 else []
        return {"totalActualAmount": self.cost if rows else 0,
                "totalBillingElectricity": 200 if rows else 0,
                "electricAndChargeList": rows}


async def drain_requested_sync(world, bridge):
    """Wait for already requested work; never create an external sync trigger."""
    while bridge._task is not None:
        await asyncio.wait_for(asyncio.shield(bridge._task), 10)
    await world.drain()
    assert not bridge._pending


async def durable_bills(world):
    # A separate HA storage manager reads the real file, without setting up or
    # reloading any integration. It cannot see the writer's in-memory payload.
    reader_hass = HomeAssistant(world.hass.config.config_dir)
    try:
        reader = CSGHistoryStore(reader_hass, world.entry.entry_id)
        await reader.async_load()
        return await reader.async_monthly_bills_snapshot(ACCOUNT)
    finally:
        await reader_hass.async_stop(force=True)


@pytest.mark.parametrize("failure", ["public-save", "physical-worker"])
@pytest.mark.parametrize("revision", [98, 0, 105], ids=["downward", "real-zero", "upward"])
def test_bill_save_failure_converges_via_normal_refresh_without_daily_or_external_trigger(
    cost_platform_world, monkeypatch, failure, revision,
):
    client = OfficialBillsClient()

    async def configure(base):
        monkeypatch.setattr(integration.CSGClient, "load", lambda _: client)

    async def scenario():
        # Initial setup may issue its normal production request. The fixture's
        # manual sync helper is explicitly disabled throughout this regression.
        async with cost_platform_world(before_setup=configure, sync_on_setup=False) as world:
            runtime = world.runtime()
            store, bridge = runtime["history_store"], runtime["energy_statistics_bridge"]
            billing = runtime["billing_coordinator"]
            await drain_requested_sync(world, bridge)
            assert [(row["state"], row["sum"]) for row in await world.query_cost()] == [(100, 100)]
            assert store.monthly_bill(ACCOUNT, (2026, 1))["cost_cny"] == 100
            assert (await durable_bills(world))["2026-01"]["cost_cny"] == 100
            client.daily_calls.clear()
            client.cost = revision
            failed = []
            pending_on_request = []
            request = bridge.request_sync

            def observed_request():
                pending_on_request.append(store._persistence_pending)
                request()

            monkeypatch.setattr(bridge, "request_sync", Mock(side_effect=observed_request))
            if failure == "public-save":
                original = store._store.async_save

                async def fail_save(payload):
                    if not failed and payload["accounts"][ACCOUNT]["monthly_bills"]["2026-01"]["cost_cny"] == revision:
                        failed.append("public-save")
                        assert store._persistence_pending
                        assert store.monthly_bill(ACCOUNT, (2026, 1))["cost_cny"] == revision
                        raise OSError("Synthetic one-shot public Store save failure")
                    await original(payload)

                monkeypatch.setattr(store._store, "async_save", fail_save)
            else:
                original = store._store._write_prepared_data

                def fail_worker(mode, prepared):
                    payload = json.loads(prepared)["data"]
                    if not failed and payload["accounts"][ACCOUNT]["monthly_bills"]["2026-01"]["cost_cny"] == revision:
                        failed.append(threading.current_thread().name)
                        raise OSError("Synthetic one-shot physical Store worker failure")
                    original(mode, prepared)

                monkeypatch.setattr(store._store, "_write_prepared_data", fail_worker)

            await billing.async_refresh()
            await drain_requested_sync(world, bridge)
            assert failed == (["public-save"] if failure == "public-save" else ["csg_plus-history-storage"])
            assert client.daily_calls == [(2026, 9), (2026, 8)]
            assert world.runtime() is runtime and runtime["energy_statistics_bridge"] is bridge
            assert billing.last_update_success
            assert not store._persistence_pending
            facts = await store.async_monthly_bills_snapshot(ACCOUNT)
            assert facts["2026-01"]["cost_cny"] == revision
            assert await durable_bills(world) == facts
            rows = await world.query_cost(types={"state", "sum", "change"})
            assert [(row["state"], row["sum"], row["change"]) for row in rows] == [(revision, revision, revision)]
            assert pending_on_request == [True]  # Request survives the swallowed save error.
            assert await store.async_daily_usage_snapshot(ACCOUNT) == {}
            count = len(world.imports)

            for _ in range(2):
                await billing.async_refresh()
                await drain_requested_sync(world, bridge)
                assert await store.async_monthly_bills_snapshot(ACCOUNT) == facts
                assert await durable_bills(world) == facts
                assert await world.query_cost(types={"state", "sum", "change"}) == rows
                assert len(world.imports) == count
            assert pending_on_request == [True, False, False]
            assert client.daily_calls == [(2026, 9), (2026, 8)] * 3
    asyncio.run(scenario())


def test_bill_refresh_requests_respect_currency_guard_and_resume_without_reload(cost_platform_world, monkeypatch):
    client = OfficialBillsClient()

    async def configure(base):
        monkeypatch.setattr(integration.CSGClient, "load", lambda _: client)

    async def scenario():
        async with cost_platform_world(before_setup=configure, sync_on_setup=False) as world:
            runtime = world.runtime()
            store, bridge = runtime["history_store"], runtime["energy_statistics_bridge"]
            billing = runtime["billing_coordinator"]
            await drain_requested_sync(world, bridge)
            before = await world.query_cost()
            world.hass.config.currency = "USD"
            access = []
            metadata = module.get_metadata
            query = module.statistics_during_period
            add = module.async_add_external_statistics

            def observed_metadata(*args, **kwargs):
                if world.cost_id in kwargs.get("statistic_ids", set()):
                    access.append("metadata")
                return metadata(*args, **kwargs)

            def observed_query(*args, **kwargs):
                if world.cost_id in args[3]:
                    access.append("query")
                return query(*args, **kwargs)

            def observed_add(hass, meta, rows):
                if meta["statistic_id"] == world.cost_id:
                    access.append("import")
                return add(hass, meta, rows)

            monkeypatch.setattr(module, "get_metadata", observed_metadata)
            monkeypatch.setattr(module, "statistics_during_period", observed_query)
            monkeypatch.setattr(module, "async_add_external_statistics", observed_add)
            client.cost = 98
            for _ in range(2):
                await billing.async_refresh()
                await drain_requested_sync(world, bridge)
                assert access == []
                assert await world.query_cost() == before
                assert (await durable_bills(world))["2026-01"]["cost_cny"] == 98
            world.hass.config.currency = "CNY"
            await billing.async_refresh()
            await drain_requested_sync(world, bridge)
            assert [(row["state"], row["sum"]) for row in await world.query_cost()] == [(98, 98)]
            assert "import" in access
            assert world.runtime() is runtime
    asyncio.run(scenario())


def test_disabled_statistics_gate_keeps_bill_refresh_recorder_io_at_zero(cost_platform_world, monkeypatch):
    client = OfficialBillsClient()
    access = []

    async def configure(base):
        monkeypatch.setattr(integration.CSGClient, "load", lambda _: client)
        base.hass.config_entries.async_update_entry(base.entry, data={
            **base.entry.data,
            CONF_SETTINGS: {**base.entry.data[CONF_SETTINGS], CONF_ENERGY_STATISTICS_ENABLED: False},
        })
        for name in ("get_metadata", "statistics_during_period", "async_add_external_statistics"):
            stub = Mock(side_effect=AssertionError("Disabled statistics performed Recorder I/O"))
            monkeypatch.setattr(module, name, stub)
            access.append(stub)

    async def scenario():
        async with cost_platform_world(before_setup=configure, sync_on_setup=False) as world:
            runtime = world.runtime()
            store, bridge = runtime["history_store"], runtime["energy_statistics_bridge"]
            assert not bridge.enabled
            request = Mock(wraps=bridge.request_sync)
            monkeypatch.setattr(bridge, "request_sync", request)
            client.cost = 98
            for _ in range(2):
                await runtime["billing_coordinator"].async_refresh()
                assert bridge._task is None
                assert (await durable_bills(world))["2026-01"]["cost_cny"] == 98
            assert request.call_count == 2
            assert await store.async_daily_usage_snapshot(ACCOUNT) == {}
            assert not world.imports
            for stub in access:
                stub.assert_not_called()
    asyncio.run(scenario())
