#!/usr/bin/env bash
# Failover: when the active server silently stops carrying traffic, the controller moves to
# another one, the Mac's traffic comes back via a new VPN IP and never leaves via the VPS; an
# exactly chosen P2P server fails over to another P2P server. Faults are nft drops on the
# VPS, removed at exit; the preferences are put back.
. "$(dirname "$0")/../lib.sh"
vps_connect
prefs0=$(api "$API/v1/prefs" | j 'json.dumps({"regions": d["regions"], "require": d["require"], "secure_core": d["secure_core"]})')
on_exit 'post PUT /v1/prefs "$prefs0" >/dev/null; echo "(preferences restored)"' 
on_exit "clear_drops failover; echo '(fault rules removed)'"
use_exit_node

# A silent drop of the current server's endpoint
st=$(status); ep=$(echo "$st" | j 'd["endpoint"].rsplit(":",1)[0]'); from=$(echo "$st" | j 'd["server"]')
ip_before=$(egress_ip 3)
log=$(mktemp); on_exit "rm -f '$log'"
drop_endpoint "$ep" failover
t0=$(date +%s); recovered=""
while [ $(( $(date +%s) - t0 )) -lt 90 ]; do
  ip=$(egress_ip 2); echo "$(( $(date +%s) - t0 )) $ip" >> "$log"
  if [ -n "$ip" ] && [ "$ip" != "$ip_before" ]; then recovered=$(( $(date +%s) - t0 )); break; fi
  sleep 0.5
done
# The controller commits the new server after one fully passing cycle: wait for it
for _ in $(seq 30); do st=$(status); to=$(echo "$st" | j 'd["server"]'); [ "$to" != "$from" ] && break; sleep 1; done
clear_drops failover
detect=$(echo "$st" | j 'next((round(e["ts"]-'"$t0"',1) for e in d["events"] if e["kind"]=="failover_start" and e["ts"]>='"$t0"'), None)')
info "failover_start at +${detect}s (controller clock); events $(echo "$st" | j '[e["kind"] for e in d["events"][-6:]]')"
if leaked "$log"; then bad "the VPS IP was seen during failover"
elif [ -n "$recovered" ] && [ "$to" != "$from" ]; then
  ok "a silently dropped server fails over: $from → $to, traffic back via a new VPN IP after ${recovered}s"
  [ "$recovered" -le 35 ] || warn "recovery took ${recovered}s (target 35 s)"
else bad "no recovery within 90 s (server now $to)"; fi

# An exact P2P server, dropped: the next one keeps P2P (same city if one is up)
country=$(status | j 'd["country"]')
read -r name ip city <<<"$(api -G "$API/v1/servers" --data-urlencode "country=$country" --data-urlencode feature=port_forward \
  | j '" ".join(next((s["name"], s["ip"], s["city"]) for s in sorted(d["servers"], key=lambda s: 50 if s["load"] is None else s["load"]) if s["enabled"] and not s["secure_core"]))')"
post POST /v1/select "{\"server\":\"$name\",\"ip\":\"$ip\"}" >/dev/null
wait_egress 20
drop_endpoint "$ip" failover
t0=$(date +%s); to="$name"
while [ $(( $(date +%s) - t0 )) -lt 120 ]; do
  s=$(status); to=$(echo "$s" | j 'd["server"]'); h=$(echo "$s" | j 'd["health"]')
  [ "$to" != "$name" ] && [ "$h" = healthy ] && break; sleep 2
done
clear_drops failover
s=$(status); p2p=$(echo "$s" | j '"port_forward" in d["features"] and "port_forward" in d["profile"]'); ncity=$(echo "$s" | j 'd["city"]')
if [ "$to" != "$name" ] && [ "$p2p" = True ]; then
  ok "failover keeps P2P: $name ($city) → $to ($ncity) in $(( $(date +%s) - t0 ))s"
  [ "$ncity" = "$city" ] || warn "failover left the city ($city → $ncity)"
else bad "failover from $name: now $to, P2P kept: $p2p"; fi

summary
