#!/usr/bin/env bash
# The exit node on the tailnet: logged in, named and tagged, advertising an approved exit
# node, not taking over the container's DNS; our routing rules and kill switch coexist with
# tailscaled's; forwarded traffic goes to wg0, the node's own via the underlay; the host's own
# Tailscale node and services are untouched. On the VPS, from the deploy dir, as a sudoer:
#   sudo bash tests/acceptance/node/tailnet.sh
. "$(dirname "$0")/../lib.sh"
HOST_SERVICE_URL=${HOST_SERVICE_URL:-http://127.0.0.1:8000/health}   # a service of the host's own
HOST_TS_NAME=${HOST_TS_NAME:-fin-intel}                               # the host's own tailnet node
x()  { docker exec "$NODE" "$@"; }
ts() { x tailscale --socket=/run/tailscale/tailscaled.sock "$@"; }

st=$(ts status --json --self 2>/dev/null | tr -d '\n ')
case "$st" in *'"BackendState":"Running"'*) ok "the node is logged in and running";; *) bad "backend state";; esac
case "$st" in *'"HostName":"'"$NODE"'"'*) ok "the node is named $NODE";; *) bad "node name";; esac
case "$st" in *'"Tags":["tag:exit"]'*) ok "the node is tagged tag:exit";; *) bad "tags";; esac
p=$(ts debug prefs 2>/dev/null | tr -d '[:space:]')
case "$p" in *'"AdvertiseRoutes":["0.0.0.0/0","::/0"]'*)
  case "$st" in *'"ExitNodeOption":true'*) ok "it advertises an exit node, approved by the tailnet policy";;
               *) bad "the exit node is advertised but not approved";; esac;;
  *) bad "no exit routes advertised";; esac
case "$p" in *'"CorpDNS":false'*) ok "tailscaled leaves the container's DNS alone (accept-dns off)";; *) bad "CorpDNS is on";; esac

rules=$(x ip rule | awk -F: '{print $1+0}' | tr '\n' ' ')
first_ts=$(x ip rule | awk -F: '/lookup 52/ && $1+0>=5000 {print $1+0; exit}')
[ -n "$first_ts" ] && [ "$first_ts" -gt 120 ] && ok "our routing rules (90-120) come before tailscaled's ($first_ts…)" || bad "rule order: $rules"
# tailscaled keeps its ts-* chains in tables ip filter/nat; its forward hook runs at priority 0,
# after our kill switch (-10), whose drop is final.
if x nft list table inet killswitch >/dev/null 2>&1 && x nft list chain ip filter ts-forward >/dev/null 2>&1; then
  ok "the kill switch (forward, priority -10) coexists with tailscaled's ts-forward (0)"
else bad "nft: the kill switch or ts-forward is missing"; fi
x ip -4 route get 1.1.1.1 iif tailscale0 from 100.85.0.1 2>&1 | grep -q 'dev wg0' \
  && ok "forwarded exit traffic routes to wg0" || bad "forwarded traffic does not route to wg0"
x sh -c 'ip -4 route get 1.1.1.1 | grep -q "dev eth0"' && ok "the node's own traffic (underlay) uses eth0" || bad "underlay route"

h=$(tailscale status --self --json 2>/dev/null | tr -d '\n ' | grep -o '"HostName":"[^"]*"' | head -1)
[ "$h" = "\"HostName\":\"$HOST_TS_NAME\"" ] && ok "the host's own Tailscale node is still $HOST_TS_NAME" || bad "host node: $h"
ss -ulpn 2>/dev/null | grep -q ':41641 ' && ok "the host's tailscaled still listens on 41641" || bad "host tailscaled port"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HOST_SERVICE_URL")
[ "$code" = 200 ] && ok "the host's own service is healthy ($HOST_SERVICE_URL)" || bad "host service $HOST_SERVICE_URL: HTTP $code"

summary
