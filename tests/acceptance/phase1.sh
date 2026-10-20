#!/usr/bin/env bash
# Phase 1 acceptance (PLAN §6): run on the VPS from the deploy dir, as a sudoer.
#   sudo tests/acceptance/phase1.sh
set -uo pipefail
C=${EXIT_NODE_NAME:-exit-proton}
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
x()   { docker exec "$C" "$@"; }
trace() { x curl -s --max-time 5 "$@" https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|loc)=' | tr '\n' ' '; }

host_ip=$(curl -s --max-time 5 https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p')
[ -n "$host_ip" ] || { echo "cannot determine host egress IP"; exit 1; }

# Wait for the tunnel (Proton warm-up, PLAN Phase 0 results)
for _ in $(seq 30); do [ -n "$(trace --interface wg0)" ] && break; sleep 1; done

t=$(trace --interface wg0)
case "$t" in *"ip=$host_ip"*|"") bad "P1.1 tunnel egress: '${t}'";; *) ok "P1.1 tunnel egress via VPN: $t";; esac

t=$(trace)
case "$t" in *"ip=$host_ip"*) ok "P1.2 underlay (main table) egress is the host IP";; *) bad "P1.2 underlay: '$t'";; esac

dns=$(x sh -c '. /usr/local/lib/exit-vpn/common.sh; load_conf; echo $VPN_DNS')
a=$(x dig +short +time=3 +tries=1 "@$dns" example.com A | head -1)
[ -n "$a" ] && ok "P1.3a dig @$dns answers ($a)" || bad "P1.3a dig @$dns"
a=$(x dig +short +time=3 +tries=1 example.com A | head -1)
[ -n "$a" ] && ok "P1.3b default resolver (Docker → VPN DNS) answers" || bad "P1.3b default resolver"
out=$(x ip route get "$dns" | head -1)
case "$out" in *"dev wg0"*) ok "P1.3c route to $dns is via wg0";; *) bad "P1.3c route to $dns: $out";; esac

x ip link set wg0 down
t=$(trace --interface wg0); t2=$(x curl -s --max-time 5 --interface wg0 -o /dev/null -w '%{http_code}' https://1.1.1.1 2>/dev/null)
a=$(x dig +short +time=2 +tries=1 example.com A | head -1)
r=$(x ip route get 1.1.1.1 oif wg0 2>&1 | head -1)
if [ -z "$t" ] && [ "$t2" = "000" ] && [ -z "$a" ]; then ok "P1.4 wg0 down: tunnel traffic and DNS fail closed ($r)"; else bad "P1.4 wg0 down: trace='$t' code=$t2 dns='$a'"; fi
x ip link set wg0 up
for _ in $(seq 30); do [ -n "$(trace --interface wg0)" ] && break; sleep 1; done
t=$(trace --interface wg0)
[ -n "$t" ] && ok "P1.4b wg0 back up: route restored, traffic flows ($t)" || bad "P1.4b recovery"

t=$(curl -s --max-time 5 https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p')
[ "$t" = "$host_ip" ] && ok "P1.5a host egress unchanged" || bad "P1.5a host egress '$t'"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health)
[ "$code" = 200 ] && ok "P1.5b fin-intel API healthy" || bad "P1.5b fin-intel API code $code"

echo "== $pass passed, $fail failed"
[ "$fail" -eq 0 ]
