"""Where provider sessions are kept: the Keychain store, through `keyring` (boundary)."""

import sys
import types

import pytest

from exitctl.sessions import SERVICE, KeychainStore


@pytest.fixture
def keyring(monkeypatch):
    """A stand-in `keyring` module (the real one would touch the login Keychain)."""
    errors = types.ModuleType("keyring.errors")

    class PasswordDeleteError(Exception):
        pass
    errors.PasswordDeleteError = PasswordDeleteError
    mod = types.ModuleType("keyring")
    mod.items = {}
    mod.errors = errors
    mod.get_password = lambda s, n: mod.items.get((s, n))
    mod.set_password = lambda s, n, v: mod.items.__setitem__((s, n), v)

    def delete(s, n):
        if (s, n) not in mod.items:
            raise PasswordDeleteError(n)
        del mod.items[(s, n)]
    mod.delete_password = delete
    monkeypatch.setitem(sys.modules, "keyring", mod)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)
    return mod


def test_a_session_is_saved_as_json_under_exitctl_and_loaded_back(keyring):
    store = KeychainStore()
    store.save("proton", {"UID": "u"})
    assert keyring.items[(SERVICE, "proton")] == '{"UID": "u"}'
    assert store.load("proton") == {"UID": "u"} and store.load("other") is None


def test_deleting_a_session_that_is_not_there_is_fine(keyring):
    store = KeychainStore()
    store.save("proton", {"UID": "u"})
    store.delete("proton")
    store.delete("proton")
    assert store.load("proton") is None
