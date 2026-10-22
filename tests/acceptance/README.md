# Acceptance tests

The deployed system, end to end: on the VPS (`node/`) and from the owner's Mac (`client/`).
Each script is one concept; each check prints `PASS|FAIL <suite>: <what it proves>`, and the
script exits non-zero if any check failed (2: it stopped before testing, e.g. a missing
prerequisite). `lib.sh` holds what they share (reporting, cleanup at exit, the controller API,
egress samples, fault injection, the Mac's exit node). Where these fit among the other tests:
[TESTING.md](../../TESTING.md).

## From the Mac

    tests/acceptance/run-client.sh             # every client suite, then a summary
    tests/acceptance/run-client.sh egress keys # some

Prerequisites:
- No other VPN app connected (the Proton app off).
- The Mac uses an exit node; the scripts that select `exit-proton` put the previous one back at
  exit and refuse to start on a Mac with none (`ALLOW_NO_EXIT=1` overrides).
- `catalog`, `keys` and `secrets` need `helper/.venv/bin/exitctl proton login`, done by the owner
  in their own terminal (password, 2FA, CAPTCHA prompts); `run-client.sh` skips them otherwise.
- SSH to the VPS over the tailnet (`VPS_SSH`, `VPS_TS_NAME`).

| Suite | Proves | Changes (and puts back) | Was |
|---|---|---|---|
| `access` | the owner's Mac reaches the API; a tagged host gets no connection | nothing | T11 |
| `egress` | traffic and DNS leave via the VPN; IPv6 via the VPN or an immediate failure, never this Mac's own; the path is direct; the kill switch holds while the tunnel is down and during a restart, never the VPS's IP; tailnet services stay reachable; throughput ≥ 20 Mbit/s | the Mac's exit node; restarts the containers | T1–T8 |
| `ipv6` | on an IPv6 server the gate opens and this Mac's IPv6 leaves via the VPN in the exit's country, never from this Mac's own networks; a closing gate refuses new IPv6 connections at once but never resets an open one; no IPv6 egress while the tunnel is down, back after; on a server without IPv6 it fails in under 2 s while IPv4 works | the Mac's exit node, the active server (put back), the gate file removed for 11 s (put back), wg0 down for 15 s | |
| `selection` | rotate within the country; one exact server with its P2P kept; Secure Core on and off; "rotate any" follows the pinned regions | the active server, preferences | T10, T23, T25, T26 |
| `preferences` | a required feature holds for every rotation; a favourite is tried first; an unknown favourite is refused | the active server, preferences | T31, T32 |
| `failover` | a silently dropped server fails over with no leak, traffic back via a new VPN IP (target 35 s); an exact P2P server fails over to another P2P server | the Mac's exit node, the active server, nft drops on the VPS | T9, T24 |
| `find` | the prober tells a friendly URL from a challenged one; the active exit is untouched while probing; a probe whose tunnel dies is an error; a found server is applied | the Mac's exit node, the active server, `wg-probe` briefly | T12 |
| `catalog` | a pushed list replaces the pinned one, carries load, drives rotation, survives a restart; a truncated push is refused; revert works | the catalog (pushed again at the end), restarts the exit | T21 |
| `keys` | rotating the exit's key goes live without a leak and applies its options, deleting the old certificate; renewing keeps key and server; an unregistered key is rolled back; find works on the prober's new key; the bootstrap files mirror the live keys | both keys (fresh at the end, NetShield 0), the Mac's exit node | T16, T17, T19, T20, T22 |
| `secrets` | no private key in the API or container logs; the provider session is nowhere on the VPS | nothing | T18 |

## On the VPS

From the deploy dir, as a sudoer: `sudo bash tests/acceptance/node/<suite>.sh`.

| Suite | Proves | Changes | Was |
|---|---|---|---|
| `tunnel` | the exit container's traffic and DNS leave via wg0, its underlay via the host; no IPv6 via the underlay; the tunnel's IPv6 via wg0; forwarded IPv6 passes the kill switch exactly while the IPv6 gate is open; the prober has no IPv6 route; it fails closed (IPv6 too) while wg0 is down and recovers; the host's egress and services are untouched | wg0 down for a moment | P1 |
| `tailnet` | the node is logged in, named, tagged, an approved exit node, not taking over DNS; our rules and kill switch coexist with tailscaled's; forwarded traffic goes to wg0; the host's own Tailscale node and services are untouched | nothing | N1–N12 |
| `provider-ipv6` | (without the stack) per server given on stdin: does IPv6 pass through the tunnel, with which address and speed; a server without IPv6 drops it silently or fails fast. Prints what it saw; no verdict | a throwaway container on key b | |
| `provider-keys` | (before or without the stack) one provider key works against different servers by swapping the peer; two keys work concurrently. Prints what it saw; no verdict | throwaway containers | V1, V2 |

"Was": the IDs these checks had in PLAN.md's results, for reading its history.

## Settings

`EXIT_NODE_NAME` (exit-proton), `API_PORT` (8080), `VPS_SSH` and `VPS_TS_NAME` (the VPS's ssh
alias and tailnet name), `EXITCTL`; per suite: `TAILNET_URL` (egress), `FRIENDLY_URL`,
`CHALLENGE_URL`, `DELAY_URL` (find), `HOST_SERVICE_URL`, `HOST_TS_NAME` (node suites).
