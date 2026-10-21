#!/usr/bin/env bash
# Phase 5a acceptance (PLAN T23-T26), run on the Mac in the tailnet (any VPN app off):
# exact select, equivalent-server failover, Secure Core on/off, pinned regions.
# Fault injection (T24) runs on the VPS and is removed by a trap; the original prefs
# (regions, Secure Core, requirements) are put back at the end.
set -uo pipefail
NODE=${EXIT_NODE_NAME:-exit-proton}
. "$(dirname "$0")/exitnode.sh"   # EXIT0: the exit node to restore; refuses a Mac with none
VPS=${VPS_SSH:-fin-intel}; VPS_TS_NAME=${VPS_TS_NAME:-fin-intel}
API="http://$NODE:${API_PORT:-8080}"
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
j()   { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
api() { curl -s --max-time "${T:-90}" "$@"; }
post() { api -X "$1" -H 'content-type: application/json' -d "$3" "$API$2"; }
trace() { curl -s --max-time "${1:-3}" https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p'; }

vps_ip=$(tailscale ip -4 "$VPS_TS_NAME")
vps_alias_host=$(ssh -G "$VPS" | awk '$1=="hostname"{print $2}')
vps() { ssh -n -o HostName="$vps_ip" -o HostKeyAlias="$vps_alias_host" -o ConnectTimeout=10 "$VPS" "$@"; }
prefs0=$(api "$API/v1/prefs" | j 'json.dumps({"regions": d["regions"], "require": d["require"], "secure_core": d["secure_core"]})')
cleanup() {
  vps "sudo docker exec $NODE sh -c 'for h in \$(nft -a list chain inet killswitch output | sed -n \"s/.*comment \\\"t24\\\".*handle \\([0-9]*\\)/\\1/p\"); do nft delete rule inet killswitch output handle \$h; done'" 2>/dev/null
  post PUT /v1/prefs "$prefs0" >/dev/null
  tailscale set --exit-node="$EXIT0" >/dev/null 2>&1; echo "(fault rule removed, prefs restored, exit node restored)"
}
trap cleanup EXIT
st() { api "$API/v1/status"; }

# T23: exact select of one physical server (name + IP) with P2P, in the current country
country=$(st | j 'd["country"]')
pick=$(api -G "$API/v1/servers" --data-urlencode "country=$country" --data-urlencode feature=port_forward \
  | j '" ".join(next((s["name"], s["ip"], s["city"]) for s in sorted(d["servers"], key=lambda s: 50 if s["load"] is None else s["load"]) if s["enabled"] and not s["secure_core"]))')
read -r name ip city <<<"$pick"
r=$(post POST /v1/select "{\"server\":\"$name\",\"ip\":\"$ip\"}")
got=$(echo "$r" | j 'd["server"], d["endpoint"].rsplit(":",1)[0], "port_forward" in d["profile"]')
[ "$got" = "$name $ip True" ] && ok "T23 exact select: $name at $ip ($city), profile $(echo "$r" | j 'd["profile"]')" \
  || bad "T23 exact select $name $ip: $(echo "$r" | head -c 300)"

# T24: silent drop of that server → failover to another P2P server, same city if one is up
tailscale set --exit-node="$NODE" --exit-node-allow-lan-access=true
for _ in $(seq 20); do [ -n "$(trace)" ] && break; sleep 1; done
vps "sudo docker exec $NODE nft insert rule inet killswitch output ip daddr $ip udp dport 51820 drop comment t24"
t0=$(date +%s); to="$name"
while [ $(( $(date +%s) - t0 )) -lt 120 ]; do
  s=$(st); to=$(echo "$s" | j 'd["server"]'); h=$(echo "$s" | j 'd["health"]')
  [ "$to" != "$name" ] && [ "$h" = healthy ] && break; sleep 2
done
vps "sudo docker exec $NODE sh -c 'for h in \$(nft -a list chain inet killswitch output | sed -n \"s/.*comment \\\"t24\\\".*handle \\([0-9]*\\)/\\1/p\"); do nft delete rule inet killswitch output handle \$h; done'"
s=$(st); feats=$(echo "$s" | j '"port_forward" in d["features"] and "port_forward" in d["profile"]'); ncity=$(echo "$s" | j 'd["city"]')
if [ "$to" != "$name" ] && [ "$feats" = True ]; then
  ok "T24 failover kept P2P: $name ($city) → $to ($ncity) in $(( $(date +%s) - t0 ))s"
  [ "$ncity" = "$city" ] || echo "WARN T24 left the city ($city → $ncity)"
else bad "T24 failover: now $to features/profile P2P=$feats"; fi

# T25: Secure Core on → only Secure Core, also after a rotation; off → regular again
post PUT /v1/prefs '{"regions":[{"country":"Switzerland"},{"country":"United States"}]}' >/dev/null
r=$(post PUT /v1/prefs '{"secure_core":true}')
s=$(st); sc=$(echo "$s" | j 'd["entry"]'); srv=$(echo "$s" | j 'd["server"]')
r2=$(post POST /v1/rotate '{"scope":"country"}'); sc2=$(echo "$r2" | j 'd["entry"]')
ep1=$(echo "$s" | j 'd["endpoint"]'); ep2=$(echo "$r2" | j 'd["endpoint"]')
ip_sc=$(trace 5)
if [ -n "$sc" ] && [ -n "$sc2" ] && [ "$ep1" != "$ep2" ]; then
  ok "T25a Secure Core on: $srv (entry $sc), rotate → $(echo "$r2" | j 'd["server"]') (entry $sc2, $ep1 → $ep2), exit $ip_sc"
else bad "T25a Secure Core: entry=$sc after-rotate=$sc2: $(echo "$r" | head -c 200) $(echo "$r2" | head -c 200)"; fi
r=$(post PUT /v1/prefs '{"secure_core":false}'); s=$(st)
[ "$(echo "$r" | j 'd["secure_core"]')" = False ] && [ -z "$(echo "$s" | j 'd["entry"]')" ] \
  && ok "T25b Secure Core off: back on regular $(echo "$s" | j 'd["server"]')" || bad "T25b: $(echo "$r" | head -c 300)"

# T26: pinned regions drive "Rotate (any)": first region Switzerland/Zurich, then the US
r=$(post PUT /v1/prefs '{"regions":[{"country":"Switzerland","city":"Zurich"},{"country":"United States","city":"New York"}]}')
post POST /v1/select '{"country":"United States","city":"New York"}' >/dev/null
r=$(post POST /v1/rotate '{"scope":"any"}')
where=$(echo "$r" | j 'd["country"], d["city"]')
[ "$where" = "Switzerland Zurich" ] && ok "T26 rotate any follows the pinned order: $(echo "$r" | j 'd["server"]') $where" \
  || bad "T26 rotate any landed in $where: $(echo "$r" | head -c 300)"
[ -n "$(trace 5)" ] && ok "T26 traffic flows after the region changes" || bad "T26 no traffic"

echo "== $pass passed, $fail failed"; [ "$fail" -eq 0 ]
