"""Subprocess helper: real Recorder queue dies with the process, facts survive."""

import asyncio
import json
import os
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).parents[1]))

import pytest
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.tasks import ImportStatisticsTask

from custom_components.csg_plus.energy_statistics import build_statistics, statistic_metadata
from test_energy_statistics_recorder import ACCOUNT, recorder_world


async def main(mode, directory, case):
    monkeypatch = pytest.MonkeyPatch()
    world_context = recorder_world.__wrapped__(Path(directory), monkeypatch)
    async with world_context() as world:
        if mode == "seed":
            await world.upsert({"2026-09-01": 2, "2026-09-02": 1})
            await world.sync()
            await world.upsert({"2026-09-01": 1, "2026-09-02": 0})
            desired = build_statistics(await world.store.async_daily_usage_snapshot(ACCOUNT))
            if case == "partial":
                async_add_external_statistics(world.hass, statistic_metadata(ACCOUNT), desired[:1])
                await world.drain()
            elif case == "all":
                await world.sync()
            entered = threading.Event()

            def block(task, recorder):
                entered.set()
                threading.Event().wait()  # Killed with this test subprocess.

            monkeypatch.setattr(ImportStatisticsTask, "run", block)
            for _ in range(3 if case == "repeated" else 1):
                async_add_external_statistics(world.hass, statistic_metadata(ACCOUNT), desired)
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
            os._exit(0)  # Genuine process exit: no queue drain or bridge cleanup.
        await world.sync()
        rows = await world.query()
        assert [(row["state"], row["sum"]) for row in rows] == [(1, 1), (0, 1)]
        assert [row["change"] for row in await world.query(types={"change"})] == [1, 0]
        count = len(world.imports)
        assert count == (0 if case == "all" else 1)
        await world.sync()
        assert len(world.imports) == count
        assert world.bridge._task is None and not world.bridge._pending
        print(json.dumps({"case": case, "state_sum": [[1, 1], [0, 1]], "change": [1, 0], "imports": count}))
    monkeypatch.undo()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
