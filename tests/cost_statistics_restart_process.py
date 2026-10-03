"""Actual process death drops queued cost imports, but preserves durable facts."""

import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).parents[1]))

import pytest
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.tasks import ImportStatisticsTask

from custom_components.csg_plus.cost_statistics import build_cost_statistics, cost_statistic_metadata
from test_cost_statistics_recorder import cost_world
from test_energy_statistics_recorder import ACCOUNT, recorder_world
from test_cost_statistics_lifecycle import assert_cost_converged


async def main(mode, directory, case):
    monkeypatch = pytest.MonkeyPatch()
    recorder_context = recorder_world.__wrapped__(Path(directory), monkeypatch)
    world_context = cost_world.__wrapped__(recorder_context, monkeypatch)
    async with world_context() as world:
        if mode == "seed":
            await world.upsert_bills({"2026-01": 100, "2026-03": 150})
            await world.sync()
            await world.upsert_bills({"2026-01": 98, "2026-02": 120})
            desired = build_cost_statistics(await world.store.async_monthly_bills_snapshot(ACCOUNT), dt.date(2026, 10, 3))
            if case == "partial":
                async_add_external_statistics(world.hass, cost_statistic_metadata(ACCOUNT), desired[:2])
                await world.drain()
            elif case == "all":
                await world.sync()
            entered = threading.Event()
            original = ImportStatisticsTask.run
            def block(task, recorder):
                if task.metadata["statistic_id"] == world.cost_id:
                    entered.set()
                    threading.Event().wait()  # Dies with this subprocess.
                return original(task, recorder)
            monkeypatch.setattr(ImportStatisticsTask, "run", block)
            for _ in range(3 if case == "repeated" else 1):
                async_add_external_statistics(world.hass, cost_statistic_metadata(ACCOUNT), desired)
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
            os._exit(0)
        assert world.bridge._lanes == {}
        await world.sync()
        count = len(world.imports)
        assert count == (0 if case == "all" else 1)
        await assert_cost_converged(world, world.bridge, [(98, 98), (120, 218), (150, 368)])
        print(json.dumps({"case": case, "state_sum": [[98, 98], [120, 218], [150, 368]], "imports": count}))
    monkeypatch.undo()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
