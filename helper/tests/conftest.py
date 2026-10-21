"""Fixtures over harness.py."""

import pytest
from harness import LOGIN, FakeSession, Runner

from exitctl.providers.proton import ProtonProvider
from exitctl.sessions import MemoryStore


@pytest.fixture
def memory_keychain():
    return MemoryStore()


@pytest.fixture
def proton(memory_keychain):
    """(provider, its fake session), not logged in."""
    sess = FakeSession()
    return ProtonProvider(store=memory_keychain, session_factory=lambda: sess), sess


@pytest.fixture
async def logged_in(proton):
    p, sess = proton
    await p.login(LOGIN)
    return p, sess


@pytest.fixture
def run(monkeypatch):
    return Runner(monkeypatch)
