#!/usr/bin/env bash
# The standby and the handover: with a standby ready, a silently dropped active server is
# handed over to it (no failover search), traffic flows again via the standby's VPN IP within
# a few seconds of the handover and never via the VPS, a connection open across the handover
# is ended with a reset (not left to stall), and the standby is then rebuilt on another
# server. When both tunnels fail at once (the uplink, simulated by dropping both servers),
# the exit holds still: no handover, no failover, no blacklisting, and it carries on when the
# fault ends. Faults are nft drops on the VPS, removed at exit.
. "$(dirname "$0")/../lib.sh"
vps_connect
on_exit "clear_drops handover; echo '(fault rules removed)'"
use_exit_node
now() { python3 -c 'import time; print(time.time())'; }
standby() { status | j 'd.get("standby") or {}'; }
sb_field() { status | j "(d.get('standby') or {}).get('$1')"; }
wait_ready() { local _; for _ in $(seq "$1"); do [ "$(sb_field state)" = ready ] && return 0; sleep 2; done; return 1; }
events_since() { status | j "[e['kind'] for e in d['events'] if e['ts'] >= $1]"; }

[ "$(status | j '"standby" in d')" = True ] || die "this node has no standby (older than the standby tunnel)"
[ "$(sb_field state)" = off ] && die "the standby has no key: exitctl proton rotate --slot c"
wait_ready 60 || die "the standby is not ready: $(standby)"

# A dead active server: handed over to the standby
st=$(status); from=$(echo "$st" | j 'd["server"]'); ep=$(echo "$st" | j 'd["endpoint"].rsplit(":",1)[0]')
to_want=$(echo "$st" | j 'd["standby"]["server"]'); ip_before=$(egress_ip 3)
info "active $from, standby $to_want"
out=$(mktemp); log=$(mktemp); on_exit "rm -f '$out' '$log'"
# A connection open across the handover: a slow download that would run for minutes
( curl -s -o /dev/null --limit-rate 2k --max-time 120 -w '%{size_download}' \
    'https://speed.cloudflare.com/__down?bytes=25000000' >"$out" 2>/dev/null; echo " $? $(now)" >>"$out" ) &
flow=$!
sleep 5
sample_start "$log"
t0=$(now)
drop_endpoint "$ep" handover
for _ in $(seq 60); do s=$(status); [ "$(echo "$s" | j 'd["server"]')" != "$from" ] && break; sleep 1; done
sleep 4
sample_stop
to=$(echo "$s" | j 'd["server"]')
ho=$(echo "$s" | j "next((e['ts'] for e in d['events'] if e['kind']=='handover' and e['ts']>=$t0), '')")
kinds=$(events_since "$t0")
if [ -n "$ho" ] && [ "$to" = "$to_want" ]; then
  ok "a dead active server is handed over to the standby: $from → $to, $(python3 -c "print(round($ho-$t0,1))") s after the drop (detection)"
  case "$kinds" in *failover_start*) bad "a failover ran as well: $kinds";; esac
else bad "no handover to the standby $to_want (now $to; events $kinds)"; fi
if leaked "$log"; then bad "the VPS IP was seen during the handover"; fi
# Traffic via the new IP: how long after the handover (sampler lines: "SECONDS ip")
first_new=$(awk -v old="$ip_before" '$2 != "" && $2 != old {print $1; exit}' "$log")
[ -n "$first_new" ] && ok "traffic flows via the standby's VPN IP ($(awk -v old="$ip_before" '$2 != "" && $2 != old {print $2; exit}' "$log"))" \
  || bad "no traffic via a new VPN IP after the handover"
wait "$flow"
read -r got rc ended <<<"$(cat "$out")"
if [ -n "$ho" ] && [ "$rc" != 0 ] && [ "$rc" != 28 ] && python3 -c "import sys; sys.exit(0 if $ended - $ho < 8 else 1)"; then
  ok "a connection open across the handover was ended at once (curl exit $rc, $(python3 -c "print(round($ended-$ho,1))") s after it), not left to stall"
else bad "the open connection: curl exit $rc, ended $( [ -n "$ho" ] && python3 -c "print(round($ended-$ho,1))") s after the handover (want a reset within 8 s)"; fi
clear_drops handover
if wait_ready 90; then
  nsb=$(sb_field server)
  [ "$nsb" != "$from" ] && ok "the standby is rebuilt on another server: $nsb" || bad "the standby went back to the dropped server $from"
else bad "no standby ready again within 3 minutes: $(standby)"; fi

# Both tunnels failing at once (the uplink): the exit holds still
st=$(status); from=$(echo "$st" | j 'd["server"]'); ep=$(echo "$st" | j 'd["endpoint"].rsplit(":",1)[0]')
sbs=$(echo "$st" | j 'd["standby"]["server"]')
sbip=$(api -G "$API/v1/servers" --data-urlencode "country=$(echo "$st" | j 'd["country"]')" \
  | j "next((s['ip'] for s in d['servers'] if s['name']=='$sbs'), '')")
[ -n "$sbip" ] || die "cannot find the standby $sbs in the server list"
t0=$(now)
drop_endpoint "$ep" handover; drop_endpoint "$sbip" handover
sleep 40
s=$(status); kinds=$(events_since "$t0")
clear_drops handover
case "$kinds" in
  *handover*|*failover_start*) bad "with both tunnels down the exit switched: $kinds";;
  *uplink_suspect*) ok "with both tunnels down the exit holds on $from (uplink suspected, no switch)";;
  *) bad "with both tunnels down: no uplink_suspect (events $kinds)";;
esac
[ "$(echo "$s" | j 'd["blacklisted"]')" = "$(echo "$st" | j 'd["blacklisted"]')" ] \
  && ok "nothing was blacklisted meanwhile" || warn "servers were blacklisted during the simulated uplink outage"
for _ in $(seq 30); do s=$(status); [ "$(echo "$s" | j 'd["health"]')" = healthy ] && break; sleep 2; done
[ "$(echo "$s" | j 'd["server"]')" = "$from" ] && [ "$(echo "$s" | j 'd["health"]')" = healthy ] \
  && ok "when the fault ends the exit carries on on $from" || bad "after the fault: $(echo "$s" | j 'd["server"], d["health"]')"

summary
