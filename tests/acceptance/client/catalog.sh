#!/usr/bin/env bash
# The server catalog: a live list pushed by exitctl replaces the pinned one, carries load,
# drives rotation, survives a restart; a truncated push is refused; revert goes back to the
# pinned list. Needs `exitctl proton login`. Leaves the node on a freshly pushed catalog.
. "$(dirname "$0")/../lib.sh"
need_login
vps_connect

out=$("$EXITCTL" proton servers --push --json); rc=$?
cat=$(status | j 'd["catalog"]'); n=$(status | j 'd["catalog"]["servers"]'); pushed=$(status | j 'd["catalog"]["pushed"]')
[ $rc -eq 0 ] && [ "$pushed" = True ] && [ "$n" -ge 1000 ] && ok "a pushed list replaces the pinned one: $n servers, $(echo "$cat" | tr -d "'" | head -c 120)" \
  || bad "push rc=$rc: $(echo "$out" | head -c 300)"
loads=$(api "$API/v1/servers?country=United%20States" | j 'sum(1 for s in d["servers"] if s.get("load") is not None)')
[ "$loads" -gt 0 ] && ok "the pushed list carries load ($loads US servers)" || bad "no loads in /v1/servers"

r=$(post POST /v1/rotate '{"scope":"country"}')
to=$(echo "$r" | j 'd.get("server")'); ep=$(echo "$r" | j 'd.get("endpoint","").rsplit(":",1)[0]')
inlive=$(api "$API/v1/servers?country=United%20States" | j "any(s['ip']=='$ep' and s['enabled'] for s in d['servers'])")
[ "$inlive" = True ] && ok "rotation picks an enabled server of the pushed list ($to)" || bad "rotate: $(echo "$r" | head -c 200)"

small=$("$PY" -I -c '
import asyncio, json
from exitctl.providers.proton import ProtonProvider
rows = asyncio.run(ProtonProvider().servers())[:100]
print(json.dumps({"provider": "proton", "servers": rows}))')
code=$(echo "$small" | curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X PUT -H 'content-type: application/json' --data-binary @- "$API/v1/catalog")
n2=$(status | j 'd["catalog"]["servers"]')
[ "$code" = 400 ] && [ "$n2" = "$n" ] && ok "a truncated push (100 rows) is refused and changes nothing" || bad "truncated push: HTTP $code, servers $n2"

vps "sudo docker restart $NODE >/dev/null"
for _ in $(seq 60); do st=$(status 2>/dev/null); [ "$(echo "$st" | j 'd["health"]' 2>/dev/null)" = healthy ] && break; sleep 2; done
[ "$(echo "$st" | j 'd["catalog"]["pushed"]' 2>/dev/null)" = True ] && ok "the pushed list survives a restart" || bad "after a restart: not the pushed list"

rev=$("$EXITCTL" proton servers --revert --json | j 'd["catalog"]["pushed"]'); src=$(status | j 'd["catalog"]["source"]')
[ "$rev" = False ] && ok "revert goes back to the pinned list ($(echo "$src" | cut -c1-60)…)" || bad "revert: pushed=$rev"
"$EXITCTL" proton servers --push --json >/dev/null && info "live list pushed again"

summary
