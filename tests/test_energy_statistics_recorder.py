"""HA 2026.9.3's real Recorder thread, ImportStatisticsTask and temporary SQLite.

Only bootstrap facilities and synthetic facts are supplied. Statistics writes
and reads use HA public APIs; no SQL or private insert/update is used.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant import loader
from homeassistant.components.energy import data as energy_data, validate as energy_validate
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, get_metadata, list_statistic_ids, statistics_during_period,
)
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.helpers import frame
from homeassistant.helpers.recorder import async_initialize_recorder
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.csg import energy_statistics as module
from custom_components.csg.const import CONF_ELE_ACCOUNTS, CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS
from custom_components.csg.csg_client import CSGElectricityAccount
from custom_components.csg.energy_statistics import EnergyStatisticsBridge, build_statistics, statistic_metadata
from custom_components.csg.history_store import CSGHistoryStore

ACCOUNT = "fictional-recorder-account"
SHANGHAI = ZoneInfo("Asia/Shanghai")
START = dt.datetime(2026, 9, 1, tzinfo=SHANGHAI)


@pytest.fixture
def recorder_world(tmp_path, monkeypatch):
    if not hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: None, raising=False)

    @asynccontextmanager
    async def world():
        old_zone = dt_util.get_default_time_zone()
        dt_util.set_default_time_zone(SHANGHAI)
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        frame.async_setup(hass)
        async_initialize_recorder(hass)
        hass.config_entries = ConfigEntries(hass, {})
        store = CSGHistoryStore(hass, "synthetic-energy-entry")
        entry = ConfigEntry(
            version=1, minor_version=1, domain="csg", title="Synthetic energy",
            data={CONF_SETTINGS: {CONF_ENERGY_STATISTICS_ENABLED: True},
                  CONF_ELE_ACCOUNTS: {ACCOUNT: CSGElectricityAccount(ACCOUNT).dump()}},
            options={}, source="user", unique_id=None, discovery_keys={}, subentries_data=None,
            entry_id="synthetic-energy-entry",
        )
        bridge = EnergyStatisticsBridge(hass, entry, store)
        imports = []
        def import_rows(hass, metadata, rows):
            imports.append((deepcopy(metadata), deepcopy(rows)))
            async_add_external_statistics(hass, metadata, rows)
        monkeypatch.setattr(module, "async_add_external_statistics", import_rows)
        try:
            assert await async_setup_component(hass, "recorder", {"recorder": {
                "db_url": f"sqlite:///{tmp_path.as_posix()}/recorder.db",
                "auto_purge": False, "auto_repack": False, "commit_interval": 0,
            }})
            await hass.async_start()
            recorder = get_instance(hass)
            await asyncio.wait_for(recorder.async_recorder_ready.wait(), 10)
            await store.async_load()
            statistic_id = statistic_metadata(ACCOUNT)["statistic_id"]

            async def drain():
                # async_block_till_done can see an empty queue while the import
                # is executing. This unconditional test-only queue barrier waits
                # behind it; public queries below verify the database result.
                await hass.async_add_executor_job(recorder.block_till_done)
                await recorder.async_block_till_done()

            async def sync(worker=bridge):
                worker.request_sync()
                await worker._task
                await drain()

            async def upsert(values):
                for day, value in values.items():
                    date = dt.date.fromisoformat(day)
                    await store.async_upsert_daily_usage(ACCOUNT, (date.year, date.month), [{"date": day, "kwh": value}])

            async def query(period="hour", types=None, start=START):
                return (await recorder.async_add_executor_job(
                    statistics_during_period, hass, start, None, {statistic_id}, period,
                    {"energy": "kWh"}, types or {"state", "sum"},
                )).get(statistic_id, [])

            yield SimpleNamespace(hass=hass, store=store, bridge=bridge, entry=entry,
                                  recorder=recorder, imports=imports, sync=sync, upsert=upsert,
                                  query=query, drain=drain, statistic_id=statistic_id)
        finally:
            await bridge.async_shutdown()
            await entry._async_process_on_unload(hass)
            await hass.async_stop(force=True)
            get_instance.cache_clear()
            dt_util.set_default_time_zone(old_zone)
    return world


def test_real_insert_same_start_update_repeat_append_revision_and_hole_fill(recorder_world):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({"2026-09-01": 10, "2026-09-02": 0, "2026-09-04": 5})
            await world.sync()
            rows = await world.query()
            assert len(rows) == 3
            assert [row["state"] for row in rows] == [10, 0, 5]
            assert [row["sum"] for row in rows] == [10, 10, 15]
            assert rows[0]["start"] == START.astimezone(dt.UTC).timestamp()
            assert world.imports[0][1][0]["start"].tzinfo is SHANGHAI
            metadata = await world.recorder.async_add_executor_job(partial(
                get_metadata, world.hass, statistic_ids={world.statistic_id},
            ))
            assert {key: metadata[world.statistic_id][1][key] for key in statistic_metadata(ACCOUNT)} == statistic_metadata(ACCOUNT)
            await world.sync()
            assert len(world.imports) == 1
            await world.upsert({"2026-09-05": 8})
            await world.sync()
            assert len(world.imports[-1][1]) == 1
            await world.upsert({"2026-09-04": 4})
            await world.sync()
            assert [(row["state"], row["sum"]) for row in world.imports[-1][1]] == [(4, 14), (8, 22)]
            rows = await world.query()
            assert len(rows) == 4 and [row["sum"] for row in rows] == [10, 10, 14, 22]
            await world.upsert({"2026-09-03": 7})
            await world.sync()
            rows = await world.query()
            assert len(rows) == 5
            assert [row["state"] for row in rows] == [10, 0, 7, 4, 8]
            assert [row["sum"] for row in rows] == [10, 10, 17, 21, 29]
            assert [row["change"] for row in await world.query(types={"change"})] == [10, 0, 7, 4, 8]
            assert await world.query(period="5minute") == []
            count = len(world.imports)
            await world.sync()
            assert len(world.imports) == count
    asyncio.run(scenario())


@pytest.mark.parametrize("period,expected", [("day", [10, 0, 5]), ("month", [15]), ("year", [15])])
def test_real_energy_aggregates_preserve_usage_without_hourly_fabrication(recorder_world, period, expected):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({"2026-09-01": 10, "2026-09-02": 0, "2026-09-04": 5})
            await world.sync()
            assert [row["change"] for row in await world.query(period=period, types={"change"})] == expected
    asyncio.run(scenario())


def test_real_restart_finds_uncommitted_part_of_suffix(recorder_world):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({"2026-09-01": 10, "2026-09-02": 8, "2026-09-03": 5, "2026-09-04": 3})
            await world.sync()
            await world.upsert({"2026-09-02": 7})
            desired = build_statistics(await world.store.async_daily_usage_snapshot(ACCOUNT))
            # Simulate a restart after only the first revised row reached DB.
            # This still goes through the real public API and import queue.
            async_add_external_statistics(world.hass, statistic_metadata(ACCOUNT), desired[1:2])
            await world.drain()
            await world.bridge.async_shutdown()
            restarted = EnergyStatisticsBridge(world.hass, world.entry, world.store)
            await world.sync(restarted)
            assert [row["state"] for row in world.imports[-1][1]] == [5, 3]
            assert [row["sum"] for row in await world.query()] == [10, 17, 22, 25]
            count = len(world.imports)
            await world.sync(restarted)
            assert len(world.imports) == count
            await restarted.async_shutdown()
    asyncio.run(scenario())


def test_real_statistics_listing_and_energy_validation_accept_external_without_entity(recorder_world):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({"2026-09-01": 12})
            await world.sync()
            listed = await world.recorder.async_add_executor_job(
                list_statistic_ids, world.hass, None, "sum",
            )
            assert len(listed) == 1
            assert listed[0]["statistic_id"] == world.statistic_id
            assert listed[0]["source"] == "csg" and listed[0]["has_sum"] is True
            assert listed[0]["unit_class"] == "energy" and listed[0]["statistics_unit_of_measurement"] == "kWh"
            assert not valid_entity_id(world.statistic_id) and world.hass.states.get(world.statistic_id) is None
            manager = await energy_data.async_get_manager(world.hass)
            # Test-only preferences in this isolated HA. Production Bridge never
            # accesses or writes Energy preferences or creates a sensor entity.
            manager.data = {"energy_sources": [{"type": "grid", "stat_energy_from": world.statistic_id}],
                            "device_consumption": []}
            assert (await energy_validate.async_validate(world.hass)).as_dict() == {
                "energy_sources": [[]], "device_consumption": [], "device_consumption_water": [],
            }
    asyncio.run(scenario())


def test_real_disabling_preserves_existing_statistics(recorder_world):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({"2026-09-01": 12})
            await world.sync()
            before = await world.query()
            world.entry.data[CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] = False
            disabled = EnergyStatisticsBridge(world.hass, world.entry, world.store)
            disabled.request_sync()
            assert disabled._task is None
            assert len(world.imports) == 1 and await world.query() == before
            await disabled.async_shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("anomaly", ["extra", "metadata"])
def test_real_recorder_anomalies_preserve_existing_statistics(recorder_world, anomaly):
    async def scenario():
        async with recorder_world() as world:
            await world.upsert({"2026-09-01": 2})
            metadata = statistic_metadata(ACCOUNT)
            values = {"2026-09-01": {"kwh": 2}}
            if anomaly == "extra":
                values["2026-09-02"] = {"kwh": 3}
            else:
                metadata["unit_of_measurement"] = "Wh"
            async_add_external_statistics(world.hass, metadata, build_statistics(values))
            await world.drain()
            before = await world.query()
            await world.sync()
            assert not world.imports and await world.query() == before
            actual = await world.recorder.async_add_executor_job(partial(
                get_metadata, world.hass, statistic_ids={world.statistic_id},
            ))
            assert {key: actual[world.statistic_id][1][key] for key in metadata} == metadata
    asyncio.run(scenario())
