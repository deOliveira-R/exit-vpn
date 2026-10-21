# Testing

How the tests are organised, what each kind proves, and where a new test goes. The code's
concepts are in [ARCHITECTURE.md](ARCHITECTURE.md); test files are named after them.

## Rules

- **One file per concept, named after it** (`test_selection.py` tests `selection`;
  `ExitNodeTests.swift` tests the exit-node parts). No files named after when they were written.
- **One behaviour per test, named as a sentence about it** (`dead candidates are skipped and
  blacklisted`). A test that fails should say what broke without reading its body.
- **Fakes and builders live in the harness** (`harness.py` in each Python package, `Harness.swift` in the app);
  `conftest.py` only turns them into fixtures. Test files never import each other.
- **Assert outcomes, not internals**: what a caller, the wire or the system sees. A test that
  only restates its fixture proves nothing.
- **The lowest kind that can prove it**: a pure function is tested as one; the API layer only
  for what the API itself adds (status codes, validation messages, auth, what it never echoes).

## Five kinds of test

| Kind | Proves | Runs | Fakes |
|---|---|---|---|
| **Logic** | parsing, selection, health, classification, crypto, preferences: pure rules | `pytest`, `swift test` | none; a real catalog fixture and a fake clock |
| **Component** | a component's real object graph behaves: failover, key slots, a probe, rotating a key, the app's stores | `pytest`, `swift test` | the edges only: tunnel, health probe, clock, Proton API, controller, processes |
| **Boundary** | our side of a system interface: `wg` output, WireGuard configs, DNS packets, Tailscale whois and status, the shell scripts' files, child processes | `pytest`, `swift test` | real `sh` and real processes where cheap |
| **Contract** | each wire format between components matches its sample in [`contracts/`](contracts/README.md), checked by producer and consumer | `pytest`, `swift test` | the samples |
| **Acceptance** | the deployed system does what it promises, end to end, on the VPS and from a Mac | `tests/acceptance/` | none |

The app also has **live** tests: the core library against a running controller and exitctl,
read-only (`EXITCONTROL_LIVE=http://exit-proton:8080 swift test --filter Live`).

## Where things are

### Controller (`controller/tests`)

`harness.py` builds a node with `node.build`, exactly as production does, around a fake tunnel
(`FakeWg`), fake health probes, a fake prober client and a fake clock, and holds every shared
builder (pushed catalog rows, keys, configs, whois stand-ins, an API client). `conftest.py` has
only fixtures (`rig`, `catalog`) and checks every event any test publishes against
`contracts/controller/events.json`.

| File | Kind | Concept |
|---|---|---|
| `test_catalog.py` | logic | both catalog sources (gluetun fallback, pushed list): parsing, validation, lookups |
| `test_selection.py` | logic | failover, rotate and find candidates |
| `test_prefs.py` | logic | preferences: validation, normalisation, updates |
| `test_health.py` | logic | the health state machine: strikes, warm-up, backoff |
| `test_state.py` | logic | the active server and the blacklist |
| `test_service.py` | component | the health cycle: failover, degraded, recovery, restart |
| `test_switching.py` | component | select, rotate, exact servers, Secure Core, all-or-nothing preference changes |
| `test_slots.py` | component | key slots: validate, try, keep or roll back, mirror |
| `test_finder.py` | component | find jobs |
| `test_api.py` | component | the HTTP layer: auth, status codes, validation, nothing secret echoed; catalog push and revert |
| `test_persistence.py` | boundary | state, event log and catalog cache files, migration of old state |
| `test_adapters.py` | boundary | `wg` dump, WireGuard config, DNS and trace probes, Tailscale whois |
| `test_shell_contract.py` | boundary | `peer.env` and `common.sh`, shared with the exit's shell scripts |
| `test_contracts.py` | contract | API responses, events, prober requests |

### Prober (`prober/tests`)

`harness.py`: a fake tunnel, exit check and engines, and `prober()` to assemble a `Prober`
(test files import it; `conftest.py` has only fixtures).

| File | Kind | Concept |
|---|---|---|
| `test_classify.py` | logic | the classification rules (and reading a page's title and text) |
| `test_probe.py` | component | one probe: tunnel session, exit check, engine escalation |
| `test_exitcheck.py` | component | traffic leaves through the VPN, in the expected country, before a page loads |
| `test_api.py` | component | the HTTP layer: `/probe`, `/healthz`, key reload |
| `test_tunnel.py` | boundary | the `wg` and `ip` commands that attach, detach and re-key `wg-probe` |
| `test_keyfile.py` | boundary | the slot-b WireGuard config |
| `test_contracts.py` | contract | `/probe` requests and responses, `/healthz` |

### exitctl (`helper/tests`)

`harness.py`: a fake Proton session, a fake exit node (controller) and a CLI runner (exitctl as
the app runs it); `conftest.py` makes the fixtures (`proton`, `logged_in`, `run`).

| File | Kind | Concept |
|---|---|---|
| `test_crypto.py` | logic | key derivation, PEM, WireGuard config rendering |
| `test_levers.py` | logic | provider levers and features |
| `test_proton_session.py` | component | login: password, 2FA, CAPTCHA, the stored session |
| `test_proton_certificates.py` | component | making a key: the certificate request and the config it yields |
| `test_keyservice.py` | component | rotate, renew, list |
| `test_proton_catalog.py` | logic | server rows from Proton's logicals |
| `test_cli.py` | component | argument parsing, generated lever flags, the interactive login |
| `test_exitnode.py` | boundary | the client of the controller API: errors, slots, the node's name |
| `test_sessions.py` | boundary | the Keychain store, through a stand-in `keyring` |
| `test_contracts.py` | contract | `exitctl --json` output and exit codes |

### App (`mac/ExitControl/Tests/ExitControlCoreTests`)

`Harness.swift`: the contract samples, builders and fakes the test files share.

| File | Kind | Concept |
|---|---|---|
| `WireTests.swift` | logic | the wire as the app writes and reads it: request bodies, the event stream, derived fields |
| `EventTests.swift` | logic | event summaries for the log and notifications |
| `VocabularyTests.swift` | logic | the app's vocabulary against the wire, badges |
| `GeographyTests.swift` | logic | flags, country codes, continents |
| `ExitURLTests.swift` | logic | `exitvpn://` URLs |
| `ServerQueryTests.swift` | logic | filtering and ordering the server list |
| `ActionRunnerTests.swift` | component | one change at a time: refuse or queue |
| `ConnectionStoreTests.swift` | component | stream state, notifications, the bounded log |
| `ProviderStoreTests.swift` | component | levers and keys from exitctl |
| `LoginFlowTests.swift` | component | the login steps |
| `ExitNodeTests.swift` | component | Tailscale status, the guard, the monitor |
| `ProcessRunnerTests.swift` | boundary | output, stderr, timeouts |
| `ExitctlTests.swift` | boundary | running exitctl: needs-input answers, options, failures, arguments |
| `ContractTests.swift` | contract | every sample the app reads decodes |
| `LiveTests.swift` | live | against the real controller and exitctl |

### Acceptance (`tests/acceptance`)

See [tests/acceptance/README.md](tests/acceptance/README.md): what each script proves,
where it runs, and what it needs.

## Running

    controller/.venv/bin/pytest -q controller
    prober/.venv/bin/pytest -q prober
    (cd helper && .venv/bin/pytest -q)
    (cd mac/ExitControl && swift test)
    tests/acceptance/run-client.sh            # from the Mac, against the live node

CI (`ci.yml`) runs everything but acceptance and live on every push.

## Adding a test

1. Find the concept in ARCHITECTURE.md; its test file is in the tables above.
2. Pick the lowest kind that can prove the behaviour.
3. Need a fake or a builder? Add it to the harness, not the test file.
4. A new wire format or field: update its sample in `contracts/` (`UPDATE_CONTRACTS=1`), so
   both sides' contract tests see it.
5. A new promise of the deployed system: a check in the acceptance script for that concept,
   and a row in its README.
