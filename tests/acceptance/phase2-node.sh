#!/usr/bin/env bash
# Phase 2, node side: run on the VPS from the deploy dir, as a sudoer.
set -uo pipefail
C=${EXIT_NODE_NAME:-exit-proton}
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
x()   { docker exec "$C" "$@"; }
ts()  { x tailscale --socket=/run/tailscale/tailscaled.sock "$@"; }

st=$(ts status --json --self 2>/dev/null)
echo "$st" | grep -q '"BackendState": *"Running"' && ok "N1 node is logged in and running" || bad "N1 backend state"
echo "$st" | tr -d '\n ' | grep -q '"HostName":"'"$C"'"' && ok "N2 hostname $C" || bad "N2 hostname"
echo "$st" | tr -d '\n ' | grep -q '"Tags":\["tag:exit"\]' && ok "N3 tagged tag:exit" || bad "N3 tags"
p=$(ts debug prefs 2>/dev/null | tr -d '\n ')
case "$p" in *'"AdvertiseRoutes":["0.0.0.0/0","::/0"]'*) ok "N4 advertising exit node";; *) bad "N4 advertise routes";; esac
case "$p" in *'"CorpDNS":false'*) ok "N5 accept-dns off (container resolv.conf untouched)";; *) bad "N5 CorpDNS";; esac

rules=$(x ip rule | awk -F: '{print $1+0}' | tr '\n' ' ')
first_ts=$(x ip rule | awk -F: '/lookup 52/ && $1+0>=5000 {print $1+0; exit}')
[ -n "$first_ts" ] && [ "$first_ts" -gt 120 ] && ok "N6 our rules (90-120) precede tailscaled's ($first_ts..): $rules" || bad "N6 rule order: $rules"
nft=$(x nft list tables)
echo "$nft" | grep -q 'inet killswitch' && echo "$nft" | grep -qiE 'ts-|tailscale' \
  && ok "N7 killswitch table coexists with tailscaled's nftables: $(echo $nft | tr '\n' ' ')" \
  || bad "N7 nft tables: $(echo $nft)"
x ip -4 route get 1.1.1.1 iif tailscale0 from 100.85.0.1 2>&1 | grep -q 'dev wg0' \
  && ok "N8 forwarded exit traffic routes to wg0" || bad "N8 route for forwarded traffic"
x sh -c 'ip -4 route get 1.1.1.1 | grep -q "dev eth0"' && ok "N9 node's own traffic (underlay) uses eth0" || bad "N9 underlay route"

h=$(tailscale status --self --json 2>/dev/null | tr -d '\n ' | grep -o '"HostName":"[^"]*"' | head -1)
[ "$h" = '"HostName":"fin-intel"' ] && ok "N10 host node still fin-intel" || bad "N10 host node: $h"
ss -ulpn 2>/dev/null | grep -q ':41641 ' && ok "N11 host tailscaled still on 41641" || bad "N11 host port"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health)
[ "$code" = 200 ] && ok "N12 fin-intel API healthy" || bad "N12 fin-intel API $code"
echo "== $pass passed, $fail failed"; [ "$fail" -eq 0 ]
