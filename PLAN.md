# exit-proton — Build Plan

ProtonVPN-backed Tailscale exit node, hosted on the existing Oracle Always-Free VPS (alongside `fin-intel`), with a controller API, a server prober, a macOS menu-bar controller, and (later) a home/Brazil residential allow-list via Tailscale app connectors.

---

## 0. Instructions for Claude Code

- Work **one phase at a time**. Each phase ends with acceptance tests that the owner runs on the real VPS / Mac and pastes back. Do not start the next phase until the current phase's tests pass.
- Claude has a shell on the VPS via the `fin-intel` SSH alias (see `local/`, gitignored). Run checks directly; for anything disruptive or outward-facing, confirm with the owner first. Keep commands copy-paste safe for zsh and bash (no unquoted globs, no smart quotes).
- **Never** commit secrets. `.env`, `secrets/`, `*.key`, `*.conf` with private keys → `.gitignore` from the first commit. Never echo secrets in logs or chat.
- **Do not touch `fin-intel`** or the host's existing Tailscale node. Every host-level change must be listed explicitly in the phase output (see §6 "Host changes").
- Phase 0 contains assumptions that were not fully verified. Verify them first; if one is false, stop and propose a design change instead of working around it silently.
- Language choices: controller and prober in **Python 3.12** (FastAPI, httpx, Playwright). Mac app in **Swift/SwiftUI** (`MenuBarExtra`, macOS 14+). Shell for entrypoints. nftables for firewalling.
- Tests: pytest for controller/prober logic (catalog, failover policy, classifier); shell scripts under `tests/acceptance/` for network behavior.

---

## 1. Goal and non-goals

**Goal**
1. A tailnet exit node `exit-proton` whose internet egress is a ProtonVPN WireGuard tunnel, with a hard kill switch (no packet ever leaves with the Oracle IP).
2. Automatic detection of silent tunnel death and cold failover to another Proton server within ~20 s.
3. A controller API: status, rotate, select country/city/server, "find a Proton server that this URL doesn't block".
4. A macOS menu-bar app driving the controller.
5. Later: per-domain routing of VPN-hostile sites through a residential IP (home (US); family home (BR)) using Tailscale app connectors.

**Non-goals**
- Hot standby / dual concurrent active tunnels (rejected: WireGuard handshake is ~1 RTT; outage time is dominated by detection, and switching exit IP kills existing TCP flows anyway).
- Wrapping or modifying Proton's client apps.
- Hard isolation from `fin-intel` (accepted risk: same host kernel; logical separation via separate container + separate Tailscale identity is sufficient).

---

## 2. Fixed decisions (with rationale)

| # | Decision | Why |
|---|---|---|
| D1 | Host on the existing Oracle A1 instance (Always Free is now 2 OCPU / 12 GB total for A1) | Splitting into two instances halves CPU for both with no meaningful safety gain at this risk level |
| D2 | `exit-proton` is a **separate Docker container with its own network namespace and its own tailscaled** → appears as a separate tailnet node | Separate identity, tag, ACLs; fin-intel untouched |
| D3 | Tailscale runs **inside the container but its underlay egresses directly** (Docker bridge → host NAT → Oracle IP), not through Proton | Avoids double tunnel traversal and DERP-relay fallback from NAT traversal through Proton |
| D4 | Only **forwarded** traffic (arriving on the container's `tailscale0`) is policy-routed into `wg0` | Clean separation of underlay vs exit traffic |
| D5 | Kill switch = **two independent layers**: routing (`unreachable` in table 100) + nftables (drop forwarded traffic from `tailscale0` to anything but `wg0`) | Routing alone is sufficient in theory; nftables guards against misordered rules/races |
| D6 | tailscaled in **kernel mode** (real TUN), never userspace networking | Userspace mode forwards via local sockets, which follow the main table → Oracle IP → bypasses Proton |
| D7 | **Cold failover** with aggressive health probing (5 s interval, 3 strikes) | See non-goals |
| D8 | One WireGuard interface, server switch via `wg set` peer swap (no interface teardown) | Same key works across Proton servers (gluetun relies on this); sub-second swap |
| D9 | Two Proton WireGuard keys: **A = active tunnel**, **B = prober** | Avoid same key/same 10.2.0.2 on one server concurrently; independent device slots |
| D10 | **Single container with s6-overlay** supervising: routing init → wg init → tailscaled → controller | Sidecars sharing a netns (`network_mode: service:`) break when the owner restarts |
| D11 | Prober is a **separate container** with its own netns and its own tunnel (key B) | Probing never disturbs the active exit |
| D12 | Proton server catalog sourced from **`qdm12/gluetun-servers` → `pkg/servers/protonvpn.json`** (split out of the gluetun repo, which now lives at `passteque/gluetun`), pinned to a commit, checked daily | Maintained list with WireGuard pubkeys; upstream updates are infrequent (last: 2026-08-06), so failover must tolerate stale entries. Proton's own API optional for load |
| D13 | Controller auth = Tailscale identity (**LocalAPI `whois`**) + ACL grant restricted to the owner's devices | No tokens to manage |
| D14 | Tailscale **API token (policy-file write) lives only on the Mac** (Keychain), never on any server | Policy write = full tailnet control; a compromised VPS must not be able to rewrite ACLs |
| D15 | Residential exits/allow-list via **app connectors** (Phase 6/7), as separate tailnet nodes | Per-domain routing that coexists with the Proton exit node; app connectors are on all plans |
| D16 | **Provider-agnostic core, Proton as the first provider.** A `provider` module supplies: catalog source (gluetun-servers has the same schema for Mullvad, AirVPN, IVPN, …), tunnel address and DNS (Proton: `10.2.0.2/32`, `10.2.0.1`; other providers assign per key), and feature metadata. Routing, kill switch, health, failover and API never hard-code Proton values. Node name = `exit-<provider>` (`exit-proton`); repo/stack = `exit-vpn` | Someone else on the tailnet may use another VPN; also lets us switch providers without a redesign |
| D17 | Proton **connection options are bound to the key**, not the config file: the dashboard registers the WireGuard public key with a signed certificate carrying `NetShieldLevel`, `SplitTCP` (VPN Accelerator), `RandomNAT` (off = Moderate NAT) and `PortForwarding` (NAT-PMP). The `# NetShield = 1` lines in the downloaded `.conf` are comments only. So one key keeps its options on every server, and changing an option means generating a new key. We store the full downloaded `.conf` (mode 600) and parse `PrivateKey` plus the option comments, so `/v1/status` can report them | Verified against Proton's open-source client (`python-proton-vpn-api-core`, `/vpn/v1/certificate`) and support docs |
| D18 | **VPN credentials helper runs on the Mac, never on a server.** Proton configs are created, listed, rotated and deleted through Proton's internal API (the same calls the web dashboard makes: `POST/GET/DELETE vpn/v1/certificate`, `Mode: persistent`, `Features`), authenticated by a Proton session (SRP login + 2FA) kept in the macOS Keychain. Only the resulting WireGuard key reaches the VPS | A Proton session grants the whole Proton account (Mail, Drive), so it gets the same treatment as the policy-write token in D14. Undocumented API → the helper is a convenience; manual dashboard generation stays the fallback |

---

## 3. Architecture

```
                         ┌──────────────────────────── Oracle A1 VPS ─────────────────────────────┐
                         │ host netns: ens3 (Oracle IP) · host tailscaled = node "fin-intel"      │
                         │             fin-intel + MCP (unchanged)                                 │
                         │                                                                         │
Mac (tailnet) ──underlay─┼──► docker NAT ──► ┌───────── container exit-proton (own netns) ───────┐ │
  exit node = exit-proton│   udp/41642       │ eth0  (docker bridge "exitnet", default route)     │ │
                         │                   │ tailscale0  (tailscaled #2, node "exit-proton",     │ │
                         │                   │              tag:exit, --port 41642)                │ │
                         │                   │ wg0   (Proton, key A, 10.2.0.2/32, Table=off)       │ │
                         │                   │ controller (FastAPI on tailnet IP:8080)             │ │
                         │                   └─────────────────────────────────────────────────────┘ │
                         │                   ┌───────── container prober (own netns) ─────────────┐ │
                         │                   │ eth0 (exitnet) · wg-probe (Proton, key B)          │ │
                         │                   │ headless Chromium (Playwright) · API on exitnet:9000│ │
                         │                   └─────────────────────────────────────────────────────┘ │
                         └─────────────────────────────────────────────────────────────────────────┘
Phase 6:  NAS (TrueNAS) container "home-gw": tailscaled node, tag:connector-home → app connector + residential exit + home probe agent
Phase 7:  Family home (BR): Raspberry Pi node, tag:connector-br → app connector + residential exit (Apple TV can be an exit node but app connectors require Linux)
```

### 3.1 Routing inside `exit-proton` netns

```
# table 100 = "exit"
ip    route add unreachable default metric 1000 table 100
ip -6 route add unreachable default metric 1000 table 100
ip    route add default dev wg0 table 100            # added after wg0 is up; removed never (wg0 persists)

# rules (Tailscale's own rules live at 5210–5270; ours must precede them)
ip    rule add priority 90  to 100.64.0.0/10        lookup 52   # tailnet destinations → Tailscale's table
ip -6 rule add priority 90  to fd7a:115c:a1e0::/48  lookup 52
ip    rule add priority 100 iif tailscale0          lookup 100  # forwarded exit traffic → Proton
ip -6 rule add priority 100 iif tailscale0          lookup 100  # (v6 only ever hits unreachable)
ip    rule add priority 110 from 10.2.0.2           lookup 100  # health probes bound to wg0
ip    rule add priority 120 to 10.2.0.1             lookup 100  # Proton DNS only via tunnel
```

Everything else (tailscaled's own control/DERP/peer traffic, catalog downloads) uses the main table → `eth0` → Oracle IP.

### 3.2 Firewall inside `exit-proton` netns (nftables, own table)

```
table inet killswitch {
  chain fwd {
    type filter hook forward priority -10; policy accept;
    iifname "tailscale0" oifname != "wg0" drop
  }
  chain out {
    type filter hook output priority -10; policy accept;
    ip daddr 10.2.0.1 oifname != "wg0" drop      # Proton DNS must never leave via eth0
  }
}
table ip nat_exit {
  chain post { type nat hook postrouting priority 100; oifname "wg0" masquerade; }
}
table inet mss {
  chain fwd { type filter hook forward priority 0; oifname "wg0" tcp flags syn tcp option maxseg size set rt mtu; }
}
```

Note: tailscaled installs its own `ts-forward` iptables(-nft) chains that ACCEPT forwarded tailnet traffic. A drop in **any** nftables base chain is final, so the separate `killswitch` table still wins. Verify (Phase 0, item V7).

### 3.3 DNS

- Container `resolv.conf`: `nameserver 10.2.0.1` (compose `dns: [10.2.0.1]`).
- Clients using the exit node send DNS to the exit node's tailscaled (PeerAPI DoH), which resolves via the container's resolv.conf → Proton DNS through the tunnel → **no DNS leak to Oracle**.
- If wg0 is down, DNS fails closed. tailscaled has bootstrap-DNS fallback for its own control plane (verify V8).
- Keep any existing Tailscale split-DNS for `<home split-DNS domain>` as is.

### 3.4 Startup ordering (s6-overlay)

1. `init-sysctl` (oneshot): assert `net.ipv4.ip_forward=1`, `net.ipv6.conf.all.forwarding=1` (namespaced; set via compose `sysctls`), `rp_filter` loose (2) on all/default.
2. `init-killswitch` (oneshot): nftables tables + ip rules + `unreachable` routes. **Must complete before anything else.**
3. `init-wg` (oneshot): `ip link add wg0 type wireguard`; `wg set wg0 private-key <PrivateKey parsed from /secrets/proton_a.conf>`; addr `10.2.0.2/32`; peer = last-good server from state (or default country); `persistent-keepalive 25`; `ip link set wg0 mtu 1420 up`; add `default dev wg0 table 100`.
4. `tailscaled` (longrun, depends on 2): `tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock --port=41642 --tun=tailscale0`.
5. `tailscale-up` (oneshot, depends on 4): `tailscale up --authkey=file:/secrets/ts_authkey --hostname=exit-proton --advertise-exit-node --advertise-tags=tag:exit` (idempotent; skip authkey if state exists).
6. `controller` (longrun, depends on 3 and 5).

---

## 4. Repository layout

```
exit-proton/
├── PLAN.md                      # this file
├── CLAUDE.md                    # short: conventions + pointer to PLAN.md
├── compose.yaml
├── .env.example                 # documents variables; real .env is gitignored
├── secrets/ (gitignored)        # proton_a.conf, proton_b.conf, ts_authkey_exit, ts_authkey_prober?(none)
├── exit/
│   ├── Dockerfile               # debian:bookworm-slim + tailscale (pkgs.tailscale.com) + wireguard-tools
│   │                            # + nftables + iproute2 + python3.12 venv + s6-overlay
│   ├── rootfs/etc/s6-overlay/s6-rc.d/{init-sysctl,init-killswitch,init-wg,tailscaled,tailscale-up,controller}/
│   └── nft/killswitch.nft
├── controller/                  # python package
│   ├── pyproject.toml
│   ├── src/exit_proton/
│   │   ├── catalog.py           # server list fetch/parse/filter
│   │   ├── wg.py                # thin wrapper over `wg`/`ip` (subprocess), peer swap
│   │   ├── health.py            # probes + state machine
│   │   ├── failover.py          # candidate selection policy
│   │   ├── state.py             # persisted state (current server, blacklist, prefs)
│   │   ├── auth.py              # LocalAPI whois
│   │   ├── prober_client.py
│   │   └── api.py               # FastAPI app
│   └── tests/
├── prober/
│   ├── Dockerfile               # mcr.microsoft.com/playwright/python (arm64) + wireguard-tools
│   ├── entrypoint.sh            # killswitch for prober netns, wg-probe init
│   └── src/prober/{api.py,tunnel.py,classify.py}
├── mac/ExitControl/             # Xcode project (SwiftUI MenuBarExtra)
├── home-gw/                     # Phase 6: TrueNAS custom app compose + home probe agent
├── tests/acceptance/            # shell scripts, run by the owner
└── docs/{runbook.md,security.md,acl.hujson.example}
```

---

## 5. Inputs the owner must provide

| Item | How | Notes |
|---|---|---|
| Proton WireGuard **config A** (exit) | account.proton.me → Downloads → WireGuard configuration; any server (D17) | NetShield: owner's choice (applies to exit clients' DNS via 10.2.0.1); Moderate NAT **off**; NAT-PMP **off**; VPN Accelerator **on**. Upload the whole `.conf` to `secrets/proton_a.conf` |
| Proton WireGuard **config B** (prober) | same | NetShield **off** (so probes see sites unfiltered, like a normal browser); Moderate NAT off; NAT-PMP off; VPN Accelerator on → `secrets/proton_b.conf` |
| Tailscale **tagged auth key** for `tag:exit` | Admin → Settings → Keys → generate, tagged `tag:exit`, pre-approved, non-reusable | Tag must exist in policy first (Phase 2) |
| VPS facts | `uname -m; cat /etc/os-release; docker --version; ip -br a; sudo iptables -S \| head -50; sudo nft list ruleset \| head -50` | Arch (expect aarch64), OS, whether Docker exists |
| Current tailnet policy file | Admin → Access controls → copy | To extend, not replace |
| Current Tailscale DNS settings | Admin → DNS | Split DNS / global nameservers / MagicDNS |
| Repo host | GitHub | |
| Default exit country preference | **US, then CH** (decided 2026-10-20) | Used for initial server and failover. A richer preference model (per-slot/per-key countries, cities, features) comes with the Phase 3b helper / Phase 5 app |

---

## 6. Phases

### Phase 0 — Verify assumptions (no building yet)

| ID | Assumption | Confidence | How to verify |
|---|---|---|---|
| V1 ✅ | A Proton WireGuard private key works against **any** Proton server's endpoint + pubkey | High (gluetun takes one key and selects servers) | Manual `wg` test on the VPS in a throwaway netns with key A against two different servers; `curl --interface` cdn-cgi/trace shows two different exit IPs |
| V2 ✅ | Keys A and B can be connected **concurrently** (to different servers) | Medium-high | Same test, both keys up at once |
| V3 ✅ | gluetun-servers `protonvpn.json` entries include `wgpubkey`, `country`, `city`, `server_name`/`hostname`, `ips`, feature flags (secure_core, tor, port_forward, stream) | Medium (schema details) | Download from the gluetun repo, inspect schema, write parser test fixtures from a real excerpt |
| V4 ✅ | Host kernel has WireGuard; Docker present (or installable) on the A1 instance | High | `modinfo wireguard` or `ip link add t0 type wireguard && ip link del t0` |
| V5 | OCI ingress for UDP 41642 can be opened (security list) and host firewall allows Docker-published UDP | Medium (OCI Ubuntu images ship restrictive iptables incl. FORWARD REJECT) | Inspect rules; after Phase 2, `tailscale ping exit-proton` from the Mac shows a direct path |
| V6 | tailscaled in kernel mode works inside an unprivileged container with `NET_ADMIN`, `NET_RAW`, `/dev/net/tun` | High | Phase 1 smoke |
| V7 | A drop in our own nftables table overrides tailscaled's `ts-forward` ACCEPT; tailscaled uses the nf_tables backend (iptables-nft) in this image | Medium-high | Phase 1 test T2 (kill switch) must pass with tailscaled running |
| V8 | With wg0 down and resolv.conf → 10.2.0.1, tailscaled stays connected (bootstrap DNS / cached DERP map) | Medium | Phase 2 test T2b: down wg0 for 5 min, node stays online in admin console |
| V9 | App connector `/32` routes take precedence over the exit node default route on macOS/iOS | Medium-high (more specific route) | Phase 6 test T13 |
| V10 | App connector device may sit behind home NAT (docs say "must have a public IP address") | Medium (community setups run at home) | Phase 6 smoke |
| V11 | Playwright Python image available for arm64 | High | `docker pull --platform linux/arm64 mcr.microsoft.com/playwright/python:<tag>` |

**Exit criterion:** V1–V4 confirmed; V5–V11 have a verification hook in later phases.

**Phase 0 results (2026-10-20)** — V1–V4 pass (`tests/acceptance/phase0-wg.sh`; key A swapped US-MI→US-DC and CH→CH with distinct Proton exit IPs, key B concurrently on a third server). Findings that change the design:
- **Session warm-up:** a fresh Proton session (new interface *or* peer swap) drops TCP data for ~4–6 s while the server probes the path (growing ICMP echoes from 10.2.0.1). Handshake and ping to 10.2.0.1 succeed during that window. → Failover and rotation must allow a ≤ 15 s warm-up before judging a candidate, and the health loop must not count warm-up failures. Revised targets: failover ≤ 35 s (20 s detection + warm-up), rotate ≤ 10 s.
- **Handshake ≠ authorised:** handshakes complete even for unregistered keys (see Phase 3 health note).
- **Rule 120 is required:** without `to 10.2.0.1 lookup 100`, DNS queries to 10.2.0.1 leave via eth0 (observed: `dig @10.2.0.1` timed out via the underlay).
- **Catalog staleness:** the server Proton assigned to config A today (US-CA#556) is absent from gluetun-servers (2026-08-06). Failover tolerates dead entries; Phase 3b's helper can also fetch the live server list with the Proton session and push it to the controller (preferred catalog when present; gluetun-servers as fallback).
- **`Bouncing` label:** key A (created for US-CA#556) works on other servers; the label does not restrict use (open question 7 closed). It appears on regular configs too, so it is not Secure Core specific.
- **Secure Core needs no special key:** key A on CH-US#3, IS-US#1, SE-US#1 entered via CH/IS/SE and exited with US IPs. Warm-up on the IS route was 14 s. → Secure Core is an opt-in lever (`features.secure_core` in prefs, default off), selected purely by catalog entry.
- Configs A, B (US) and spare C are in `/opt/exit-node/secrets/proton_{a,b,c}.conf` (root, 0600), all NetShield 0 / Moderate NAT off / NAT-PMP off / VPN Accelerator on.

### Phase 1 — Data plane container (no Tailscale yet)

Build `exit/` image and compose service `exit-proton` with tailscaled **disabled** (s6 service down) to test routing in isolation.

Compose essentials:

```yaml
services:
  exit-proton:
    build: ./exit
    container_name: exit-proton
    hostname: exit-proton
    cap_add: [NET_ADMIN, NET_RAW]
    devices: [/dev/net/tun:/dev/net/tun]
    sysctls:
      net.ipv4.ip_forward: "1"
      net.ipv6.conf.all.forwarding: "1"
      net.ipv4.conf.all.rp_filter: "2"
    dns: [10.2.0.1]
    ports: ["41642:41642/udp"]
    volumes:
      - ./secrets:/secrets:ro
      - exit-state:/var/lib/exit-proton
      - ts-state:/var/lib/tailscale
    networks: [exitnet]
    restart: unless-stopped
networks:
  exitnet: {}
volumes: { exit-state: {}, ts-state: {} }
```

Deliverables: Dockerfile, s6 service tree, `killswitch.nft`, `init-wg` script (initial peer from a hardcoded server for now), `tests/acceptance/phase1.sh`.

Acceptance (run inside container via `docker exec`):
- **P1.1** `curl -s --interface wg0 https://www.cloudflare.com/cdn-cgi/trace` → `ip=` is a Proton IP, `loc=` matches the server country.
- **P1.2** `curl -s https://www.cloudflare.com/cdn-cgi/trace` (no interface) → Oracle IP (main table untouched: underlay path works).
- **P1.3** `dig @10.2.0.1 example.com` answers; `nft monitor`/counters show it left via wg0.
- **P1.4** `ip link set wg0 down` → `curl --interface wg0 …` fails fast (unreachable), never returns Oracle IP. Restore.
- **P1.5** Host check: `curl -s https://www.cloudflare.com/cdn-cgi/trace` on the **host** → Oracle IP; fin-intel MCP reachable as before.

### Phase 2 — Tailscale node + ACL

1. Extend the tailnet policy (keep existing `tag:nas`, `tag:server` rules):

```jsonc
"tagOwners": {
  "tag:nas":    ["autogroup:admin"],
  "tag:server": ["autogroup:admin"],
  "tag:exit":   ["autogroup:admin"]
},
"autoApprovers": { "exitNode": ["tag:exit"] },
"grants": [
  // ...existing...
  {"src": ["autogroup:member"], "dst": ["autogroup:internet"], "ip": ["*"]},   // may use exit nodes
  {"src": ["autogroup:member"], "dst": ["tag:exit"], "ip": ["tcp:8080", "icmp:*"]}  // controller API + ping
  // no grants with src tag:exit → it reaches nothing on the tailnet
],
"tests": [
  {"src": "tag:exit",   "deny":   ["tag:server:22", "tag:nas:443", "<NAS_LAN_IP>:443"]},
  {"src": "tag:server", "deny":   ["tag:exit:8080"]}
]
```

2. Generate the tagged auth key; place at `secrets/ts_authkey_exit`.
3. Enable `tailscaled` + `tailscale-up` s6 services.
4. Host changes (list them in the phase output): OCI security list ingress UDP 41642; host firewall rule if needed.

Acceptance (from the Mac with exit node = `exit-proton`):
- **T1** `curl -s https://www.cloudflare.com/cdn-cgi/trace` → Proton IP and country.
- **T2** Kill switch: `docker exec exit-proton ip link set wg0 down` → Mac curl fails/times out; **never** Oracle IP. Restore → recovers.
- **T2b** wg0 down for 5 min → `exit-proton` stays online in admin console (V8).
- **T3** DNS leak: `dig +short TXT o-o.myaddr.l.google.com @ns1.google.com` and `dig +short whoami.akamai.net` → resolver IPs belong to Proton, not Oracle.
- **T4** IPv6: `curl -6 -s --max-time 5 https://ifconfig.co` → fails cleanly (no Oracle v6).
- **T5** `tailscale ping exit-proton` → "via <public ip>:41642" (direct), not DERP. If DERP, fix V5 before proceeding.
- **T6** Startup race: run `while true; do curl -s --max-time 2 https://www.cloudflare.com/cdn-cgi/trace | grep ip=; sleep 0.5; done` on the Mac while `docker compose restart exit-proton` → output contains only Proton IPs or failures, **never** the Oracle IP.
- **T7** fin-intel unaffected: host egress still Oracle IP; MCP works from the Mac; policy `tests` pass on save.
- **T8** Throughput sanity: `speedtest`/`iperf`-style download via exit node; record baseline (expect tens to hundreds of Mbps; flag if <20).

### Phase 3 — Controller: catalog, health, failover, rotation, API

**Catalog (`catalog.py`)**
- Fetch gluetun `servers.json` (pin to a commit or release tag; refresh daily; keep last-good copy in `/var/lib/exit-proton/catalog.json`).
- Normalize to `Server{name, country, city, endpoint_ip, wg_pubkey, features{secure_core, tor, p2p, streaming}}`.
- Exclude secure_core and tor by default; secure_core can be enabled via prefs (works with any key, Phase 0).
- Optional later: Proton logicals API for load/score; must not be a hard dependency.

**Tunnel ops (`wg.py`)**
- `swap_peer(old, new)`: `wg set wg0 peer <old_pub> remove peer <new_pub> endpoint <ip>:51820 allowed-ips 0.0.0.0/0,::/0 persistent-keepalive 25` (single invocation).
- `latest_handshake_age()` from `wg show wg0 latest-handshakes`.

**Health (`health.py`)** — loop every 5 s:
- (a) HTTPS GET via `wg0`-bound client (source 10.2.0.2) to two targets, 3 s timeout each: `https://www.cloudflare.com/cdn-cgi/trace` (also yields exit `ip`/`loc`) and `https://www.google.com/generate_204`. Pass if ≥1 OK.
- (b) DNS A query to 10.2.0.1 for a stable name, 2 s timeout.
- (c) handshake age < 180 s (probes generate traffic, so a stale handshake under load = dead).
- Note (Phase 0 finding): Proton servers complete the WireGuard handshake even for unregistered keys, yet pass no traffic. A fresh handshake therefore never proves health on its own; (a) is the authoritative signal.
- Cycle fails if (a) fails, or (b) fails, or (c) fails. **3 consecutive failed cycles → UNHEALTHY → failover.**
- States: `HEALTHY`, `SUSPECT(n)`, `FAILING_OVER`, `DEGRADED` (no candidate worked; kill switch holding).

**Failover policy (`failover.py`)**
- Candidates: same city → same country → preference list countries; exclude servers that failed in the last 30 min; exclude secure_core/tor; shuffle within tier to spread load.
- For each candidate (max 5 per tier): swap peer, allow ≤ 20 s warm-up (Phase 0 finding; up to 14 s seen on Secure Core) for one passing probe cycle; on success persist state; on failure blacklist and continue.
- No automatic fail-back. Exponential backoff in `DEGRADED` (retry every 30 s → 5 min).
- Pure-function core (`choose_candidates(catalog, state, prefs, now)`) with unit tests.

**State (`state.py`)**: JSON at `/var/lib/exit-proton/state.json`: current server, preferences, blacklist with expiry, last N events. Atomic writes.

**Auth (`auth.py`)**: for each request, LocalAPI `GET /localapi/v0/whois?addr=<remote_ip>:<port>` over `/var/run/tailscale/tailscaled.sock`. Allow iff the node is **untagged** and `UserProfile.LoginName` ∈ configured owners. Otherwise 403.

**API (`api.py`)** — bind to the node's tailnet IPv4 on :8080 only (read it from `tailscale ip -4` at startup; retry until available):

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/v1/status` | – | server, country, city, exit_ip, health state, handshake age, since, last events |
| GET | `/v1/servers` | `?country=&city=&feature=` | filtered catalog |
| POST | `/v1/rotate` | `{scope: "city"\|"country"\|"any"}` | new status |
| POST | `/v1/select` | `{country?, city?, server?}` | new status |
| PUT | `/v1/prefs` | `{countries: [...], auto_failover: bool}` | prefs |
| POST | `/v1/find` | `{url, countries?, max_candidates?=10}` | `{job_id}` (Phase 4) |
| GET | `/v1/find/{job_id}` | – | progress + per-server results (Phase 4) |
| POST | `/v1/find/{job_id}/apply` | `{server}` | new status (Phase 4) |
| GET | `/v1/events` | – | SSE stream of status/health changes |

Acceptance:
- **T9** Silent-drop simulation: `docker exec exit-proton nft add rule inet killswitch out ip daddr <current_endpoint> udp dport 51820 drop` (blackholes the tunnel without taking wg0 down). Controller detects within ≤ 20 s, swaps server, traffic recovers within ≤ 35 s total, Mac curl shows a **different** Proton IP. Remove the rule.
- **T10** `POST /v1/rotate {"scope":"country"}` → new exit IP, same country, < 10 s (includes Proton warm-up).
- **T11** API from fin-intel host (`curl http://exit-proton:8080/v1/status`) → blocked by ACL; from the Mac → 200.
- Unit tests for catalog parsing (real fixture), candidate selection, health state machine (fake clock).

### Phase 3b — VPN credentials helper (Mac) + key slots on the controller

Goal: no more manual config generation; options (NetShield, VPN Accelerator, …) become levers we set.

**Mac CLI `exitctl` (Python, later wrapped by the Phase 5 app)**
- `exitctl proton login`: SRP login + 2FA via Proton's open-source `proton-vpn-api-core` (check its licence, GPLv3 expected, before vendoring/importing); session/refresh token in the Keychain (`keyring`). Never written to disk elsewhere, never sent to the VPS.
- `exitctl proton create --slot a|b --netshield 0|1|2 --accelerator on|off [--moderate-nat] [--port-forward]`: generate Ed25519 key pair in memory → derive the X25519 WireGuard key → `POST vpn/v1/certificate {ClientPublicKey, Mode: "persistent", DeviceName: "exit-<slot>-<date>", Features}` → render the `.conf` in memory → upload it to the controller → record serial + expiry.
- `exitctl proton list | delete <serial>`: show/delete certificates (`GET vpn/v1/certificate/all`, `DELETE vpn/v1/certificate`), flag certs that no slot uses.
- `exitctl proton rotate --slot a`: create new → upload → controller swaps key → health passes → delete the old certificate. On failure, keep the old key and certificate.
- The helper is provider-pluggable (D16): `exitctl <provider> …`; a provider without an API just accepts `exitctl <provider> import <file.conf>`.

**Controller additions**
- `PUT /v1/keys/{slot}` (body: `.conf`), owner-only via whois (D13). Validates, writes `secrets/<provider>_<slot>.conf` atomically (0600), hot-swaps with `wg set wg0 private-key` for slot a (prober reload for slot b), runs a health cycle, rolls back on failure. Keys never appear in responses or logs.
- `GET /v1/keys`: per slot: provider, public-key fingerprint, options (parsed from the `#` comments), certificate expiry if the helper supplied it. No private material.
- `/v1/status` warns when a certificate expires within 14 days.

Acceptance:
- **T16** `exitctl proton create --slot a --netshield 1` → new key live, Mac curl still shows a Proton IP; `dig` of a known malware test domain via the exit is blocked; old certificate still listed until `rotate` deletes it.
- **T17** Upload a config with a bogus key → controller rolls back within one failover window; exit never leaks (T6-style curl loop).
- **T18** Grep VPS filesystem, container logs and controller responses for the Proton session/refresh token → nothing.

### Phase 4 — Prober and "find a server for this URL"

**Prober container**: own netns; `entrypoint.sh` sets up `wg-probe` with key B (`Table=off`, own table, `unreachable` fallback, nft drop of any non-wg-probe egress except to `exitnet` subnet and the current WireGuard endpoint). API on `exitnet:9000` only (not published).

Compose addition:

```yaml
  prober:
    build: ./prober
    cap_add: [NET_ADMIN, NET_RAW]
    devices: [/dev/net/tun:/dev/net/tun]
    sysctls: { net.ipv4.conf.all.rp_filter: "2" }
    dns: [10.2.0.1]
    volumes: ["./secrets/proton_b.conf:/secrets/proton_b.conf:ro"]
    networks: [exitnet]
    mem_limit: 2g
    restart: unless-stopped
```

**Probe procedure** (`POST /probe {server, url}`):
1. Swap `wg-probe` peer to `server` (never the active server; controller enforces).
2. Wait for handshake ≤ 5 s; verify exit via cdn-cgi/trace: `ip` ≠ Oracle IP and `loc` matches. Abort the probe if not.
3. Fresh Playwright browser context (persistent Chromium process, new context per probe; realistic UA/locale/timezone for the server country), `goto(url, wait_until="domcontentloaded")`, then wait up to 10 s for `networkidle`. 20 s hard cap.
4. Classify (`classify.py`), return `{server, exit_ip, exit_loc, outcome, signals[], http_status, final_url, title, elapsed_ms}`.

**Classifier** (ordered; first match wins for outcome, but collect all signals):
- Navigation error / TLS failure / connection reset → `error`.
- Response header `cf-mitigated: challenge`, or title matches `/^Just a moment/i`, or Turnstile/hCaptcha/reCAPTCHA iframes present → `challenge`.
- Main document status 403 / 451 / 429 → `blocked`.
- Final URL's registrable domain ≠ requested domain → `redirected` (counts as blocked unless it's a known login/SSO domain list).
- Body text matches `/(vpn|proxy|anonymi[sz]er|unusual traffic|access denied|not available in your (region|country))/i` → `suspicious`.
- Otherwise `ok`.
- Phase 6 adds a baseline comparison against the home probe (title equality, text-length ratio).

**Controller `/v1/find` job**: build candidate list (requested countries or current country first; streaming-flagged servers first; diversify across cities; exclude active server); probe sequentially; stream progress; stop early after the first 2 `ok` results or `max_candidates`. `apply` performs a normal `select`.

Acceptance:
- **T12** `find` against a known-friendly URL returns `ok` for the first candidate; against a Cloudflare-challenged test page returns `challenge`; active exit IP unchanged throughout (Mac curl loop shows no change).
- **T12b** Kill `wg-probe` mid-probe → probe returns `error`, never a result fetched via the Oracle IP.
- Unit tests for the classifier using saved HTML/header fixtures.

### Phase 5 — macOS menu-bar app (`ExitControl`)

- SwiftUI `MenuBarExtra`, macOS 14+, no Dock icon (`LSUIElement`).
- Base URL configurable, default `http://exit-proton:8080` (MagicDNS).
- Menu: status line (country flag, city, exit IP, health colour), **Rotate (same country)**, **Rotate (any)**, country submenu (from `/v1/servers` countries), **Find server for URL…** (sheet: URL field, country filter, live results list from polling `/v1/find/{id}` or SSE, "Switch to this server" buttons), **Open status JSON**, Preferences.
- Detect whether the Mac's current exit node is `exit-proton` (via `tailscale status --json` if the CLI is available; otherwise skip) and show a warning badge if not. Changing the Tailscale exit node itself is out of scope for v1.
- No secrets in this phase (auth is Tailscale identity).
- Acceptance: all menu actions work against the live controller; app tolerates controller-down (shows offline state, no hangs).

### Phase 6 — Home allow-list (residential exit via app connector)

**Home node**: a custom TrueNAS app (compose) `home-gw`: `tailscale/tailscale` image, kernel mode, `TS_EXTRA_ARGS=--advertise-connector --advertise-exit-node --advertise-tags=tag:connector-home --hostname=home-gw`, `TS_USERSPACE=false`, `net.ipv4.ip_forward=1`. Separate node from the NAS's existing Tailscale subnet router (do not modify that one).

**Home probe agent**: small container on the same TrueNAS app (`home-gw` compose), Playwright, same `/probe` contract minus the tunnel (direct home egress), listening on the `home-gw` tailnet IP :9001.

**Policy additions** (verify exact syntax against the current app-connector setup docs before applying):

```jsonc
"tagOwners": { ..., "tag:connector-home": ["autogroup:admin"] },
"autoApprovers": {
  "exitNode": ["tag:exit", "tag:connector-home"],
  "routes":   { "0.0.0.0/0": ["tag:connector-home"], "::/0": ["tag:connector-home"] }
},
"grants": [
  ...,
  {"src": ["autogroup:member"], "dst": ["tag:connector-home"], "ip": ["tcp:9001", "icmp:*"]}
],
"nodeAttrs": [
  {
    "target": ["*"],
    "app": {
      "tailscale.com/app-connectors": [
        {"name": "home-us", "connectors": ["tag:connector-home"], "domains": ["example-blocked-site.com"]}
      ]
    }
  }
]
```

**Mac app additions**:
- "Try via home" in the Find sheet: calls the home probe agent; shows result next to Proton results; this result is also the classifier's baseline.
- "Add to home allow-list": edits `nodeAttrs[...].app["tailscale.com/app-connectors"]` → `home-us.domains` via the Tailscale API (`GET`/`POST /api/v2/tailnet/-/acl` with HuJSON and `If-Match` ETag). Adds the registrable domain plus hosts observed during the probe's navigation (redirect chain, main-document hosts). Token stored in **macOS Keychain** only (D14). Show a diff and require confirmation before posting.
- Allow-list management view (list/remove entries).

Acceptance:
- **T13** With exit node = `exit-proton`: an allow-listed domain shows the **home** IP (cdn-cgi/trace on that domain, or a test domain you control); any other domain shows the Proton IP (V9).
- **T14** Policy edit from the Mac: invalid edits are rejected by the API (policy `tests` still enforced); ETag conflict handled.
- **T15** NAS/home down → allow-listed domains fail (expected, fail-closed); everything else still works via Proton.

### Phase 7 (optional) — Brazil residential node

- Raspberry Pi (Linux; app connectors require Linux) at the family home (BR), `tag:connector-br`, app connector `home-br` + exit node. Same compose as `home-gw` (arm64).
- Mac app allow-list entries gain a target: `home-us` | `home-br`.
- UniFi Site Magic is **not** used for this (it would hairpin through the US home); independent of this project.

---

## 7. Security model

| Asset | Where | Protection |
|---|---|---|
| Proton keys A/B | `secrets/` on VPS, mounted read-only into one container each | file mode 0400 root; not in images; not logged |
| Tailscale node keys | `ts-state` volume | tagged nodes, key expiry disabled by tagging; `tag:exit` has no outbound tailnet grants |
| Tailscale API token (policy write) | Mac Keychain only | never on VPS/NAS (D14) |
| Controller API | tailnet IP only, ACL to members, whois check | no public exposure; no published port except UDP 41642 (tailscaled) |
| fin-intel | host | unchanged; `tag:server` ACLs unchanged; `tag:exit` cannot reach it |

Accepted risk: host-root compromise of the VPS compromises both fin-intel and the exit node.

---

## 8. Operations runbook (docs/runbook.md)

- Logs: JSON lines to stdout; `docker compose logs -f exit-proton prober`.
- Status: `curl http://exit-proton:8080/v1/status` from the Mac.
- Rotate Proton keys: create new config in Proton dashboard → replace `secrets/proton_a.conf` → `docker compose restart exit-proton` → delete old config in Proton dashboard.
- Update: pin image digests; monthly rebuild; re-run T1–T7 after updates.
- Catalog stale/broken: controller keeps last-good catalog; alert in `/v1/status`.
- Emergency: Mac → Tailscale menu → Exit node → None.
- Always-Free hygiene: total A1 usage ≤ 2 OCPU / 12 GB; prober `mem_limit` keeps Chromium bounded.

---

## 9. Acceptance test matrix (summary)

| ID | Phase | What it proves |
|---|---|---|
| P1.1–P1.5 | 1 | tunnel up, underlay direct, DNS via tunnel, local kill switch, host untouched |
| T1 | 2 | exit traffic leaves via Proton |
| T2 / T2b | 2 | kill switch with tailscaled running; node survives tunnel outage |
| T3 / T4 | 2 | no DNS leak; no IPv6 leak |
| T5 | 2 | direct (non-DERP) path to the exit node |
| T6 | 2 | no leak during container restart |
| T7 / T8 | 2 | fin-intel unaffected; throughput baseline |
| T9 / T10 / T11 | 3 | silent-drop failover ≤ 20 s; rotation; API ACL |
| T12 / T12b | 4 | find-server works; probing never disturbs active exit; prober fails closed |
| T13–T15 | 6 | per-domain residential routing over the exit node; safe policy edits; fail-closed |

---

## 10. Open questions (ask the owner before Phase 1)

1. VPS OS, arch, Docker present? (§5)
2. Existing Tailscale DNS config (MagicDNS, split DNS for `<home split-DNS domain>`, global nameservers)?
3. ~~Default country preference~~ → US, then CH.
4. ~~NetShield on generated keys~~ → 0 on all keys; filtering lever to be done on our side (own resolver) if wanted.
5. ~~Repo location~~ → GitHub.
6. **Decided: IPv4 first**, IPv6 after v4 is solid. Proton configs now include in-tunnel IPv6 (`2a07:b944::2:2/128`, DNS `2a07:b944::2:1`). Keep IPv6 blocked at the exit (T4 as written), or carry IPv6 through wg0 (dual-stack exit; T4 becomes "IPv6 exits via Proton")?
7. ~~`Bouncing` label~~ → does not restrict which servers a key can use (Phase 0).
