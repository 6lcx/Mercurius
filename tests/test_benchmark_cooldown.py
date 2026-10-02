"""A benchmark must observe the circuit clock, not assume a sleep advanced it."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

from scripts.benchmark import local


async def test_wait_rechecks_coarse_monotonic_clock(monkeypatch):
    ticks = iter([1.0, 1.0, 1.0, 1.015625, 1.015625])
    sleep = AsyncMock()
    monkeypatch.setattr(local, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(local, 'asyncio', SimpleNamespace(sleep=sleep))
    elapsed = await local.wait_monotonic(.012)
    assert sleep.await_count == 2
    assert elapsed >= .012
