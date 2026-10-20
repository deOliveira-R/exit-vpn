#!/usr/bin/env bash
# Phase 0, V1 + V2: run on the VPS as a sudoer.
#   V1: one Proton WireGuard key works against two different servers (peer swap, no
#       interface teardown) -> two different exit IPs.
#   V2: keys A and B are connected concurrently (to different servers).
# Everything happens inside throwaway containers (own netns); no host network changes.
# Usage: sudo tests/acceptance/phase0-wg.sh [SECRETS_DIR] [COUNTRY]
set -euo pipefail

SECRETS=${1:-/opt/exit-node/secrets}
COUNTRY=${2:-United States}
IMAGE=alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
CATALOG_URL=https://raw.githubusercontent.com/qdm12/gluetun-servers/main/pkg/servers/protonvpn.json

work=$(mktemp -d)
trap 'docker rm -f p0-a p0-b >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
# Only the PrivateKey is used; Proton binds the connection options to the key (PLAN D17).
for k in a b; do
  conf="$SECRETS/proton_$k.conf"
  [ -s "$conf" ] || { echo "missing $conf" >&2; exit 1; }
  (umask 077; sed -n 's/^PrivateKey[[:space:]]*=[[:space:]]*//p' "$conf" > "$work/key_$k")
  [ -s "$work/key_$k" ] || { echo "no PrivateKey in $conf" >&2; exit 1; }
  echo "proton_$k.conf options: $(grep -E '^# *(NetShield|Moderate NAT|NAT-PMP|VPN Accelerator)' "$conf" | sed 's/^# *//' | paste -sd ';' -)"
done
curl -fsSL -o "$work/protonvpn.json" "$CATALOG_URL"

# Three distinct non-secure-core, non-tor WireGuard servers: "name ip pubkey" per line.
python3 -I - "$work/protonvpn.json" "$COUNTRY" > "$work/servers" <<'PY'
import json, random, sys
servers = [s for s in json.load(open(sys.argv[1]))["servers"]
           if s["vpn"] == "wireguard" and s["country"] == sys.argv[2]
           and not s.get("secure_core") and not s.get("tor") and not s.get("free")]
by_city = {}
for s in servers:
    by_city.setdefault(s["city"], []).append(s)
cities = random.sample(sorted(by_city), k=min(3, len(by_city)))
picks = [random.choice(by_city[c]) for c in cities]
while len(picks) < 3:
    picks.append(random.choice([s for s in servers if s not in picks]))
for s in picks:
    print(s["server_name"].replace(" ", "_"), s["ips"][0], s["wgpubkey"])
PY
mapfile -t S < "$work/servers"
echo "servers (name ip):"; cut -d" " -f1,2 "$work/servers"

# Container-side script: bring up wg0 with the given key and peer, then print exit info.
cat > "$work/tun.sh" <<'SH'
#!/bin/sh
set -eu
apk add -q --no-cache wireguard-tools-wg iproute2 curl >/dev/null
# A fresh Proton session drops TCP data for ~4 s while the server probes the path
# (growing ICMP echoes from 10.2.0.1), so retry for up to 15 s and report the delay.
trace() {
  t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -lt 15 ]; do
    r=$(curl -s --max-time 3 --interface wg0 https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|loc)=' | tr '\n' ' ')
    [ -n "$r" ] && { echo "$r(after $(( $(date +%s) - t0 ))s)"; return; }
  done
  echo "FAIL (no traffic within 15s)"
}
peer() { wg set wg0 peer "$2" endpoint "$1:51820" allowed-ips 0.0.0.0/0 persistent-keepalive 25; }
ip link add wg0 type wireguard
wg set wg0 private-key /key
ip addr add 10.2.0.2/32 dev wg0
ip link set wg0 mtu 1420 up
ip route add default dev wg0 table 100
ip rule add priority 100 oif wg0 lookup 100
ip rule add priority 110 from 10.2.0.2 lookup 100
underlay=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace | grep '^ip=')
echo "underlay $underlay"
peer "$1" "$2"; echo "peer1 $(trace)"
if [ -n "${3:-}" ]; then
  wg set wg0 peer "$2" remove; peer "$3" "$4"; echo "peer2 $(trace)"
fi
touch /tmp/ready; sleep 60
SH
chmod +x "$work/tun.sh"

run() { # name keyfile args...
  local name=$1 key=$2; shift 2
  docker run -d --rm --name "$name" --cap-add NET_ADMIN \
    -v "$work/$key:/key:ro" -v "$work/tun.sh:/tun.sh:ro" "$IMAGE" /tun.sh "$@" >/dev/null
}
set -- ${S[0]} ; A1_IP=$2 A1_PK=$3
set -- ${S[1]} ; A2_IP=$2 A2_PK=$3
set -- ${S[2]} ; B_IP=$2  B_PK=$3

echo "== V1 (key A, peer swap ${S[0]%% *} -> ${S[1]%% *}) and V2 (key B on ${S[2]%% *}, concurrently)"
run p0-a key_a "$A1_IP" "$A1_PK" "$A2_IP" "$A2_PK"
run p0-b key_b "$B_IP" "$B_PK"
for _ in $(seq 90); do
  docker exec p0-a test -f /tmp/ready 2>/dev/null && docker exec p0-b test -f /tmp/ready 2>/dev/null && break
  sleep 1
done
echo "-- key A:"; docker logs p0-a 2>&1
echo "-- key B:"; docker logs p0-b 2>&1
echo "-- key A after B is up (concurrency check):"
docker exec p0-a sh -c 'curl -s --max-time 8 --interface wg0 https://www.cloudflare.com/cdn-cgi/trace | grep -E "^(ip|loc)=" | tr "\n" " "; echo'
echo "-- handshakes:"; docker exec p0-a wg show wg0 latest-handshakes | cut -c1-8,45-; docker exec p0-b wg show wg0 latest-handshakes | cut -c1-8,45-
echo
echo "PASS criteria: V1 = key A peer1 ip != peer2 ip, both != underlay. V2 = key B has a Proton ip"
echo "while key A still answers in the concurrency check."
