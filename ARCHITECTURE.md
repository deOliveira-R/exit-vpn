# Architecture

How exit-vpn is put together: the parts, the concepts each one is built from, and how they
connect. PLAN.md holds the decisions (D1–D21) and the history; this file describes the code as
it is. Wire formats between the parts are in [`contracts/`](contracts/README.md).

## The system

```
 Mac                                     tailnet                   VPS (Oracle, Docker)
┌──────────────────────────┐                              ┌──────────────────────────────────────┐
│ ExitControl (menu bar) ──┼── HTTP + SSE (owner only) ──►│ exit container  "exit-proton"        │
│   └─ runs exitctl --json │                              │   tailscaled (exit node, tag:exit)   │
│ exitctl ─────────────────┼── PUT keys / catalog ───────►│   controller (FastAPI, :8080)        │
│   └─ provider API, keys  │                              │   wg0 ──────────────────────────────►│── WireGuard ──► Proton server
│ other devices ───────────┼── exit-node traffic ────────►│   kill switch (nftables, table 100)  │
└──────────────────────────┘                              │ prober container (own netns)         │
                                                          │   wg-probe ─────────────────────────►│── WireGuard ──► candidate servers
                                                          └──────────────────────────────────────┘
```

- **Exit container**: a Tailscale exit node whose only way out is WireGuard to one Proton
  server (`wg0`). Kill switch and policy routing are set up before anything else and do not
  depend on Python: if the controller dies, traffic still cannot leave unprotected.
- **Controller**: decides *which* server wg0 talks to. It runs health checks and fails over,
  takes the owner's orders over HTTP, and holds the server catalog and the two key slots.
- **Prober**: a separate container with its own tunnel (key slot b). It tests other servers
  against a URL ("find a server this site accepts") without touching the exit.
- **exitctl**: the Mac-side provider client. It logs in to Proton (session in the Keychain,
  never on the VPS), makes and renews keys, and pushes the live server list.
- **ExitControl**: the menu-bar app, a thin client of the controller and of exitctl.

Who may do what: only the owner's untagged devices reach the controller (the tailnet policy
plus a whois check on every request; no tokens). Provider credentials never leave the Mac.

## Exit container: boot order and data plane

s6-overlay services, in order (`exit/rootfs/etc/s6-overlay/s6-rc.d`):

1. `init-sysctl`: forwarding and reverse-path settings inside the container.
2. `init-killswitch`: two layers. Routing: forwarded traffic uses table 100, whose fallback
   route is *unreachable*. nftables (`killswitch.nft`): exit traffic may leave only through
   wg0, and the underlay carries only WireGuard and Tailscale's own packets.
3. `init-wg`: wg0 from the slot-a config (uploaded key, else the bootstrap file), on the
   last good peer from `peer.env` if there is one, and table 100's default route via wg0.
4. `route-keeper`, `tailscaled`, `tailscale-up`: the kernel drops table-100 routes when wg0
   bounces; route-keeper restores them. tailscaled advertises the exit node.
5. `controller`: `python -m exit_vpn`.

Shared between the shell scripts and Python, and tested on both sides:
- `peer.env` (`PEER_NAME`, `PEER_PUBKEY`, `PEER_ENDPOINT`), written by the controller.
- The slot-a key precedence: `KEY_DIR_A/wg.conf` over the bootstrap config.

## Controller (`controller/src/exit_vpn`)

**Domain** (pure, no I/O):

| Concept | Module | What it is |
|---|---|---|
| Server, Region, features | `servers` | A server is identified by (public key, entry IP); its name is logical. Features: P2P (hard), streaming and IPv6 (soft). A region is a country or a city in it. |
| Catalog | `catalog` | Every server the exit may use: lookups by id, name and place, canonical spellings, ISO codes. Parsing and validation of gluetun files and pushed lists. |
| Prefs | `prefs` | Pinned regions in order, Secure Core mode, automatic failover, required features, favourites. Immutable: `updated()` validates a change against the catalog and returns a new Prefs. |
| State | `state` | `ActiveServer` (the peer wg0 uses, with the profile of features a pick keeps) and `Blacklist` (recently failed keys). |
| Selection | `selection` | Pure functions that order candidates: failover, rotation, region and exact picks, Secure Core switches, find. One set of rules (eligibility, tiers, favourites, profile, load). |
| Health | `health` | `HealthTracker` (starting → healthy ⇄ suspect → failing over → degraded) and `Backoff`. |

**Services** (async, own the lock, get their collaborators injected):

| Concept | Module | Responsibilities |
|---|---|---|
| ExitService | `service` | The active server, prefs, blacklist and catalog. One lock for every change. Runs the health cycle (`step`), failover and degraded retry, and the owner's changes (rotate, select, update_prefs, replace catalog, replace exit key). `snapshot()` is the status. |
| Tunnel | `tunnel` | The only code that changes wg0: check health, try a peer, try a private key. Each is judged by real traffic, never by a handshake. |
| EventBus | `events` | Every event: logged, kept (last 50, `events.json`), sent to SSE subscribers. |
| KeySlots | `slots` | Uploading keys. Slot a is tried on wg0, then saved. Slot b is saved, then the prober reloads it and tests it. |
| FindService | `finder` | "Find a server for this URL" jobs, run through the prober one at a time. |
| CatalogSource | `catalog_source` | Where the catalog comes from: cache, pinned download, or a validated push. |

**Adapters**:
- `wg`: the `wg` CLI.
- `probes.HealthProbe`: HTTPS, DNS and handshake checks bound to the tunnel address.
- `prober_client`: the prober's HTTP API.
- `auth`: whois.
- `persistence`: `state.json`, `peer.env`, `events.json`, `catalog.json`.
- `keys.KeyStore`: the key files.
- `api`: input shapes; errors mapped to HTTP (PolicyError → 400, SwitchError → 409).

**Composition**: `node.build()` wires everything, for production (`__main__`) and for the tests
(`tests/harness.py`), so tests run the real object graph with fake wg, probes and clock.

```
api ──► ExitService ──► Tunnel ──► WireGuard, HealthProbe
 │        ├──► selection (pure) ◄── Catalog, Prefs, State
 │        ├──► EventBus ──► EventStore, SSE subscribers
 │        └──► StateStore, CatalogCache
 ├──► KeySlots ──► ExitService.replace_exit_key, KeyStore, ProberClient
 ├──► FindService ──► ExitService.find_candidates / switch_to, ProberClient
 └──► CatalogSource
```

Rules worth knowing:
- **One lock.** Nothing changes wg0 concurrently: the health loop, failover and API requests all take ExitService's lock.
- **Failover keeps the pick's hard features.** A P2P server never fails over to a non-P2P one. No match anywhere → degraded, and the kill switch holds; it is never a silent downgrade.
- **Secure Core is a mode.** Turning it on or off switches servers at once. If nothing in the new mode works, nothing changes; `PUT /v1/prefs` is all-or-nothing.
- **The blacklist is per public key.** A failed server is avoided under all its entry IPs.

## Prober (`prober/src/prober`)

| Concept | Module | What it is |
|---|---|---|
| Model | `model` | `Target` (the server), `Observation` (what one engine saw), `Verdict`, `TierResult`, `ProbeResult` (the response), `Outcome`, `Engine`. |
| Classifier | `classify` | Ordered `RULES`: error, challenge, blocked, redirected, suspicious, inconclusive; the first match wins and every match is a signal. |
| Engines | `engines` | `HttpEngine` (curl_cffi, Chrome's TLS fingerprint, no JS) and `BrowserEngine` (headless Chromium over CDP), one interface; `Persona` (locale and timezone of the exit country). Engines never raise. |
| ExitCheck | `exitcheck` | Before anything else: the session carries traffic, does not exit from a forbidden (non-VPN) IP, and exits in the server's country. |
| ProbeTunnel | `tunnel` | wg-probe peer control; `session()` always detaches. |
| KeyFile | `keyfile` | Slot b's key: uploaded, else bootstrap (the same choice as `entrypoint.sh`). |
| Prober | `probe` | One probe, start to finish, under the lock; `needs_browser` is the escalation policy. |

`api` maps the HTTP request onto `Prober.run`; `__main__` builds the parts. The prober's
netns has its own kill switch: everything leaves through wg-probe or not at all.

## exitctl (`helper/src/exitctl`)

| Concept | Module | What it is |
|---|---|---|
| Model | `model` | `Lever` (an option bound to a key) and `resolve_levers`, `KeyMeta` (the facts the controller records), `NewKey`, `Certificate`, `Account`, and the errors: `ProviderError`, `NeedInput` (exit 3), `NodeError` (409 → exit 2). |
| Provider | `providers/base` | The adapter interface, with `Credentials`. `capabilities` decide which CLI commands exist. |
| Proton | `providers/proton` | The adapter (`__init__`). `session` holds the proton-core session and the login steps (password → CAPTCHA → 2FA, each a `NeedInput`). Also `crypto` (Ed25519 → X25519), `levers` and `catalog`. |
| Sessions | `sessions` | Where provider sessions live: the Keychain (`KeychainStore`) or memory (tests). |
| ExitNode | `exitnode` | The controller's API, with typed errors. |
| KeyService | `keyservice` | Rotate (make, swap, delete the old certificate; roll back if refused), renew, certificates with slots and orphans, import, push servers. `DeviceNaming` names the exit node's keys. |
| CLI | `cli` | Parses, asks (interactive login), prints (`Output`: JSON or human), maps errors to exit codes. |

Adding a provider: see [`helper/README.md`](helper/README.md).

## ExitControl (`mac/ExitControl`)

The core library (`Sources/ExitControlCore`, tested with `swift test`) holds everything but
the views:

| Concept | Files | What it is |
|---|---|---|
| Vocabulary | `Vocabulary` | `Health`, `RotationScope`, `Feature`, `FindMode`, `FindOutcome`, `SettingsTab`, `AppRoute`. Wire strings as types; unknown values are tolerated. |
| Wire models | `ControllerModels`, `Event`, `ExitctlModels`, `Geography` | The contract shapes; flags and continents. |
| Controller | `ControllerAPI`, `ControllerClient`, `EventStream` | `ControllerCommand` is every change the owner can ask for; one `perform`; `updates()` is the SSE stream (with `.incompatible` when a status can't be read). |
| Provider | `Exitctl`, `ProcessRunner` | `ProviderCommand`. `ProcessRunner` reads stdout and stderr concurrently, has a timeout, and stops the process on cancel. |
| ActionRunner | `ActionRunner` | One change at a time. Menu clicks are refused while busy; URLs and Shortcuts queue in order. |
| Stores | `ConnectionStore`, `CatalogStore`, `ProviderStore`, `ExitNodeMonitor`, `AppSettings` | Live status and events (which events notify); the server list; the account, levers and keys; whether this Mac uses the exit node; the app's settings. |
| Helpers | `ServerQuery`, `StatusText`, `LoginFlow`, `FindSession`, `ExitURL` | Browser filter and order; status lines and icon; the login steps; a find job; `exitvpn://` URLs. |

The app target (`Sources/ExitControl`):
- **`AppContainer`:** the composition root and the one place actions go through.
- **`Router`:** which window to open; the menu-bar label opens it.
- **Views:** the menu, Server Browser, Find, Settings and the login sheet.
- **`Intents`:** the Shortcuts actions.
- **`Updater`:** Sparkle.
- **`Notifier`.**

```
AppSettings ──► ControllerClient ──► ConnectionStore ──► Notifier
                               ├──► CatalogStore (reloads on catalog events)
            └─► Exitctl ───────┴──► ProviderStore
menu / URLs / Shortcuts ──► AppContainer ──► ActionRunner ──► ControllerClient.perform / ProviderStore.perform
```

Signing, releases and updates: [`mac/ExitControl/README.md`](mac/ExitControl/README.md).

## Contracts

Every wire format between the parts has a sample in `contracts/`. The producer's tests check
that it emits exactly that shape. The consumer's tests check that it reads it:

| Format | Producer → consumer |
|---|---|
| Controller API and events | controller → the app |
| Prober request | controller → prober |
| Prober response | prober → controller |
| exitctl output and exit codes | exitctl → the app |

A change to a format is a change to its sample (`UPDATE_CONTRACTS=1`), reviewed like code.

## Where to look

| To change… | Look at |
|---|---|
| which servers failover and rotation try | `controller/…/selection.py` |
| the health cycle, failover, degraded | `controller/…/service.py` (`step`, `_failover`), `health.py` |
| what a probe decides about a page | `prober/…/classify.py` (`RULES`), `probe.py` (`needs_browser`) |
| how keys are made, renewed, named | `helper/…/keyservice.py`, `providers/proton/` |
| what the menu shows | `mac/…/ExitControl/MenuContent.swift`, `ExitControlCore/StatusText.swift` |
| what notifies | `ExitControlCore/ConnectionStore.swift` (`notableKinds`), `AppContainer.notify` |
| a wire format | `contracts/` (both sides' tests) |
