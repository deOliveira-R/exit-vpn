"""Fixtures for the prober's tests; the fakes themselves are in harness.py."""

import pytest

import prober.exitcheck as exitcheck


@pytest.fixture
def no_wait(monkeypatch):
    """The exit check's one-second retries return at once."""
    real_sleep = exitcheck.asyncio.sleep

    async def instant(_seconds):
        await real_sleep(0)
    monkeypatch.setattr(exitcheck.asyncio, "sleep", instant)
