#!/usr/bin/env bash
# Preferences that steer every choice: a required feature holds for every rotation scope, a
# favourite is tried first, an unknown favourite is refused. Through the controller API (the
# app's menu and URLs call the same API). The preferences are put back at the end; the Mac's
# exit node is not touched.
. "$(dirname "$0")/../lib.sh"
prefs0=$(api "$API/v1/prefs" | j 'json.dumps({"regions": d["regions"], "require": d["require"], "favorites": d.get("favorites", [])})')
on_exit 'post PUT /v1/prefs "$prefs0" >/dev/null; echo "(preferences restored)"'
[ "$(status | j 'd["prefs"]["secure_core"]')" = False ] || die "turn Secure Core off first"

# Require P2P: every rotation lands on a P2P server, whatever the current one has
post PUT /v1/prefs '{"require":["port_forward"]}' >/dev/null
seen=""; good=1
for scope in city country any; do
  line=$(post POST /v1/rotate "{\"scope\":\"$scope\"}" | j 'd["server"], "port_forward" in d["features"]' 2>/dev/null)
  seen="$seen $scope→${line% *}"
  [ "${line##* }" = True ] || good=0
done
[ $good = 1 ] && ok "a required feature holds for every rotation:$seen" || bad "required P2P not kept:$seen"
post PUT /v1/prefs '{"require":[]}' >/dev/null

# A favourite in the current city is tried first, even when it is the busiest there
s=$(status); cur=$(echo "$s" | j 'd["server"]'); country=$(echo "$s" | j 'd["country"]'); city=$(echo "$s" | j 'd["city"]')
fav=$(api -G "$API/v1/servers" --data-urlencode "country=$country" \
  | j "next(s['name'] for s in sorted(d['servers'], key=lambda s: -(s['load'] or 0)) if s['city'] == '''$city''' and s['enabled'] and not (s['secure_core'] or s['free'] or s['tor']) and s['name'] != '''$cur''')")
r=$(post PUT /v1/prefs "{\"favorites\":[\"$fav\"]}")
[ "$(echo "$r" | j 'd["favorites"]')" = "['$fav']" ] || bad "setting the favourite: $r"
got=$(post POST /v1/rotate '{"scope":"city"}' | j 'd["server"]')
[ "$got" = "$fav" ] && ok "a favourite is tried first: $cur → $fav (the busiest in $city)" || bad "favourite $fav, rotate city gave $got"

r=$(post PUT /v1/prefs '{"favorites":["XX#999"]}')
echo "$r" | grep -q "unknown server" && ok "an unknown favourite is refused" || bad "unknown favourite: $r"

summary
