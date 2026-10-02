"""Whole-month costs through HA 2026.9.3's real import and aggregation APIs."""

from __future__ import annotations

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.energy import data as energy_data, validate as energy_validate
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util

from test_cost_statistics_recorder import cost_world
from test_energy_statistics_recorder import recorder_world


@pytest.mark.parametrize("zone", [
    "Asia/Shanghai", "UTC", "America/Los_Angeles", "Pacific/Kiritimati", "Pacific/Pago_Pago",
])
def test_real_monthly_cost_attribution_delta_revisions_and_energy_validation(cost_world, zone):
    async def scenario():
        async with cost_world() as world:
            await world.hass.config.async_set_time_zone(zone)
            tz = ZoneInfo(zone)
            assert world.hass.config.time_zone == zone
            assert dt_util.get_default_time_zone() == tz
            await world.upsert({"2026-01-01": 10})
            await world.upsert_bills({
                "2025-12": 40, "2026-01": 100, "2026-03": 150,
                "2026-04": 0, "2026-05": None, "2026-10": 20,
            })
            await world.sync()

            async def query(period):
                return (await world.recorder.async_add_executor_job(
                    statistics_during_period, world.hass,
                    dt.datetime(2025, 1, 1, tzinfo=dt.UTC), None,
                    {world.cost_id}, period, None, {"state", "sum", "change"},
                ))[world.cost_id]

            async def months():
                return [(dt.datetime.fromtimestamp(row["start"], tz).strftime("%Y-%m"), row["change"])
                        for row in await query("month")]

            async def years():
                return [(dt.datetime.fromtimestamp(row["start"], tz).year, row["change"])
                        for row in await query("year")]

            hourly = await query("hour")
            assert [row["start"] for row in hourly] == [
                dt.datetime(year, month, 15, 4, tzinfo=dt.UTC).timestamp()
                for year, month in ((2025, 12), (2026, 1), (2026, 3), (2026, 4))
            ]
            assert [(row["state"], row["sum"]) for row in hourly] == [
                (40, 40), (100, 140), (150, 290), (0, 290),
            ]
            assert await months() == [("2025-12", 40), ("2026-01", 100), ("2026-03", 150), ("2026-04", 0)]
            assert await years() == [(2025, 40), (2026, 250)]

            await world.upsert_bills({"2026-02": 120})
            await world.sync()
            assert await months() == [
                ("2025-12", 40), ("2026-01", 100), ("2026-02", 120), ("2026-03", 150), ("2026-04", 0),
            ]
            assert await years() == [(2025, 40), (2026, 370)]
            for revision in (98, 103):
                await world.upsert_bills({"2026-01": revision})
                await world.sync()
                assert await months() == [
                    ("2025-12", 40), ("2026-01", revision), ("2026-02", 120), ("2026-03", 150), ("2026-04", 0),
                ]
                assert await years() == [(2025, 40), (2026, revision + 270)]
                assert [row["sum"] for row in await query("hour")] == [
                    40, 40 + revision, 160 + revision, 310 + revision, 310 + revision,
                ]
            count = len(world.imports)
            await world.sync()
            assert len(world.imports) == count

            manager = await energy_data.async_get_manager(world.hass)
            # Only isolated synthetic test preferences pair these IDs.
            manager.data = {"energy_sources": [{
                "type": "grid", "stat_energy_from": world.statistic_id, "stat_cost": world.cost_id,
            }], "device_consumption": []}
            assert (await energy_validate.async_validate(world.hass)).as_dict() == {
                "energy_sources": [[]], "device_consumption": [], "device_consumption_water": [],
            }
    asyncio.run(scenario())
