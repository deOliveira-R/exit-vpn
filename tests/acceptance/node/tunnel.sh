#!/usr/bin/env bash
# The exit container's tunnel: its traffic and DNS leave via wg0, its own underlay via the
# host, everything fails closed while wg0 is down and recovers when it is back, and the host's
# own egress and services are untouched. On the VPS, from the deploy dir, as a sudoer:
#   sudo bash tests/acceptance/node/tunnel.sh
. "$(dirname "$0")/../lib.sh"
HOST_SERVICE_URL=${HOST_SERVICE_URL:-http://127.0.0.1:8000/health}   # a service of the host's own
x() { docker exec "$NODE" "$@"; }
ctrace() { x curl -s --max-time 5 "$@" https://www.cloudflare.com/cdn-cgi/trace | awk -F= '$1=="ip"{ip=$2} $1=="loc"{loc=$2} END{if (ip) print ip, loc}'; }

host_ip=$(egress_ip)
[ -n "$host_ip" ] || die "cannot determine the host's egress IP"
for _ in $(seq 30); do [ -n "$(ctrace --interface wg0)" ] && break; sleep 1; done   # VPN warm-up

t=$(ctrace --interface wg0)
case "$t" in ""|"$host_ip "*) bad "tunnel egress: '$t'";; *) ok "the container's traffic leaves via the VPN: $t";; esac
t=$(ctrace)
case "$t" in "$host_ip "*) ok "the container's own underlay (main table) leaves via the host";; *) bad "underlay: '$t'";; esac

dns=$(x sh -c '. /usr/local/lib/exit-vpn/common.sh; load_conf; echo $VPN_DNS')
a=$(x dig +short +time=3 +tries=1 "@$dns" example.com A | head -1)
[ -n "$a" ] && ok "the VPN's resolver ($dns) answers" || bad "no answer from the VPN's resolver $dns"
a=$(x dig +short +time=3 +tries=1 example.com A | head -1)
[ -n "$a" ] && ok "the default resolver (Docker → VPN DNS) answers" || bad "no answer from the default resolver"
out=$(x ip route get "$dns" | head -1)
case "$out" in *"dev wg0"*) ok "the route to the VPN's resolver is via wg0";; *) bad "route to $dns: $out";; esac

x ip link set wg0 down
on_exit "docker exec '$NODE' ip link set wg0 up 2>/dev/null"
t=$(ctrace --interface wg0); code=$(x curl -s --max-time 5 --interface wg0 -o /dev/null -w '%{http_code}' https://1.1.1.1 2>/dev/null)
a=$(x dig +short +time=2 +tries=1 example.com A | head -1)
if [ -z "$t" ] && [ "$code" = 000 ] && [ -z "$a" ]; then ok "with wg0 down, tunnel traffic and DNS fail closed"
else bad "with wg0 down: trace '$t', HTTP $code, DNS '$a'"; fi
x ip link set wg0 up
for _ in $(seq 30); do [ -n "$(ctrace --interface wg0)" ] && break; sleep 1; done
t=$(ctrace --interface wg0)
[ -n "$t" ] && ok "with wg0 back up, the route is restored and traffic flows ($t)" || bad "no recovery after wg0 came back"

[ "$(egress_ip)" = "$host_ip" ] && ok "the host's own egress is unchanged" || bad "the host's egress changed"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HOST_SERVICE_URL")
[ "$code" = 200 ] && ok "the host's own service is healthy ($HOST_SERVICE_URL)" || bad "host service $HOST_SERVICE_URL: HTTP $code"

summary
