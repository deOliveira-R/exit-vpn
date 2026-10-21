# Controller (`exit_vpn`)

The exit node's brain: health checks and failover for wg0, the owner's HTTP API, the server
catalog and the two key slots. It runs as the s6 longrun `controller` inside the exit
container, after the kill switch and wg0 are up. Concepts and how they connect:
[ARCHITECTURE.md](../ARCHITECTURE.md).

## Run the tests

    uv venv -p 3.13 && uv pip install -e '.[test]'
    .venv/bin/pytest -q

The tests build the real object graph (`node.build`) with a fake tunnel, fake health
probes and a fake clock (`tests/harness.py`). `test_contracts.py` and the `event_contract`
fixture (`conftest.py`) check the API and every event against `contracts/controller/`.
`test_units.py` also checks the shell contract with `common.sh`.

## API

`http://<node>:8080/v1/…`, bound to the node's tailnet IPv4. Only the owner's untagged devices
may call it (`EXIT_OWNERS`, checked by whois). Routes and shapes are listed in
[contracts/README.md](../contracts/README.md).

## Files

| Path | What |
|---|---|
| `/var/lib/exit-vpn/state.json` | prefs, active server, blacklist (`persistence.StateStore`) |
| `/var/lib/exit-vpn/peer.env` | the active peer, read by `init-wg` at the next start |
| `/var/lib/exit-vpn/events.json` | the last 50 events |
| `/var/lib/exit-vpn/catalog.json` | the last good catalog |
| `/keys/{a,b}/wg.conf`, `meta.json`, `wg.conf.prev` | uploaded keys (`keys.KeyStore`) |
| `/secrets/proton_{a,b}.conf` | bootstrap configs, kept in step with the live keys |

## Settings

From the environment (compose passes `.env`): see `config.Settings.from_env` and
`.env.example`. The timings that matter for failover are `HEALTH_INTERVAL` (5 s),
`HEALTH_STRIKES` (3), `WARMUP_SECONDS` (20, a fresh session drops TCP for 4–14 s),
`KEY_WARMUP_SECONDS` (45) and `BLACKLIST_SECONDS` (1800).
