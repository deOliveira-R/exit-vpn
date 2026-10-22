#!/usr/bin/env bash
# The exit container's tunnels: traffic and DNS leave via the active tunnel, the standby's own
# traffic via the standby tunnel (another VPN IP), the container's own underlay via the host,
# forwarded traffic follows the active tunnel, IPv6 only via a tunnel and forwarded only while
# the IPv6 gate is open (refused otherwise), everything fails closed while the active tunnel is
# down and recovers when it is back, the prober has no IPv6 route, and the host's own egress
# and services are untouched. On the VPS, from the deploy dir, as a sudoer:
#   sudo bash tests/acceptance/node/tunnel.sh
. "$(dirname "$0")/../lib.sh"
HOST_SERVICE_URL=${HOST_SERVICE_URL:-http://127.0.0.1:8000/health}   # a service of the host's own
x() { docker exec "$NODE" "$@"; }
ctrace() { x curl -s --max-time 5 "$@" https://www.cloudflare.com/cdn-cgi/trace | awk -F= '$1=="ip"{ip=$2} $1=="loc"{loc=$2} END{if (ip) print ip, loc}'; }

host_ip=$(egress_ip)
[ -n "$host_ip" ] || die "cannot determine the host's egress IP"
act=$(x sh -c '. /usr/local/lib/exit-vpn/common.sh; active_if')
sb=$([ "$act" = wg0 ] && echo wg1 || echo wg0)
for _ in $(seq 30); do [ -n "$(ctrace -4 --interface "$act")" ] && break; sleep 1; done   # VPN warm-up

t=$(ctrace -4 --interface "$act")
case "$t" in ""|"$host_ip "*) bad "tunnel egress: '$t'";; *) ok "the active tunnel ($act) leaves via the VPN: $t";; esac
out=$(x ip route show table 100 | grep '^default dev' | head -1)
case "$out" in "default dev $act "*) ok "forwarded traffic follows the active tunnel ($out)";; *) bad "table 100: '$out' (active $act)";; esac
if [ -n "$(x wg show "$sb" peers 2>/dev/null)" ]; then
  t2=$(ctrace -4 --interface "$sb")
  case "$t2" in ""|"$host_ip "*) bad "standby egress: '$t2'";;
    "${t%% *} "*) warn "the standby ($sb) leaves via the same VPN IP as the active: $t2";;
    *) ok "the standby tunnel ($sb) leaves via the VPN on its own server: $t2";; esac
else info "no standby key: $sb has no peer"; fi
t=$(ctrace)
case "$t" in "$host_ip "*) ok "the container's own underlay (main table) leaves via the host";; *) bad "underlay: '$t'";; esac

dns=$(x sh -c '. /usr/local/lib/exit-vpn/common.sh; load_conf; echo $VPN_DNS')
a=$(x dig +short +time=3 +tries=1 "@$dns" example.com A | head -1)
[ -n "$a" ] && ok "the VPN's resolver ($dns) answers" || bad "no answer from the VPN's resolver $dns"
a=$(x dig +short +time=3 +tries=1 example.com A | head -1)
[ -n "$a" ] && ok "the default resolver (Docker → VPN DNS) answers" || bad "no answer from the default resolver"
out=$(x ip route get "$dns" | head -1)
case "$out" in *"dev $act"*) ok "the route to the VPN's resolver is via the active tunnel";; *) bad "route to $dns: $out";; esac

# IPv6: each tunnel's own sockets via that tunnel; forwarded IPv6 only while the gate is open
src6=$(x sh -c '. /usr/local/lib/exit-vpn/common.sh; load_conf; echo $VPN_SRC6')
t6=$(x curl -6 -s --max-time 4 https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p')
[ -z "$t6" ] && ok "unbound IPv6 does not leave (the main table has no IPv6 route, the tunnels no IPv6 address)" || bad "unbound IPv6 left: $t6"
gate6() { x test -e /run/exit-vpn/ipv6 && echo open || echo closed; }
inset6() { x nft get element inet killswitch ipv6_gate "{ $act }" >/dev/null 2>&1 && echo open || echo closed; }
route6() { x ip -6 route show table 100 | grep -q "^default dev $act" && echo yes || echo no; }
if [ -z "$src6" ]; then
  info "the exit key's config has no IPv6 address (or EXIT_IPV6=off): the exit is IPv4-only"
  [ "$(inset6)" = closed ] && [ "$(x nft list set inet killswitch ipv6_gate | grep -c elements)" = 0 ] \
    && ok "without IPv6 the gate never opens: forwarded IPv6 is refused at once (TCP reset), not left to stall" \
    || bad "IPv6 gate open without an IPv6 address"
else
  t=$(ctrace -6 --interface "$src6")
  if [ -n "$t" ]; then ok "the tunnel's own IPv6 leaves via the VPN: $t"
  else info "this server carries no IPv6 (the tunnel's IPv6 trace failed)"; fi
  g=$(gate6); r=$(inset6)
  if [ "$g" = "$r" ]; then ok "forwarded IPv6 passes the kill switch exactly while the IPv6 gate is open (now $g)"
  else bad "IPv6 gate file $g but the kill switch's ipv6_gate is $r"; fi
fi
out=$(docker exec "$NODE-prober" ip -6 route get 2606:4700::1111 2>&1 | head -1)
case "$out" in *unreachable*|*"Network is unreachable"*|*"No route"*) ok "the prober has no IPv6 route";;
  *) bad "the prober routes IPv6: $out";; esac

x ip link set "$act" down
on_exit "docker exec '$NODE' ip link set '$act' up 2>/dev/null"
t=$(ctrace --interface "$act"); code=$(x curl -s --max-time 5 --interface "$act" -o /dev/null -w '%{http_code}' https://1.1.1.1 2>/dev/null)
a=$(x dig +short +time=2 +tries=1 example.com A | head -1)
if [ -z "$t" ] && [ "$code" = 000 ] && [ -z "$a" ]; then ok "with the active tunnel ($act) down, its traffic and DNS fail closed"
else bad "with $act down: trace '$t', HTTP $code, DNS '$a'"; fi
if [ -n "$src6" ]; then
  sleep 2   # one route-keeper pass
  t=$(ctrace -6 --interface "$src6")
  if [ -z "$t" ] && [ "$(route6)" = no ]; then ok "with $act down, IPv6 fails closed too (no IPv6 route via it)"
  else bad "with $act down: IPv6 trace '$t', forwarded IPv6 route $(route6)"; fi
fi
x ip link set "$act" up
for _ in $(seq 30); do [ -n "$(ctrace -4 --interface "$act")" ] && break; sleep 1; done
t=$(ctrace -4 --interface "$act")
[ -n "$t" ] && ok "with $act back up, its routes are restored and traffic flows ($t)" || bad "no recovery after $act came back"

[ "$(egress_ip)" = "$host_ip" ] && ok "the host's own egress is unchanged" || bad "the host's egress changed"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HOST_SERVICE_URL")
[ "$code" = 200 ] && ok "the host's own service is healthy ($HOST_SERVICE_URL)" || bad "host service $HOST_SERVICE_URL: HTTP $code"

summary
