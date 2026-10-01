"""Pytest configuration for the custom integration source tree."""

from __future__ import annotations

import sys
import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def daily_usage_history_dependency(request, monkeypatch):
    """Supply the new dependency to B2's constructor-bypassing test factory."""
    if request.module.__name__ != "test_daily_usage":
        return
    from custom_components.csg.history_store import CSGHistoryStore
    from custom_components.csg.sensor import BillingCoordinator, RealtimeCoordinator

    original = request.module.make_coordinator

    def make_coordinator(kind, client, ledger):
        coordinator = original(kind, client, ledger)
        if kind in (RealtimeCoordinator, BillingCoordinator):
            coordinator.history_store = AsyncMock(spec=CSGHistoryStore)
            coordinator.energy_statistics_bridge = None
        return coordinator

    monkeypatch.setattr(request.module, "make_coordinator", make_coordinator)


class RecorderHarness:
    """Narrow in-memory model of the audited Recorder sum contract.

    Production query/correction methods stay real. This adapter models queued
    writes, fixed timestamp rows and the hourly start floor, not HA threads or
    a complete Recorder database. It is not an independent HA API audit.
    """

    def __init__(self):
        self.rows = {}
        self.adjustments = []
        self.pending = []
        self.queries = []
        self.fail_read_after_write = False
        self.fail_next_read = False

    def add(self, statistic_id, period, start, value):
        self.rows.setdefault((statistic_id, period), []).append(
            {"start": start.timestamp(), "sum": value}
        )

    def sums(self, statistic_id, period):
        return [row["sum"] for row in self.rows.get((statistic_id, period), [])]

    async def async_add_executor_job(
        self, function, hass, start, end, statistic_ids, period, units, types
    ):
        assert function.__name__ == "statistics_during_period"
        assert period in ("5minute", "hour")
        assert types == {"sum"}
        assert start.utcoffset() == end.utcoffset() == dt.timedelta(0)
        self.queries.append((start, end, statistic_ids, period))
        if self.fail_next_read:
            self.fail_next_read = False
            raise RuntimeError("Synthetic after-read failure")
        return {
            statistic_id: [
                dict(row) for row in self.rows.get((statistic_id, period), [])
                if start.timestamp() <= row["start"] < end.timestamp()
            ]
            for statistic_id in statistic_ids
        }

    def async_adjust_statistics(self, statistic_id, start, adjustment, unit):
        request = (statistic_id, start, adjustment, unit)
        self.adjustments.append(request)
        self.pending.append(request)

    async def async_block_till_done(self):
        for statistic_id, start, adjustment, unit in self.pending:
            assert unit in ("kWh", "CNY")
            for period in ("5minute", "hour"):
                cutoff = start if period == "5minute" else start.replace(minute=0)
                for row in self.rows.get((statistic_id, period), []):
                    if row["start"] >= cutoff.timestamp():
                        row["sum"] += adjustment
        self.pending.clear()
        if self.fail_read_after_write:
            self.fail_next_read = True
            self.fail_read_after_write = False


@pytest.fixture
def recorder_harness(monkeypatch):
    recorder = RecorderHarness()

    class Registry:
        def async_get_entity_id(self, domain, platform, unique_id):
            assert (domain, platform) == ("sensor", "csg")
            return f"sensor.{unique_id.rsplit('.', 1)[-1]}"

    monkeypatch.setattr("custom_components.csg.sensor.er.async_get", lambda hass: Registry())
    monkeypatch.setattr(
        "homeassistant.components.recorder.get_instance", lambda hass: recorder
    )
    return recorder
