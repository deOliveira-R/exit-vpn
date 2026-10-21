#!/usr/bin/env bash
# Choosing the server: rotate within the country, select one exact physical server, Secure
# Core on and off, and "rotate any" following the pinned regions in order. The preferences
# are put back at the end.
. "$(dirname "$0")/../lib.sh"
prefs0=$(api "$API/v1/prefs" | j 'json.dumps({"regions": d["regions"], "require": d["require"], "secure_core": d["secure_core"]})')
on_exit 'post PUT /v1/prefs "$prefs0" >/dev/null; echo "(preferences restored)"'
use_exit_node

# Rotate within the country: another server, another exit IP
st=$(status); before=$(echo "$st" | j 'd["server"]'); before_ip=$(echo "$st" | j 'd["exit_ip"]')
t0=$(date +%s); r=$(post POST /v1/rotate '{"scope":"country"}'); dt=$(( $(date +%s) - t0 ))
after=$(echo "$r" | j 'd.get("server")'); after_ip=$(echo "$r" | j 'd.get("exit_ip")')
if [ "$after" != "$before" ] && [ "$after_ip" != "$before_ip" ] && [ "$after_ip" != None ]; then
  ok "rotate within the country: $before → $after, exit IP $before_ip → $after_ip in ${dt}s"
  [ "$dt" -le 10 ] || warn "rotation took ${dt}s (target 10 s)"
else bad "rotate within the country: $(echo "$r" | head -c 300)"; fi

# One exact physical server (name and IP) with P2P, in the current country
country=$(status | j 'd["country"]')
read -r name ip city <<<"$(api -G "$API/v1/servers" --data-urlencode "country=$country" --data-urlencode feature=port_forward \
  | j '" ".join(next((s["name"], s["ip"], s["city"]) for s in sorted(d["servers"], key=lambda s: 50 if s["load"] is None else s["load"]) if s["enabled"] and not s["secure_core"]))')"
r=$(post POST /v1/select "{\"server\":\"$name\",\"ip\":\"$ip\"}")
got=$(echo "$r" | j 'd["server"], d["endpoint"].rsplit(":",1)[0], "port_forward" in d["profile"]')
[ "$got" = "$name $ip True" ] && ok "an exact server is selected and its P2P kept: $name at $ip ($city)" \
  || bad "exact select $name $ip: $(echo "$r" | head -c 300)"

# Secure Core on: only Secure Core routes, also after a rotation; off: regular again
post PUT /v1/prefs '{"regions":[{"country":"Switzerland"},{"country":"United States"}]}' >/dev/null
r=$(post PUT /v1/prefs '{"secure_core":true}')
s=$(status); entry=$(echo "$s" | j 'd["entry"]'); srv=$(echo "$s" | j 'd["server"]')
r2=$(post POST /v1/rotate '{"scope":"country"}'); entry2=$(echo "$r2" | j 'd["entry"]')
ep1=$(echo "$s" | j 'd["endpoint"]'); ep2=$(echo "$r2" | j 'd["endpoint"]')
if [ -n "$entry" ] && [ -n "$entry2" ] && [ "$ep1" != "$ep2" ]; then
  ok "Secure Core on: $srv (entry $entry), rotate → $(echo "$r2" | j 'd["server"]') (entry $entry2), exit $(egress_ip)"
else bad "Secure Core on: entry '$entry', after rotate '$entry2': $(echo "$r" | head -c 200)"; fi
r=$(post PUT /v1/prefs '{"secure_core":false}'); s=$(status)
[ "$(echo "$r" | j 'd["secure_core"]')" = False ] && [ -z "$(echo "$s" | j 'd["entry"]')" ] \
  && ok "Secure Core off: back on a regular server, $(echo "$s" | j 'd["server"]')" || bad "Secure Core off: $(echo "$r" | head -c 300)"

# Pinned regions drive "rotate any": the first region (Zurich) before the second (New York)
post PUT /v1/prefs '{"regions":[{"country":"Switzerland","city":"Zurich"},{"country":"United States","city":"New York"}]}' >/dev/null
post POST /v1/select '{"country":"United States","city":"New York"}' >/dev/null
r=$(post POST /v1/rotate '{"scope":"any"}'); where=$(echo "$r" | j 'd["country"], d["city"]')
[ "$where" = "Switzerland Zurich" ] && ok "rotate any follows the pinned order: $(echo "$r" | j 'd["server"]') in $where" \
  || bad "rotate any landed in $where: $(echo "$r" | head -c 300)"
wait_egress 10 && ok "traffic flows after the region changes" || bad "no traffic after the region changes"

summary
