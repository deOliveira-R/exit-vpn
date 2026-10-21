# Shared helpers for the exit-vpn init scripts (sourced, POSIX sh).
# Reads a standard wg-quick config (provider-agnostic) given by EXIT_VPN_CONF.
set -eu

: "${EXIT_VPN_CONF:?EXIT_VPN_CONF must point to the WireGuard config}"
# An uploaded key (slot a) takes precedence over the bootstrap config.
# Must match the controller's KeyStore.path("a") (controller/src/exit_vpn/keys.py).
KEY_DIR_A=${KEY_DIR_A:-/keys/a}
if [ -r "$KEY_DIR_A/wg.conf" ]; then EXIT_VPN_CONF=$KEY_DIR_A/wg.conf; fi
WG_IF=${WG_IF:-wg0}
EXIT_TABLE=${EXIT_TABLE:-100}      # forwarded exit traffic; its IPv6 default only while the gate is open
TUNNEL6_TABLE=${TUNNEL6_TABLE:-101} # the tunnel's own IPv6 sockets (health probe): always via wg0
# The IPv6 gate: written by the controller while IPv6 through the tunnel is proven to work;
# route-keeper routes forwarded IPv6 via wg0 only while it exists. /run starts empty.
IPV6_GATE=${IPV6_GATE:-/run/exit-vpn/ipv6}
EXIT_IPV6=${EXIT_IPV6:-on}         # off: no IPv6 through the exit at all (.env)
STATE_DIR=${STATE_DIR:-/var/lib/exit-vpn}   # peer.env: written by persistence.StateStore

log() { echo "exit-vpn[$(basename "$0")]: $*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# conf_value <section> <key>: first value of key in [section], comments stripped.
conf_value() {
  awk -v sec="$1" -v key="$2" '
    /^[[:space:]]*\[/ { cur = $0; gsub(/[][[:space:]]/, "", cur); next }
    cur == sec {
      line = $0; sub(/#.*/, "", line)
      split(line, kv, "=")
      k = kv[1]; gsub(/[[:space:]]/, "", k)
      if (k == key) { v = substr(line, index(line, "=") + 1); gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); print v; exit }
    }' "$EXIT_VPN_CONF"
}

# first_ipv4 / first_ipv6 "<comma list>": the first entry of that family.
first_ipv4() { echo "$1" | tr ',' '\n' | tr -d ' ' | grep -E '^[0-9]+(\.[0-9]+){3}(/[0-9]+)?$' | head -n1; }
first_ipv6() { echo "$1" | tr ',' '\n' | tr -d ' ' | grep -E '^[0-9a-fA-F]*:[0-9a-fA-F:]*(/[0-9]+)?$' | head -n1 || true; }

load_conf() {
  [ -r "$EXIT_VPN_CONF" ] || die "cannot read $EXIT_VPN_CONF"
  VPN_ADDR=$(first_ipv4 "$(conf_value Interface Address)")
  VPN_DNS=$(first_ipv4 "$(conf_value Interface DNS)")
  CONF_PEER_PUBKEY=$(conf_value Peer PublicKey)
  CONF_PEER_ENDPOINT=$(conf_value Peer Endpoint)
  [ -n "$VPN_ADDR" ] || die "no IPv4 Address in $EXIT_VPN_CONF"
  [ -n "$VPN_DNS" ] || die "no IPv4 DNS in $EXIT_VPN_CONF"
  [ -n "$CONF_PEER_PUBKEY" ] && [ -n "$CONF_PEER_ENDPOINT" ] || die "no [Peer] PublicKey/Endpoint in $EXIT_VPN_CONF"
  case "$VPN_ADDR" in */*) ;; *) VPN_ADDR="$VPN_ADDR/32" ;; esac
  VPN_SRC=${VPN_ADDR%/*}
  # IPv6 is optional: none in the config (or EXIT_IPV6=off) means the exit stays IPv4-only
  VPN_ADDR6=""
  [ "$EXIT_IPV6" = off ] || VPN_ADDR6=$(first_ipv6 "$(conf_value Interface Address)")
  case "$VPN_ADDR6" in ""|*/*) ;; *) VPN_ADDR6="$VPN_ADDR6/128" ;; esac
  VPN_SRC6=${VPN_ADDR6%/*}
}

# Tailscale
TS_SOCKET=${TS_SOCKET:-/run/tailscale/tailscaled.sock}
TS_STATE_DIR=${TS_STATE_DIR:-/var/lib/tailscale}
TS_PORT=${TS_PORT:-41642}
ts() { tailscale --socket="$TS_SOCKET" "$@"; }
