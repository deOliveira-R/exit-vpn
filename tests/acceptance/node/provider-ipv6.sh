#!/usr/bin/env bash
# What the IPv6 design assumes of the provider, checked without the stack: per server, does
# IPv6 pass through the tunnel (the config's in-tunnel v6 address, ::/0 allowed), with which
# exit address, how fast; and does a server without IPv6 fail fast or drop silently. Everything
# happens in a throwaway container (own netns) on the prober's key; no host network changes.
# Servers on stdin, "name ip pubkey" per line (e.g. from /v1/servers with and without ipv6):
#   sudo tests/acceptance/node/provider-ipv6.sh [SECRETS_DIR] < servers
# Prints what it saw; read the result (no automatic verdict).
set -euo pipefail

SECRETS=${1:-/opt/exit-node/secrets}
IMAGE=alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
conf="$SECRETS/proton_b.conf"

work=$(mktemp -d)
trap 'docker rm -f provider-ipv6 >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
(umask 077; sed -n 's/^PrivateKey[[:space:]]*=[[:space:]]*//p' "$conf" > "$work/key")
[ -s "$work/key" ] || { echo "no PrivateKey in $conf" >&2; exit 1; }
# The config's v6 address and DNS, else Proton's fixed in-tunnel ones (configs exitctl wrote
# before IPv6 carry only IPv4)
v6of() { sed -n "s/^$1[[:space:]]*=//p" "$conf" | tr ',' '\n' | tr -d ' ' | grep ':' | head -n1 || true; }
addr6=$(v6of Address); addr6=${addr6:-${ADDR6:-2a07:b944::2:2/128}}
dns6=$(v6of DNS); dns6=${dns6:-${DNS6:-2a07:b944::2:1}}
echo "in-tunnel v6 address $addr6, dns $dns6"
cat > "$work/servers"

cat > "$work/t.sh" <<'SH'
#!/bin/sh
set -u
apk add -q --no-cache wireguard-tools-wg iproute2 curl iputils >/dev/null
TRACE=https://www.cloudflare.com/cdn-cgi/trace
ip link add wg0 type wireguard; wg set wg0 private-key /key
ip addr add 10.2.0.2/32 dev wg0; ip -6 addr add "$ADDR6" dev wg0
ip link set wg0 mtu 1420 up
ip route add default dev wg0 table 100; ip -6 route add default dev wg0 table 100
ip rule add priority 100 oif wg0 lookup 100; ip rule add priority 110 from 10.2.0.2 lookup 100
ip -6 rule add priority 100 oif wg0 lookup 100; ip -6 rule add priority 110 from "${ADDR6%/*}" lookup 100
echo "underlay v6 (want none): $(curl -6 -s --max-time 4 "$TRACE" | grep '^ip=' || echo none)"
old=""
while read -r name ip pk; do
  [ -n "$old" ] && wg set wg0 peer "$old" remove
  wg set wg0 peer "$pk" endpoint "$ip:51820" allowed-ips 0.0.0.0/0,::/0 persistent-keepalive 25; old=$pk
  t0=$(date +%s); v4=""
  while [ $(( $(date +%s) - t0 )) -lt 25 ] && [ -z "$v4" ]; do
    v4=$(curl -4 -s --max-time 3 --interface wg0 "$TRACE" | sed -n 's/^ip=//p')
  done
  out=$(curl -6 -sS --max-time 8 --interface wg0 -w 'took=%{time_total}\n' "$TRACE" 2>&1); rc=$?
  ms=$(echo "$out" | sed -n 's/^took=//p' | awk '{printf "%d", $1*1000}')
  v6=$(echo "$out" | sed -n 's/^ip=//p'); loc=$(echo "$out" | sed -n 's/^loc=//p')
  [ -n "$v6" ] && res="ip=$v6 loc=$loc" || res="FAIL rc=$rc ($(echo "$out" | head -c 80))"
  png=$(ping -6 -c 2 -W 2 -I wg0 "$DNS6" 2>&1 | grep -o '[0-9]* received' || echo none)
  sp=""; [ -n "$v6" ] && sp=$(curl -6 -s -o /dev/null --max-time 20 --interface wg0 -w '%{speed_download}' \
    'https://speed.cloudflare.com/__down?bytes=10000000' | awk '{printf "%.1f Mbit/s", $1*8/1e6}')
  echo "$name: v4=${v4:-FAIL} v6: $res after ${ms} ms; ping6 dns: $png; v6 speed: ${sp:--}"
done < /servers
SH
chmod +x "$work/t.sh"

docker run --rm --name provider-ipv6 --cap-add NET_ADMIN --sysctl net.ipv6.conf.all.disable_ipv6=0 \
  -e ADDR6="$addr6" -e DNS6="$dns6" \
  -v "$work/key:/key:ro" -v "$work/t.sh:/t.sh:ro" -v "$work/servers:/servers:ro" "$IMAGE" /t.sh
echo
echo "criteria: flagged servers give a v6 exit address (not the underlay, which has none) in the"
echo "server's country; for unflagged ones note fast failure (ms, rc 7) vs a stall (rc 28)."
