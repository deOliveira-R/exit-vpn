#!/usr/bin/env bash
# IPv6 through the exit node: on a server that carries it, this Mac's IPv6 leaves via the VPN
# (never with an address of its own network); while the tunnel is down there is no IPv6
# egress at all, and it comes back; on a server without IPv6 it fails at once (an error, not a
# stall) while IPv4 works. The active server is put back at the end. From a Mac with no other
# VPN app connected.
. "$(dirname "$0")/../lib.sh"
vps_connect
st=$(status)
[ "$(echo "$st" | j '"ipv6" in d')" = True ] || die "this node does not report IPv6 (older than IPv6 through the exit)"
read -r srv0 ip0 country <<<"$(echo "$st" | j 'd["server"], d["endpoint"].rsplit(":",1)[0], d["country"].replace(" ", "_")')"
country=${country//_/ }
on_exit "post POST /v1/select '{\"server\":\"$srv0\",\"ip\":\"$ip0\"}' >/dev/null; echo '(server $srv0 restored)'"
use_exit_node

# This Mac's own IPv6 networks (/64), which must never be the egress
own=$(ifconfig | awk '$1=="inet6" && $2 !~ /^(fe80|fd7a|::1)/ {split($2,a,":"); print a[1]":"a[2]":"a[3]":"a[4]":"}' | sort -u)
mine() { local p; for p in $own; do case "$1" in "$p"*) return 0;; esac; done; return 1; }
trace6() { curl -6 -s --max-time "${1:-5}" https://www.cloudflare.com/cdn-cgi/trace \
  | awk -F= '$1=="ip"{ip=$2} $1=="loc"{loc=$2} END{if (ip) print ip, loc}'; }
servers() { # feature filter (python expression over s) → "name ip" lines, least loaded first
  api -G "$API/v1/servers" --data-urlencode "country=$country" | j "'\n'.join(s['name']+' '+s['ip'] for s in sorted(d['servers'], key=lambda s: 50 if s['load'] is None else s['load']) if s['enabled'] and not s['secure_core'] and not s['free'] and ($1))"
}
select_server() { post POST /v1/select "{\"server\":\"$1\",\"ip\":\"$2\"}" | j 'd.get("server")'; }
wait_ipv6() { # want (True|False) seconds → the status once ipv6 is `want`, else empty
  local s; for _ in $(seq "$2"); do s=$(status); [ "$(echo "$s" | j 'd["ipv6"]')" = "$1" ] && { echo "$s"; return; }; sleep 1; done
}

# A server that carries IPv6
read -r name ip <<<"$(servers 's["ipv6"]' | head -1)"
[ -n "$name" ] || die "no IPv6 server in $country"
[ "$(select_server "$name" "$ip")" = "$name" ] || die "cannot select $name"
s=$(wait_ipv6 True 30)
if [ -n "$s" ]; then
  e6=$(echo "$s" | j 'd["exit_ip6"]'); loc=$(echo "$s" | j 'd["exit_loc"]')
  ok "on $name the IPv6 gate opens: the exit's IPv6 address is $e6"
  # The first IPv6 flows to a destination sometimes stall for 4-8 s upstream at Proton (seen
  # from the exit itself too; Happy Eyeballs hides it from apps): allow three tries
  for try in 1 2 3; do t=$(trace6 10); [ -n "$t" ] && break; done; v6=${t%% *}
  [ -n "$t" ] && [ "$try" -gt 1 ] && info "the first IPv6 request stalled; try $try answered"
  if [ -z "$t" ]; then bad "IPv6 from this Mac fails on $name although the gate is open"
  elif mine "$v6"; then bad "IPv6 LEAKED: this Mac's own address $v6"
  elif [ "${t#* }" != "$loc" ]; then bad "IPv6 egress $t is not in the exit's country $loc"
  else ok "this Mac's IPv6 leaves via the VPN: $t (exit reports $e6; never this Mac's own networks)"; fi
  bps=$(curl -6 -s -o /dev/null --max-time 30 -w '%{speed_download}' 'https://speed.cloudflare.com/__down?bytes=25000000')
  info "IPv6 throughput $(python3 -c "print(round(${bps:-0}*8/1e6,1))") Mbit/s"
else bad "on $name (an IPv6 server) the gate stays closed: $(status | j 'd["warnings"]')"; fi

# Tunnel down for 15 s (restored by the VPS itself): no IPv6 egress at all, then back
vps "sudo docker exec -d $NODE sh -c 'ip link set wg0 down; sleep 15; ip link set wg0 up'"
sleep 2; seen=""; end=$((SECONDS+10))
while [ $SECONDS -lt $end ]; do t=$(trace6 2); [ -n "$t" ] && seen="$seen ${t%% *}"; sleep 0.5; done
[ -z "$seen" ] && ok "kill switch: no IPv6 egress while the tunnel is down" || bad "IPv6 while the tunnel was down:$seen"
s=$(wait_ipv6 True 60)
[ -n "$s" ] && [ -n "$(trace6)" ] && ok "IPv6 comes back with the tunnel: $(trace6)" || bad "no IPv6 after the tunnel came back"

# A server without IPv6 (the catalog's flag may be stale: try a few)
found=""
while read -r name ip; do
  [ -n "$name" ] || continue
  [ "$(select_server "$name" "$ip")" = "$name" ] || continue
  sleep 15   # three health cycles: the gate must stay closed
  if [ "$(status | j 'd["ipv6"]')" = False ]; then found=$name; break; fi
  info "$name carries IPv6 although the catalog says not"
done <<<"$(servers 'not s["ipv6"]' | head -3)"
if [ -n "$found" ]; then
  t0=$(python3 -c 'import time; print(time.time())')
  t=$(trace6 8)
  dt=$(python3 -c "import time; print(round(time.time()-$t0, 2))")
  if [ -z "$t" ] && python3 -c "import sys; sys.exit(0 if $dt < 2 else 1)"; then
    ok "on $found (no IPv6) IPv6 fails at once (${dt}s, an error, not a stall)"
  else bad "on $found IPv6: '$t' after ${dt}s (want a failure in under 2 s)"; fi
  t=$(trace)
  case "$t" in ""|"$VPS_EGRESS "*) bad "IPv4 on $found: '$t'";; *) ok "IPv4 works meanwhile on $found: $t";; esac
else warn "no server without IPv6 found in $country (all carry it): the fast failure is untested"; fi

summary
