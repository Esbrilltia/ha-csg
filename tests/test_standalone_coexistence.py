"""M7 real HA registries, private Stores and SQLite coexist with upstream csg.

Only fictional data and the existing synthetic cloud are used. No second cloud
client, copied upstream implementation or production migration is required.
"""

import asyncio
from copy import deepcopy
import datetime as dt
from functools import partial
import hashlib
from pathlib import Path
from unittest.mock import Mock

from homeassistant.components import persistent_notification
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, get_metadata, statistics_during_period,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from custom_components.csg_plus import energy_statistics as energy
from custom_components.csg_plus.const import DOMAIN
from custom_components.csg_plus.cost_statistics import build_cost_statistics, cost_statistic_metadata
from custom_components.csg_plus.energy_statistics import build_statistics, statistic_metadata
from test_energy_statistics_recorder import ACCOUNT, recorder_world
from test_energy_statistics_recovery import platform_world
from test_standalone_identity import old_entry


def test_old_registries_stores_runtime_and_notifications_survive_plus_lifecycle(platform_world, monkeypatch):
    old = old_entry()
    saved = {}
    observed_io = []
    old_runtime = {old.entry_id: {"fictional_owner": object()}}
    digest = hashlib.sha256(ACCOUNT.encode()).hexdigest()
    old_lanes = {f"csg:energy_{digest}": object(), f"csg:cost_{digest}": object()}
    original_lanes = dict(old_lanes)
    legacy_notification = None
    old_device = old_entity = None
    original_entry = deepcopy(old.as_dict())

    async def seed(base):
        nonlocal legacy_notification, old_device, old_entity
        hass = base.hass
        hass.config_entries._entries[old.entry_id] = old
        hass.data["csg"] = old_runtime
        hass.data["csg_energy_import_lanes"] = old_lanes
        old_device = dr.async_get(hass).async_get_or_create(
            config_entry_id=old.entry_id, identifiers={("csg", ACCOUNT)},
            name=f"CSGAccount-{ACCOUNT}", manufacturer="CSG",
        )
        old_entity = er.async_get(hass).async_get_or_create(
            "sensor", "csg", f"csg.{ACCOUNT}.balance", config_entry=old,
            device_id=old_device.id, suggested_object_id=f"{ACCOUNT}_balance",
        )
        # Include both the old entry's suffix and the new entry's suffix, so
        # even an attempted legacy lookup by the new entry ID is caught.
        for entry_id in (old.entry_id, base.entry.entry_id):
            for namespace in ("csg.energy_ledger", "csg.history_store"):
                key = f"{namespace}.{entry_id}"
                store = Store(hass, 1, key)
                await store.async_save({"accounts": {ACCOUNT: {"fictional_legacy_value": 999}}})
                path = Path(store.path)
                saved[key] = (path, path.read_bytes(), path.stat().st_mtime_ns)
        for name in ("async_load", "async_save", "async_remove"):
            original = getattr(Store, name)

            async def guard(self, *args, _name=name, _original=original, **kwargs):
                observed_io.append((_name, self.key))
                assert not self.key.startswith("csg."), f"Old-domain Store accessed: {_name} {self.key}"
                return await _original(self, *args, **kwargs)

            monkeypatch.setattr(Store, name, guard)
        legacy_notification = f"csg_{base.entry.entry_id}_usage_{ACCOUNT}"
        persistent_notification.async_create(hass, "Fictional upstream failure", notification_id=legacy_notification)

    def assert_old_untouched(world):
        assert world.hass.data["csg"] is old_runtime
        assert world.hass.data["csg_energy_import_lanes"] is old_lanes
        assert old_lanes == original_lanes
        assert old.as_dict() == original_entry
        assert world.hass.config_entries.async_entries("csg") == [old]
        assert world.hass.config_entries.async_entries(DOMAIN) == [world.entry]
        assert dr.async_get(world.hass).async_get(old_device.id) is old_device
        assert er.async_get(world.hass).async_get(old_entity.entity_id) is old_entity
        for path, content, mtime in saved.values():
            assert path.read_bytes() == content
            assert path.stat().st_mtime_ns == mtime
        assert not any(key.startswith("csg.") for _, key in observed_io)
        assert legacy_notification in persistent_notification._async_get_or_create_notifications(world.hass)

    def assert_plus_identity(world):
        assert world.entry.domain == "csg_plus"
        runtime = world.runtime()
        assert runtime is not old_runtime[old.entry_id]
        store = runtime["history_store"]
        assert store._store.key == f"csg_plus.history_store.{world.entry.entry_id}"
        assert store._verification_store.key == store._store.key
        assert Path(store._store.path).is_file()
        bridge = runtime["energy_statistics_bridge"]
        assert energy._IMPORT_LANES == "csg_plus_energy_import_lanes"
        assert bridge._lanes is world.hass.data["csg_plus_energy_import_lanes"]
        assert bridge._lanes is not old_lanes
        registry = er.async_get(world.hass)
        device_ids = set()
        for entity in world.component.entities:
            assert entity.unique_id.startswith(f"csg_plus.{ACCOUNT}.")
            registered = registry.async_get(entity.entity_id)
            assert registered.platform == "csg_plus"
            assert registered.config_entry_id == world.entry.entry_id
            assert entity.entity_id != old_entity.entity_id
            device_ids.add(registered.device_id)
        assert len(device_ids) == 1
        new_device = dr.async_get(world.hass).async_get(device_ids.pop())
        assert new_device.id != old_device.id
        assert new_device.identifiers == {("csg_plus", ACCOUNT)}
        assert new_device.name == f"CSG Plus Account-{ACCOUNT}"
        assert new_device.manufacturer == "CSG"

    async def scenario():
        async with platform_world(before_setup=seed) as world:
            assert_old_untouched(world)
            assert_plus_identity(world)
            coordinator = world.runtime()["realtime_coordinator"]
            coordinator._notify_failure(ACCOUNT, "usage", ValueError("fictional error"))
            notifications = persistent_notification._async_get_or_create_notifications(world.hass)
            new_id = f"csg_plus_{world.entry.entry_id}_usage_{ACCOUNT}"
            assert new_id in notifications and legacy_notification in notifications
            assert notifications[new_id]["title"] == "CSG Plus update failed"
            coordinator._clear_failure(ACCOUNT, "usage")
            assert new_id not in notifications
            assert_old_untouched(world)
            lanes = world.runtime()["energy_statistics_bridge"]._lanes
            assert await world.hass.config_entries.async_reload(world.entry.entry_id)
            await world.hass.async_block_till_done()
            await world.sync(world.runtime()["energy_statistics_bridge"])
            assert_plus_identity(world)
            assert world.runtime()["energy_statistics_bridge"]._lanes is lanes
            assert_old_untouched(world)
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            assert world.entry.entry_id not in world.hass.data[DOMAIN]
            assert_old_untouched(world)
    asyncio.run(scenario())


def test_old_energy_and_cost_rows_survive_plus_import_revision_reload_unload(platform_world, monkeypatch):
    monkeypatch.setattr(energy, "_csg_today", lambda: dt.date(2026, 10, 3))
    digest = hashlib.sha256(ACCOUNT.encode()).hexdigest()
    old_ids = set()
    before_rows = before_metadata = None

    async def read_old(world):
        return await world.recorder.async_add_executor_job(
            statistics_during_period, world.hass, energy._QUERY_START, None, old_ids,
            "hour", None, {"state", "sum"},
        )

    async def seed(base):
        nonlocal before_rows, before_metadata
        base.hass.config.currency = "CNY"
        for old_id, metadata, rows in (
            (f"csg:energy_{digest}", statistic_metadata(ACCOUNT), build_statistics({"2026-09-01": {"kwh": 999}})),
            (f"csg:cost_{digest}", cost_statistic_metadata(ACCOUNT), build_cost_statistics(
                {"2026-09": {"cost_cny": 123}}, dt.date(2026, 10, 3),
            )),
        ):
            old_ids.add(old_id)
            async_add_external_statistics(base.hass, {
                **metadata, "source": "csg", "statistic_id": old_id,
                "name": metadata["name"].replace("CSG Plus ", "CSG ", 1),
            }, rows)
        await base.drain()
        before_rows = await read_old(base)
        assert sorted(rows[0]["sum"] for rows in before_rows.values()) == [123, 999]
        before_metadata = await base.recorder.async_add_executor_job(partial(
            get_metadata, base.hass, statistic_ids=old_ids,
        ))
        original_get = energy.get_metadata
        original_query = energy.statistics_during_period
        original_import = energy.async_add_external_statistics

        def guard_get(hass, *args, **kwargs):
            assert not old_ids.intersection(kwargs.get("statistic_ids", set()))
            return original_get(hass, *args, **kwargs)

        def guard_query(hass, start, end, statistic_ids, *args, **kwargs):
            assert not old_ids.intersection(statistic_ids)
            return original_query(hass, start, end, statistic_ids, *args, **kwargs)

        def guard_import(hass, metadata, rows):
            assert metadata["source"] == "csg_plus"
            assert metadata["statistic_id"].startswith("csg_plus:")
            assert metadata["statistic_id"] not in old_ids
            return original_import(hass, metadata, rows)

        monkeypatch.setattr(energy, "get_metadata", guard_get)
        monkeypatch.setattr(energy, "statistics_during_period", guard_query)
        monkeypatch.setattr(energy, "async_add_external_statistics", guard_import)
        for name in ("async_clear_statistics", "async_adjust_statistics"):
            monkeypatch.setattr(Recorder, name, Mock(side_effect=AssertionError(name)))

    async def assert_old_untouched(world):
        assert await read_old(world) == before_rows
        assert await world.recorder.async_add_executor_job(partial(
            get_metadata, world.hass, statistic_ids=old_ids,
        )) == before_metadata

    async def query_new(world):
        new_ids = {statistic_metadata(ACCOUNT)["statistic_id"], cost_statistic_metadata(ACCOUNT)["statistic_id"]}
        result = await world.recorder.async_add_executor_job(
            statistics_during_period, world.hass, energy._QUERY_START, None, new_ids,
            "hour", None, {"state", "sum"},
        )
        metadata = await world.recorder.async_add_executor_job(partial(
            get_metadata, world.hass, statistic_ids=new_ids | old_ids,
        ))
        assert set(metadata) == new_ids | old_ids
        assert not new_ids.intersection(old_ids)
        assert {key.partition(":")[2] for key in new_ids} == {key.partition(":")[2] for key in old_ids}
        assert all(metadata[key][1]["source"] == "csg_plus" for key in new_ids)
        return result

    async def scenario():
        async with platform_world(before_setup=seed) as world:
            await assert_old_untouched(world)
            store = world.runtime()["history_store"]
            await store.async_upsert_monthly_bill(ACCOUNT, (2026, 9), usage_kwh=None, cost_cny=20)
            await world.sync(world.runtime()["energy_statistics_bridge"])
            rows = await query_new(world)
            assert rows[statistic_metadata(ACCOUNT)["statistic_id"]][0]["sum"] == 1
            assert rows[cost_statistic_metadata(ACCOUNT)["statistic_id"]][0]["sum"] == 20
            await assert_old_untouched(world)
            world.cloud.value = 3
            await world.runtime()["realtime_coordinator"].async_refresh()
            await store.async_upsert_monthly_bill(ACCOUNT, (2026, 9), usage_kwh=None, cost_cny=18)
            await world.sync(world.runtime()["energy_statistics_bridge"])
            rows = await query_new(world)
            assert rows[statistic_metadata(ACCOUNT)["statistic_id"]][0]["sum"] == 3
            assert rows[cost_statistic_metadata(ACCOUNT)["statistic_id"]][0]["sum"] == 18
            await assert_old_untouched(world)
            assert await world.hass.config_entries.async_reload(world.entry.entry_id)
            await world.hass.async_block_till_done()
            await world.sync(world.runtime()["energy_statistics_bridge"])
            assert await query_new(world) == rows
            await assert_old_untouched(world)
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            await assert_old_untouched(world)
    asyncio.run(scenario())
