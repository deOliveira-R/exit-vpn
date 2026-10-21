#!/usr/bin/env bash
# Phase 5b acceptance (PLAN T31-T32), run on the Mac in the tailnet: requirements and
# favourites, through the controller API (the app's menu and URLs call the same API).
# The original prefs (regions, requirements, favourites) are put back at the end; the
# Mac's exit-node choice is not touched.
set -uo pipefail
NODE=${EXIT_NODE_NAME:-exit-proton}
API="http://$NODE:${API_PORT:-8080}"
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
j()   { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
api() { curl -s --max-time "${T:-120}" "$@"; }
post() { api -X "$1" -H 'content-type: application/json' -d "$3" "$API$2"; }
st() { api "$API/v1/status"; }

prefs0=$(api "$API/v1/prefs" | j 'json.dumps({"regions": d["regions"], "require": d["require"], "favorites": d.get("favorites", [])})')
trap 'post PUT /v1/prefs "$prefs0" >/dev/null; echo "(prefs restored)"' EXIT
[ "$(st | j 'd["prefs"]["secure_core"]')" = False ] || { echo "turn Secure Core off first"; exit 1; }

# T31: require P2P → every rotation lands on a P2P server, whatever the current one has
post PUT /v1/prefs '{"require":["port_forward"]}' >/dev/null
seen=""; good=1
for scope in city country any; do
  s=$(post POST /v1/rotate "{\"scope\":\"$scope\"}")
  line=$(echo "$s" | j 'd["server"], "port_forward" in d["features"]' 2>/dev/null)
  seen="$seen $scope→${line% *}"
  [ "${line##* }" = True ] || good=0
done
[ $good = 1 ] && ok "T31 require P2P: every rotation on a P2P server:$seen" || bad "T31 require P2P:$seen"
post PUT /v1/prefs '{"require":[]}' >/dev/null

# T32: a favourite in the current city is tried first, even when it is the busiest there
s=$(st); cur=$(echo "$s" | j 'd["server"]'); country=$(echo "$s" | j 'd["country"]'); city=$(echo "$s" | j 'd["city"]')
fav=$(api -G "$API/v1/servers" --data-urlencode "country=$country" \
  | j "next(s['name'] for s in sorted(d['servers'], key=lambda s: -(s['load'] or 0)) if s['city'] == '''$city''' and s['enabled'] and not (s['secure_core'] or s['free'] or s['tor']) and s['name'] != '''$cur''')")
r=$(post PUT /v1/prefs "{\"favorites\":[\"$fav\"]}"); [ "$(echo "$r" | j 'd["favorites"]')" = "['$fav']" ] || bad "T32 set favourite: $r"
got=$(post POST /v1/rotate '{"scope":"city"}' | j 'd["server"]')
[ "$got" = "$fav" ] && ok "T32 favourite first: $cur → $fav (the busiest in $city)" || bad "T32 favourite $fav, rotate city gave $got"
r=$(post PUT /v1/prefs '{"favorites":["XX#999"]}'); echo "$r" | grep -q "unknown server" \
  && ok "T32 unknown favourite refused" || bad "T32 unknown favourite: $r"

echo "== $pass passed, $fail failed"
[ $fail = 0 ]
