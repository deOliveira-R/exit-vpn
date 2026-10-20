#!/usr/bin/env bash
# Phase 2, client side: run on a Mac in the tailnet (any VPN app disconnected).
# Selects the exit node, runs T1-T8, and restores "no exit node" on exit.
# The VPS is reached over the tailnet (not through the exit node), and every
# kill-switch step restores itself on the server, so nothing here can strand the node.
set -uo pipefail
NODE=${EXIT_NODE_NAME:-exit-proton}
VPS=${VPS_SSH:-fin-intel}            # ssh alias for the VPS
VPS_TS_NAME=${VPS_TS_NAME:-fin-intel} # its tailnet name
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
trace() { curl -s --max-time "${1:-5}" https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|loc)=' | tr '\n' ' '; }

vps_ip=$(tailscale ip -4 "$VPS_TS_NAME") || { echo "cannot resolve $VPS_TS_NAME in tailnet"; exit 1; }
# Same server, same host key: verify it against the alias's known_hosts entry
vps_alias_host=$(ssh -G "$VPS" | awk '$1=="hostname"{print $2}')
vps() { ssh -o HostName="$vps_ip" -o HostKeyAlias="$vps_alias_host" -o ConnectTimeout=10 "$VPS" "$@"; }
oracle=$(vps 'curl -s --max-time 5 https://www.cloudflare.com/cdn-cgi/trace | sed -n s/^ip=//p')
[ -n "$oracle" ] || { echo "cannot get the VPS's own egress IP"; exit 1; }
before=$(trace)
case "$before" in *"ip=$oracle"*) echo "Mac already egresses via the VPS?"; exit 1;; esac

trap 'tailscale set --exit-node= >/dev/null 2>&1; echo "(exit node cleared)"' EXIT
tailscale set --exit-node="$NODE" --exit-node-allow-lan-access=true || { echo "cannot select $NODE"; exit 1; }
for _ in $(seq 20); do t=$(trace 3); [ -n "$t" ] && [ "$t" != "$before" ] && break; sleep 1; done

t=$(trace)
case "$t" in ""|*"ip=$oracle"*) bad "T1 egress via exit node: '$t'";; *) ok "T1 egress via exit node is a VPN IP: $t";; esac

# The first lookup through a freshly selected exit node can time out: retry
for _ in 1 2 3; do r=$(dig +short +time=3 +tries=1 whoami.akamai.net @100.100.100.100 | head -1); [ -n "$r" ] && break; done
g=$(dig +short +time=3 +tries=1 TXT o-o.myaddr.l.google.com @ns1.google.com | tr -d '"' | head -1)
if [ -n "$r" ] && [ "$r" != "$oracle" ] && [ -n "$g" ] && [ "$g" != "$oracle" ]; then
  ok "T3 DNS: resolver egress $r, direct-query egress $g (neither is the VPS)"
else bad "T3 DNS leak check: resolver='$r' direct='$g'"; fi

v6=$(curl -6 -s --max-time 5 https://ifconfig.co 2>/dev/null)
[ -z "$v6" ] && ok "T4 IPv6 fails cleanly through the exit node" || bad "T4 IPv6 answered: $v6"

p=$(tailscale ping -c 5 "$NODE" 2>&1 | tail -1)
case "$p" in *"via DERP"*) bad "T5 path is relayed: $p";; *"via "*) ok "T5 direct path: $p";; *) bad "T5 tailscale ping: $p";; esac

# T2: tunnel down for 15 s (self-restoring on the server): never the VPS IP
vps -n "sudo docker exec -d $NODE sh -c 'ip link set wg0 down; sleep 15; ip link set wg0 up'"
# Sample only inside the outage window (2-12 s of the 15 s), however slow each probe is
sleep 2; seen=""; end=$((SECONDS+10))
while [ $SECONDS -lt $end ]; do seen="$seen [$(trace 2)]"; sleep 0.5; done
case "$seen" in *"ip=$oracle"*) bad "T2 kill switch LEAKED: $seen";; *"ip="*) bad "T2 traffic while wg0 down?: $seen";; *) ok "T2 kill switch: no egress while wg0 is down";; esac
for _ in $(seq 40); do t=$(trace 3); [ -n "$t" ] && break; sleep 1; done
[ -n "$t" ] && ok "T2 recovers after wg0 is back: $t" || bad "T2 no recovery"

# T6: container restart race: only VPN IPs or failures, never the VPS IP
log=$(mktemp)
( end=$((SECONDS+60)); while [ $SECONDS -lt $end ]; do echo "$(trace 2)" >> "$log"; sleep 0.5; done ) &
loop=$!
sleep 3; vps -n "cd /opt/exit-node && sudo docker compose restart >/dev/null 2>&1"
wait "$loop"
n=$(wc -l < "$log" | tr -d ' '); okn=$(grep -c 'ip=' "$log")
if grep -q "ip=$oracle" "$log"; then bad "T6 restart LEAKED the VPS IP"; elif [ "$(tail -3 "$log" | grep -c ip=)" -ge 1 ]; then ok "T6 restart: $okn/$n samples via VPN, rest failed closed, recovered"; else bad "T6 no recovery after restart"; fi
rm -f "$log"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$VPS_TS_NAME.$(tailscale status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["MagicDNSSuffix"])')/health")
[ "$code" = 200 ] && ok "T7 fin-intel API reachable over the tailnet" || bad "T7 fin-intel API $code"

bps=$(curl -s -o /dev/null --max-time 30 -w '%{speed_download}' 'https://speed.cloudflare.com/__down?bytes=25000000')
mbps=$(python3 -c "print(round(${bps:-0}*8/1e6,1))")
echo "INFO T8 download via exit node: $mbps Mbit/s"
python3 -c "import sys; sys.exit(0 if ${mbps} >= 20 else 1)" && ok "T8 throughput >= 20 Mbit/s" || bad "T8 throughput $mbps Mbit/s"

echo "== $pass passed, $fail failed"; [ "$fail" -eq 0 ]
