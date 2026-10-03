"""M5 retires A3/A4/A5 by design; it does not patch their algorithms.

Use real HA 2026.9.3 platform lifecycles, temporary Store files and isolated
SQLite. All accounts, usage, costs and legacy data are fictional.
"""

import ast
import asyncio
from copy import deepcopy
import datetime as dt
import hashlib
import inspect
from pathlib import Path
from unittest.mock import Mock

import pytest
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
from homeassistant.helpers import entity_registry as er, storage as ha_storage

from custom_components.csg_plus import energy_statistics as energy, sensor
from custom_components.csg_plus.const import CONF_ENERGY_STATISTICS_ENABLED, CONF_SETTINGS, DOMAIN
from custom_components.csg_plus.energy_statistics import EnergyStatisticsBridge
from test_energy_statistics import rig
from test_energy_statistics_recorder import ACCOUNT, START, recorder_world
from test_energy_statistics_recovery import platform_world

ROOT = Path(__file__).parents[1] / "custom_components" / "csg_plus"
EXPECTED_SUFFIXES = {
    "yesterday_kwh", "balance", "arrears", "current_ladder",
    "current_ladder_remaining_kwh", "current_ladder_tariff",
    "latest_settlement_day_kwh", "latest_settlement_day_cost",
    "this_month_total_usage", "this_month_total_cost",
    "last_month_total_usage", "last_month_total_cost",
    "this_year_total_usage", "this_year_total_cost",
    "last_year_total_usage", "last_year_total_cost",
}


def test_client_demo_uses_approved_daily_usage_api_without_retired_calls():
    source = (ROOT / "csg_client_demo.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "get_month_daily_usage_detail" in calls
    assert calls.isdisjoint({
        "get_month_daily_cost_detail", "get_yesterday_kwh", "api_query_day_electric_charge_by_m_point",
    })
    assert "queryDayElectricChargeByMPoint" not in source


@pytest.mark.parametrize("forbidden", [
    "EnergyLedger", "ENERGY_TOTAL", "SETTLED_COST_TOTAL",
    "SUFFIX_ENERGY_TOTAL", "SUFFIX_SETTLED_COST_TOTAL",
    "async_adjust_statistics", "pending_corrections", "energy_total", "settled_cost_total",
    "energy_total_at", "energy_counted_at", "energy_started_on",
    "reported_realtime", "counted_realtime", "counted_at",
    "async_acknowledge_corrections", "async_record_realtime", "async_record_billing",
    "_ramp_fraction", "_handle_interpolation_tick", "_unsub_interpolation",
    "_async_correct_statistics", "_async_statistic_sum_at", "_statistic_id",
])
def test_retired_production_symbols_and_fields_are_absent(forbidden):
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = getattr(node, "id", getattr(node, "attr", getattr(node, "name", None)))
            assert name != forbidden, (path, node.lineno, forbidden)
            if isinstance(node, ast.Constant):
                assert node.value != forbidden, (path, node.lineno, forbidden)


def test_sensor_has_no_recorder_registry_or_storage_dependency():
    source = (ROOT / "sensor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not any(part in (node.module or "") for part in ("recorder", "storage", "entity_registry"))
        if isinstance(node, ast.Attribute):
            assert node.attr != "TOTAL_INCREASING"
    assert "energy_ledger" not in "".join(p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py"))
    for kind in (sensor.CSGCoordinator, sensor.RealtimeCoordinator, sensor.CurrentCoordinator, sensor.BillingCoordinator):
        assert "ledger" not in inspect.signature(kind).parameters


@pytest.mark.parametrize("enabled", [None, True, False], ids=["old-missing", "explicit-on", "explicit-off"])
def test_bridge_setting_missing_is_on_and_explicit_off_has_zero_activity(rig, enabled):
    async def scenario():
        await rig.upsert({"2026-09-01": 2})
        if enabled is None:
            rig.entry.data[CONF_SETTINGS].pop(CONF_ENERGY_STATISTICS_ENABLED)
        else:
            rig.entry.data[CONF_SETTINGS][CONF_ENERGY_STATISTICS_ENABLED] = enabled
        worker = EnergyStatisticsBridge(rig.hass, rig.entry, rig.store)
        assert worker.enabled is (enabled is not False)
        await rig.sync(worker)
        await worker.async_shutdown()
        if enabled is False:
            assert not rig.recorder.reads and not rig.recorder.imports and not rig.tasks
        else:
            assert rig.recorder.reads and rig.recorder.imports
            assert rig.recorder.rows[rig.statistic_id][0]["sum"] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [None, True, False], ids=["old-missing", "explicit-on", "explicit-off"])
def test_real_sensor_entity_set_and_only_yesterday_timer(platform_world, monkeypatch, enabled):
    intervals = []
    track = sensor.async_track_time_interval

    def register(hass, callback, interval):
        intervals.append(interval)
        return track(hass, callback, interval)

    monkeypatch.setattr(sensor, "async_track_time_interval", register)
    adjust = Mock(side_effect=AssertionError("Retired sensor statistics adjustment"))
    monkeypatch.setattr(Recorder, "async_adjust_statistics", adjust)
    disabled_calls = []

    async def configure(base):
        settings = {**base.entry.data[CONF_SETTINGS]}
        if enabled is None:
            settings.pop(CONF_ENERGY_STATISTICS_ENABLED)
        else:
            settings[CONF_ENERGY_STATISTICS_ENABLED] = enabled
        base.hass.config_entries.async_update_entry(base.entry, data={**base.entry.data, CONF_SETTINGS: settings})
        if enabled is False:
            for name in ("get_instance", "statistics_during_period", "async_add_external_statistics"):
                stub = Mock(side_effect=AssertionError("Explicit off touched Recorder"))
                monkeypatch.setattr(energy, name, stub)
                disabled_calls.append(stub)

    async def scenario():
        async with platform_world(before_setup=configure) as world:
            actual = {entity.unique_id for entity in world.component.entities}
            assert actual == {f"csg_plus.{ACCOUNT}.{suffix}" for suffix in EXPECTED_SUFFIXES}
            assert not any(entity.state_class and entity.state_class.value == "total_increasing" for entity in world.component.entities)
            assert intervals == [dt.timedelta(minutes=1)]
            runtime = world.runtime()
            assert runtime["energy_statistics_bridge"].enabled is (enabled is not False)
            for key in ("realtime_coordinator", "billing_coordinator"):
                assert not hasattr(runtime[key], "ledger")
                await runtime[key].async_refresh()
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            adjust.assert_not_called()
            for stub in disabled_calls:
                stub.assert_not_called()
    asyncio.run(scenario())


def test_real_legacy_store_bytes_registry_and_recorder_history_untouched(platform_world, monkeypatch):
    legacy = {}
    touched = []

    async def seed(base):
        key = f"csg.energy_ledger.{base.entry.entry_id}"
        store = ha_storage.Store(base.hass, 1, key)
        await store.async_save({"accounts": {ACCOUNT: {
            "energy_total": 999, "settled_cost_total": 123,
            "realtime": {"2026-09-01": 999}, "pending_corrections": {"synthetic": []},
        }}})
        path = Path(store.path)
        legacy.update(path=path, data=path.read_bytes(), mtime=path.stat().st_mtime_ns)
        registry = er.async_get(base.hass)
        ids = []
        for suffix, value, unit, unit_class in (
            ("energy_total", 999, "kWh", "energy"),
            ("settled_cost_total", 123, "CNY", None),
        ):
            registered = registry.async_get_or_create(
                "sensor", DOMAIN, f"csg_plus.{ACCOUNT}.{suffix}",
                suggested_object_id=f"synthetic_legacy_{suffix}", config_entry=base.entry,
            )
            ids.append(registered.entity_id)
            async_import_statistics(base.hass, {
                "source": "recorder", "statistic_id": registered.entity_id,
                "name": "Synthetic legacy total", "unit_of_measurement": unit,
                "unit_class": unit_class, "has_sum": True, "mean_type": StatisticMeanType.NONE,
            }, [{"start": START, "state": value, "sum": value}])
        await base.drain()
        legacy["ids"] = ids

        async def read_old():
            return await base.recorder.async_add_executor_job(
                statistics_during_period, base.hass, START, None, set(ids), "hour", None, {"state", "sum"},
            )

        legacy["read"] = read_old
        legacy["rows"] = await read_old()
        assert [legacy["rows"][entity_id][0]["sum"] for entity_id in ids] == [999, 123]
        for name in ("async_load", "async_save", "async_remove"):
            original = getattr(ha_storage.Store, name)

            async def guard(self, *args, _original=original, _name=name, **kwargs):
                if self.key == key:
                    touched.append(_name)
                    pytest.fail(f"Retired Store {_name} was called")
                return await _original(self, *args, **kwargs)

            monkeypatch.setattr(ha_storage.Store, name, guard)

    async def assert_untouched(world):
        assert not touched
        assert hashlib.sha256(legacy["path"].read_bytes()).digest() == hashlib.sha256(legacy["data"]).digest()
        assert legacy["path"].read_bytes() == legacy["data"]
        assert legacy["path"].stat().st_mtime_ns == legacy["mtime"]
        assert await legacy["read"]() == legacy["rows"]
        registry = er.async_get(world.hass)
        for suffix, entity_id in zip(("energy_total", "settled_cost_total"), legacy["ids"], strict=True):
            assert registry.async_get_entity_id("sensor", DOMAIN, f"csg_plus.{ACCOUNT}.{suffix}") == entity_id

    async def scenario():
        async with platform_world(before_setup=seed) as world:
            await assert_untouched(world)
            assert [row["sum"] for row in await world.query()] == [1]
            world.cloud.value = 3
            for key in ("realtime_coordinator", "billing_coordinator"):
                await world.runtime()[key].async_refresh()
            await world.sync(world.runtime()["energy_statistics_bridge"])
            assert [row["sum"] for row in await world.query()] == [3]
            await assert_untouched(world)
            assert await world.hass.config_entries.async_reload(world.entry.entry_id)
            await world.hass.async_block_till_done()
            await assert_untouched(world)
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            await assert_untouched(world)
    asyncio.run(scenario())


def test_real_sensor_producers_external_initial_revision_hole_fill_and_restart(platform_world, monkeypatch):
    adjust = Mock(side_effect=AssertionError("Retired sensor statistics adjustment"))
    monkeypatch.setattr(Recorder, "async_adjust_statistics", adjust)

    async def scenario():
        async with platform_world() as world:
            facts = {"2026-09-01": 10, "2026-09-02": 0, "2026-09-04": 5}

            def daily(account, month):
                rows = [{"date": day, "kwh": value} for day, value in facts.items()] if month == (2026, 9) else []
                return sum(row["kwh"] for row in rows), deepcopy(rows)

            world.cloud.get_month_daily_usage_detail = daily
            preferences = Path(world.hass.config.path(".storage", "energy"))
            before = preferences.read_bytes() if preferences.exists() else None

            async def refresh(kind):
                runtime = world.runtime()
                await runtime[f"{kind}_coordinator"].async_refresh()
                await world.sync(runtime["energy_statistics_bridge"])
                return [(row["state"], row["sum"]) for row in await world.query()]

            assert await refresh("realtime") == [(10, 10), (0, 10), (5, 15)]
            facts["2026-09-01"] = 8
            assert await refresh("billing") == [(8, 8), (0, 8), (5, 13)]
            facts["2026-09-03"] = 7
            assert await refresh("billing") == [(8, 8), (0, 8), (7, 15), (5, 20)]
            count = len(world.imports)
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            assert await world.hass.config_entries.async_setup(world.entry.entry_id)
            await world.hass.async_block_till_done()
            await world.sync(world.runtime()["energy_statistics_bridge"])
            assert [(row["state"], row["sum"]) for row in await world.query()] == [(8, 8), (0, 8), (7, 15), (5, 20)]
            assert len(world.imports) == count
            assert (preferences.read_bytes() if preferences.exists() else None) == before
            adjust.assert_not_called()
    asyncio.run(scenario())
