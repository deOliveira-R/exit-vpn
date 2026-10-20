#!/usr/bin/env bash
# Phase 3b.2 acceptance (PLAN T21), run on the Mac in the tailnet after `exitctl proton login`.
# Leaves the exit node on a freshly pushed live catalog.
set -uo pipefail
cd "$(dirname "$0")/../.."
NODE=${EXIT_NODE_NAME:-exit-proton}
VPS=${VPS_SSH:-fin-intel}; VPS_TS_NAME=${VPS_TS_NAME:-fin-intel}
API="http://$NODE:${API_PORT:-8080}"
EXITCTL=${EXITCTL:-helper/.venv/bin/exitctl}
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
j()   { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
api() { curl -s --max-time "${T:-40}" "$@"; }
vps_ip=$(tailscale ip -4 "$VPS_TS_NAME")
vps_alias_host=$(ssh -G "$VPS" | awk '$1=="hostname"{print $2}')
vps() { ssh -n -o HostName="$vps_ip" -o HostKeyAlias="$vps_alias_host" -o ConnectTimeout=10 "$VPS" "$@"; }

# T21a push
out=$($EXITCTL proton servers --push --json); rc=$?
cat=$(api "$API/v1/status" | j 'd["catalog"]')
n=$(api "$API/v1/status" | j 'd["catalog"]["servers"]'); pushed=$(api "$API/v1/status" | j 'd["catalog"]["pushed"]')
[ $rc -eq 0 ] && [ "$pushed" = True ] && [ "$n" -ge 1000 ] && ok "T21a push: $n servers, $(echo "$cat" | tr -d "'" | head -c 160)" \
  || bad "T21a push rc=$rc: $(echo "$out" | head -c 300)"
loads=$(api "$API/v1/servers?country=United%20States" | j 'sum(1 for s in d["servers"] if s.get("load") is not None)')
[ "$loads" -gt 0 ] && ok "T21b /v1/servers carries load ($loads US servers with a load)" || bad "T21b no loads"

# T21c rotate picks an enabled server of the pushed catalog
r=$(T=90 api -X POST -H 'content-type: application/json' -d '{"scope":"country"}' "$API/v1/rotate")
to=$(echo "$r" | j 'd.get("server")'); ep=$(echo "$r" | j 'd.get("endpoint","").rsplit(":",1)[0]')
inlive=$(api "$API/v1/servers?country=United%20States" | j "any(s['ip']=='$ep' and s['enabled'] for s in d['servers'])")
[ "$inlive" = True ] && ok "T21c rotate → $to, an enabled server of the live catalog" || bad "T21c rotate: $(echo "$r" | head -c 200)"

# T21d a truncated push is refused and changes nothing
small=$($EXITCTL proton servers --json >/dev/null; helper/.venv/bin/python -I -c '
import asyncio, json
from exitctl.providers.proton import ProtonProvider
rows = asyncio.run(ProtonProvider().servers())[:100]
print(json.dumps({"provider": "proton", "servers": rows}))')
code=$(echo "$small" | curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X PUT -H 'content-type: application/json' --data-binary @- "$API/v1/catalog")
n2=$(api "$API/v1/status" | j 'd["catalog"]["servers"]')
[ "$code" = 400 ] && [ "$n2" = "$n" ] && ok "T21d truncated push (100 rows) refused: 400, catalog still $n2" || bad "T21d code=$code servers=$n2"

# T21e restart keeps the pushed catalog
vps "sudo docker restart $NODE >/dev/null"
for _ in $(seq 60); do st=$(api "$API/v1/status" 2>/dev/null); [ "$(echo "$st" | j 'd["health"]' 2>/dev/null)" = healthy ] && break; sleep 2; done
p2=$(echo "$st" | j 'd["catalog"]["pushed"]' 2>/dev/null)
[ "$p2" = True ] && ok "T21e after a container restart: still the pushed catalog, health $(echo "$st" | j 'd["health"]')" || bad "T21e pushed=$p2"

# T21f revert to the pinned list, then push again
rev=$($EXITCTL proton servers --revert --json | j 'd["catalog"]["pushed"]')
src=$(api "$API/v1/status" | j 'd["catalog"]["source"]')
[ "$rev" = False ] && ok "T21f revert → pinned ($(echo "$src" | cut -c1-60)…)" || bad "T21f revert: $rev"
$EXITCTL proton servers --push --json >/dev/null && echo "INFO live catalog pushed again"

# T22 the bootstrap secrets mirror the live keys (rotate slot b to exercise the mirror)
$EXITCTL proton rotate --slot b --json >/dev/null || echo "WARN slot b rotate failed"
same=$(vps 'for s in a b; do sudo cmp -s /var/lib/docker/volumes/exit-vpn_keys-$s/_data/wg.conf /opt/exit-node/secrets/proton_$s.conf && echo same; done' | grep -c same)
[ "$same" = 2 ] && ok "T22 secrets/proton_{a,b}.conf match the live keys" || bad "T22 $same of 2 bootstrap files match"

echo "== $pass passed, $fail failed"; [ "$fail" -eq 0 ]
