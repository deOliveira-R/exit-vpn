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
│   └─ provider API, keys  │                              │   wg0 / wg1 (active, standby) ──────►│── WireGuard ──► two Proton servers
│ other devices ───────────┼── exit-node traffic ────────►│   kill switch (nftables, table 100)  │
└──────────────────────────┘                              │ prober container (own netns)         │
                                                          │   wg-probe ─────────────────────────►│── WireGuard ──► candidate servers
                                                          └──────────────────────────────────────┘
```

- **Exit container**: a Tailscale exit node whose only way out is WireGuard to Proton. Two
  tunnels: the *active* one carries forwarded traffic, the *standby* one (key slot c) is kept
  ready on another server of the same region, so a failing server is replaced by one route
  change (a *handover*). Keys stay with interfaces (`wg0`: slot a, `wg1`: slot c); the roles
  move. Kill switch and policy routing are set up before anything else and do not depend on
  Python: if the controller dies, traffic still cannot leave unprotected.
- **Controller**: decides which servers the tunnels talk to and which tunnel is active. It runs
  health checks on both, hands over or fails over, takes the owner's orders over HTTP, and
  holds the server catalog and the key slots. Everything that decides runs here, on the VPS.
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
2. `init-killswitch`: two layers. Routing: forwarded traffic uses table 100 (default via the
   active tunnel); each tunnel's own sockets (bound to it: the health probes) use its own table
   (102 for wg0, 103 for wg1); every table falls back to *unreachable* (IPv4 and IPv6).
   nftables (`killswitch.nft`): exit traffic may leave only through a tunnel, forwarded IPv6
   only while the active tunnel is in the `ipv6_gate` set, and the underlay carries only
   WireGuard and Tailscale's own packets. Each forwarded connection is marked with the tunnel
   it was opened through; after a handover its packets would leave through the other tunnel
   with a source the far end ignores, so they are refused (TCP reset) and clients reconnect at
   once instead of stalling.
3. `init-wg`: wg0 from the slot-a config (uploaded key, else the bootstrap file) on the last
   good peer (`peer.env`), and wg1 from slot c's (`standby.env`; without a slot c key wg1 is
   up, keyless, until one is uploaded). Both carry the same IPv4 address; wg0 starts as the
   active tunnel. The config's IPv6 address (unless `EXIT_IPV6=off`) becomes a local route on
   `lo`, not an address of a tunnel: an interface address would become the source of every
   unbound IPv6 socket in the container and pull tailscaled's own traffic into the tunnel. The
   health probe binds to it (`ip_nonlocal_bind`), forwarded IPv6 is SNATed to it. IPv6 routes
   via the tunnels exist even with IPv6 off, so forwarded IPv6 meets the kill switch and is
   refused at once (the gate never opens) instead of stalling on an unreachable route.
4. `route-keeper`, `tailscaled`, `tailscale-up`: route-keeper owns the routes via the tunnels.
   The kernel drops them when a tunnel bounces, and route-keeper restores them; table 100's
   default goes to the active tunnel named in `/run/exit-vpn/active` (the controller writes it,
   and changes the route itself at a handover: `ip route replace`, one netlink message). It
   also applies the **IPv6 gate** (the file `/run/exit-vpn/ipv6`, written by the controller):
   while it exists, the active tunnel is in the kill switch's `ipv6_gate` set; otherwise new forwarded IPv6 connections are
   refused at once (TCP reset, else ICMP) and clients use IPv4, while connections already open
   carry on (a close never resets them). An unreachable route would not do: Tailscale
   clients drop its ICMP error and stall. tailscaled advertises the exit node (`0.0.0.0/0`, `::/0`).
5. `controller`: `python -m exit_vpn`.

Shared between the shell scripts and Python, and tested on both sides:
- `peer.env` and `standby.env` (`PEER_NAME`, `PEER_PUBKEY`, `PEER_ENDPOINT`): the active and
  standby servers, written by the controller.
- The key precedence for slots a and c: `KEY_DIR_A/wg.conf` / `KEY_DIR_C/wg.conf` over the
  bootstrap configs.
- The active tunnel file (`ACTIVE_FILE`, `Settings.active_file`).
- The tunnel addresses read from that config (`common.sh load_conf`, `wgconf`), `EXIT_IPV6`, and
  the IPv6 gate file (`IPV6_GATE`, `Settings.ipv6_gate_file`).

## Controller (`controller/src/exit_vpn`)

**Domain** (pure, no I/O):

| Concept | Module | What it is |
|---|---|---|
| Server, Region, features | `servers` | A server is identified by (public key, entry IP); its name is logical. Features: P2P (hard), streaming and IPv6 (soft). A region is a country or a city in it. |
| Catalog | `catalog` | Every server the exit may use: lookups by id, name and place, canonical spellings, ISO codes. Parsing and validation of gluetun files and pushed lists. |
| Prefs | `prefs` | Pinned regions in order, Secure Core mode, automatic failover, required features, favourites. Immutable: `updated()` validates a change against the catalog and returns a new Prefs. |
| State | `state` | `TunnelServer` (the server a tunnel talks to, with the profile of features a pick keeps), the active and standby ones, and `Blacklist` (recently failed keys). |
| Selection | `selection` | Pure functions that order candidates: failover, rotation, region and exact picks, Secure Core switches, find. One set of rules (eligibility, tiers, favourites, profile, load). |
| Health | `health` | `HealthTracker` (starting → healthy ⇄ suspect → failing over → degraded), `Backoff`, and `IPv6Gate` (closed on every server or key change, open after one good IPv6 probe, closed after three failed in a row; after a close on the same server, reopened only after 3, 6, 12, then 24 good probes in a row). |

**Services** (async, own the lock, get their collaborators injected):

| Concept | Module | Responsibilities |
|---|---|---|
| ExitService | `service` | The two tunnels and their roles, prefs, blacklist and catalog. Runs the active health cycle (`step`: a failing active tunnel is handed over to a standby that passes a fresh check; if the standby fails too, the uplink is suspected and nothing changes for up to `uplink_hold`; without a standby, failover and degraded retry), the standby cycle (`standby_step`: keep it on a fitting server of the active server's country, find another when it fails or no longer fits), and the owner's changes (rotate, select, find: made on the standby, then handed over; update_prefs, replace catalog, replace a tunnel's key). `snapshot()` is the status. |
| Tunnel | `tunnel` | One exit tunnel (wg0 or wg1), the only code that changes its interface: check health, try a peer, try a private key. Each is judged by real traffic, never by a handshake. On the active tunnel each cycle's IPv6 probe feeds the IPv6 gate, which every peer or key change and every handover closes first. |
| EventBus | `events` | Every event: logged, kept (last 50, `events.json`), sent to SSE subscribers. |
| KeySlots | `slots` | Uploading keys. Slots a and c are tried on their tunnel (wg0, wg1), then saved; a first slot c key brings the standby up. Slot b is saved, then the prober reloads it and tests it. |
| FindService | `finder` | "Find a server for this URL" jobs, run through the prober one at a time. |
| CatalogSource | `catalog_source` | Where the catalog comes from: cache, pinned download, or a validated push. |

**Adapters**:
- `wg`: the `wg` CLI.
- `probes.HealthProbe`: HTTPS, DNS and handshake checks bound to the tunnel's interface (both
  tunnels share one address), and on the active tunnel an
  IPv6 check bound to its IPv6 address (reported apart: it never decides health; it has its own,
  longer timeout and runs across cycles, so a slow first IPv6 connection neither fails it nor
  delays the IPv4 checks).
- `routes.GateFile`: the IPv6 gate file route-keeper reads. `routes.ActiveRoute`: the handover
  (table 100's default, and the active tunnel file route-keeper reads).
- `prober_client`: the prober's HTTP API.
- `auth`: whois.
- `persistence`: `state.json`, `peer.env`, `standby.env`, `events.json`, `catalog.json`.
- `keys.KeyStore`: the key files.
- `api`: input shapes; errors mapped to HTTP (PolicyError → 400, SwitchError → 409).

**Composition**: `node.build()` wires everything, for production (`__main__`) and for the tests
(`tests/harness.py`), so tests run the real object graph with fake wg, probes and clock.

```
api ──► ExitService ──► Tunnel ×2 (active, standby) ──► WireGuard, HealthProbe
 │        ├──► ActiveRoute (the handover)
 │        ├──► selection (pure) ◄── Catalog, Prefs, State
 │        ├──► EventBus ──► EventStore, SSE subscribers
 │        └──► StateStore, CatalogCache
 ├──► KeySlots ──► ExitService.replace_exit_key, KeyStore, ProberClient
 ├──► FindService ──► ExitService.find_candidates / switch_to, ProberClient
 └──► CatalogSource
```

Rules worth knowing:
- **Two locks, one order.** Nothing changes a tunnel concurrently: changes to the active tunnel take `lock`, to the standby `standby_lock`, a handover both (that order). The standby's searches never hold up the active tunnel's health cycle.
- **A handover never moves the exit elsewhere.** The standby is always in the active server's country, eligible under the prefs and the pick's hard features. Owner switches are made before breaking: brought up on the standby, then handed over; if no pick works, the exit is unchanged.
- **Second opinion.** Both tunnels failing at once means the uplink (or the test site): no handover, no failover, no blacklisting, for up to `uplink_hold`. The standby's own failed servers are avoided by the standby only, never blacklisted for the active tunnel (a bad standby key must not steer failover); several in a row make its key suspect, and it stops.
- **Failover keeps the pick's hard features.** A P2P server never fails over to a non-P2P one. No match anywhere → degraded, and the kill switch holds; it is never a silent downgrade.
- **Secure Core is a mode.** Turning it on or off switches servers at once. If nothing in the new mode works, nothing changes; `PUT /v1/prefs` is all-or-nothing.
- **The blacklist is per public key.** A failed server is avoided under all its entry IPs.
- **IPv6 is open only while proven.** A server without IPv6 drops it silently, which stalls
  clients; the closed gate refuses new IPv6 connections at once instead, and never resets open
  ones. IPv6 never causes a failover.

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
netns has its own kill switch: everything leaves through wg-probe or not at all. It is IPv4
only (its IPv6 table holds just an unreachable route): probes judge the IPv4 exit.

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
| Guard | `ExitNodeGuard` | Fed by `ExitNodeMonitor`: re-selects the exit node when this Mac has none (unless turned off or paused by "Go Direct"); leaves another exit node and Tailscale-off alone. |
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
| which test covers what, where a new one goes | `TESTING.md` |
