#!/usr/bin/env bash
# Who may use the controller API: the owner's untagged devices, not tagged hosts (the tailnet
# policy). From the owner's Mac; the VPS stands in for a tagged host. Changes nothing.
. "$(dirname "$0")/../lib.sh"
vps_connect

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API/v1/status")
[ "$code" = 200 ] && ok "the owner's Mac reaches the API: 200" || bad "the owner's Mac gets HTTP $code"
info "status: $(status | j '{k: d[k] for k in ("server","country","city","health","exit_ip")}')"

node_ip=$(tailscale ip -4 "$NODE")
code=$(vps "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://$node_ip:8080/v1/status")
[ "$code" = 000 ] && ok "a tagged host gets no connection (tailnet policy)" || bad "a tagged host gets HTTP $code"

summary
