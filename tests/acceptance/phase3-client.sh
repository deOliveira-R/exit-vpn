#!/usr/bin/env bash
# Phase 3 acceptance (PLAN T9-T11), run on a Mac in the tailnet (any VPN app off).
# Selects the exit node for T9, restores "no exit node" on exit. Fault injection runs
# on the VPS over the tailnet and is removed by a trap even if the script fails.
set -uo pipefail
NODE=${EXIT_NODE_NAME:-exit-proton}
# the exit node this Mac used before the test, restored on exit ("" = none)
EXIT0=$(tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); print(next((p["TailscaleIPs"][0] for p in (s.get("Peer") or {}).values() if p.get("ExitNode")), ""))')
VPS=${VPS_SSH:-fin-intel}; VPS_TS_NAME=${VPS_TS_NAME:-fin-intel}
API="http://$NODE:${API_PORT:-8080}"
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
j()   { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
api() { curl -s --max-time "${T:-40}" "$@"; }
trace() { curl -s --max-time "${1:-3}" https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p'; }

vps_ip=$(tailscale ip -4 "$VPS_TS_NAME")
vps_alias_host=$(ssh -G "$VPS" | awk '$1=="hostname"{print $2}')
vps() { ssh -n -o HostName="$vps_ip" -o HostKeyAlias="$vps_alias_host" -o ConnectTimeout=10 "$VPS" "$@"; }
cleanup() {
  vps "sudo docker exec $NODE sh -c 'for h in \$(nft -a list chain inet killswitch output | sed -n \"s/.*comment \\\"t9\\\".*handle \\([0-9]*\\)/\\1/p\"); do nft delete rule inet killswitch output handle \$h; done'" 2>/dev/null
  tailscale set --exit-node="$EXIT0" >/dev/null 2>&1; echo "(fault rule removed, exit node restored)"
}
trap cleanup EXIT

# T11a: owner's Mac → 200
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API/v1/status")
[ "$code" = 200 ] && ok "T11a API from the owner's Mac: 200" || bad "T11a API from Mac: $code"
st=$(api "$API/v1/status")
echo "INFO status: $(echo "$st" | j '{k: d[k] for k in ("server","country","city","health","exit_ip")}')"

# T11b: the server (tag:server) is blocked by the ACL
node_ip=$(tailscale ip -4 "$NODE")
code=$(vps "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://$node_ip:8080/v1/status")
[ "$code" = 000 ] && ok "T11b API from tag:server host: blocked by ACL (no connection)" || bad "T11b from tag:server: HTTP $code"

# T10: rotate within the country
before=$(echo "$st" | j 'd["server"]'); before_ip=$(echo "$st" | j 'd["exit_ip"]')
t0=$(date +%s)
r=$(T=90 api -X POST -H 'content-type: application/json' -d '{"scope":"country"}' "$API/v1/rotate")
dt=$(( $(date +%s) - t0 ))
after=$(echo "$r" | j 'd.get("server")'); after_ip=$(echo "$r" | j 'd.get("exit_ip")'); c=$(echo "$r" | j 'd.get("country")')
if [ "$after" != "$before" ] && [ "$after_ip" != "$before_ip" ] && [ "$after_ip" != None ]; then
  ok "T10 rotate(country): $before → $after ($c), exit IP $before_ip → $after_ip in ${dt}s"
  [ "$dt" -le 10 ] || echo "WARN T10 took ${dt}s (target ≤ 10 s)"
else bad "T10 rotate: $(echo "$r" | head -c 300)"; fi

# T9: silent drop of the current endpoint; controller must fail over
tailscale set --exit-node="$NODE" --exit-node-allow-lan-access=true
for _ in $(seq 20); do [ -n "$(trace)" ] && break; sleep 1; done
oracle=$(vps 'curl -s --max-time 5 https://www.cloudflare.com/cdn-cgi/trace | sed -n s/^ip=//p')
st=$(api "$API/v1/status"); ep=$(echo "$st" | j 'd["endpoint"].rsplit(":",1)[0]'); from=$(echo "$st" | j 'd["server"]')
ip_before=$(trace)
log=$(mktemp)
vps "sudo docker exec $NODE nft insert rule inet killswitch output ip daddr $ep udp dport 51820 drop comment t9"
t0=$(date +%s)
recovered=""
while [ $(( $(date +%s) - t0 )) -lt 90 ]; do
  ip=$(trace 2); echo "$(( $(date +%s) - t0 )) ${ip:-fail}" >> "$log"
  if [ -n "$ip" ] && [ "$ip" != "$ip_before" ]; then recovered=$(( $(date +%s) - t0 )); break; fi
  sleep 0.5
done
# The controller commits the new server after one fully passing cycle: wait for it
for _ in $(seq 30); do
  st=$(api "$API/v1/status"); to=$(echo "$st" | j 'd["server"]')
  [ "$to" != "$from" ] && break; sleep 1
done
detect=$(echo "$st" | j 'next((round(e["ts"]-'"$t0"',1) for e in d["events"] if e["kind"]=="failover_start" and e["ts"]>='"$t0"'), None)')
done_s=$(echo "$st" | j 'next((round(e["ts"]-'"$t0"',1) for e in d["events"] if e["kind"]=="failover_done" and e["ts"]>='"$t0"'), None)')
echo "INFO T9 failover_start at +${detect}s, failover_done at +${done_s}s (controller clock)"
if grep -q " $oracle$" "$log"; then bad "T9 LEAK: Oracle IP seen during failover"
elif [ -n "$recovered" ] && [ "$to" != "$from" ]; then
  ok "T9 silent drop: $from → $to, traffic back via a new Proton IP after ${recovered}s (target ≤ 35 s)"
  [ "$recovered" -le 35 ] || echo "WARN T9 recovery ${recovered}s exceeds 35 s"
else bad "T9 no recovery within 90 s (server now $to)"; fi
echo "INFO T9 events: $(echo "$st" | j '[e["kind"] for e in d["events"][-6:]]')"
rm -f "$log"

echo "== $pass passed, $fail failed"; [ "$fail" -eq 0 ]
