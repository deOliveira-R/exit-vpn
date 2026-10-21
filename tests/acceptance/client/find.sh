#!/usr/bin/env bash
# Finding a server for a URL: the prober, on its own tunnel, tells a friendly site from a
# challenged one, never disturbs the active exit, fails (not leaks) when its tunnel dies
# mid-probe, and a found server can be applied. FRIENDLY_URL should load normally through the
# VPN, CHALLENGE_URL should be challenged (Indeed served Cloudflare challenges to Proton IPs
# on 2026-10-20; override if that changes).
. "$(dirname "$0")/../lib.sh"
FRIENDLY_URL=${FRIENDLY_URL:-https://example.com/}
CHALLENGE_URL=${CHALLENGE_URL:-https://www.indeed.com/}
DELAY_URL=${DELAY_URL:-https://httpbin.org/delay/10}
vps_connect
on_exit "vps 'sudo docker exec $NODE-prober ip link set wg-probe up' 2>/dev/null; echo '(wg-probe up)'"
use_exit_node

find_job() { # request JSON → waits, prints the job JSON
  local id v; id=$(post POST /v1/find "$1" | j 'd["job_id"]')
  for _ in $(seq 120); do v=$(api "$API/v1/find/$id"); [ "$(echo "$v" | j 'd["state"]')" != running ] && break; sleep 2; done
  echo "$v"
}

# The active exit is sampled while both jobs run
active_ip=$(egress_ip 3); active=$(status | j 'd["server"]')
log=$(mktemp); on_exit "rm -f '$log'"
sample_start "$log"

v=$(find_job "{\"url\":\"$FRIENDLY_URL\",\"max_candidates\":2}")
o=$(echo "$v" | j 'd["results"][0]["outcome"] if d["results"] else None')
[ "$o" = ok ] && ok "a friendly URL loads on the first candidate ($(echo "$v" | j 'd["results"][0]["server"]'))" || bad "friendly URL: $o"
fid=$(echo "$v" | j 'd["job_id"]'); fsrv=$(echo "$v" | j 'd["results"][0]["server"]'); fip=$(echo "$v" | j 'd["results"][0]["ip"]')

v=$(find_job "{\"url\":\"$CHALLENGE_URL\",\"max_candidates\":2}")
outs=$(echo "$v" | j '",".join(r["outcome"] for r in d["results"])')
case "$outs" in *challenge*|*blocked*) ok "a challenged URL is recognised: $outs ($(echo "$v" | j 'd["results"][0]["signals"][:2]'))";;
                *) bad "challenged URL: $outs";; esac

sample_stop
n=$(awk 'NF==2' "$log" | wc -l | tr -d ' '); other=$(awk -v a="$active_ip" 'NF==2 && $2!=a' "$log" | wc -l | tr -d ' ')
info "$(awk 'NF==1' "$log" | wc -l | tr -d ' ') samples timed out (not an exit change)"
if leaked "$log"; then bad "the VPS IP was seen while probing"
elif [ "$other" -eq 0 ]; then ok "the active exit is untouched while probing ($n samples, all $active_ip, server $active)"
else bad "the active exit changed while probing: $(awk 'NF==2 {print $2}' "$log" | sort -u | tr '\n' ' ')"; fi

# The prober's tunnel cut while the browser waits for a slow response: an error, never a
# result fetched via the VPS
id=$(post POST /v1/find "{\"url\":\"$DELAY_URL\",\"max_candidates\":1,\"mode\":\"browser\"}" | j 'd["job_id"]')
sleep 10; vps "sudo docker exec $NODE-prober ip link set wg-probe down"
for _ in $(seq 60); do v=$(api "$API/v1/find/$id"); [ "$(echo "$v" | j 'd["state"]')" != running ] && break; sleep 2; done
o=$(echo "$v" | j 'd["results"][0]["outcome"]'); eip=$(echo "$v" | j 'd["results"][0].get("exit_ip")')
if [ "$eip" = "$VPS_EGRESS" ]; then bad "a probe left via the VPS"
elif [ "$o" != ok ]; then ok "a probe whose tunnel dies is an error: $o ($(echo "$v" | j 'd["results"][0]["signals"][:1]'))"
else bad "a probe still 'ok' after its tunnel went down: $(echo "$v" | j 'd["results"][0]')"; fi
vps "sudo docker exec $NODE-prober ip link set wg-probe up"

r=$(post POST "/v1/find/$fid/apply" "{\"server\":\"$fsrv\",\"ip\":\"$fip\"}")
now=$(echo "$r" | j 'd.get("server")')
[ "$now" = "$fsrv" ] && ok "a found server is applied: the active exit is now $now ($(echo "$r" | j 'd["exit_ip"]'))" || bad "apply: $r"

summary
