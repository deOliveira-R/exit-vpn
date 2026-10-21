"""Logging in to Proton: password, 2FA, CAPTCHA, the stored session (component)."""

import pytest
from harness import LOGIN, FakeSession, captcha_error

from exitctl.model import NeedInput, ProviderError
from exitctl.providers.base import Credentials
from exitctl.providers.proton import ProtonProvider
from exitctl.sessions import MemoryStore


async def test_2fa_asks_for_the_code_and_a_new_process_can_finish_the_login(proton, memory_keychain):
    p, sess = proton
    sess.twofa = "123456"
    with pytest.raises(NeedInput) as e:
        await p.login(LOGIN)
    assert e.value.kind == "totp" and (await p.account()).needs_2fa
    p2 = ProtonProvider(store=memory_keychain, session_factory=lambda: sess)
    assert (await p2.login(Credentials(totp="123456"))).logged_in


async def test_the_password_is_never_stored(proton, memory_keychain):
    p, sess = proton
    sess.twofa = "123456"
    with pytest.raises(NeedInput):
        await p.login(LOGIN)
    assert memory_keychain.data and "pw" not in str(memory_keychain.data)


async def test_a_wrong_password_is_a_provider_error(proton):
    p, _ = proton
    with pytest.raises(ProviderError):
        await p.login(Credentials(username="u", password="wrong"))


async def test_no_credentials_asks_for_the_password():
    with pytest.raises(NeedInput) as e:
        await ProtonProvider(store=MemoryStore(), session_factory=FakeSession).login(Credentials())
    assert e.value.kind == "password"


async def test_a_captcha_asks_for_human_verification_with_the_link(proton):
    p, sess = proton

    async def auth(user, password, additional_headers=None):
        raise captcha_error()
    sess.async_authenticate = auth
    with pytest.raises(NeedInput) as e:
        await p.login(LOGIN)
    assert e.value.kind == "human_verification" and "token=hvtok" in e.value.info["url"]


async def test_certificates_need_a_login(proton):
    p, _ = proton
    with pytest.raises(ProviderError, match="not logged in"):
        await p.certificates()
