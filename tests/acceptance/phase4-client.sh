#!/usr/bin/env bash
# Phase 4 acceptance (PLAN T12, T12b, apply), run on a Mac in the tailnet (VPN apps off).
# FRIENDLY_URL should load normally through Proton; CHALLENGE_URL should be challenged
# (defaults: example.com, and Indeed, which served Cloudflare challenges to Proton IPs
# on 2026-10-20; override if that changes).
set -uo pipefail
NODE=${EXIT_NODE_NAME:-exit-proton}; A="http://$NODE:${API_PORT:-8080}"
VPS=${VPS_SSH:-fin-intel}; VPS_TS_NAME=${VPS_TS_NAME:-fin-intel}
FRIENDLY_URL=${FRIENDLY_URL:-https://example.com/}
CHALLENGE_URL=${CHALLENGE_URL:-https://www.indeed.com/}
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
j()   { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
vps_ip=$(tailscale ip -4 "$VPS_TS_NAME"); vps_alias_host=$(ssh -G "$VPS" | awk '$1=="hostname"{print $2}')
vps() { ssh -n -o HostName="$vps_ip" -o HostKeyAlias="$vps_alias_host" -o ConnectTimeout=10 "$VPS" "$@" 2>/dev/null; }
trap 'tailscale set --exit-node= >/dev/null 2>&1; vps "sudo docker exec $NODE-prober ip link set wg-probe up"; echo "(exit node cleared, wg-probe up)"' EXIT

find_job() { # json -> waits, prints the job JSON
  local id; id=$(curl -s -X POST -H 'content-type: application/json' -d "$1" "$A/v1/find" | j 'd["job_id"]')
  for _ in $(seq 120); do
    v=$(curl -s "$A/v1/find/$id"); [ "$(echo "$v" | j 'd["state"]')" != running ] && break; sleep 2
  done
  echo "$v"
}
oracle=$(vps 'curl -s --max-time 5 https://www.cloudflare.com/cdn-cgi/trace | sed -n s/^ip=//p')

# T12: active exit untouched while probing (Mac uses the exit node, curl loop)
tailscale set --exit-node="$NODE" --exit-node-allow-lan-access=true
for _ in $(seq 20); do t=$(curl -s --max-time 3 https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p'); [ -n "$t" ] && break; sleep 1; done
active_ip=$t; active=$(curl -s "$A/v1/status" | j 'd["server"]')
log=$(mktemp)
( while :; do curl -s --max-time 3 https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p' >> "$log"; sleep 1; done ) & loop=$!

v=$(find_job "{\"url\":\"$FRIENDLY_URL\",\"max_candidates\":2}")
o=$(echo "$v" | j 'd["results"][0]["outcome"] if d["results"] else None')
[ "$o" = ok ] && ok "T12a friendly URL: first candidate ok ($(echo "$v" | j 'd["results"][0]["server"]'))" || bad "T12a friendly: $o"
fid=$(echo "$v" | j 'd["job_id"]'); fsrv=$(echo "$v" | j 'd["results"][0]["server"]'); fip=$(echo "$v" | j 'd["results"][0]["ip"]')

v=$(find_job "{\"url\":\"$CHALLENGE_URL\",\"max_candidates\":2}")
outs=$(echo "$v" | j '",".join(r["outcome"] for r in d["results"])')
case "$outs" in *challenge*|*blocked*) ok "T12b challenged URL: $outs ($(echo "$v" | j 'd["results"][0]["signals"][:2]'))";; *) bad "T12b challenged URL: $outs";; esac

kill $loop 2>/dev/null; wait $loop 2>/dev/null
n=$(grep -c . "$log"); other=$(grep -cv "^$active_ip$" "$log")
[ "$other" -eq 0 ] && ok "T12c active exit unchanged during probes ($n samples, all $active_ip, server $active)" || bad "T12c active exit changed: $(sort -u "$log" | tr '\n' ' ')"
grep -q "^$oracle$" "$log" && bad "T12c LEAK of the VPS IP"
rm -f "$log"

# T12b': kill wg-probe mid-probe: error, never a result fetched via the VPS IP
# A response delayed 10 s, so the cut lands while the browser's request is in flight
# (tunnel verify takes ~5 s, then the browser waits for the response)
id=$(curl -s -X POST -H 'content-type: application/json' -d "{\"url\":\"${DELAY_URL:-https://httpbin.org/delay/10}\",\"max_candidates\":1,\"mode\":\"browser\"}" "$A/v1/find" | j 'd["job_id"]')
sleep 10; vps "sudo docker exec $NODE-prober ip link set wg-probe down"
for _ in $(seq 60); do v=$(curl -s "$A/v1/find/$id"); [ "$(echo "$v" | j 'd["state"]')" != running ] && break; sleep 2; done
r=$(echo "$v" | j 'd["results"][0]')
o=$(echo "$v" | j 'd["results"][0]["outcome"]'); eip=$(echo "$v" | j 'd["results"][0].get("exit_ip")')
if [ "$eip" = "$oracle" ]; then bad "T12d LEAK: probe exit IP is the VPS"
elif [ "$o" != ok ]; then ok "T12d wg-probe killed mid-probe: $o ($(echo "$v" | j 'd["results"][0]["signals"][:1]'))"
else bad "T12d probe still 'ok' after wg-probe went down: $r"; fi
vps "sudo docker exec $NODE-prober ip link set wg-probe up"

# Apply: switch the active exit to the server found in T12a
r=$(curl -s -X POST -H 'content-type: application/json' -d "{\"server\":\"$fsrv\",\"ip\":\"$fip\"}" "$A/v1/find/$fid/apply")
now=$(echo "$r" | j 'd.get("server")')
[ "$now" = "$fsrv" ] && ok "T12e apply: active exit is now $now ($(echo "$r" | j 'd["exit_ip"]'))" || bad "T12e apply: $r"

echo "== $pass passed, $fail failed"; [ "$fail" -eq 0 ]
