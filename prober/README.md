# Prober

Tests whether a site accepts a VPN server, without touching the exit: its own container, its
own network namespace and kill switch, its own tunnel `wg-probe` with key slot b. The
controller's `FindService` sends it one candidate at a time. Concepts:
[ARCHITECTURE.md](../ARCHITECTURE.md).

A probe, in order (`probe.Prober.run`):
1. Attach wg-probe to the candidate.
2. Prove where traffic exits: it must carry traffic within 20 s, not exit from the host's own IP, and exit in the candidate's country.
3. Load the URL with curl_cffi.
4. Load it again in headless Chromium if the policy asks (`needs_browser`).
5. Classify each load (`classify.RULES`).
6. Detach, whatever happened.

Request, response and outcomes: [contracts/README.md](../contracts/README.md).

    uv venv -p 3.13 && uv pip install -e '.[test]'
    .venv/bin/pytest -q

`entrypoint.sh` sets up the netns kill switch and wg-probe before the API starts. Its key
choice and interface settings must stay in step with `keyfile.py` and `tunnel.py`.
