"""Real HA 2026.9.3 SQLite and Energy validation for sparse official costs."""

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from functools import partial

import pytest
from homeassistant.components.energy import data as energy_data, validate as energy_validate
from homeassistant.components.recorder.statistics import async_add_external_statistics, get_metadata, statistics_during_period

from custom_components.csg import energy_statistics as module
from custom_components.csg.cost_statistics import build_cost_statistics, cost_statistic_metadata
from test_energy_statistics_recorder import ACCOUNT, recorder_world


@pytest.fixture
def cost_world(recorder_world, monkeypatch):
    monkeypatch.setattr(module, "_csg_today", lambda: dt.date(2026, 10, 3))

    @asynccontextmanager
    async def world():
        async with recorder_world() as runtime:
            runtime.hass.config.currency = "CNY"
            runtime.cost_id = cost_statistic_metadata(ACCOUNT)["statistic_id"]

            async def upsert_bills(values):
                for key, cost in values.items():
                    year, month = map(int, key.split("-"))
                    await runtime.store.async_upsert_monthly_bill(ACCOUNT, (year, month), usage_kwh=None, cost_cny=cost)

            async def query_cost(period="hour", types=None):
                return (await runtime.recorder.async_add_executor_job(
                    statistics_during_period, runtime.hass, module._QUERY_START, None,
                    {runtime.cost_id}, period, None, types or {"state", "sum"},
                )).get(runtime.cost_id, [])

            runtime.upsert_bills = upsert_bills
            runtime.query_cost = query_cost
            yield runtime
    return world


def test_real_cost_hole_fill_downward_upward_revision_and_repeat(cost_world):
    async def scenario():
        async with cost_world() as world:
            await world.upsert_bills({"2026-01": 100, "2026-03": 150})
            await world.sync()
            rows = await world.query_cost()
            assert [(row["state"], row["sum"]) for row in rows] == [(100, 100), (150, 250)]
            assert rows[0]["start"] == dt.datetime(2026, 1, 15, 4, tzinfo=dt.UTC).timestamp()
            meta = await world.recorder.async_add_executor_job(partial(get_metadata, world.hass, statistic_ids={world.cost_id}))
            assert {key: meta[world.cost_id][1][key] for key in cost_statistic_metadata(ACCOUNT)} == cost_statistic_metadata(ACCOUNT)
            await world.upsert_bills({"2026-02": 120})
            await world.sync()
            assert [(row["state"], row["sum"]) for row in await world.query_cost()] == [(100, 100), (120, 220), (150, 370)]
            assert [row["state"] for row in world.imports[-1][1]] == [120, 150]
            for revision in (98, 103):
                await world.upsert_bills({"2026-01": revision})
                await world.sync()
                assert [(row["state"], row["sum"]) for row in await world.query_cost()] == [(revision, revision), (120, revision + 120), (150, revision + 270)]
            count = len(world.imports)
            await world.sync()
            assert len(world.imports) == count
            assert await world.query_cost(period="5minute") == []
    asyncio.run(scenario())


def test_real_energy_validator_accepts_paired_external_usage_and_cost(cost_world):
    async def scenario():
        async with cost_world() as world:
            await world.upsert({"2026-09-01": 10})
            await world.upsert_bills({"2026-09": 100})
            await world.sync()
            manager = await energy_data.async_get_manager(world.hass)
            # Isolated test-only preferences; production never writes these.
            manager.data = {"energy_sources": [{"type": "grid", "stat_energy_from": world.statistic_id, "stat_cost": world.cost_id}], "device_consumption": []}
            assert (await energy_validate.async_validate(world.hass)).as_dict() == {
                "energy_sources": [[]], "device_consumption": [], "device_consumption_water": [],
            }
            assert world.hass.states.get(world.cost_id) is None
    asyncio.run(scenario())


def test_real_closed_months_zero_and_sparse_day_month_aggregates(cost_world):
    async def scenario():
        async with cost_world() as world:
            await world.upsert_bills({"2026-01": 100, "2026-03": 150, "2026-04": 0, "2026-10": 20, "2026-11": 30})
            await world.sync()
            rows = await world.query_cost()
            assert [(row["state"], row["sum"]) for row in rows] == [(100, 100), (150, 250), (0, 250)]
            for period in ("hour", "day", "month"):
                assert [row["change"] for row in await world.query_cost(period=period, types={"change"})] == [100, 150, 0]
            assert [row["change"] for row in await world.query_cost(period="year", types={"change"})] == [250]
            facts = await world.store.async_monthly_bills_snapshot(ACCOUNT)
            assert facts["2026-10"]["cost_cny"] == 20 and facts["2026-11"]["cost_cny"] == 30
    asyncio.run(scenario())


@pytest.mark.parametrize("anomaly", ["extra-earlier", "extra-later", "metadata"])
def test_real_cost_anomalies_preserve_existing_rows_without_cleanup(cost_world, caplog, anomaly):
    async def scenario():
        async with cost_world() as world:
            await world.upsert_bills({"2026-03": 150})
            values = {"2026-03": {"cost_cny": 150}}
            meta = cost_statistic_metadata(ACCOUNT)
            if anomaly == "metadata":
                meta = {**meta, "unit_class": "energy", "unit_of_measurement": "kWh"}
            else:
                values["2026-01" if anomaly == "extra-earlier" else "2026-04"] = {"cost_cny": 100}
            async_add_external_statistics(world.hass, meta, build_cost_statistics(values, dt.date(2026, 10, 3)))
            await world.drain()
            before = await world.query_cost()
            await world.sync()
            assert await world.query_cost() == before
            assert not any(metadata["statistic_id"] == world.cost_id for metadata, _ in world.imports)
            assert world.cost_id in caplog.text
            # Final anomaly is an optional view failure. This test calls the
            # Bridge directly, so consume its error rather than claiming success.
            with pytest.raises(ValueError):
                await world.bridge.async_shutdown()
    asyncio.run(scenario())


def test_real_previous_month_start_anchor_is_preserved_as_anomaly(cost_world, caplog):
    async def scenario():
        async with cost_world() as world:
            await world.upsert_bills({"2026-01": 100})
            desired = build_cost_statistics({"2026-01": {"cost_cny": 100}}, dt.date(2026, 10, 3))
            previous = [{**row, "start": row["start"].replace(day=1, hour=0)} for row in desired]
            async_add_external_statistics(world.hass, cost_statistic_metadata(ACCOUNT), previous)
            await world.drain()
            before = await world.query_cost()
            assert before[0]["start"] == dt.datetime(2025, 12, 31, 16, tzinfo=dt.UTC).timestamp()
            await world.sync()
            assert await world.query_cost() == before
            assert not world.imports
            assert world.cost_id in caplog.text
            with pytest.raises(ValueError, match="without a corresponding source fact"):
                await world.bridge.async_shutdown()
    asyncio.run(scenario())
