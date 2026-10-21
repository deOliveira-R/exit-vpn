"""The controller's errors, by what the caller did wrong (the API maps them to HTTP)."""


class PolicyError(ValueError):
    """A request the rules refuse: an unknown country or server, a pick in the wrong
    Secure Core mode, a malformed catalog push (HTTP 400)."""


class SwitchError(RuntimeError):
    """Nothing to switch to, or no candidate worked; the exit stays as it was (HTTP 409)."""
