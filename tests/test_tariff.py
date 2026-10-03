"""Explicit current policy combinations, Decimal thresholds and complete dates."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.components.sensor import SensorStateClass

from custom_components.csg_plus import sensor
from custom_components.csg_plus.const import (
    ATTR_KEY_CURRENT_LADDER_START_DATE, CONF_SETTINGS, CONF_TARIFF_PROFILES,
    SUFFIX_CURRENT_LADDER, SUFFIX_CURRENT_LADDER_REMAINING_KWH, SUFFIX_CURRENT_LADDER_TARIFF,
)
from custom_components.csg_plus.tariff import (
    BASE_EX_FUNDS, FIXED_ADDONS, FLAT_RATIO, PEAK_RATIO, VALLEY_RATIO,
    GUANGZHOU_PROFILES, current_ladder, resolve_tariff_profile, tou_period, validate_tariff_selection,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 9, 3, 10, tzinfo=SHANGHAI)
CHOICES = [{"scheme": key[0], "multi_person": key[1], "tou": key[2]} for key in GUANGZHOU_PROFILES]


def profile(multi=False, tou=False, combined=False):
    return resolve_tariff_profile("080000", {"scheme": "combined" if combined else "ladder", "multi_person": multi, "tou": tou})


@pytest.mark.parametrize("selection,expected", [
    (None, None), ({}, None), ({"scheme": "unconfigured"}, None),
    ({"scheme": "ladder", "multi_person": False, "tou": False}, "guangzhou_standard"),
    ({"scheme": "ladder", "multi_person": True, "tou": False}, "guangzhou_multi_person"),
    ({"scheme": "ladder", "multi_person": False, "tou": True}, "guangzhou_tou"),
    ({"scheme": "ladder", "multi_person": True, "tou": True}, "guangzhou_multi_person_tou"),
    ({"scheme": "combined", "multi_person": False, "tou": False}, "guangzhou_combined"),
])
def test_explicit_combinations_and_no_regional_default(selection, expected):
    resolved = resolve_tariff_profile("080000", selection)
    assert (resolved.profile_id if resolved else None) == expected
    assert resolve_tariff_profile("030000", selection) is None
    assert resolve_tariff_profile("", selection) is None
    if resolved:
        assert resolved.area_code == "080000" and resolved.customer_type == "residential"
        assert resolved.billing_period == "calendar_month"
        assert isinstance(resolved.effective_from, dt.date)
        assert all(isinstance(value, Decimal) for value in resolved.rate_table.values())
        assert all(any(identifier in source for source in resolved.source) for identifier in ("135", "498", "826", "553", "331"))


@pytest.mark.parametrize("choice", [
    {"scheme": "combined", "tou": True}, {"scheme": "combined", "multi_person": True},
    {"scheme": "unconfigured", "tou": True}, {"scheme": "unconfigured", "multi_person": True},
    {"scheme": "unknown"}, {"scheme": "ladder", "tou": "false"},
    {"scheme": "ladder", "multi_person": 1}, {"scheme": []},
])
def test_unsupported_combinations_fail_closed(choice):
    with pytest.raises((ValueError, TypeError)):
        validate_tariff_selection(choice)
    assert resolve_tariff_profile("080000", choice) is None


@pytest.mark.parametrize("month,multi,first,second", [
    (5, False, 260, 600), (10, False, 260, 600), (1, False, 200, 400), (11, False, 200, 400),
    (5, True, 360, 700), (10, True, 360, 700), (1, True, 300, 500), (11, True, 300, 500),
])
@pytest.mark.parametrize("boundary", ["first", "above-first", "second", "above-second"])
def test_decimal_threshold_boundaries(month, multi, first, second, boundary):
    policy = profile(multi=multi)
    assert policy.thresholds(month) == (Decimal(first), Decimal(second))
    values = {"first": Decimal(first), "above-first": Decimal(first) + Decimal("0.0000001"), "second": Decimal(second), "above-second": Decimal(second) + Decimal("0.0000001")}
    usage = values[boundary]
    result = current_ladder(policy, dt.date(2026, month, 3), usage, [])
    expected_tier = {"first": 1, "above-first": 2, "second": 2, "above-second": 3}[boundary]
    assert result.tier == expected_tier
    assert result.remaining_kwh == (Decimal(first if expected_tier == 1 else second) - usage if expected_tier < 3 else None)
    assert result.start_date is None


@pytest.mark.parametrize("clock,expected", [
    ("00:00:00", "valley"), ("07:59:59", "valley"), ("08:00:00", "flat"),
    ("09:59:59", "flat"), ("10:00:00", "peak"), ("11:59:59", "peak"),
    ("12:00:00", "flat"), ("13:59:59", "flat"), ("14:00:00", "peak"),
    ("18:59:59", "peak"), ("19:00:00", "flat"), ("23:59:59", "flat"),
])
def test_tou_boundaries_and_timezone_conversion(clock, expected):
    now = dt.datetime.combine(NOW.date(), dt.time.fromisoformat(clock), SHANGHAI)
    assert tou_period(now) == expected
    assert tou_period(now.astimezone(ZoneInfo("America/Los_Angeles"))) == expected
    rates = {"peak": Decimal("0.99500875"), "flat": Decimal("0.58886875"), "valley": Decimal("0.22914475")}
    for multi in (False, True):
        policy = profile(multi=multi, tou=True)
        assert [policy.current_rate(tier, now) for tier in (1, 2, 3)] == [rates[expected] + surcharge for surcharge in (Decimal(0), Decimal("0.05"), Decimal("0.30"))]


def test_tou_components_exclude_funds_from_ratios_and_match_current_policy():
    policy = profile(tou=True)
    assert BASE_EX_FUNDS == Decimal("0.5802")
    assert FIXED_ADDONS == Decimal("0.00866875")
    assert (PEAK_RATIO, FLAT_RATIO, VALLEY_RATIO) == (Decimal("1.7"), Decimal("1"), Decimal("0.38"))
    expected = {"peak": Decimal("0.99500875"), "flat": Decimal("0.58886875"), "valley": Decimal("0.22914475")}
    for period, ratio in (("peak", PEAK_RATIO), ("flat", FLAT_RATIO), ("valley", VALLEY_RATIO)):
        assert policy.rate_table[period] == expected[period] == BASE_EX_FUNDS * ratio + FIXED_ADDONS
        if period != "flat":
            assert policy.rate_table[period] != policy.rate_table["flat"] * ratio
    assert policy.effective_from == dt.date(2021, 10, 1)
    assert any("331" in source and "1.7:1:0.38" in source for source in policy.source)
    assert any("2024" in source for source in policy.source)


def test_final_ordinary_rates_and_combined_have_no_tou_change():
    ordinary = profile()
    assert [ordinary.current_rate(tier, NOW) for tier in (1, 2, 3)] == [Decimal("0.58886875"), Decimal("0.63886875"), Decimal("0.88886875")]
    combined = profile(combined=True)
    assert not combined.ladder_enabled and not combined.tou_enabled
    assert combined.multi_person_allowance == 0
    assert combined.current_rate(None, NOW) == Decimal("0.62586875")
    with pytest.raises(ValueError, match="no ladder"):
        current_ladder(combined, NOW.date(), 500, [])


def test_daily_holes_keep_authoritative_tier_and_remaining_but_hide_start_date():
    daily = [{"date": "2026-09-01", "kwh": 250}, {"date": "2026-09-03", "kwh": 20}]
    missing = current_ladder(profile(), NOW.date(), 270, daily)
    assert (missing.tier, missing.remaining_kwh, missing.start_date) == (2, Decimal(330), None)
    daily.append({"date": "2026-09-02", "kwh": 0})
    complete = current_ladder(profile(), NOW.date(), 270, daily)
    assert (complete.tier, complete.remaining_kwh, complete.start_date) == (2, Decimal(330), "2026-09-03")
    assert current_ladder(profile(), NOW.date(), 250, [{"date": "2026-09-02", "kwh": 250}]).start_date is None
    assert current_ladder(profile(), NOW.date(), 250, [{"date": "2026-09-01", "kwh": 250}]).start_date == "2026-09-01"


def test_decimal_accumulation_cannot_create_a_false_crossing_at_260():
    daily = [{"date": f"2026-09-{day:02d}", "kwh": value} for day, value in enumerate([259.8, 0.1, 0.1, 0.01], 1)]
    result = current_ladder(profile(), dt.date(2026, 9, 4), "260.01", daily)
    assert result.tier == 2 and result.start_date == "2026-09-04"


@pytest.mark.parametrize("value", [None, "NaN", -1])
def test_invalid_newest_published_reading_cannot_shorten_coverage(value):
    daily = [{"date": "2026-09-01", "kwh": 250}, {"date": "2026-09-02", "kwh": 20}, {"date": "2026-09-03", "kwh": value}]
    result = current_ladder(profile(), NOW.date(), 270, daily)
    assert (result.tier, result.remaining_kwh, result.start_date) == (2, Decimal(330), None)


@pytest.mark.parametrize("selection,area", [(None, "080000"), ({}, "080000"), ({"scheme": "combined"}, "080000"), (CHOICES[0], "030000")])
def test_unconfigured_combined_and_other_regions_need_no_ladder_api(monkeypatch, selection, area):
    coordinator = sensor.CurrentCoordinator.__new__(sensor.CurrentCoordinator)
    account = SimpleNamespace(account_number="fictional-tariff-account", area_code=area)
    coordinator.entry = SimpleNamespace(data={CONF_SETTINGS: {CONF_TARIFF_PROFILES: {account.account_number: selection}}})
    coordinator._accounts = lambda: [account]
    coordinator._client = AsyncMock(side_effect=AssertionError("Unneeded cloud client"))
    coordinator._fetch = AsyncMock(side_effect=AssertionError("Unneeded ladder daily API"))
    result = asyncio.run(coordinator._async_update_data())[account.account_number]
    assert result[SUFFIX_CURRENT_LADDER] == result[SUFFIX_CURRENT_LADDER_REMAINING_KWH] == STATE_UNAVAILABLE
    assert result[SUFFIX_CURRENT_LADDER_TARIFF] == (0.62586875 if selection == {"scheme": "combined"} and area == "080000" else STATE_UNAVAILABLE)
    coordinator._client.assert_not_called()
    coordinator._fetch.assert_not_called()


@pytest.mark.parametrize("choice", CHOICES[:4])
def test_configured_coordinator_uses_current_policy_and_authoritative_usage(monkeypatch, choice):
    monkeypatch.setattr(sensor, "_csg_now", lambda: NOW)
    coordinator = sensor.CurrentCoordinator.__new__(sensor.CurrentCoordinator)
    account = SimpleNamespace(account_number="fictional-tariff-account", area_code="080000")
    coordinator.entry = SimpleNamespace(data={CONF_SETTINGS: {CONF_TARIFF_PROFILES: {account.account_number: choice}}})
    coordinator._accounts = lambda: [account]
    client = SimpleNamespace(get_month_daily_usage_detail=Mock())
    coordinator._client = AsyncMock(return_value=client)
    coordinator._fetch = AsyncMock(return_value=("400", [{"date": "2026-09-02", "kwh": 400}]))
    coordinator._clear_failure = Mock()
    coordinator._notify_failure = Mock()
    coordinator.data = asyncio.run(coordinator._async_update_data())
    result = coordinator.data[account.account_number]
    assert result[SUFFIX_CURRENT_LADDER] == 2
    assert result[SUFFIX_CURRENT_LADDER_REMAINING_KWH] == (300 if choice["multi_person"] else 200)
    assert result[ATTR_KEY_CURRENT_LADDER_START_DATE][ATTR_KEY_CURRENT_LADDER_START_DATE] is None
    assert coordinator.current_tariff(account.account_number) == (1.04500875 if choice["tou"] else 0.63886875)
    coordinator._fetch.assert_awaited_once_with(client.get_month_daily_usage_detail, account, (2026, 9))
    monkeypatch.setattr(sensor, "_csg_now", lambda: NOW.replace(month=10, day=1))
    assert coordinator.current_tariff(account.account_number) == STATE_UNAVAILABLE


def test_tariff_entity_keeps_unique_id_but_uses_unit_price_semantics():
    description = next(item for item in sensor.CURRENT_DESCRIPTIONS if item.suffix == SUFFIX_CURRENT_LADDER_TARIFF)
    entity = sensor.CSGSensor(SimpleNamespace(data={}, last_update_success=True), "fictional-tariff-account", description)
    assert entity.unique_id == "csg_plus.fictional-tariff-account.current_ladder_tariff"
    assert entity.native_unit_of_measurement == "CNY/kWh"
    assert entity.device_class is None
    assert entity.state_class is SensorStateClass.MEASUREMENT
