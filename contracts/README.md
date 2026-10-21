# Contracts

The wire formats between the parts of exit-vpn, one sample file per format. Each format
has a **producer** and a **consumer**, and both test against the same sample:

| Format | Producer (checks it emits the sample's shape) | Consumer (checks it reads the sample) |
|---|---|---|
| `controller/*.json` | controller (`controller/tests/test_contracts.py`, events via `conftest.py`) | ExitControl (`ContractTests.swift`) |
| `prober/probe_request.json` | controller (`ProberClient`) | prober (`ProbeRequest`) |
| `prober/probe_response*.json`, `prober/healthz.json` | prober (`prober/tests/test_contracts.py`) | controller (`Finder`, via `find_job.json`) |
| `exitctl/*.json` | exitctl (`helper/tests/test_contracts.py`) | ExitControl (`ContractTests.swift`) |

The checker is `shape.py` (stdlib only):
- **Exact keys:** every object a producer emits has exactly the sample's keys.
- **Matching types:** each value has the sample value's JSON type.
- **Maps:** an object written `{"*": …}` is a map: any keys, each value shaped like `…`.
- **`null` means any:** `null` in a sample allows any value, for optional fields or values that are sometimes a number and sometimes a boolean (provider levers).
- **Lists:** a list sample has one element, the union of every element seen.
- **Optional keys:** events and a few named objects (key `meta`) may leave out keys of their sample, but may not add keys the sample lacks.

Consumers ignore fields they do not know and must tolerate `null` where the sample shows
one. The values in the samples are examples from the test fixtures, never real servers,
accounts or keys.

## Changing a contract

1. Change the producer.
2. Run its tests with `UPDATE_CONTRACTS=1`. The output is merged into the samples: new fields are added, fields added by hand stay, and removing a field means editing the file.
3. Review the diff of `contracts/` like code.
4. Update the consumer in the same commit, and run every suite: the three `pytest` suites and `swift test`.

The app updates itself and the exit node does not, so a field the app needs must stay optional until every node has it.

## Controller API (`http://exit-proton:8080/v1/…`, owner's devices only)

| Sample | Route |
|---|---|
| `status.json` | `GET /status`, and the result of `POST /rotate`, `POST /select`, `POST /find/{id}/apply`. Its `events` are the last 10 events (below); `catalog` is also the result of `PUT /catalog` and `DELETE /catalog` |
| `servers.json` | `GET /servers[?country=&city=&feature=]`; `countries` is null when `country` is given |
| `prefs.json` | `GET /prefs`, and the result of `PUT /prefs` |
| `keys.json`, `key_slot.json` | `GET /keys`; one slot is the result of `PUT /keys/{slot}` and `PUT /keys/{slot}/meta`. `meta` holds what exitctl recorded (`KeyMeta` in `api.py`); it is empty for a bootstrap key |
| `find_job.json` | `POST /find` (202) and `GET /find/{id}`; `results[]` are prober responses plus the candidate's `ip`, `country`, `city` |
| `events.json` | one sample per event kind (below) |

Errors are `{"detail": "…"}` with 400 (bad input), 404, 409 (nothing worked, or the change
was refused: the node keeps what it had), 502/503 (prober or pinned catalog unavailable).
Validation errors are 422 without echoing the request (it may carry keys).

**Live updates (`GET /events`, server-sent events):** first `event: status` with a status
object, then one `event: <kind>` per event with the event object as `data`, and a
`: keepalive` comment every 15 s. Clients refresh the status after events; a lost
connection means the controller is unreachable.

### Events

Every event has `ts` (Unix seconds) and `kind`; the other fields by kind:

| Kind | Fields |
|---|---|
| `candidate_failed` | `server` |
| `catalog` | `source`, `servers`, `before` |
| `catalog_reverted` | `by` |
| `degraded` | `tried` |
| `failover_done` | `old`, `new`, `exit_ip`, `seconds` |
| `failover_start` | `server`, `reason` |
| `find_started` | `by`, `url`, `job`, `candidates` |
| `health` | `state`, `errors` |
| `key_meta` | `slot`, `by`, `expires_at` |
| `key_rejected` | `slot`, `reason`, `restored_healthy` |
| `key_replaced` | `slot`, `reason`, `seconds` |
| `prefs` | `by`, `prefs` |
| `recovered` | `server` |
| `started` | `server` |
| `swap_error` | `server`, `error` |
| `switch_reverted` | `server`, `reason` |
| `switched` | `old`, `new`, `reason`, `exit_ip`, `seconds` |
| `unhealthy_no_failover` | `errors` |

A test fails if the controller can emit a kind that has no sample here.

## Prober (`http://172.30.42.10:9000`, the controller only)

- `POST /probe`, with the request in `probe_request.json`:
  - `mode`: `auto` (HTTP first, the browser when unsure), `http`, or `browser`.
  - `js`: always also use the browser.
  - `forbid_ips`: exit addresses that prove the tunnel is not in use; the host's own address goes here.
  - An empty `country_code` skips the country check.
- **Replies:** `probe_response.json`, with one entry in `tiers` per engine that ran. When the tunnel fails, the reply is `probe_response_error.json`: no `exit_ip` or `exit_loc`, and `outcome: "error"`.
- **Outcomes:** `ok`, `blocked`, `challenge`, `redirected`, `suspicious`, `inconclusive`, `error`.
- **Other routes:** `GET /healthz` (`healthz.json`), and `POST /key/reload`, which re-reads the slot b key.

## exitctl (`exitctl … --json`, run by the app)

| Sample | Command |
|---|---|
| `providers.json` | `exitctl providers` |
| `options.json` | `exitctl <provider> options`; `kind` is `bool` or `choice`, and `choices` maps a value to its label |
| `login.json`, `whoami.json`, `logged_out.json` | `exitctl <provider> login --stdin` (stdin: `{username, password, totp, hv_token, hv_type}`), `whoami`, and `whoami` or `logout` when logged out |
| `list.json` | `exitctl <provider> list`; `slot` is the slot that uses the certificate, and `orphan` marks an exit-node certificate no slot uses |
| `keys.json` | `exitctl keys` (the controller's `keys.json`) |
| `rotate.json`, `renew.json` | `exitctl <provider> rotate --slot a|b [--<lever> …]`, `renew --slot a|b` |
| `servers_push.json` | `exitctl <provider> servers --push` |
| `error.json`, `need_*.json` | errors (below) |

Exit codes:
- **0:** done; the result is on stdout.
- **1:** error, written as `error.json` on stdout.
- **2:** the exit node refused (409); same `error.json` shape.
- **3:** more input needed. The reply is `need_totp.json`, or `need_human_verification.json` (open `url` and retry with `hv_token`). A login stopped at 2FA continues in a new process: the half-open session waits in the Keychain.

In `--json` mode errors go to **stdout**, and progress messages are not printed.
