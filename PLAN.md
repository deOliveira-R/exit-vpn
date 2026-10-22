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
| D14 | **Tailnet policy is GitOps-managed** in a separate private repo (`tailnet-policy`, it holds LAN details and covers the whole tailnet). PRs are validated and tested by CI with a read-only credential; merges to `main` apply it. Auth is Tailscale federated identity (GitHub OIDC): **no policy-write secret exists anywhere** (not on the VPS, the NAS or the Mac); the write credential only accepts tokens for `refs/heads/main` of that repo. Console edits are locked ("Prevent edits in the admin console"). Undo = revert the PR | Policy write = full tailnet control; this removes the long-lived token entirely and gives history and one-click rollback |
| D15 | Residential exits/allow-list via **app connectors** (Phase 6/7), as separate tailnet nodes | Per-domain routing that coexists with the Proton exit node; app connectors are on all plans |
| D16 | **Provider-agnostic core, Proton as the first provider.** A `provider` module supplies: catalog source (gluetun-servers has the same schema for Mullvad, AirVPN, IVPN, …), tunnel address and DNS (Proton: `10.2.0.2/32`, `10.2.0.1`; other providers assign per key), and feature metadata. Routing, kill switch, health, failover and API never hard-code Proton values. Node name = `exit-<provider>` (`exit-proton`); repo/stack = `exit-vpn` | Someone else on the tailnet may use another VPN; also lets us switch providers without a redesign |
| D17 | Proton **connection options are bound to the key**, not the config file: the dashboard registers the WireGuard public key with a signed certificate carrying `NetShieldLevel`, `SplitTCP` (VPN Accelerator), `RandomNAT` (off = Moderate NAT) and `PortForwarding` (NAT-PMP). The `# NetShield = 1` lines in the downloaded `.conf` are comments only. So one key keeps its options on every server, and changing an option means generating a new key. We store the full downloaded `.conf` (mode 600) and parse `PrivateKey` plus the option comments, so `/v1/status` can report them | Verified against Proton's open-source client (`python-proton-vpn-api-core`, `/vpn/v1/certificate`) and support docs |
| D18 | **VPN credentials helper runs on the Mac, never on a server.** Proton configs are created, listed, rotated and deleted through Proton's internal API (the same calls the web dashboard makes: `POST/GET/DELETE vpn/v1/certificate`, `Mode: persistent`, `Features`), authenticated by a Proton session (SRP login + 2FA) kept in the macOS Keychain. Only the resulting WireGuard key reaches the VPS | A Proton session grants the whole Proton account (Mail, Drive), so it gets the same treatment as the policy-write token in D14. Undocumented API → the helper is a convenience; manual dashboard generation stays the fallback |
| D19 | **Prober = two tiers.** Tier 1: `curl_cffi` (Chrome TLS/HTTP2 impersonation, no JS) for fast HTTP-level verdicts (403/451/429, Cloudflare/WAF challenges, geo redirects) across many candidates. Tier 2: Debian's full `chromium --headless=new`, driven over CDP (Playwright `connect_over_cdp` or a small CDP client, not Playwright's bundled browsers), for finalists and JS-revealed blocks. Verdicts compare against a same-engine baseline | Benchmark 2026-10-20 (below): the engine itself is part of what sites detect; only full Chromium new-headless loaded Amazon, Hulu and Netflix normally (headless shell was flagged as a bot by Amazon; Lightpanda 1.0.1 broke Hulu's JS). curl_cffi is ~20 MB/page vs Chromium's 300–600 MB. Lightpanda revisit tracked in an issue |
| D20 | **Provider adapters on the Mac (`exitctl`)**: one adapter per VPN service behind a common interface (levers as a schema, key create/renew/list/delete, optional live catalog); a `manual` adapter imports `.conf` files for providers without an API. Exposed as a CLI with `--json` on every command, which is the contract the Phase 5 menu-bar app drives | Provider-agnostic like the server side (D16); the app gets every provider for free; provider sessions never leave the Mac (D18) |
| D21 | **Menu-bar app = thin client** (`mac/ExitControl`, SwiftUI): controller over HTTP + SSE for state and server choice, `exitctl --json` for provider actions, a launchd agent for the hourly catalog push. One controller, one VPN subscription. The Tailscale exit-node choice stays in the Tailscale app (shown, not changed) | Mirrors Tailscale (daemon + LocalAPI + equal CLI/GUI clients); live updates without polling; the app stays MIT because proton-core is never bundled |

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
ip -6 rule add priority 100 iif tailscale0          lookup 100  # v6 via wg0 only while the IPv6 gate is open
ip    rule add priority 110 from 10.2.0.2           lookup 100  # health probes bound to wg0
ip    rule add priority 120 to 10.2.0.1             lookup 100  # Proton DNS only via tunnel

# IPv6 (Phase V6): the tunnel's v6 address is a local route, not on wg0 (an interface address
# would become the source of every unbound v6 socket); sysctl net.ipv6.ip_nonlocal_bind=1 lets
# the health probe bind to it; nat_exit SNATs forwarded v6 to it
ip -6 route add local 2a07:b944::2:2/128 dev lo table local
# table 101 = sockets bound to it, always via wg0
ip -6 route add unreachable default metric 1000 table 101
ip -6 route add default dev wg0 metric 1 table 101   # metric 1: v6 routes default to 1024
ip -6 rule add priority 105 oif wg0                   lookup 101
ip -6 rule add priority 110 from 2a07:b944::2:2       lookup 101
ip -6 route add default dev wg0 metric 1 table 100   # forwarded v6: then the gate (nftables)
# route-keeper: wg0 in `inet killswitch` set ipv6_gate while /run/exit-vpn/ipv6 exists (the
# controller's IPv6 gate); otherwise forwarded v6 is rejected (TCP reset, else ICMP)
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
table inet nat_exit {   # inet since Phase V6
  chain post { type nat hook postrouting priority 100;
    oifname "wg0" meta nfproto ipv4 masquerade
    oifname "wg0" meta nfproto ipv6 snat ip6 to 2a07:b944::2:2 }   # added by init-killswitch
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
│   ├── Dockerfile               # debian:trixie-slim + chromium (--headless=new) + curl_cffi + wireguard-tools (D19)
│   ├── entrypoint.sh            # killswitch for prober netns, wg-probe init
│   └── src/prober/{api.py,tunnel.py,classify.py}
├── helper/                      # exitctl: provider adapters + controller client (Mac, D20)
├── mac/ExitControl/             # Phase 5 menu-bar app (drives exitctl --json)
├── home-gw/                     # Phase 6: TrueNAS custom app compose + home probe agent
├── tests/acceptance/            # shell scripts, run by the owner
└── docs/{runbook.md,security.md}       # tailnet policy: separate private repo `tailnet-policy` (D14)
```

---

## 5. Inputs the owner must provide

| Item | How | Notes |
|---|---|---|
| Proton WireGuard **config A** (exit) | account.proton.me → Downloads → WireGuard configuration; any server (D17) | NetShield: owner's choice (applies to exit clients' DNS via 10.2.0.1); Moderate NAT **off**; NAT-PMP **off**; VPN Accelerator **on**. Upload the whole `.conf` to `secrets/proton_a.conf` |
| Proton WireGuard **config B** (prober) | same | NetShield **off** (so probes see sites unfiltered, like a normal browser); Moderate NAT off; NAT-PMP off; VPN Accelerator on → `secrets/proton_b.conf` |
| Tailscale **tagged auth key** for `tag:exit` | Admin → Settings → Keys → generate, tagged `tag:exit`, pre-approved, non-reusable | Tag must exist in policy first (Phase 2) |
| VPS facts | `uname -m; cat /etc/os-release; docker --version; ip -br a; sudo iptables -S \| head -50; sudo nft list ruleset \| head -50` | Arch (expect aarch64), OS, whether Docker exists |
| Current tailnet policy file | Admin → Access controls → copy | Baseline (first commit) of the `tailnet-policy` repo; all changes by PR from then on |
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
| V5 ✅ | OCI ingress for UDP 41642 can be opened (security list) and host firewall allows Docker-published UDP | Medium (OCI Ubuntu images ship restrictive iptables incl. FORWARD REJECT) | Inspect rules; after Phase 2, `tailscale ping exit-proton` from the Mac shows a direct path |
| V6 ✅ | tailscaled in kernel mode works inside an unprivileged container with `NET_ADMIN`, `NET_RAW`, `/dev/net/tun` | High | Phase 1 smoke |
| V7 ✅ | A drop in our own nftables table overrides tailscaled's `ts-forward` ACCEPT; tailscaled uses the nf_tables backend (iptables-nft) in this image | Medium-high | Phase 1 test T2 (kill switch) must pass with tailscaled running |
| V8 ✅ | With wg0 down and resolv.conf → 10.2.0.1, tailscaled stays connected (bootstrap DNS / cached DERP map) | Medium | Phase 2 test T2b: down wg0 for 5 min, node stays online in admin console |
| V9 | App connector `/32` routes take precedence over the exit node default route on macOS/iOS | Medium-high (more specific route) | Phase 6 test T13 |
| V10 | App connector device may sit behind home NAT (docs say "must have a public IP address") | Medium (community setups run at home) | Phase 6 smoke |
| V11 ✅ | A browser engine that works for probing on arm64 | — | Superseded by D19: Debian `chromium` (arm64, security-updated) + `curl_cffi` (aarch64 wheels); benchmarked 2026-10-20 |

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

**Phase 1 results (2026-10-20)** — `tests/acceptance/phase1.sh`: 9/9 pass, also after `docker compose restart`. Notes:
- Implementation: config is any wg-quick file (`EXIT_VPN_CONF`); address, DNS and initial peer are parsed from it, nothing Proton-specific is hard-coded (D16). Rule 105 `oif wg0 lookup 100` added for sockets bound to wg0.
- The kernel deletes table-100 routes via wg0 when the link goes down; the `route-keeper` longrun restores `default dev wg0` when wg0 is up again (until then `unreachable` holds).
- Docker's embedded DNS (127.0.0.11) forwards to the VPN DNS from inside the container's netns, so it obeys rule 120 and the `output` drop: with wg0 down, DNS fails closed (P1.4).
- Capabilities: `NET_ADMIN`, `NET_RAW` only; read-only root; `/run` tmpfs must be `exec` for s6-overlay; s6 user bundle lives in `user-bundles.d` (3.2.3+).
- The VPS has no buildx; compose builds with its built-in BuildKit, downloads verified by `sha256sum -c`.
- Host changes so far: `/opt/exit-node` (repo checkout, `.env` 0600, `secrets/` 0700); Docker network `exit-vpn_exitnet` (172.30.42.0/24, Docker-managed bridge + iptables rules); volumes `exit-vpn_exit-state`, `exit-vpn_ts-state`; images `exit-vpn/exit:latest`, `alpine:3.22` (Phase 0 tests). No published ports, no host sysctl or firewall edits.

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

**Phase 2 results (2026-10-20)** — node side `tests/acceptance/phase2-node.sh` 12/12, client side `tests/acceptance/phase2-client.sh` 9/9 (T1–T8), T2b passed (5 min wg0 down: node online and directly reachable throughout, tunnel recovered). Notes:
- Tailnet policy change went through GitOps (`tailnet-policy` PR #1, D14): `tag:exit`, `autoApprovers.exitNode`, members → `autogroup:internet`, members → `tag:exit` ICMP, isolation tests.
- The node logged in with an **untagged** auth key and `--advertise-tags=tag:exit` (allowed: the key's owner is an admin and admins own the tag); control auto-approved the exit routes. Restarts reuse the state (same node, same IP, no new login).
- **V5:** T5 shows a direct path `via <VPS public IP>:41642` with the OCI security list **still closed**: Docker's published port gives a stable mapping and NAT traversal does the rest. No OCI change needed.
- **V7:** tailscaled (nftables mode via `TS_DEBUG_FIREWALL_MODE=nftables`, no iptables binary in the image) keeps its `ts-*` chains in `ip filter`/`ip nat` at priority 0; our `inet killswitch` forward chain at −10 drops first. T2/T6 prove it end to end.
- tailscaled always warns it cannot write `src_valid_mark` (read-only `/proc/sys`); compose sets it to 1, so the warning is cosmetic.
- T6 (restart under a 0.5 s curl loop): 56/59 samples via Proton, 3 failed closed, none via Oracle. T8: 93 Mbit/s.
- `--accept-dns=false` on the node: clients' DNS (MagicDNS on, no global nameservers) goes to the node's resolver → Docker DNS → Proton DNS through wg0 (T3).
- Host changes in this phase: Docker-published UDP 41642 (Docker-managed DNAT/FORWARD rules). Nothing else.

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
- **T9** Silent-drop simulation: `docker exec exit-proton nft insert rule inet killswitch output ip daddr <current_endpoint> udp dport 51820 drop` (blackholes the tunnel without taking wg0 down). Controller detects within ≤ 20 s, swaps server, traffic recovers within ≤ 35 s total, Mac curl shows a **different** Proton IP. Remove the rule.
- **T10** `POST /v1/rotate {"scope":"country"}` → new exit IP, same country, < 10 s (includes Proton warm-up).
- **T11** API from fin-intel host (`curl http://exit-proton:8080/v1/status`) → blocked by ACL; from the Mac → 200.
- Unit tests for catalog parsing (real fixture), candidate selection, health state machine (fake clock).

**Phase 3 results (2026-10-20)** — 41 unit tests (`controller/tests`); acceptance `tests/acceptance/phase3-client.sh` 4/4: T11a owner's Mac → 200, T11b `tag:server` host → no connection (ACL), T10 rotate(country) in 5 s, **T9 silent drop: failover_start at +14.7 s, traffic back via a new Proton IP at +19.6 s**, never the Oracle IP. Phase 1/2 suites re-run green (T2 now also triggers a real failover: 15 s of wg0 down is 3 strikes). Implementation notes and deviations:
- Package `exit_vpn` (provider-agnostic, D16) on Debian's Python 3.13; deps pinned by hash (`controller/requirements.lock`, `pip --require-hashes`); runs as s6 longrun `controller` after `tailscale-up`. Build context is the repo root with a `.dockerignore` that keeps `secrets/`, `.env`, `local/` out of the builder. Container memory ~86 MiB.
- **Catalog pinned to a gluetun-servers commit** (not `main`) and every hostname must end in `.protonvpn.net`: the catalog supplies endpoints and public keys, so a tampered one could steer exit traffic to a WireGuard server we don't control. No daily refresh (pinned = nothing to refresh); bump the commit deliberately. Last-good copy cached in `/var/lib/exit-vpn/catalog.json`.
- **A server's identity is (public key, endpoint IP).** 246 of 782 Proton public keys appear in several entries (Secure Core entries reuse their exit server's key with the entry IP). A key-only lookup misnamed the config's server (US-CA#556, absent from the catalog) as `SE-US#1`. Blacklisting stays per key (same physical exit).
- Servers not in the catalog get their country from the exit's ISO code (`loc`), mapped to catalog names via Proton's hostname scheme (`us-…`, `node-ch-…`).
- Health loop runs on a fixed 5 s cadence (a failing cycle's probe timeouts are not added on top), and the warm-up grace ends at the first passing cycle (else a server dying within 20 s of a switch went unnoticed).
- `rotate` scopes: `city`, `country` (any city in it), `any` = the preference countries. `select` by `server` name ignores eligibility filters (explicit choice).
- Policy: `tailnet-policy` PR #2 opened `tcp:8080` from members to `tag:exit`, with tests (members reach `:8080` only; `tag:server` cannot).
- Not reproduced: two SSH sessions over the VPS's public IP were reset during deploys; a controlled recreate kept a public-IP session and 70/70 fin-intel API samples intact. Deploys now go over the tailnet SSH path.

### Phase 3b — Provider adapters (`exitctl`) + key slots on the controller

Goal: no more manual config generation; options (NetShield, VPN Accelerator, …) become
levers we set; the same tool serves any VPN provider, and the Phase 5 menu-bar app drives it.

**Shape (D20)** — `helper/`, Python package `exitctl`, runs on the Mac:
- `providers/`: one adapter per VPN service behind one interface:
  - `levers()`: the provider's levers as a schema (name, type, choices, default, help);
    CLI flags and app controls are generated from it, not hard-coded.
  - `account()` / `login(Credentials)` (one step; `NeedInput` names the next) / `logout()`,
    `create_key(device_name, values, peer)` → `NewKey(conf, KeyMeta)`, `renew_key(KeyMeta)`,
    `certificates()`, `delete_certificate(id)`; optional `servers()` (live catalog).
    (As of R4; the interface is in `helper/src/exitctl/providers/base.py`.)
  - Each adapter declares its capabilities. `manual` = `import <file.conf>` for providers
    without an API. First API adapter: `proton`.
- `controller.py`: client for the exit node's API (`/v1/status`, `/v1/keys`), authenticated
  by the Mac's tailnet identity (D13); no tokens.
- `cli.py`: `exitctl <provider> <command>`, every command with `--json`. That JSON CLI is the
  contract the Phase 5 app uses ("connected to" the app); a Python app may import the package
  instead ("inside"). Either way provider sessions stay in the Mac's Keychain.
- Locked with `uv.lock`. `python-proton-core` comes from git, pinned to a commit: the PyPI
  name `proton-core` is an unrelated placeholder, never install it from PyPI.

**Proton adapter** (verified against Proton's open-source web dashboard, WebClients
`containers/vpn/WireGuardConfigurationSection`, and `python-proton-core`):
- Session: SRP login + 2FA (TOTP) via `python-proton-core` (GPLv3). Human verification
  (CAPTCHA) → print the verify URL, accept the token. Session state in the Keychain
  (`keyring`), refreshed on use.
- Create = what the dashboard does: random Ed25519 key in memory →
  `POST vpn/v1/certificate {ClientPublicKey: base64(raw Ed25519 public key), Mode: "persistent",
  DeviceName, Features: {NetShieldLevel, SplitTCP, RandomNAT, PortForwarding}}` → WireGuard
  private key = X25519 clamp of `sha512(seed)[:32]` → render the wg-quick file like the
  dashboard (`getConfigTemplate`), peer = the controller's current server (the file's peer only
  matters at bootstrap). The Ed25519 private key is then discarded.
- Renew = the same call with the same public key and `Renew: true`: expiry extended, key
  unchanged, so expiry never forces a key change. Needs only the public key (kept in meta).
- List = `GET vpn/v1/certificate/all?Mode=persistent`; delete =
  `DELETE vpn/v1/certificate {ClientPublicKeyFingerprint}`.
- Device names `<node>-<slot>-<yyyymmdd>` (e.g. `exit-proton-a-20261020`), so `list` shows which
  certificate each slot uses and flags orphans.

**Controller additions**
- Key slots `a` (exit tunnel) and `b` (prober), in named volumes `keys-a` (exit only) and
  `keys-b` (exit rw, prober ro): `wg.conf` (0600) + `meta.json`. `secrets/<provider>_{a,b}.conf`
  stays the bootstrap fallback while a slot is empty.
- `PUT /v1/keys/{slot}` body `{conf, meta}`, owner-only (D13). Validates (PrivateKey = 32 bytes
  base64, Address/DNS equal to the running tunnel's, since the kill switch is built on them).
  Slot a: under the switch lock, `wg set wg0 private-key` (via stdin), then one passing health
  cycle within the warm-up, else restore the previous key → 409. Slot b: write, the prober
  reloads (`POST /key/reload`) and proves traffic through one server, else restore → 409.
  Keys and request bodies never reach logs or error responses (validation errors don't echo
  the input).
- `GET /v1/keys`: per slot: source (uploaded | bootstrap), WireGuard public key, options, meta
  (provider, certificate id, device name, created, expires). No private material.
- `/v1/status` warns when a slot's certificate expires within 14 days.

**Commands**
```
exitctl proton login                         # password + 2FA prompted in your terminal
exitctl proton options                       # the levers
exitctl proton rotate --slot a --netshield 1 # new key → upload → healthy → delete old cert
exitctl proton renew --slot a                # extend expiry, same key
exitctl proton list                          # certificates, with slot / orphan flags
exitctl proton delete <fingerprint>
exitctl keys                                 # GET /v1/keys
exitctl manual import --slot b file.conf     # providers without an API
```
Changing a lever = `rotate` with that lever (D17: options are bound to the key); unspecified
levers keep the slot's current values.

**3b.2 — live catalog from the provider's API**
- `exitctl <provider> servers [--push]`: the adapter's optional `servers()` returns a
  provider-neutral list (name, country code, city, hostname, IP, WireGuard public key, feature
  flags, **load**, **enabled**). Proton: `GET vpn/v1/logicals?SecureCoreFilter=all&WithState=true`
  (one row per WireGuard peer = (`X25519PublicKey`, `EntryIP`), deduplicated: 2,899 physical
  entries are 1,077 distinct peers; `Features` bits 1 SC, 2 Tor, 4 P2P, 8 streaming; `Tier` 0 =
  free; logical and physical `Status` = enabled). Same size as the pinned list (1,048), but
  current, with load and maintenance state.
- Controller `PUT /v1/catalog` (owner-only, D13): validates like the pinned path and more
  (hostname suffix, global IPv4, 32-byte keys, at least `MIN_SERVERS` and at least 30 % of the
  current catalog's size), resolves country codes to the catalog's names, swaps the catalog under
  the switch lock, caches it. A pushed catalog wins over the pinned one at startup.
  `DELETE /v1/catalog` reverts to the pinned list (undo). `/v1/status` warns when a pushed
  catalog is older than 7 days.
- Trust: D12 pinned the catalog because a tampered one could steer exit traffic. A pushed one is
  as trustworthy as its path: Proton's API over proton-core's TLS-pinned session, uploaded by an
  owner's device over the tailnet. The pinned list stays as the fallback.
- Use of the new fields: disabled servers are never candidates; within a failover tier,
  lower-load servers come first (buckets of 20 %, random within a bucket to spread load).
- Refresh: manual for now; the Phase 5 app pushes periodically (e.g. hourly while running).

Acceptance:
- **T16** `rotate --slot a --netshield 2` → new key live, Mac curl still shows a Proton IP, ad
  domains no longer resolve via Proton's resolver, `GET /v1/keys` shows NetShield 2, the old
  certificate is gone from `list`. Rotate back to `--netshield 0`. (Level 2 rather than 1:
  well-known ad domains make a measurable test; no reliable public malware test domain.)
- **T17** Upload a bogus key → 409 within one warm-up window, previous key restored, the exit
  never leaks (T6-style curl loop).
- **T18** Grep the VPS filesystem (outside the key volumes), container logs and controller
  responses for the Proton session tokens and private keys → nothing.
- **T19** `renew --slot a` → expiry moves, the WireGuard public key and the session don't change.
- **T20** `rotate --slot b` → the prober reloads; a `/v1/find` job succeeds afterwards.
- **T21** `servers --push` → `/v1/status` reports the pushed source and ~1,000+ servers; `/v1/servers`
  has loads; rotate and a T9-style failover pick enabled servers from it; restart keeps it;
  `DELETE /v1/catalog` returns to the pinned list. A truncated push (< 30 %) is refused (400).

**Phase 3b results (2026-10-20)** — unit tests: 53 controller, 19 prober, 13 helper (fake Proton
session: exact API payloads, 2FA in two steps, CAPTCHA retry, rotate/rollback/renew flows,
X25519 derivation vs the registered Ed25519 key). Acceptance `tests/acceptance/phase3b-client.sh`
10/10 against the live Proton API: T16 rotate slot a → NetShield 2 in 9 s (≈1.5 s of dropped
samples during the key swap, never the Oracle IP), 0 → 3 of 3 ad domains blocked by Proton's
resolver, the old (dashboard-made) certificate deleted from the account; T19 renew: expiry moved,
same key, same server; T17 an unregistered key → 409 after 49 s (45 s key warm-up), previous key
back, no leak; T20 slot b rotated, prober reloaded, find job probes went through `wg-probe`;
T18 no session token in container logs, `/opt/exit-node` or the stack's volumes, no private key in
API responses or logs. Phase 3 suite re-run green (T9 recovery 19 s). Notes:
- **Proton accepts this client.** `x-pm-appversion: Other` (proton-core's default) works for
  login and for persistent certificates. The first login from a new client gets a CAPTCHA (422,
  code 9001): open the printed `verify.proton.me` URL, solve, press Enter; the retry carries the
  token headers. proton-core raises 9001 from `/auth` as a plain `ProtonAPIError` (fixed in our
  adapter by matching the code).
- **Certificates: 365 days**, renewable in place (`Renew: true`, same key; expiry is then
  counted from the renewal). `list` matches certificates to slots through the Ed25519 → X25519
  public-key map, so dashboard-made keys are recognised and cleaned up on the first rotate;
  other devices' certificates are never touched (exact key match only).
- Key swap on the exit costs ~1.5 s of traffic (fresh session warm-up); a rejected key costs the
  45 s warm-up plus the re-handshake of the old key.
- **The bootstrap configs in `secrets/` are now dead keys** (their certificates were deleted by
  the first rotations). They are only used while a slot volume is empty; if `keys-a`/`keys-b` are
  ever lost, the exit fails closed (kill switch) until `exitctl proton rotate --slot a|b`, which
  works because the controller API rides on the tailnet underlay, not on wg0.
- An unnamed spare dashboard certificate (the old config C) is still on the account, unused.
- Host-level changes: two Docker-managed named volumes, `exit-vpn_keys-a` and `exit-vpn_keys-b`.
  Nothing else on the host changed.

**Phase 3b.2 results (2026-10-20)** — unit tests: 62 controller, 14 helper. Acceptance
`tests/acceptance/phase3b2-client.sh` 7/7: T21a push of 1,077 live servers (1,072 enabled) from
Proton's API; T21b `/v1/servers` carries load; T21c rotate picked an enabled live server; T21d a
truncated push (100 rows) → 400, catalog unchanged; T21e a container restart keeps the pushed
catalog; T21f `servers --revert` → pinned list, then pushed again; T22 after a slot b rotation,
`secrets/proton_{a,b}.conf` are byte-identical to the live keys. Phase 3 suite re-run on the live
catalog: green (T9 recovery 24 s, within the 35 s target). Also done in this step:
- **Bootstrap secrets fixed**: the exit container mounts `secrets/` read-write and the key store
  mirrors every accepted key (and every rollback) to `secrets/<provider>_<slot>.conf`; synced once
  by hand at deploy. The spare config C was deleted from the Proton account and from `secrets/`.
- **Licence: MIT** (`LICENSE`), permissive and compatible with GPLv2 and GPLv3; `helper/`
  depends on (does not vendor) GPLv3 proton-core, so a distributed bundle that includes it is
  GPLv3 (see CLAUDE.md).
- Host-level changes: none beyond the deploy (the `secrets/` bind mount is now read-write for
  the exit container; the files stay root 0600).

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

**Engine benchmark (2026-10-20, VPS, datacenter egress, one run per site)** — basis for D19:

| Engine | Image | Peak RAM/page | Amazon | Hulu | Netflix | bot.sannysoft.com |
|---|---|---|---|---|---|---|
| curl_cffi 0.16.3 (impersonate chrome) | 274 MB | ~20 MB | WAF 202 (empty) | page | page (pre-JS) | – |
| Lightpanda 1.0.1 | 266 MB | 7–133 MB | page | **JS error** | page | 6 failed |
| chromium-headless-shell 155 (Debian) | 919 MB | 74–503 MB | **bot interstitial** | page | page | 9 failed |
| chromium 155 `--headless=new` (Debian) | 1.15 GB | 300–600 MB | page | page | page | 1 failed (WebGL renderer) |

All engines saw Reddit's datacenter block (HTTP 403).

**Probe procedure** (`POST /probe {server, url}`):
1. Swap `wg-probe` peer to `server` (never the active server; controller enforces).
2. Wait for handshake ≤ 5 s; verify exit via cdn-cgi/trace: `ip` ≠ Oracle IP and `loc` matches. Abort the probe if not.
3. Tier 1 `curl_cffi` fetch; if inconclusive or the site is marked JS-dependent, tier 2: fresh browser context in a persistent `chromium --headless=new` process over CDP (new context per probe; realistic UA/locale/timezone for the server country), `goto(url, wait_until="domcontentloaded")`, then wait up to 10 s for `networkidle`. 20 s hard cap.
4. Classify (`classify.py`), return `{server, url, exit_ip, exit_loc, outcome, signals[], tiers[], elapsed_ms}`
   with one `tiers` entry per engine (`engine, outcome, signals, http_status, final_url, title,
   text_len, error`); when the tunnel fails, no `exit_ip`/`exit_loc` and outcome `error`
   (contracts/prober/).

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

**Phase 4 results (2026-10-20)** — 17 prober + 44 controller unit tests; acceptance `tests/acceptance/phase4-client.sh` 5/5: T12a friendly URL → `ok` on the first candidate; T12b Indeed → `challenge` on both candidates (`cf-mitigated: challenge`, "Just a moment...", captcha iframe); T12c the active exit stayed on the same IP for all 33 samples taken while probing; T12d `wg-probe` taken down during an in-flight request (`httpbin.org/delay/10`) → `error`, exit IP was Proton's; T12e `apply` switched the active exit to the found server. Phase 1–3 suites re-run green. Notes:
- Prober netns: wg-quick-style fwmark routing (all traffic → `wg-probe`, table 200, `unreachable` fallback) plus an nftables **output policy of drop** that admits only `lo`, `wg-probe`, the tunnel's own marked UDP/51820 packets, and exitnet. Without a peer: DNS fails and direct IPs get "No route to host". Its API (exitnet:9000) admits only the exit container's fixed IP (172.30.42.2); the host gets no answer.
- Every probe first proves its exit: Cloudflare trace through the tunnel within the warm-up, `loc` must match the server's country, and the exit IP must not be the node's underlay IP (learned by the controller via the main table). Otherwise the probe is an `error` and nothing is fetched.
- Chromium runs as the unprivileged `browser` user (`--no-sandbox`: the container is the sandbox), with a fresh context per probe, the exit country's locale/timezone and a non-"Headless" UA. The prober has `SETUID/SETGID/CHOWN/KILL` for that, besides `NET_ADMIN`. Image 1.53 GB; ~410 MiB resident with an idle browser.
- Auto mode: tier 1 alone when it already shows an IP-level verdict (403/451/429) or a plain `ok`; escalate to Chromium on `challenge`, `inconclusive` (near-empty pre-JS page) or `suspicious`, and always when the request sets `js: true`.
- Observed through Proton (2026-10-20): Reddit `ok` (it 403s the datacenter IP), Hulu/Zillow/Etsy/Netflix `ok`; Indeed, G2, Crunchbase `challenge`.
- Known limit: geo-blocks that only appear at playback (e.g. BBC iPlayer's homepage loads anywhere) need a playback/content URL to be detected; a homepage probe says `ok`.
- Jobs run one at a time (the prober has one tunnel); `POST /v1/find` queues them.

### Phase 5 — macOS menu-bar app (`ExitControl`)

**Shape (D21)** — a native SwiftUI app (Xcode project in `mac/ExitControl/`, macOS 14+,
`MenuBarExtra`, no Dock icon) that is a thin client like Tailscale's own GUI:
- **Controller** (the "daemon"): HTTP for actions, SSE `/v1/events` for live state, so the
  icon and windows update without polling. Base URL configurable, default
  `http://exit-proton:8080` (MagicDNS); auth stays Tailscale identity (no secrets in the app).
- **Provider actions** (login, keys, levers, catalog push): runs `exitctl --json` (D20),
  installed separately with `uv tool install ./helper`. The app never bundles proton-core
  (GPLv3), so it stays MIT; `NeedInput` (exit code 3: totp / captcha) becomes a sheet.
- **Background agent**: a launchd agent (installed/removed from the app) runs
  `exitctl <provider> servers --push` hourly and warns about keys expiring within 14 days.
- **Scope**: one controller, one VPN subscription (Proton here; another user's might be a
  different provider). Choosing *whether* this Mac uses `exit-proton` stays in the Tailscale
  app: ExitControl only shows it (read-only, from `tailscale status --json`; a warning badge
  when another exit node or none is selected). ExitControl chooses *which provider server*
  `exit-proton` uses, for every device routed through it.

**Controller changes first (5a, with unit tests):**
1. **Exact server**: `/v1/select {server, ip}` picks one physical server (a logical name
   covers several; identity is (pubkey, ip)). Records the pick as manual.
2. **Equivalent-server failover**: the current server's features (P2P, streaming, IPv6) are
   the profile failover keeps. Tiers stay same city → same country → pinned regions; within
   them, servers with the full profile come first. **P2P is hard** (never fail over from a
   P2P server to a non-P2P one: traffic may be peer-to-peer); streaming and IPv6 are
   preferred, then relaxed. No hard match anywhere → `degraded` (kill switch), not a silent
   downgrade.
3. **Secure Core on/off** (no "allowed"): on → only Secure Core servers are eligible
   (entry CH/IS/SE → exit in the chosen region), off → none. Failover never crosses the mode;
   no Secure Core server working → `degraded`. Turning it on or off switches immediately
   (best Secure Core server in the current country, or the first pinned region).
4. **Pinned regions**: `prefs.regions` = ordered list of `{country, city?}` replacing
   `prefs.countries` (migrated on load; `countries` kept read-only in the API for older
   clients). Failover and "Rotate (any)" walk them in order.
5. **Requirements** (v1.1): `prefs.require` = features every candidate must have (e.g. P2P),
   independent of the current server.
6. **Catalog**: `ipv6` flag (Proton feature bit 16) in pushed rows and `Server`; `/v1/servers`
   filter accepts it. IPv6 *through* the exit is still out of scope (Q6).

**App v1:**
- **Menu**: status line (flag, server, city, exit IP, load; Secure Core shows "CH → US"),
  icon coloured by health (healthy / failing over / degraded = kill switch holding) and
  offline when the controller is unreachable (no hangs); the Tailscale exit-node indicator;
  Rotate (same city / same country / any); pinned regions as one-click items, the rest under
  "More countries…"; Secure Core toggle; Auto-failover toggle; Find server for URL…; Server
  browser…; Settings…
- **Server browser** (window): searchable table of the live catalog (name, city, load bar,
  feature badges, enabled state), filters (country, P2P, streaming, Secure Core, IPv6, free,
  hide disabled), Connect = exact select. Filters are view-only.
- **Find server for URL** (sheet): URL, countries, mode; live results per candidate; Switch.
- **Settings → Regions**: drag-to-reorder pinned regions (country or city).
- **Settings → Connection**: provider levers from `exitctl <p> options --json` (Proton:
  NetShield 0/1/2, VPN Accelerator, Moderate NAT, NAT-PMP). They live in the key (D17), so
  Apply = rotate slot a (~9 s, ~1.5 s of drops), and the pane says so.
- **Settings → Account**: provider login state and login sheet (password, then totp/captcha
  steps), slots a/b with options and expiry (Renew, Rotate), the provider's key list with
  orphans flagged (Delete), catalog source and age (Push now / Revert to pinned), launchd
  agent on/off.
- **Notifications**: failover done, degraded, recovered, key expiring, catalog stale.
- Launch at login (`SMAppService`).

**Phase 5a results (2026-10-20)** — controller changes deployed; 73 controller, 14 helper,
19 prober unit tests green; `tests/acceptance/phase5a-client.sh` 6/6, Phase 3 regression 4/4
(T9 recovery 21 s):
- T23 exact select CH#697 at its IP; profile `[port_forward, stream, ipv6]`.
- T24 silent drop of that server → CH#821, same city, still P2P, in 27 s.
- T25 Secure Core on → CH-US#3 (entry CH); rotate stays Secure Core (other entry IP);
  off → a regular server.
- T26 regions `[Zurich, New York]` → "Rotate (any)" from New York lands in Zurich.
- Saved state migrated `countries` → `regions` on start. The re-pushed catalog marks 317 of
  1,077 servers IPv6-capable.
- Host-level changes: none.

**Acceptance (T23–T30):** T23 exact select lands on the requested (name, ip); T24 failover
from a P2P server stays P2P and in the same city when one is up; T25 Secure Core on → only
Secure Core servers through failover and rotate, off → none; T26 pinned regions drive
"Rotate (any)" and failover order, and old `countries` state migrates; T27 every menu action
works against the live controller, live updates arrive over SSE; T28 controller down → offline
state within 5 s (refused) or 20 s (silent), no hangs, recovers by itself; T29 the app's Connection pane rotates slot a
with NetShield 2 and back; T30 the launchd agent pushes the catalog and the app shows its age.

**Phase 5 results (2026-10-21)** — `mac/ExitControl` built (`./build-app.sh`, universal,
ad-hoc signed) and run from the Mac against the live controller; 12 app unit tests green (+5
live tests with `EXITCONTROL_LIVE=1`); checked visually window by window (`screencapture -l`)
and click by click (`EXITCONTROL_DEBUG=1` log + controller events):
- T27 menu: ⌘2 US-CA#556 → US-TX#153; pinned region Switzerland → CH#921, Los Angeles →
  US-CA#1072; Secure Core on → CH-US#3 (entry CH), off → US-CA#756. Each action's events
  and the new status arrived over SSE.
- T28 refused (closed port) → offline in < 1 s; silent (port the tailnet policy drops) →
  offline in ~20 s ("request timed out"); back on the right URL it reconnects by itself.
- T29 Connection pane NetShield 2 → Apply (new slot a key), then 0 → Apply; slot a ends on
  NetShield 0.
- T30 Account → "Push hourly": the launchd agent (`org.exit-vpn.catalog-push`) installed,
  ran at load (exit 0) and pushed 1,077 servers; Push Now works too; the pane shows the age.
- Found and fixed while testing: flags came from hostnames (Proton hosts some virtual
  locations elsewhere: NG#1 is `node-es-…`), now from server names; "health"/"prefs" events
  had no detail; turning Secure Core on logged a stale prefs event before the switch (now one,
  after it); the acceptance scripts cleared the Mac's exit node at the end, now they restore
  the one in use before.
- Host-level changes: none (controller redeployed with the prefs-event fix). Mac: the
  LaunchAgent above and `exitctl` as a uv tool (Homebrew Python 3.13).

### Phase 5b — App v1.1

- **Requirements** (controller item 5, already in `prefs.require`): Settings → Connection
  "Every server must have: P2P / Streaming / IPv6". Failover and rotation only consider
  servers with all of them; nothing matching → `degraded`, as with the P2P profile.
- **Favourite servers**: `prefs.favorites` = logical server names (`CH#821`; the physical IPs
  behind a name change, the name does not). Within each failover/rotate tier, favourites come
  first, then the profile and load order. App: a star in the server browser, a "Favourites"
  submenu (one-click exact select of the least-loaded physical server of that name).
- **Action URLs**: `exitvpn://rotate?scope=city|country|any`,
  `exitvpn://connect?country=…&city=…`, `exitvpn://connect?server=CH%23821`,
  `exitvpn://securecore?on=1|0`. They act only from the app (no web page can trigger them
  silently: macOS asks before a browser opens a custom scheme).
- **Shortcuts (App Intents)**: "Rotate Exit Server", "Connect Exit to Region", "Exit Status"
  (returns server, country, exit IP, health). App Intents need Xcode's metadata extraction,
  so this item also decides the build (SwiftPM vs an Xcode project generated from a spec).
- **Event log**: done in v1 as Settings → Events; add "Event Log…" to the menu.
- Later: per-candidate latency from the prober; forwarded port display with NAT-PMP.

**Acceptance (T31–T34):** T31 `require: [port_forward]` → rotate any and failover land only
on P2P servers; T32 a favourite in the current city is chosen first by "Rotate (city)"; T33
each `exitvpn://` action works and shows in the controller events; T34 the Shortcuts app lists
the three intents and "Exit Status" returns the live values.

**Phase 5b results (2026-10-21)** — controller (favourites, ISO country names) deployed; 75
controller and 13 app unit tests green; `tests/acceptance/phase5b-client.sh` 3/3:
- T31 `require: [port_forward]`: rotate city / country / any all landed on P2P servers.
- T32 the busiest server in Los Angeles as a favourite → "Rotate (city)" picked it; an unknown
  favourite is refused and changes nothing.
- T33 four `exitvpn://` URLs sent back to back (rotate, Secure Core on, off, connect Los
  Angeles) ran in order (a FIFO queue; menu clicks still refuse while busy).
- T34 the Shortcuts app lists Rotate Exit Server, Connect Exit to Region and Exit Status;
  `shortcuts run` of a shortcut with Exit Status returns the live server, place, exit IP and
  health. **App Intents only run for apps signed by an Apple-issued certificate with a team
  ID**: linkd rejects ad-hoc and self-signed apps ("requiresValidatedBundle", Shortcuts says
  it "couldn't communicate with the app"). The build therefore signs with the owner's free
  Personal Team "Apple Development" certificate (no paid program), set in the gitignored
  `mac/ExitControl/Local.xcconfig`; without it the build is ad-hoc and everything but
  Shortcuts works. The certificate needs Apple's WWDR G3 intermediate in the keychain.
- Also: the app build moved to XcodeGen + xcodebuild (`swift build` does not extract App
  Intents metadata); flags from server names; China was named "CN" (ISO fallback); server
  browser columns star | server | country | city | load | features | IP, nearest first, and a
  static load bar (the animated one moved between reused rows while scrolling).
- Host-level changes: none. Mac: the Apple Development certificate (Xcode) and the WWDR G3
  intermediate in the login keychain.

### Phase 5c — Release: CI, signed builds, in-app updates

- **CI** (GitHub Actions, public repo → free macOS runners): on every push/PR run the
  controller, prober and helper unit tests (Linux) and `swift test` + app build (macOS).
- **Release** on a `v*` tag: build the universal app, sign, (notarize), zip/dmg, GitHub
  Release with notes; version from the tag (`CFBundleShortVersionString`).
- **Updates**: Sparkle 2 (the standard for non-App-Store Mac apps): an appcast feed served
  from GitHub (Releases or Pages), each update signed with an EdDSA key held as a CI secret;
  "Check for Updates…" in the menu and automatic checks.
- **exitctl**: stays a separate install (GPLv3 proton-core is not bundled, D21); releases
  pin a helper version, and the app tells when the installed `exitctl` is older.
- **Signing (Q8, decided 2026-10-21): no paid Apple Developer account.** Builds are signed
  with the owner's free Personal Team "Apple Development" certificate (needed for Shortcuts,
  5b T34; it also keeps one identity across updates, so macOS keeps the app's permissions).
  Not notarized: the first launch of a download needs an explicit Open (right-click → Open,
  or System Settings → Privacy & Security → Open Anyway). CI signs with that certificate
  (exported .p12 as a GitHub secret; it expires yearly, so renew and update the secret).
  Sparkle verifies every update with its EdDSA signature, which protects the update channel.

**Acceptance (T35–T37):** T35 CI green on a PR; T36 a `v*` tag produces a GitHub Release with
the app; T37 an installed older build offers the newer release, updates and relaunches.

**Phase 5c results (2026-10-21)** — CI and releases live; ExitControl installed from a release:
- T35 `.github/workflows/ci.yml` green on `main`: controller, prober and helper unit tests
  (Ubuntu, uv, Python 3.13) and `swift test` + app build (macos-26, ad-hoc). Actions pinned by
  commit SHA.
- T36 tag `v0.2.0` → `release.yml` built the app signed with the owner's Apple Development
  identity (a throwaway keychain in the runner; WWDR G3 checked against a pinned SHA-256),
  zipped it, wrote a one-item appcast with its EdDSA signature (Sparkle 2.10.0
  `generate_appcast`) and published both as a GitHub Release.
  `releases/latest/download/appcast.xml` served 0.2.0 (build 52 = commit count).
- T37 0.2.0 from the release installed in `~/Applications`; tag `v0.2.1` (version line in the
  menu) → "Check for Updates…" offered 0.2.1, installed it and relaunched; the installed app is
  0.2.1 (53), signature valid.
- Secrets (repo, used only by `release.yml`): `MACOS_SIGNING_P12`, `MACOS_SIGNING_P12_PASSWORD`,
  `MACOS_DEVELOPMENT_TEAM`, `SPARKLE_ED_KEY`. The Sparkle private key also stays in the owner's
  login keychain (account `exit-vpn-ExitControl`); the public key is in Info.plist. The
  certificate expires 2027-10: renew in Xcode, re-export, update the two P12 secrets.
- Found while testing: `swift test` built the package's old app target (no Sparkle), so the
  package is now the core library and tests only; LaunchServices sends `exitvpn://` and
  Shortcuts to any registered copy, so dev builds are no longer registered.
- Host-level changes: none.

### Phase 5d — Keep the Mac on the exit node (app v0.4.0)

Why: the exit fails closed only while a device has it selected. On 2026-10-21 a hand-run check
cleared the Mac's exit node (`tailscale set --exit-node=`), the next acceptance run saved and
"restored" that empty value, and the Mac's traffic went out directly via the home ISP.
Tailscale's own answer, a mandatory exit node (`ExitNodeID` system policy), needs a Premium or
Enterprise plan and device management.

- **ExitNodeGuard** (ExitControlCore): fed by ExitNodeMonitor (now every 5 s; one second after
  a change). When this Mac has *no* exit node, it selects the controller's node again
  (`tailscale set --exit-node=<node>`; LAN access untouched) and notifies. Another exit node is
  a choice and is left alone; Tailscale being off (`BackendState` ≠ Running, new
  `ExitNodeUse.tailscaleOff`) is VPN On Demand's job. A failure is shown in the menu and
  notified once per distinct message. Setting "Keep this Mac on the exit node" (on by default).
- **Go Direct for 10 Minutes** (menu, for captive portals): clears the exit node and pauses the
  guard, which selects it again when the pause ends ("Use the Exit Node Now" ends it early).
- **Acceptance scripts** source `tests/acceptance/exitnode.sh`: they record the exit node in use
  and refuse to run on a Mac with none (exit 2; `ALLOW_NO_EXIT=1` overrides), so a test can no
  longer end by "restoring" none.

Acceptance: T41 with the guard on, clearing the exit node gets it back within one interval;
T42 a client script on a Mac with no exit node stops before changing anything.

**Phase 5d results (2026-10-21)**:
- T41 dev build (live Tailscale): `tailscale set --exit-node=` → `exit-proton` selected again
  after 1 s ("this Mac had no exit node; selected exit-proton"), `ExitNodeAllowLANAccess` kept.
  Unit tests: the guard acts only on `.none`, not when disabled or paused, the pause ends by
  itself, a repeated failure is reported once; the monitor re-reads one second after a change;
  `BackendState` Stopped parses as `tailscaleOff`. 28 tests.
- T42 `exitnode.sh` with a fake `tailscale` reporting no exit node: exit 2 and the message;
  with `ALLOW_NO_EXIT=1` it continues; on the real Mac it records exit-proton.
- Host-level changes: none.

### Phase R — Architecture pass (before Phase 6)

Goal: for each component, one can say "these are the important concepts, they are represented
by these objects and connected this way", and the code and docs say it too. Behaviour stays the
same except for the bugs listed; every step leaves unit tests and the acceptance suites green.
Based on four read-only reviews (2026-10-21) of the controller, prober, exitctl and the app.

**Bugs found by the reviews (fixed in the step that touches them):**
- Controller: `PUT /v1/prefs` with a Secure Core change can fail (409) after regions,
  favourites and flags were already saved (half-applied). Fix: validate all, switch, then commit.
- Controller: the degraded-recovery cycle exists twice (`health_step` and `run`); production uses
  the untested copy. Fix: one `step()`, tested with a fake clock.
- Controller: some events (`health`, `recovered`, `catalog`, `key_meta`) are lost on restart
  (saved only when something else saves). Fix: the event log gets its own persistence.
- Prober: if Chromium fails while opening a page context, the probe returns 500 and the tunnel
  peer is not cleared (the kill switch still holds). Fix: tunnel session with cleanup in `finally`.
- exitctl: unexpected errors (bad stdin JSON, missing slot, keyring) escape as tracebacks with no
  JSON on stdout; the app shows a stderr line. Fix: one error→exit-code table.
- App: reading exitctl's stdout before stderr can deadlock on a large stderr; no timeout or
  cancel. A status that no longer decodes is silently ignored (stays "online" with stale data).

**R1 — Contracts first.** The wire formats between components are untyped dicts checked
nowhere. Add golden JSON fixtures in `contracts/` (controller status, servers, keys, every event
kind; prober request/response; exitctl output per command, error object and exit codes) that the
producer's tests must emit and the consumer's tests must parse (pytest on both Python sides,
swift-testing in the app). Write `contracts/README.md`: each format, required fields,
compatibility rules (unknown fields ignored, unknown enum values tolerated).

*R1 done (2026-10-21):* 24 samples in `contracts/` (controller 7, prober 4, exitctl 13) with
`shape.py` and a README (formats, exit codes, SSE framing, event catalogue, how to change a
contract). Producer checks: controller API responses and every event any test emits (and a
test that every event kind in the code has a sample), the controller's prober requests, the
prober's responses, exitctl's `--json` output and exit codes. Consumer checks: the app decodes
every controller and exitctl sample (and has a summary line for every event kind). Found while
writing them: `recovered`, `find_started` and `swap_error` are never emitted by any test (the
first confirms the untested degraded-recovery path, fixed in R2).

**R2 — Controller** (target concepts):
- Domain (pure): `Server`/`ServerId`, `Feature` (one vocabulary, hard vs soft), `Region`,
  `Catalog` (lookups, canonical names/regions, no file I/O), `Prefs` (`updated(...)` validates
  and canonicalises), `ActiveServer` (refers to a `Server`), `Blacklist`, selection functions
  (`selection.py`, renamed from failover: failover, rotate, select, exact, find), `HealthTracker`.
- Services: `ExitService` (the lock, active server, prefs, catalog; `step()`/`run()`, rotate,
  select, select_exact, apply_find, update_prefs, replace_catalog, replace_exit_key, snapshot),
  `Tunnel` (the only object touching wg0: try a server, try a key, check), `EventBus` (publish,
  subscribe, recent; the only way to emit), `KeySlots`, `FindService`, `CatalogSource`.
- Adapters: `wg`, `HealthProbe` (renamed from probes.Prober), `ProberClient`, `auth`,
  `persistence` (state.json, peer.env for init-wg, catalog cache, key files), `api` (input shapes
  and error→HTTP only). `__main__` is the composition root.
- Steps: EventBus and no private access → rules out of api.py (atomic prefs) → one degraded
  cycle (failover drill on the VPS outside the blackout windows) → key wiring without
  monkey-patched callbacks → value types (state.json stays readable) → Tunnel → CatalogSource
  and composition root, dead code out → shell/Python contract (peer.env, slot-a precedence) tested.

*R2 done (2026-10-21):* the controller is now `servers` (Server, Region, features),
`catalog`, `prefs` (immutable, `updated()` validates), `state` (ActiveServer, Blacklist),
`selection` (pure), `tunnel` (the only code touching wg0), `service.ExitService` (one lock, one
health `step()`, use cases, `snapshot()`), `events.EventBus` (persisted in `events.json`),
`slots.KeySlots`, `finder.FindService`, `prober_client`, `catalog_source`, `persistence`,
`errors`, `node.build` (composition root, also used by the tests) and a thin `api`. 86 unit
tests (78 before; new: degraded recovery and retry, all-or-nothing prefs, events across a
restart, a failing find candidate, the peer.env and slot-a key shell contracts). Deployed;
acceptance phase2 9/9 (T6 needed a second run: the first restart came right after a
kill-switch failover and recovered after the 60 s window; no leak either time), phase3 4/4
(T9 failover 19 s), phase3b 10/10, phase3b2 7/7, phase4 5/5, phase5a 6/6, phase5b 3/3. Behaviour
changes: PUT /v1/prefs is all-or-nothing; a region pick orders candidates like every other
list (favourites, profile, load) instead of at random; an unknown country in a pick is 400.
Host-level changes: none (new file `events.json` in the state volume).

**R3 — Prober:** `Prober` service (one probe, start to finish, with the lock), `ProbeTunnel`
with a session context, `ExitCheck` (no engine import), `Engine` interface (`HttpEngine`,
`BrowserEngine`, `Persona`), `Classifier` with explicit rule precedence, `Outcome` enum (same
strings), `needs_browser` policy, `KeyFile`; `api.py` maps typed request/response models.
Controller side: `ProberClient` in its own module with keyword arguments, `Finder` builds a
typed `FindResult`. Re-run phase4 and phase5a suites.

*R3 done (2026-10-21):* the prober is `model` (Outcome, Engine, Target, Observation,
Verdict, TierResult, ProbeResult), `classify` (ordered `RULES`), `html`, `engines` (`HttpEngine`,
`BrowserEngine`, `Persona`), `exitcheck.ExitCheck` (no engine import), `keyfile.KeyFile`,
`tunnel.ProbeTunnel` (`session()` always detaches), `probe.Prober` (one probe start to finish,
`needs_browser` policy) and a thin `api`; `__main__` builds it. 29 unit tests (23 before); the
contract samples did not change. Fix: an engine failure (Chromium dying while a context opens)
is an `error` result and the peer is detached, instead of a 500 with the peer left on
wg-probe. Deployed; phase4 5/5, phase3b 10/10 (T20 covers the key reload). Host-level changes:
none.

**R4 — exitctl:** typed model (`Lever`, `Account`, `Certificate`, `KeyMeta`, `NewKey`,
`ServerRow`, `SlotId`), ports (`Provider`, `SessionStore` with Keychain and memory
implementations, `ExitNode` controller client with typed errors), `KeyService` (rotate, renew,
certificates with orphan flags, import, push catalog: the use cases now in cli.py),
`DeviceNaming`; Proton adapter split (API, explicit login state machine, crypto); the CLI builds
commands from capabilities and prints through JSON/human presenters. JSON output unchanged;
contract changes (one account shape, `count` for pushed servers) only together with the app.

*R4 done (2026-10-21):* exitctl is `model` (Lever, `resolve_levers`, KeyMeta, NewKey,
Certificate, Account, and the errors ExitctlError/ProviderError/NeedInput/NodeError),
`sessions` (SessionStore: Keychain, memory), `exitnode.ExitNode` (typed errors; a missing slot
is an error, not StopIteration), `keyservice` (KeyService: rotate, renew, certificates,
import, servers; DeviceNaming), `providers/base` (the interface, `Credentials`,
`renewal_meta`), `providers/proton/` (adapter, `session.ProtonSession` with the login steps,
`crypto`, `levers`, `catalog`), generic `wgkeys`, and `cli` (commands by capability, one
`Output`, an exit-code table, the interactive login loop). JSON output unchanged (the contract
tests pass as before). Fixes: unexpected errors (bad --stdin JSON, a missing slot) are JSON
errors with exit 1 instead of tracebacks; `--json`/`--url` before a subcommand were reset by
it (`exitctl --json keys` printed text; the app always passed them last); `list` no longer
calls every exit-node certificate an orphan when the node is unreachable. 21 unit tests (14
before). Real account: keys, list, whoami, servers read correctly; phase3b 10/10, phase3b2 7/7,
the app's live tests 5/5. The installed tool needs `uv tool install --force --reinstall` after
a change (uv caches the build by version).

**R5 — App:** core gets protocols over the controller client and exitctl, typed vocabulary
(`RotationScope`, `Feature`, `Health`, `FindMode`, `FindOutcome`, `SettingsTab`, `AppRoute`),
`ControllerCommand`/`ProviderCommand` with labels, one `ActionRunner` (menu clicks refuse while
busy, URLs and Shortcuts queue in order; awaits instead of polling), stores (`ConnectionStore`,
`CatalogStore`, `ProviderStore`, `ExitNodeMonitor`, `AppSettings`), small state machines
(`FindSession`, `LoginFlow`), pure helpers (`ServerQuery`, `Geography`, `StatusText`), a
hardened `ProcessRunner`. The app target keeps views, `AppContainer` (composition root),
`Router`, Intents, Updater. Unchanged: UserDefaults keys, bundle id, LaunchAgent label,
`exitvpn://` grammar, Intent names and parameters. Release only after T27–T34 by hand.

*R5 done (2026-10-21):* the app's core library is now the vocabulary (`Health`,
`RotationScope`, `Feature`, `FindMode`, `FindOutcome`, `SettingsTab`, `AppRoute`), wire models
(`ControllerModels`, `Event`, `ExitctlModels`, `Geography`), `ControllerAPI` +
`ControllerCommand` (the client conforms; one `perform`), `ProviderCLI` + `ProviderCommand`
(`Exitctl`), `ProcessRunner` (concurrent pipes, timeout, terminate on cancel; exitctl,
tailscale, launchctl), `ActionRunner` (menu: refuse while busy; URLs and Shortcuts: queue in
order), `ConnectionStore`, `CatalogStore`, `ProviderStore` (lever resolution as exitctl's),
`ExitNodeMonitor`, `AppSettings` (same UserDefaults keys), `ServerQuery`, `StatusText`,
`LoginFlow`, `FindSession`, and typed `ExitURL`. The app target is `AppContainer` (composition
root, replaces the 343-line AppModel), `Router`, `Updater` (owned, not a singleton), the views
and Intents (names and parameters unchanged). 26 core tests (17 before). Behaviour changes:
warnings already present at launch no longer notify on every start; the server list reloads
when the catalog changes; an unreadable status shows "can't be read" instead of stale data;
`exitvpn://settings/<unknown>` opens General. Checked: build without warnings, the four URLs
in a row ran in order (T33), the windows render live data.

**R6 — Documentation:** `ARCHITECTURE.md` (the system, then one concept map per component:
concepts, objects, connections, locks, files on disk, startup order), component READMEs
(`controller/`, `prober/`, `helper/` with "adding a provider"), module/class docstrings stating
invariants, the event catalogue and status schema (from R1), and PLAN corrections where the code
has moved on (Phase 3b interface, Phase 4 response shape).

**Acceptance:** each component's unit tests (with the new contract tests) and the acceptance
suites green after each step; a failover drill after R2's degraded-cycle step; the owner reads
ARCHITECTURE.md and the code and agrees the concepts are clear.

*R6 done (2026-10-21):* `ARCHITECTURE.md` (the system, the exit container's boot order and
data plane, a concept map per component, contracts, where to look), READMEs for `controller/`,
`prober/`, `helper/` (with adding a provider) and the app's, module docstrings stating the
invariants (written with R2–R5), and the PLAN passages that had drifted (Phase 3b interface,
Phase 4 response). Released as ExitControl 0.3.0.

### Phase D — Published node images (deployable without a checkout)

- **Images**: a tag builds `exit` and `prober` for amd64 and arm64 in GitHub Actions and pushes
  them to GHCR (`ghcr.io/deoliveira-r/exit-vpn-exit`, `…-prober`), public like the repo, with
  build provenance (`actions/attest-build-provenance`) and an SBOM; deploys check them with
  `gh attestation verify`.
- **Deploy kit**: `compose.yaml` uses the published images by version and digest (the `build:`
  sections move to a dev override file); `install.sh` creates `secrets/` (0700) and `.env` (0600),
  takes the Tailscale auth key and WireGuard configs (or points to exitctl), pulls and verifies
  the images, and starts the stack. The README lists the tailnet policy entries it needs.
- **Updates**: change the version in `.env`, pull, restart. The controller reports its version
  in `/v1/status`; the app can later show "exit node update available".
- **Migration**: the VPS moves from the git checkout to the kit after a test run there.
- **Versions (decided 2026-10-21)**: separate tag series: the app keeps `vX.Y.Z`, the node
  images use `node-vX.Y.Z`; the controller's API version (in `/v1/status`) says which app versions
  work with it. Still to confirm: the VPS architecture; public images.
- **Acceptance**: T38 a node tag publishes both images (both architectures) with verifiable
  provenance; T39 a fresh directory with the kit, `.env` and secrets brings up a working exit;
  T40 the VPS runs the published images and the Phase 3/5a suites pass.

**Phase D results (2026-10-21)**:
- T38 `node-v1.0.0`: `node-release.yml` built `exit` and `prober` on native amd64 and arm64
  runners, merged multi-arch images to `ghcr.io/deoliveira-r/exit-vpn-{exit,prober}:1.0.0`
  (+ `latest`), with BuildKit provenance and SBOM. GitHub attestations verify (`gh attestation
  verify`: built by node-release.yml from refs/tags/node-v1.0.0 at 2ad2953). The packages are
  public (anonymous pulls work). The release carries the deploy kit (compose.yaml,
  .env.example, install.sh, README, images.env with the digests) and is not "latest": the app's
  feed still serves 0.3.1.
- T40 `deploy/rollout.sh 1.0.0` from the Mac: verified both images, pinned the digests in the
  VPS's `.env`, pulled, restarted, and saw `node-v1.0.0` healthy after 84 s. Keys, the pushed
  catalog and the event log carried over (same project, same volumes). Then phase3 4/4 (T9 18 s),
  phase5a 6/6, phase4 5/5 and phase2 8/9: T7 (fin-intel's API) failed with a TLS "internal
  error" alert from fin-intel's own HTTPS server, while it had passed at 13:00 UTC. That is
  fin-intel's side (this stack has its own Tailscale node and restarted only its containers);
  left to the owner, not investigated on the host.
- T39 partly: the kit's files and install.sh's checks are in place, but a run on a fresh host
  needs a second host (a node on the VPS beside the live one would need its own tailnet
  identity, keys, ports and subnet); left for the next new host.
- `/v1/status` reports `version` (`node-v1.0.0`; `dev` for a source build) and `api` (1).
  Host-level changes: none (the deploy directory is still the git checkout, now running the
  published images; `docker compose up -d` there uses them).

**node-v1.0.1 (2026-10-21)**: Tailscale 1.102.5 → 1.104.2, built with Go 1.27.3 (1.102.5 had Go
1.26.6, without the crypto/x509 denylist for the compromised eMudhra intermediates,
CVE-2026-111943). The images' Debian `ca-certificates` (20250419+deb13u1) and certifi
(2026.10.19) already exclude the affected roots. `deploy/rollout.sh 1.0.1`: live and healthy.
Its `grep` checks ran without sudo on the 0600 `.env`, so each rollout appended the image lines
a second time (same values); fixed, and the duplicates removed from the host's `.env`. Then
phase3 4/4 (T9 23 s), phase5a 6/6, phase4 5/5, phase2 8/9: T3 failed once (no answer from
MagicDNS via the exit, three tries) and passed on the rerun; T7 (fin-intel's API) still fails,
now with no connection (000). Host-level changes: none (the `.env` dedupe is inside the deploy
dir).

### Phase RT — Test organisation

Why: the tests had a sound underlying structure (logic, component, boundary, contract,
acceptance) that their files did not show. Files were named after the phase that added them
(`test_phase5`, `phase3b-client.sh`) or were grab bags (`test_units`, `test_helper`, `CoreTests`);
fakes lived in test files and were imported across them; one API test checked eight behaviours;
the acceptance scripts carried nine copies of the same helpers. Principle (owner): name things
after concepts in the present code; phases and steps are ephemeral.

- RT1 `TESTING.md`: the five kinds, the rules, one table per component (file → kind → concept).
- RT2–RT5 controller, prober, exitctl, app: files per concept; fakes in the harness; one
  behaviour per test; every existing behaviour keeps a test (old → new mapping), coverage not lower.
- RT6 acceptance: scripts named by concept (`client/egress.sh`, `client/failover.sh`, ...;
  `node/...` on the VPS), a shared `lib.sh`, checks named by concept, a README with what each
  proves (and the old PLAN IDs, for history), `run-client.sh`.
- RT7 the same principle in code comments: references to phases replaced by the concept or
  document they meant.

**Phase RT results (2026-10-21)**:
- Unit tests 164 → 300, all green; every old test's behaviour kept (old → new mapping checked
  per component), none dropped; line coverage not lower anywhere, higher in places
  (controller `api` 82→84%, `prefs`/`selection` →100%; prober `api` 91→96%, `tunnel` 77→87%,
  `exitcheck` →100%; exitctl `exitnode` 43→93%, `sessions` 59→97%).
  - controller 86 → 118: 14 files per concept; the eight-behaviour API test split down to the
    lowest kind (new `test_prefs`, pure); new checks for 403/503, select without a target,
    maintenance picks.
  - prober 29 → 69: `test_probe` through `Prober.run`, `test_exitcheck` and `test_keyfile` split
    out; new: malformed requests, failed key reload (409, no echo), personas, key via stdin.
  - exitctl 21 → 51: the `test_helper` grab bag split into seven concepts; new boundary tests for
    the controller client (`httpx.MockTransport`) and the Keychain store (a stand-in `keyring`).
  - app 28 → 62: one file per concept; the inline status JSON replaced by the contract sample.
  - Each Python package has `harness.py` (fakes, builders) and a `conftest.py` with fixtures
    only; the app has `Harness.swift`. No test file imports another.
- Acceptance: `tests/acceptance/{lib.sh, run-client.sh, README.md, client/, node/}`; checks print
  `PASS <suite>: <what it proves>`; regrouped by concept (failover now holds both failover
  checks, keys the bootstrap mirror, secrets on its own). Live: access 2/2, egress 8/9,
  selection 6/6, preferences 3/3, failover 2/2, find 5/5 (after fixing a sampler check that
  counted timed-out samples as exit changes), catalog 6/6, keys 8/8, secrets 3/3; on the VPS
  tunnel 9/9, tailnet 12/12. The one failure is the tailnet service check (fin-intel's API over
  the tailnet, HTTP 000), while fin-intel's service answers locally on the host: its tailnet
  HTTPS side, not this stack.
- RT7: no references to phases or decision IDs left outside PLAN.md (code, tests, scripts,
  workflows, compose, READMEs); CLAUDE.md records the naming rule.
- Host-level changes: the acceptance scripts ran from a temporary copy in `/tmp` on the VPS,
  removed afterwards.

### Phase V6 — IPv6 through the exit

Why: open question 6 chose "IPv4 first". Today every IPv6 packet a client sends through the
exit hits table 100's `unreachable` route: a fast ICMPv6 error, so clients fall back to IPv4
(T4). Sites that are IPv6-only, or that treat IPv6 users differently, are out of reach, and a
dual-stack client learns nothing from its v6 path. Proton's configs already carry an in-tunnel
IPv6 address (`2a07:b944::2:2/128`; DNS `2a07:b944::2:1`) and the catalog marks
IPv6-capable servers (feature bit 16; 101 of the 141 enabled US servers in the live catalog).

What holds today (read on the VPS, 2026-10-21): the host has no global IPv6 (only its
tailnet `fd7a:` address); in the exit container `eth0` has `disable_ipv6=1`, `wg0` has no v6
address, table 100 has only `unreachable default` for v6; tailscaled already advertises `::/0`
and has its own `ip6` tables. So the tunnel is the only way IPv6 could ever leave, and adding it
cannot open a path via the VPS.

Principle: **IPv6 is open only while proven.** A server may not carry IPv6 (unflagged, or
flagged but broken); then the exit keeps today's behaviour, a fast unreachable, never a silent
drop that stalls clients without Happy Eyeballs.

- **V6-0 Verify first** (throwaway container on the VPS, key b, own netns, like
  `node/provider-keys.sh`; no change to the stack): on two flagged and two unflagged US
  servers, with `2a07:b944::2:2/128` on wg0 and `::/0` allowed: does `curl -6` reach
  Cloudflare's trace, with which address; does an unflagged server drop silently or answer
  with an error; v6 throughput; does `ping -6 2a07:b944::2:1` answer. Decides whether the gate
  below may rely on the flag or must probe (expected: probe).
- **V6-1 Data plane** (`common.sh`, `init-wg`, `init-killswitch`, `killswitch.nft`,
  `route-keeper`): `first_ipv6` beside `first_ipv4` (optional: a config without v6 behaves as
  today); the v6 address on wg0; v6 rules 105 (`oif wg0`) and 110 (`from` the v6 tunnel
  address); `nat_exit` becomes `table inet` (masquerade v4 and v6 on wg0: clients' `fd7a:`
  sources become the tunnel address). The kill switch's forward drop is already `inet`. DNS
  stays IPv4 (AAAA answers come over it). The **IPv6 gate**: `route-keeper` owns table 100's
  v6 default via wg0 and keeps it only while `/run/exit-vpn/ipv6` exists; otherwise the
  `unreachable` route answers (today's behaviour). One owner of routes, the controller decides.
- **V6-2 Controller**: `wgconf` reads the optional v6 address; the health probe adds an IPv6
  trace bound to it (`exit_ip6`); a **gate** in the health domain: closed on every server
  change (new exit, new v6 address), open after one good v6 probe, closed after two failed in
  a row. IPv6 never causes a failover (it is a soft feature, as in selection): a flagged
  server whose v6 fails gets a warning. `EXIT_IPV6=off` in `.env` keeps the gate closed.
  Status gains `ipv6` (gate open) and `exit_ip6`; an `ipv6` event when the gate changes
  (contracts updated on both sides).
- **V6-3 App**: the status line shows the IPv6 exit address when open ("IPv6: off on this
  server" otherwise); the event in the log. Nothing to configure.
- **V6-4 Prober**: stays IPv4 (its exit check and find results are about the v4 exit). Its
  netns has no v6 route; a node check keeps it that way.
- **V6-5 Tests** (TESTING.md kinds): logic: the gate; boundary: `first_ipv6` (shell contract),
  `wgconf` v6, the v6 probe; component: gate closes on switch, opens after a probe, writes and
  removes the flag; contract: status and event samples.

Acceptance:
- T43 (client `egress`) on a flagged server: `curl -6` through the exit gets a Proton v6
  address, in the exit's country, never one in the Mac's own v6 prefix; DNS still via the VPN.
- T44 (client `egress`) exact-select an unflagged (or v6-failing) server: `curl -6` fails in
  under 1 s (unreachable, not a stall); IPv4 unaffected; status `ipv6: false`.
- T45 (client `egress`) kill switch: while wg0 is down and during a restart, no v6 egress at
  all; v6 back after recovery and after a failover (`failover`).
- T46 (node `tunnel`) from the exit container: v6 leaves only via wg0; with the flag removed,
  v6 is unreachable; the prober has no v6 route; `fin-intel` and the host untouched.
- Host-level changes: none expected (container netns only).

Decided (owner, 2026-10-21): the gate is opened by the probe (the catalog flag is only a hint
for selection); IPv6 never triggers failover, even when `ipv6` is a required feature;
`EXIT_IPV6=off` in `.env` is the off switch (no app preference).

**Phase V6 results (2026-10-21)**: node-v1.1.3 and ExitControl v0.5.0.
- V6-0 (`node/provider-ipv6.sh`, key b): flagged servers carry IPv6 (US-GA#208, US-WA#451:
  Proton exit addresses `2a02:6ea0:…`, 71 and 94 Mbit/s, the v6 DNS answers); unflagged ones
  (US-NJ#41, US-AZ#106) **drop it silently** (8 s timeouts), so the gate must be proven by probe.
  exitctl's configs had no IPv6 address: exitctl now writes Proton's (`2a07:b944::2:2/128`,
  DNS `2a07:b944::2:1`), and slot a was rotated before the rollout to get one.
- Changed from the draft by what the VPS showed (node-v1.1.0 → v1.1.3):
  - The tunnel's IPv6 address is **not an address of wg0** but a local route on `lo`
    (`net.ipv6.ip_nonlocal_bind=1` for the probe, `snat ip6 to` it for forwarded traffic).
    As an interface address, Linux picked it as the source of every unbound IPv6 socket
    (it retries a failed lookup after choosing a source), rule 110 sent those through the
    tunnel, and tailscaled's own netcheck went via the VPN; wg0 going down also flushed it.
  - The gate is an **nftables set** (`inet killswitch` `ipv6_gate`), not the route: forwarded
    IPv6 is always routed to wg0 and, while closed, rejected with a TCP reset (ICMP for the
    rest). The unreachable route's ICMPv6 error is dropped by the Tailscale client, so
    connections stalled for their full timeout (8 s); with the reset they fail in 0.2 s.
  - The gate is **damped**: after a close on the same server it reopens only after 3, 6, 12,
    then 24 good probes in a row. US-FL#371's IPv6 came and went (32 gate changes in 25
    minutes); each close resets clients' IPv6 connections. The close event carries the error.
- Observed upstream, not ours: the first IPv6 connection to a destination sometimes stalls
  4–8 s at Proton (also from the exit itself, bound to the tunnel address); later ones and
  throughput match IPv4 (exit 133 vs 121 Mbit/s, Mac 64 vs 57). Happy Eyeballs hides it from
  apps; `client/ipv6.sh` allows three tries for its first request and reports a stall.
- Unit tests 300 → 330: controller 118 → 147 (`test_ipv6`, the gate and its damping, the v6
  probe, the gate file, shell contract for both addresses and `EXIT_IPV6`), exitctl 51 → 52,
  app 62 → 64. Contracts: status `ipv6`, `exit_ip6`; event `ipv6` (a digit in a kind had kept
  the harness from checking it: its regex now allows digits).
- Acceptance: T43–T45 as `client/ipv6.sh` 6/6 (IPv6 via the VPN in the exit's country, never
  this Mac's own networks; none while the tunnel is down, back after; 0.21 s failure on a server
  without IPv6 while IPv4 works); `egress` 9/9 (IPv6 via the VPN); T46 in `node/tunnel.sh` 14/14
  (unbound IPv6 does not leave, the tunnel's IPv6 via the VPN, the gate file and the kill
  switch's set agree, the prober has no IPv6 route, IPv6 fails closed with wg0 down);
  `tailnet` 12/12, `failover` 2/2, `access` 2/2. On node-v1.1.1: selection 6/6, preferences 3/3,
  find 5/5, catalog 6/6, keys 8/8, secrets 3/3.
- Host-level changes: the deploy checkout fast-forwarded to the release commits (it was at
  Phase D; compose now passes `EXIT_IPV6` and sets `net.ipv6.ip_nonlocal_bind`, both namespaced);
  throwaway containers (`provider-ipv6`, script tests) removed after use; slot a rotated.

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
- "Add to home allow-list": opens a **pull request** on the `tailnet-policy` repo (D14) editing `nodeAttrs[...].app["tailscale.com/app-connectors"]` → `home-us.domains`: the registrable domain plus hosts observed during the probe's navigation (redirect chain, main-document hosts). Shows the diff first; CI tests it; merging applies it. Credential: a fine-grained GitHub token limited to that repo (contents + pull requests), in the macOS Keychain. Optional auto-merge when CI passes.
- Allow-list management view (list/remove entries).

Acceptance:
- **T13** With exit node = `exit-proton`: an allow-listed domain shows the **home** IP (cdn-cgi/trace on that domain, or a test domain you control); any other domain shows the Proton IP (V9).
- **T14** Policy edit from the Mac: an invalid edit fails CI on its PR and is never applied; two concurrent edits conflict as normal git merges.
- **T15** NAS/home down → allow-listed domains fail (expected, fail-closed); everything else still works via Proton.

### Phase 7 (optional) — Brazil residential node

- Raspberry Pi (Linux; app connectors require Linux) at the family home (BR), `tag:connector-br`, app connector `home-br` + exit node. Same compose as `home-gw` (arm64).
- Mac app allow-list entries gain a target: `home-us` | `home-br`.
- UniFi Site Magic is **not** used for this (it would hairpin through the US home); independent of this project.

### Phase M — Connection drops: measure, fix, measure again

**Why.** Since IPv6 went live (Phase V6) the owner's impression is that connections drop more
often. Plan: about two days of baseline with the connection monitor (`monitor/`, on the Mac),
then conclusions and a fix, then a week of monitoring with the same measure.

**Measure** (records: `monitor/connection_monitor.py`): fresh IPv4/IPv6 requests every 10 s with
timings; one long-lived trickle download per family, so resets of *established* connections
are seen; DNS through the exit; the tailnet path to the exit (direct/DERP, online); the Mac's
physical network and sleep gaps; every exit event (`/v1/events`) and its status each minute;
owner notes (`monitor/mark.sh`). The exit's side: its container log for the same window.

**Suspect before the baseline** (the exit's log, 2026-10-21/22, node-v1.1.3):
- The IPv6 gate closed ~30 times in about 9 hours, on every server used (US-FL#292, US-CA#756,
  US-CA#96), each time on `ConnectTimeout` of the 3 s IPv6 probe; damping then keeps it closed
  ~2 min. Proton's first IPv6 connection to a destination stalls 4–8 s (Phase V6 results), longer
  than the probe's timeout, so the gate likely closes on healthy servers.
- A closed gate rejects *every* forwarded IPv6 packet, established flows included: each close
  resets all the clients' open IPv6 TCP connections, which apps prefer over IPv4.
- The monitor's first 35 s showed both: the Mac's first IPv6 connect took 5.2 s, then the gate
  closed on a probe timeout.
- The drops the owner sees are Claude Code sessions failing: either a retry timer (recovered) or
  "API Error: Connection lost mid-response" (the request is lost). Claude Code's transcripts
  record each error with a timestamp. The four overnight (2026-10-22 00:52:43, 02:27:15,
  03:53:50, 03:54:30 UTC) fall within a second of an IPv6 gate close in the exit's log
  (00:52:43, 02:27:14, 03:53:50, 03:54:30). `api.anthropic.com` has an AAAA record, so its
  streams use IPv6 while the gate is open, and a close cut them mid-reply.

**Conclusion and fix (node-v1.1.4)**, so the baseline was cut short:
1. The closed gate refuses only *new* IPv6 connections (`ct state new` in `killswitch.nft`); open
   ones carry on. On a server whose IPv6 really broke they stall and time out, as IPv4 ones do.
2. The IPv6 probe gets its own 10 s timeout (`probe6_timeout`), longer than the 4-8 s first-
   connection stall, and runs across health cycles (a cycle waits for it no longer than for the
   IPv4 probes; until it finishes there is no IPv6 verdict), so IPv4 health keeps its cadence. A
   server or key change drops a probe still running for the old one.
3. The gate closes after three failed IPv6 probes in a row (was two).
Acceptance: `ipv6` gains "a closing gate refuses new IPv6 connections at once but never resets
an open one". The monitor keeps running for a week after the deploy; the measure is the same
(Claude Code connection errors vs gate closes, the monitor's long-lived flows).

Deployed 2026-10-22 04:24 UTC (node-v1.1.4). Acceptance: `ipv6` 8/8 (new: a closed gate
refuses a new IPv6 connection at once while an open one carries on, 100 KB in 25 s, never
reset), `egress` 9/9, VPS `tunnel` 14/14. Seen meanwhile: Proton's IPv6 connection setup
stalls intermittently, not only on a destination's first connection, and it varies by server
and time. On US-CA#96 (04:18-04:22 UTC) 10 of the Mac's 15 fresh IPv6 requests took 1-7 s to
connect (SYN retransmits); right after, on US-CA#152, 0 of 16 from the Mac and 2 of 16 from the
exit itself, IPv4 never. Clients' Happy Eyeballs falls back to IPv4 within ~250 ms, and the 10 s
probe no longer closes the gate on it. Still the last Claude Code error before the deploy (04:21:14 UTC,
"The response stopped arriving", gate open since 04:20:47) is not a gate close: watch for
mid-stream stalls on IPv6 during the week (the monitor's IPv6 flow).

IPv6 switched off (2026-10-22 ~05:05 UTC). With the gate open and nothing reset, the errors
went on ("The response stopped arriving", 04:53:04). Most of the Mac's fresh IPv6 connections
needed SYN retransmits (2-7 s) on every US server tried. A capture in the exit's netns on CH#293
showed all 12 of the exit's own IPv6 SYNs leave via wg0 with no SYN-ACK: the loss is upstream.
Owner-driven rotations (04:49-04:56) caused errors of their own (each switch ends every open
connection). **Host-level change:** `EXIT_IPV6=off` in `/opt/exit-node/.env`, exit container
recreated. The monitoring week runs IPv4-only and is the baseline for Phase Q. To do: with IPv6
off, forwarded IPv6 meets the unreachable route (an ICMP error the Tailscale client drops), so
it stalls until timeout instead of being refused; clients fall back to IPv4 (Happy Eyeballs),
but the exit should refuse it with a TCP reset as the closed gate does (next node release, at
a time the owner picks: it restarts the exit).

**Done when:** the baseline names the causes of the drops it saw (or shows there are none
beyond the Mac's own network), a fix is deployed, and a week of monitoring shows the drops
attributed to the exit gone or rare; results recorded here.

### Phase Q — Quality of service: measured rotation between an active and a standby tunnel

**Why.** A failover today takes detection (3 failed cycles, ~15 s) plus a fresh session's
warm-up (Proton drops TCP for 4-14 s; up to 20 s waited), and the next server is untested until
then. The exit also cannot tell a dead server from a broken uplink or test site, and it learns
that a server degrades only once it fails. No handover can save connections already open (the
exit's public IP changes), so the aim is: switch rarely, switch to a connection already proven,
switch in under a second, and switch before failing where possible.

**Design (owner's sequence, 2026-10-22):** a proactive, measured rotation over three
connections: *active* (carries forwarded traffic), *standby* (kept ready for a handover) and
*find* (the prober, key b: unused most of the time; it searches for a server that unblocks a site,
or for a good server for the standby).
- **Both exit tunnels always measured.** The exit holds `wg0` and `wg1` on keys of slots a and c;
  roles, not interfaces, are active and standby. Keys stay with their interfaces.
- **Quality states: Healthy, Degraded, Critical** (Critical includes a total drop), from live
  measurement only: round-trip time and loss (a frequent, cheap probe through each tunnel),
  HTTPS time to first byte, DNS time, jitter, over a sliding window of a few minutes. No server
  history: past quality would bias the choice against servers that improved.
- **The roles differ in tolerance:**

  | | Healthy | Degraded | Critical |
  |---|---|---|---|
  | active | stay | hand over at a quiet moment | hand over immediately |
  | standby | stay ready | find searches; the standby moves when a good server is found | standby and find both search; the first good server becomes the standby |

  - A handover goes only to a Healthy standby (a degraded one for a degraded active only ends
    connections).
  - Worst case, active Critical while the standby is not ready: the active joins the search, so
    up to three searches run at once (active, standby, find), each on different candidates. The
    first exit tunnel to reach a healthy server becomes the active, the next the standby. Find
    cannot carry traffic (its tunnel is the prober's): a server it proves is taken up by
    whichever exit tunnel is still searching.
  - Second opinion: if the standby fails at the same moment as the active, the fault is the
    uplink or the test site; the exit stays.
  - A quiet moment: no active forwarded flows (conntrack, tailscale0 counters), e.g. between
    Claude Code replies; after a limit the handover happens anyway.
  - The standby stays in the chosen region (prefs: country/city, required features), so a
    handover never moves the exit somewhere the owner did not pick.
- **Handover = one atomic operation.** Everything else is in place beforehand: the kill switch
  and NAT allow both tunnel interfaces (a set), each tunnel has its own probe routing. The
  switch itself is `ip route replace default dev <standby> table 100`: one netlink message,
  packets before it use the old tunnel, after it the new one. Then the roles swap, and the
  former active, now standby, looks for a better server.
  - Both tunnels carry the same in-tunnel address (Proton gives every config `10.2.0.2`), so
    each tunnel's probes bind to its interface, not to the address.
  - Connections open at the handover would stall (their packets now leave with a different
    public IP, which the far end ignores) until apps time out ("The response stopped
    arriving"). Right after the swap the exit ends them with a reset (conntrack entries of the
    old tunnel removed, their next packets refused), so apps reconnect at once.
- **IPv6 later, as a measured attribute.** Off for now (Phase M). Per-server IPv6 quality can
  join the states later, at the cost of more frequent rotation; its benefit to the owner is small.

**Where it runs (owner, 2026-10-22):** everything above runs on the VPS, in the controller: both
tunnels, their probes, the states, the standby's search, the handover and the second opinion.
Quality of service here means VPS ↔ Proton, measured at the VPS, so nothing depends on any
client being on. The Mac's monitor (`monitor/`) only measures the outcome end to end (Mac ↔
tailnet ↔ VPS ↔ Proton); its tailnet records separate drops on the Mac ↔ VPS leg, which no
server change can fix and which Phase Q must never react to.

**Steps** (each deployable and measured with `monitor/` against the Phase M baseline):
- Q-1 Standby tunnel: slot c key (`exitctl proton rotate --slot c`), `wg1`, the interface set in
  the kill switch and NAT, per-tunnel probes; the standby kept warm and health-checked; failover
  = atomic role swap with the reset of old connections; the second opinion. Events and status
  gain the standby (`standby`, `handover`).
- Q-2 Quality measurement on both tunnels; states in status and the app; measure many servers
  to calibrate what Healthy, Degraded and Critical mean.
- Q-3 Standby rules: moves on Degraded (with find), both search on Critical.
- Q-4 Active rules: handover on Critical at once, on Degraded at a quiet moment.
- Later, experiment only: both tunnels active at once, flows split per destination (a failure
  ends half the connections; sites may see two exit IPs).

**Acceptance (Q-1):** T47 (client `handover`) with the standby ready, a silently dropped active
server is handed over to it with no failover search and no leak, traffic flows via the standby's
VPN IP, a connection open across the handover is reset within seconds (not left to stall), and
the standby is rebuilt on another server. T48 (client `handover`) both tunnels dropped at once:
no handover, no failover, no blacklisting, `uplink_suspect`; the exit carries on when the fault
ends. `tunnel`, `tailnet`, `egress`, `ipv6`, `failover` keep passing with two tunnels (kill-switch
checks take both down).

**Decided (owner, 2026-10-22):** three connections (active, standby, find); find serves both
the standby's search and unblocking; no server history; tolerances calibrated from Q-2's
measurements. A quiet moment is waited for at most 2 minutes (to tune). The IPv6 refusal fix ships with Q-1.

**Done when:** a killed active tunnel hands over in under a second with traffic flowing on the
standby (acceptance), the uplink-down case causes no switch, a degraded active is left for the
standby only at a quiet moment, and a week of monitoring shows fewer and shorter drops than
the Phase M baseline.

**Q-1 progress (2026-10-22, 08:30 UTC):** node-v1.2.3 deployed (1.2.0 → 1.2.3 the same morning),
app v0.6.0; the standby is ready on a second Zurich server with the slot c key. Findings:
- Every slot c key was rejected ("5 servers in a row carried no traffic"), yet wg1's DNS
  probe passed and curl through wg1 worked. Cause: httpx applies `socket_options`
  (SO_BINDTODEVICE) *after* the TCP connect, so the HTTPS probe's SYN followed rule 110 into the
  active tunnel, then broke when the socket was bound to wg1. Every check of the standby failed
  (its search, its health, the second opinion). The wg0 probe only passed because wg0 was
  active. Fixed in 1.2.3: the probe opens its own socket (device, bind, connect, TLS). 1.2.2
  added each failed standby candidate's errors to the events, which showed it.
- The 90 s warm-up for a first standby key (1.2.1) was a wrong guess; it stays (harmless).
- Sparkle downloaded app 0.6.0 but never installed it; `mac/ExitControl/update-app.sh` now
  installs a release from the CLI with the same signature checks.
- Rollouts ran with the Mac switched to direct (`exitvpn://direct`, then `exitvpn://exitnode`),
  so the owner's sessions kept working.
- Acceptance: `tailnet` 12/12. Pending, with the owner present (they cut or reset the Mac's
  connections): client `handover` (T47, T48), `egress`, `ipv6`, `failover`; VPS `tunnel`.
- Host changes: `.env` `EXIT_IPV6=off`; image digests via rollout; the `keys-c` volume (from
  compose); the checkout at `/opt/exit-node` pulled to the deployed commit.

---

## 7. Security model

| Asset | Where | Protection |
|---|---|---|
| Proton keys A/B | `secrets/` on VPS, mounted read-only into one container each | file mode 0400 root; not in images; not logged |
| Tailscale node keys | `ts-state` volume | tagged nodes, key expiry disabled by tagging; `tag:exit` has no outbound tailnet grants |
| Tailnet policy write | Tailscale federated identity, bound to `main` of the private `tailnet-policy` repo | no stored token anywhere (D14); Mac only holds a GitHub token scoped to PRs on that repo |
| Controller API | tailnet IP only, ACL to members, whois check | no public exposure; no published port except UDP 41642 (tailscaled) |
| fin-intel | host | unchanged; `tag:server` ACLs unchanged; `tag:exit` cannot reach it |

Accepted risk: host-root compromise of the VPS compromises both fin-intel and the exit node.

---

## 8. Operations runbook (docs/runbook.md)

- Logs: JSON lines to stdout; `docker compose logs -f exit-proton prober`.
- Status: `curl http://exit-proton:8080/v1/status` from the Mac.
- Rotate VPN keys: `exitctl proton rotate --slot a|b` (Phase 3b); extend expiry: `exitctl proton renew --slot a|b`. Manual fallback: dashboard config → `exitctl manual import --slot a file.conf` → delete the old config in the dashboard.
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
| T16–T22 | 3b | key rotation via the provider API sets levers; bad keys roll back; no session/key leaks; renew; prober key reload; live catalog |
| T23–T30 | 5 | exact select; equivalent-server failover; Secure Core on/off; pinned regions; app actions, SSE, offline state; levers and catalog agent from the app |
| T31–T34 | 5b | requirements; favourites; action URLs; Shortcuts |
| T35–T37 | 5c | CI; tagged release; in-app update |
| R | R1–R6 | contracts pinned; unit + acceptance suites green after each step; failover drill; owner review |
| T38–T40 | D | published images with provenance; fresh deploy from the kit; VPS on published images |
| T41–T42 | 5d | the app re-selects a cleared exit node; client scripts refuse a Mac with none |
| T13–T15 | 6 | per-domain residential routing over the exit node; safe policy edits; fail-closed |

---

## 10. Open questions (ask the owner before Phase 1)

1. VPS OS, arch, Docker present? (§5)
2. Existing Tailscale DNS config (MagicDNS, split DNS for `<home split-DNS domain>`, global nameservers)?
3. ~~Default country preference~~ → US, then CH.
4. ~~NetShield on generated keys~~ → 0 on all keys; filtering lever to be done on our side (own resolver) if wanted.
5. ~~Repo location~~ → GitHub.
6. **Decided: IPv4 first**, IPv6 after v4 is solid. Proton configs now include in-tunnel IPv6 (`2a07:b944::2:2/128`, DNS `2a07:b944::2:1`). Keep IPv6 blocked at the exit (T4 as written), or carry IPv6 through wg0 (dual-stack exit; T4 becomes "IPv6 exits via Proton")? → Phase V6.
7. ~~`Bouncing` label~~ → does not restrict which servers a key can use (Phase 0).
8. ~~Apple Developer Program~~ → no paid program: free Personal Team signing, not notarized, an explicit Open on first launch (Phase 5c).
