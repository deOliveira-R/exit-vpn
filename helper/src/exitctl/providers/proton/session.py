"""The Proton session (python-proton-core: SRP login, 2FA, token refresh) and its login steps.

Login is a small state machine; each step that needs the user raises NeedInput and the next
`login` call continues:

    start ── password ──► (CAPTCHA asked?) ── human_verification ──► password again with the
             token ──► (2FA on?) ── totp ──► logged in

After the password step the half-open session is saved in the store, so the 2FA step can run
in another process (the app runs one exitctl per step).
"""

from __future__ import annotations

import os
from typing import NoReturn

from ...model import Account, NeedInput, ProviderError
from ...sessions import SessionStore
from ..base import Credentials

STORE_NAME = "proton:session"
VERIFY_URL = "https://verify.proton.me/?methods={methods}&token={token}"
USER_AGENT = "exitctl/0.1 (macOS)"
CAPTCHA_CODES = (9001, 12087)   # Proton's "human verification required" codes


class _StoreObserver:
    """proton-core calls this around every change of session state (login, refresh,
    logout); the state is mirrored into the store."""

    def __init__(self, store: SessionStore):
        self.store = store

    def _acquire_session_lock(self, account_name, session_data):
        pass

    def _release_session_lock(self, account_name, new_session_data):
        if new_session_data:
            self.store.save(STORE_NAME, new_session_data)
        else:
            self.store.delete(STORE_NAME)


def default_session():
    from proton.session import Session
    return Session(appversion=os.environ.get("EXITCTL_PROTON_APPVERSION", "Other"),
                   user_agent=USER_AGENT)


class ProtonSession:
    def __init__(self, store: SessionStore, factory=default_session):
        self.store, self.factory = store, factory
        self._s = None

    def get(self, fresh: bool = False):
        """The proton-core session, restored from the store (or a new, empty one)."""
        if self._s is None or fresh:
            s = self.factory()
            state = None if fresh else self.store.load(STORE_NAME)
            if state:
                s.__setstate__(state)
            s.register_persistence_observer(_StoreObserver(self.store))
            self._s = s
        return self._s

    def account(self) -> Account:
        s = self.get()
        return Account(s.AccountName, bool(s.needs_twofa)) if s.UID else Account(None)

    async def login(self, creds: Credentials) -> Account:
        s = self.get()
        if s.UID and s.needs_twofa and not creds.password:
            return await self._second_factor(s, creds.totp)   # step 2, maybe another process
        if not creds.username or not creds.password:
            raise NeedInput("password", "username and password are needed")
        s = self.get(fresh=True)
        headers = None
        if creds.hv_token:
            headers = {"x-pm-human-verification-token": creds.hv_token,
                       "x-pm-human-verification-token-type": creds.hv_type}
        try:
            ok = await s.async_authenticate(creds.username, creds.password,
                                            additional_headers=headers)
        except Exception as e:
            self._raise_for_login_error(e, creds)
        if not ok:
            raise ProviderError("login failed (wrong username or password?)")
        if s.needs_twofa:
            return await self._second_factor(s, creds.totp)
        return Account(s.AccountName)

    @staticmethod
    def _raise_for_login_error(e: Exception, creds: Credentials) -> NoReturn:
        from proton.session.exceptions import ProtonAPIAuthenticationNeeded, ProtonAPIError
        if not isinstance(e, ProtonAPIError):
            raise e
        if isinstance(e, ProtonAPIAuthenticationNeeded):
            raise ProviderError("login failed (wrong username or password?)") from None
        # During login proton-core raises CAPTCHA requests (422/9001) as a plain
        # ProtonAPIError, not ProtonAPIHumanVerificationNeeded, so match the code.
        if e.body_code not in CAPTCHA_CODES:
            raise ProviderError(f"Proton login: {e}") from None
        if creds.hv_token:
            raise ProviderError("Proton still asks for verification: the CAPTCHA was not "
                                "completed (or expired); run login again") from None
        details = (e.json_data or {}).get("Details") or {}
        token, methods = details.get("HumanVerificationToken"), details.get("Methods") or ["captcha"]
        if not token:
            raise ProviderError(f"Proton login: {e}") from None
        raise NeedInput("human_verification", "Proton asks for human verification",
                        token=token, methods=methods,
                        url=VERIFY_URL.format(methods=",".join(methods), token=token)) from None

    @staticmethod
    async def _second_factor(s, code: str) -> Account:
        if not code:
            raise NeedInput("totp", "a 2FA code is needed (run login again with it)")
        if not await s.async_validate_2fa_code(code.strip()):
            raise ProviderError("2FA code rejected")
        return Account(s.AccountName)

    async def logout(self) -> None:
        s = self.get()
        try:
            if s.UID:
                await s.async_logout()
        finally:
            self.store.delete(STORE_NAME)

    async def api(self, endpoint: str, jsondata=None, method=None, params=None) -> dict:
        from proton.session.exceptions import (ProtonAPI2FANeeded, ProtonAPIAuthenticationNeeded,
                                               ProtonAPIError)
        s = self.get()
        if not s.UID:
            raise ProviderError("not logged in: run `exitctl proton login`")
        try:
            return await s.async_api_request(endpoint, jsondata=jsondata, method=method, params=params)
        except ProtonAPI2FANeeded:
            raise ProviderError("login incomplete: run `exitctl proton login` and give the 2FA code") from None
        except ProtonAPIAuthenticationNeeded:
            raise ProviderError("Proton session expired: run `exitctl proton login`") from None
        except ProtonAPIError as e:
            raise ProviderError(f"Proton API {endpoint}: {e}") from None
