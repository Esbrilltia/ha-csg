"""Unit tests for CSG sensor data handling."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE

from custom_components.csg.const import (
    ATTR_KEY_SETTLEMENT_DATE,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from custom_components.csg.csg_client import CSGAPIError
from custom_components.csg.sensor import (
    BILLING_DESCRIPTIONS,
    CURRENT_DESCRIPTIONS,
    REALTIME_DESCRIPTIONS,
    BillingCoordinator,
    CSGSensor,
    RealtimeCoordinator,
    _KEY_YESTERDAY_DATE,
    _csg_today,
    _ladder_data,
    _set_latest_day,
)


def run(coroutine):
    """Run an async unit under pytest without pytest-asyncio."""
    return asyncio.run(coroutine)


@pytest.fixture(autouse=True)
def stable_default_clock(monkeypatch):
    """Keep daily snapshots independent of the calendar day CI runs."""
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    )


def test_energy_sensor_descriptions_have_correct_statistics_semantics() -> None:
    """Snapshot entities cannot become an alternate cumulative Energy path."""
    snapshots = (*REALTIME_DESCRIPTIONS, *CURRENT_DESCRIPTIONS, *BILLING_DESCRIPTIONS)
    assert all(
        description.state_class is not SensorStateClass.TOTAL_INCREASING
        for description in snapshots
    )


def freeze_utcnow(monkeypatch, moment: dt.datetime) -> None:
    """Pin the CSG calendar clock for daily snapshots."""
    monkeypatch.setattr("custom_components.csg.sensor.dt_util.utcnow", lambda: moment)


def test_sensor_uses_initial_coordinator_data_and_clears_missing_values() -> None:
    """Sensors expose the first refresh and never retain a failed value."""
    coordinator = SimpleNamespace(
        data={"account": {"balance": 3.5}}, last_update_success=True
    )
    sensor = CSGSensor(coordinator, "account", next(d for d in REALTIME_DESCRIPTIONS if d.suffix == "balance"))

    assert sensor.native_value == 3.5
    assert sensor.available
    coordinator.data = {"account": {"balance": STATE_UNAVAILABLE}}
    sensor._update_from_coordinator()
    assert sensor.native_value is None
    assert not sensor.available


def test_csg_today_uses_china_standard_time(monkeypatch) -> None:
    """CSG API dates must not depend on Home Assistant's configured timezone."""
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 3, 16, tzinfo=dt.UTC),
    )

    assert _csg_today() == dt.date(2026, 8, 4)


def test_set_latest_day_marks_missing_data_unavailable() -> None:
    """Latest settlement sensors are unavailable when no daily bill exists."""
    data: dict = {}
    _set_latest_day(data, [])
    assert data == {
        SUFFIX_LATEST_DAY_KWH: STATE_UNAVAILABLE,
        SUFFIX_LATEST_DAY_COST: STATE_UNAVAILABLE,
    }

    _set_latest_day(data, [{"date": "2026-08-03", "kwh": 4.5, "charge": 2.0}])
    assert data[SUFFIX_LATEST_DAY_KWH] == 4.5
    assert data[SUFFIX_LATEST_DAY_COST] == 2.0
    assert data[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-08-03"}


def test_ladder_data_handles_missing_values() -> None:
    """Null ladder fields are exposed as unavailable rather than invalid values."""
    data = _ladder_data({})
    assert all(value == STATE_UNAVAILABLE for key, value in data.items() if key != "current_ladder_start_date")


class FakeBillingCoordinator:
    """Small collaborator for testing BillingCoordinator._update_account."""

    _fetch = staticmethod(lambda function, *args: _call(function, *args))

    def __init__(self) -> None:
        self.history_store = AsyncMock()
        self.energy_statistics_bridge = None

    async def _add_year_data(self, client, account, data: dict) -> None:
        return None

    def _notify_failure(self, account: str, kind: str, err: Exception) -> None:
        return None

    def _clear_failure(self, account: str, kind: str) -> None:
        return None


async def _call(function, *args):
    return function(*args)


class FakeClient:
    """Provide deterministic daily responses without network I/O."""

    def get_month_daily_usage_detail(self, account, year_month):
        if year_month == (2026, 8):
            return 0.0, []
        return 4.5, [{"date": "2026-07-31", "kwh": 4.5}]


def test_billing_coordinator_falls_back_to_last_month_settlement(monkeypatch) -> None:
    """The latest settlement day uses last month when current month is empty."""
    coordinator = FakeBillingCoordinator()
    account = SimpleNamespace(account_number="account")
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC))

    data = run(
        BillingCoordinator._update_account(
            coordinator, FakeClient(), account, [(2026, 8), (2026, 7)]
        )
    )

    assert data[SUFFIX_LATEST_DAY_KWH] == 4.5
    assert data[SUFFIX_LATEST_DAY_COST] == STATE_UNAVAILABLE
    assert data[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-07-31"}
    coordinator.history_store.async_upsert_daily_usage.assert_any_await(
        "account", (2026, 7), [{"date": "2026-07-31", "kwh": 4.5}]
    )


def test_billing_coordinator_marks_failed_month_unavailable(monkeypatch) -> None:
    """A failed month request leaves its snapshots explicitly unavailable."""
    class FailingClient:
        def get_month_daily_usage_detail(self, account, year_month):
            raise CSGAPIError("failure")

    coordinator = FakeBillingCoordinator()
    account = SimpleNamespace(account_number="account")
    freeze_utcnow(monkeypatch, dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC))

    data = run(
        BillingCoordinator._update_account(
            coordinator, FailingClient(), account, [(2026, 8), (2026, 7)]
        )
    )

    assert data[SUFFIX_LATEST_DAY_KWH] == STATE_UNAVAILABLE
    assert data[SUFFIX_LATEST_DAY_COST] == STATE_UNAVAILABLE
    coordinator.history_store.async_upsert_daily_usage.assert_not_awaited()


class FakeUsageClient:
    """Serve dated monthly usage rows through the current API only."""

    def __init__(self, usage: float | None | Exception, day="2026-08-01") -> None:
        self.usage = usage
        self.day = day
        self.calls = []

    def get_balance_and_arrears(self, account):
        return 1.0, 0.0

    def get_month_daily_usage_detail(self, account, year_month):
        self.calls.append(year_month)
        if isinstance(self.usage, Exception):
            raise self.usage
        if self.usage is None or self.day[:7] != f"{year_month[0]}-{year_month[1]:02d}":
            return 0.0, []
        return self.usage, [{"date": self.day, "kwh": self.usage}]


def make_realtime_coordinator(client: FakeUsageClient):
    """Drive RealtimeCoordinator._async_update_data without Home Assistant."""
    coordinator = RealtimeCoordinator.__new__(RealtimeCoordinator)
    coordinator.hass = object()
    coordinator.entry = SimpleNamespace(entry_id="entry")
    coordinator.history_store = AsyncMock()
    coordinator.energy_statistics_bridge = None

    async def _client():
        return client

    async def _fetch(function, *args):
        return function(*args)

    coordinator._client = _client
    coordinator._fetch = _fetch
    coordinator._accounts = lambda: (SimpleNamespace(account_number="account"),)
    return coordinator


def capture_notifications(monkeypatch) -> tuple[list[str], list[str]]:
    """Record every persistent notification the coordinator tries to touch."""
    created: list[str] = []
    dismissed: list[str] = []
    monkeypatch.setattr(
        "custom_components.csg.sensor.persistent_notification.async_create",
        lambda hass, message, title=None, notification_id=None: created.append(notification_id),
    )
    monkeypatch.setattr(
        "custom_components.csg.sensor.persistent_notification.async_dismiss",
        lambda hass, notification_id: dismissed.append(notification_id),
    )
    return created, dismissed


def notification_ids(kind: str, ids: list[str]) -> list[str]:
    return [value for value in ids if f"_{kind}_" in value]


def warning_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def yesterdays_kwh_description():
    return next(
        description
        for description in REALTIME_DESCRIPTIONS
        if description.suffix == SUFFIX_YESTERDAY_KWH
    )


def test_realtime_coordinator_treats_missing_yesterday_usage_as_a_gap(monkeypatch, caplog) -> None:
    """An unpublished yesterday reading must not be reported as a failure."""
    caplog.set_level(logging.DEBUG)
    created, dismissed = capture_notifications(monkeypatch)
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    coordinator = make_realtime_coordinator(FakeUsageClient(None))

    data = run(coordinator._async_update_data())

    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert "energy_total" not in data["account"]
    assert created == []
    assert warning_messages(caplog) == []
    assert data["account"][_KEY_YESTERDAY_DATE] is None
    # A successful request clears any earlier usage failure.
    assert notification_ids("usage", dismissed) == ["csg_entry_usage_account"]

    yesterday = CSGSensor(
        SimpleNamespace(data=data, last_update_success=True),
        "account",
        yesterdays_kwh_description(),
    )
    assert not yesterday.available


def test_realtime_coordinator_notifies_a_failed_yesterday_request(monkeypatch, caplog) -> None:
    """A real request failure still warns and raises a notification."""
    caplog.set_level(logging.DEBUG)
    created, dismissed = capture_notifications(monkeypatch)
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    coordinator = make_realtime_coordinator(FakeUsageClient(CSGAPIError("boom")))

    data = run(coordinator._async_update_data())

    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert "energy_total" not in data["account"]
    assert notification_ids("usage", created) == ["csg_entry_usage_account"] * 2
    assert notification_ids("usage", dismissed) == []
    assert all("Could not update daily usage" in message for message in warning_messages(caplog))
    assert len(warning_messages(caplog)) == 2


def test_realtime_coordinator_records_a_published_yesterday_reading(monkeypatch, caplog) -> None:
    """A published reading is still recorded and clears earlier failures."""
    caplog.set_level(logging.DEBUG)
    created, dismissed = capture_notifications(monkeypatch)
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 2, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    coordinator = make_realtime_coordinator(FakeUsageClient(25))

    data = run(coordinator._async_update_data())

    assert data["account"][SUFFIX_YESTERDAY_KWH] == 25
    assert "energy_total" not in data["account"]
    assert created == []
    assert warning_messages(caplog) == []
    assert notification_ids("usage", dismissed) == ["csg_entry_usage_account"]


class FakeRealtimeClient(FakeUsageClient):
    """Monthly daily-usage fake with separate value/error constructor inputs."""

    def __init__(self, usage: float | None = None, error: Exception | None = None) -> None:
        super().__init__(error if error is not None else usage, day="2026-08-02")


class FakeRealtimeCoordinator(RealtimeCoordinator):
    """Small collaborator for testing RealtimeCoordinator._async_update_data.

    Only the collaborators are replaced, so the helpers under test stay real.
    """

    def __init__(self, client: FakeRealtimeClient) -> None:
        self.client = client
        self.history_store = AsyncMock()
        self.energy_statistics_bridge = None
        self.notifications: list[tuple[str, str, Exception]] = []
        self.dismissed: list[tuple[str, str]] = []

    async def _client(self):
        return self.client

    def _accounts(self):
        return [SimpleNamespace(account_number="account")]

    async def _fetch(self, function, *args):
        return function(*args)

    def _notify_failure(self, account: str, kind: str, err: Exception) -> None:
        self.notifications.append((account, kind, err))

    def _clear_failure(self, account: str, kind: str) -> None:
        self.dismissed.append((account, kind))


def _patch_utcnow(monkeypatch) -> None:
    """Pin the coordinator's clock so "yesterday" is a fixed day."""
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.utcnow",
        lambda: dt.datetime(2026, 8, 3, 4, tzinfo=dt.UTC),
    )


def test_empty_yesterday_usage_is_not_a_failed_request(monkeypatch, caplog) -> None:
    """An unpublished yesterday total is a data gap, not a broken account."""
    caplog.set_level(logging.DEBUG)
    _patch_utcnow(monkeypatch)
    coordinator = FakeRealtimeCoordinator(FakeRealtimeClient(usage=None))

    data = run(RealtimeCoordinator._async_update_data(coordinator))

    assert coordinator.notifications == []
    assert ("account", "usage") in coordinator.dismissed
    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert "energy_total" not in data["account"]
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert coordinator.client.calls == [(2026, 8), (2026, 7)]


def test_failed_yesterday_usage_request_still_notifies(monkeypatch) -> None:
    """A real yesterday request failure still reports the account as failing."""
    _patch_utcnow(monkeypatch)
    error = CSGAPIError("boom")
    coordinator = FakeRealtimeCoordinator(FakeRealtimeClient(error=error))

    data = run(RealtimeCoordinator._async_update_data(coordinator))

    assert coordinator.notifications == [("account", "usage", error)] * 2
    assert coordinator.client.calls == [(2026, 8), (2026, 7)]
    assert ("account", "usage") not in coordinator.dismissed
    assert data["account"][SUFFIX_YESTERDAY_KWH] == STATE_UNAVAILABLE
    assert "energy_total" not in data["account"]
