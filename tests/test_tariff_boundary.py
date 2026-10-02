"""Real HA entity and time tracker: TOU updates locally across Shanghai bounds."""

import asyncio
from copy import deepcopy
import datetime as dt
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import event
from homeassistant.util import dt as dt_util

from custom_components.csg import sensor
from custom_components.csg.const import CONF_ELE_ACCOUNTS, CONF_SETTINGS, CONF_TARIFF_PROFILES
from test_energy_statistics_recorder import ACCOUNT, recorder_world
from test_energy_statistics_recovery import platform_world


def test_tou_entity_tracks_shanghai_boundaries_without_cloud_refresh_and_cleans_up(platform_world, monkeypatch):
    clock = [dt.datetime(2026, 9, 3, 7, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))]
    monkeypatch.setattr(sensor, "_csg_now", lambda: clock[0])

    async def configure(base):
        accounts = deepcopy(base.entry.data[CONF_ELE_ACCOUNTS])
        accounts[ACCOUNT]["area_code"] = "080000"
        base.hass.config_entries.async_update_entry(base.entry, data={
            **base.entry.data, CONF_ELE_ACCOUNTS: accounts,
            CONF_SETTINGS: {**base.entry.data[CONF_SETTINGS], CONF_TARIFF_PROFILES: {
                ACCOUNT: {"scheme": "ladder", "multi_person": False, "tou": True},
            }},
        })

    async def scenario():
        async with platform_world(before_setup=configure) as world:
            # HA's display zone does not determine Guangzhou's billing clock.
            dt_util.set_default_time_zone(ZoneInfo("America/Los_Angeles"))
            tariff = next(entity for entity in world.component.entities if entity.unique_id.endswith(".current_ladder_tariff"))
            assert tariff.native_value == 0.29876875
            tracker = tariff._unsub_tariff_boundary.__self__
            cloud = Mock(side_effect=AssertionError("TOU boundary requested cloud data"))
            monkeypatch.setattr(world.cloud, "get_month_daily_usage_detail", cloud)
            for hour, rate in [(8, 0.58886875), (10, 0.96596875), (12, 0.58886875), (14, 0.96596875), (19, 0.58886875), (0, 0.29876875)]:
                clock[0] = clock[0].replace(hour=hour, minute=0, second=0)
                # Exercise HA's real registered timer/job at its next boundary,
                # advancing the clock instead of waiting hours of wall time.
                previous = clock[0] - dt.timedelta(seconds=1)
                assert tracker._calculate_next(previous.astimezone(dt.UTC)).replace(microsecond=0) == clock[0].astimezone(dt.UTC)
                tracker._cancel_callback()
                monkeypatch.setattr(event, "time_tracker_utcnow", lambda: clock[0].astimezone(dt.UTC))
                tracker._pattern_time_change_listener(clock[0].astimezone(dt.UTC))
                assert tariff.native_value == rate
                assert world.hass.states.get(tariff.entity_id).state == str(rate)
            cloud.assert_not_called()
            clock[0] = clock[0].replace(month=10, day=1)
            tariff._handle_tariff_boundary(clock[0])
            assert not tariff.available and world.hass.states.get(tariff.entity_id).state == STATE_UNAVAILABLE
            assert await world.hass.config_entries.async_unload(world.entry.entry_id)
            assert tariff._unsub_tariff_boundary is None
            assert tracker._cancel_callback.__self__._cancel_callback.cancelled()
    asyncio.run(scenario())
