#!/usr/bin/env bash
# A Mac's traffic through the exit node: it leaves via the VPN, DNS too, IPv6 fails cleanly,
# the path is direct, and it fails closed (never the VPS's own IP) while the tunnel is down or
# the containers restart. Every outage restores itself on the VPS, so nothing here can strand
# the node. From a Mac with no other VPN app connected.
. "$(dirname "$0")/../lib.sh"
vps_connect
before=$(egress_ip)
[ "$before" = "$VPS_EGRESS" ] && die "this Mac already leaves via the VPS"
use_exit_node
for _ in $(seq 20); do t=$(egress_ip 3); [ -n "$t" ] && [ "$t" != "$before" ] && break; sleep 1; done

t=$(trace)
case "$t" in ""|"$VPS_EGRESS "*) bad "egress via the exit node: '$t'";; *) ok "egress via the exit node is a VPN IP: $t";; esac

# The first lookup through a freshly selected exit node can time out: retry
for _ in 1 2 3; do r=$(dig +short +time=3 +tries=1 whoami.akamai.net @100.100.100.100 | head -1); [ -n "$r" ] && break; done
g=$(dig +short +time=3 +tries=1 TXT o-o.myaddr.l.google.com @ns1.google.com | tr -d '"' | head -1)
if [ -n "$r" ] && [ "$r" != "$VPS_EGRESS" ] && [ -n "$g" ] && [ "$g" != "$VPS_EGRESS" ]; then
  ok "DNS leaves via the VPN: resolver egress $r, direct-query egress $g"
else bad "DNS: resolver egress '$r', direct-query egress '$g'"; fi

v6=$(curl -6 -s --max-time 5 https://ifconfig.co 2>/dev/null)
[ -z "$v6" ] && ok "IPv6 fails cleanly through the exit node" || bad "IPv6 answered: $v6"

p=$(tailscale ping -c 5 "$NODE" 2>&1 | tail -1)
case "$p" in *"via DERP"*) bad "the path is relayed: $p";; *"via "*) ok "the path to the exit node is direct";; *) bad "tailscale ping: $p";; esac

# Tunnel down for 15 s (restored by the VPS itself); sample only inside the window (2-12 s)
vps "sudo docker exec -d $NODE sh -c 'ip link set wg0 down; sleep 15; ip link set wg0 up'"
sleep 2; seen=""; end=$((SECONDS+10))
while [ $SECONDS -lt $end ]; do ip=$(egress_ip 2); [ -n "$ip" ] && seen="$seen $ip"; sleep 0.5; done
case " $seen " in
  *" $VPS_EGRESS "*) bad "kill switch LEAKED the VPS IP:$seen";;
  "  ") ok "kill switch: no egress while the tunnel is down";;
  *) bad "traffic while the tunnel was down:$seen";;
esac
wait_egress 40 && ok "traffic recovers when the tunnel is back: $(trace)" || bad "no recovery after the tunnel came back"

# Restarting the containers: only VPN IPs or failures, never the VPS IP
log=$(mktemp); on_exit "rm -f '$log'"
sample_start "$log"
sleep 3; vps "cd /opt/exit-node && sudo docker compose restart >/dev/null 2>&1"
sleep 57; sample_stop
n=$(wc -l < "$log" | tr -d ' '); via=$(awk 'NF==2' "$log" | wc -l | tr -d ' ')
if leaked "$log"; then bad "a restart LEAKED the VPS IP"
elif [ "$(tail -3 "$log" | awk 'NF==2' | wc -l)" -ge 1 ]; then ok "a restart fails closed and recovers: $via/$n samples via the VPN, the rest failed"
else bad "no recovery after a restart"; fi

# Tailnet services stay reachable while this Mac uses the exit node
url=${TAILNET_URL:-https://$VPS_TS_NAME.$(tailscale status --json | j 'd["MagicDNSSuffix"]')/health}
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url")
[ "$code" = 200 ] && ok "a tailnet service stays reachable ($url)" || bad "tailnet service $url: HTTP $code"

bps=$(curl -s -o /dev/null --max-time 30 -w '%{speed_download}' 'https://speed.cloudflare.com/__down?bytes=25000000')
mbps=$(python3 -c "print(round(${bps:-0}*8/1e6,1))")
python3 -c "import sys; sys.exit(0 if $mbps >= 20 else 1)" && ok "throughput $mbps Mbit/s (at least 20)" || bad "throughput $mbps Mbit/s (under 20)"

summary
