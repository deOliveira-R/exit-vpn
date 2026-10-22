# Shared helpers for the exit-vpn init scripts (sourced, POSIX sh).
# Reads a standard wg-quick config (provider-agnostic) given by EXIT_VPN_CONF.
set -eu

: "${EXIT_VPN_CONF:?EXIT_VPN_CONF must point to the WireGuard config}"
# An uploaded key (slot a) takes precedence over the bootstrap config.
# Must match the controller's KeyStore.path("a") (controller/src/exit_vpn/keys.py).
KEY_DIR_A=${KEY_DIR_A:-/keys/a}
if [ -r "$KEY_DIR_A/wg.conf" ]; then EXIT_VPN_CONF=$KEY_DIR_A/wg.conf; fi
# The standby tunnel's key (slot c): uploaded, else the bootstrap config if there is one.
# Must match the controller's KeyStore.path("c").
KEY_DIR_C=${KEY_DIR_C:-/keys/c}
STANDBY_VPN_CONF=${STANDBY_VPN_CONF:-}
if [ -r "$KEY_DIR_C/wg.conf" ]; then STANDBY_VPN_CONF=$KEY_DIR_C/wg.conf; fi
# Two tunnels, two roles: the *active* one carries forwarded traffic (table 100's default),
# the *standby* one is kept ready for a handover. Keys stay with interfaces (wg0: slot a,
# wg1: slot c); the roles move. At start wg0 is active.
WG_IF=${WG_IF:-wg0}
STANDBY_IF=${STANDBY_IF:-wg1}
TUNNELS="$WG_IF $STANDBY_IF"
EXIT_TABLE=${EXIT_TABLE:-100}      # forwarded exit traffic: default via the active tunnel
# Each tunnel's own sockets (bound to the interface: the health probes): via that tunnel only
tunnel_table() { if [ "$1" = "$WG_IF" ]; then echo 102; else echo 103; fi; }
# The active tunnel, written by the controller at each handover (absent: wg0)
ACTIVE_FILE=${ACTIVE_FILE:-/run/exit-vpn/active}
active_if() { a=$(cat "$ACTIVE_FILE" 2>/dev/null || true); echo "${a:-$WG_IF}"; }
# The IPv6 gate: written by the controller while IPv6 through the tunnel is proven to work;
# route-keeper lets forwarded IPv6 through (killswitch.nft's ipv6_gate) only while it exists.
# /run starts empty.
IPV6_GATE=${IPV6_GATE:-/run/exit-vpn/ipv6}
EXIT_IPV6=${EXIT_IPV6:-on}         # off: no IPv6 through the exit at all (.env)
STATE_DIR=${STATE_DIR:-/var/lib/exit-vpn}   # peer.env: written by persistence.StateStore

log() { echo "exit-vpn[$(basename "$0")]: $*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# conf_value <section> <key>: first value of key in [section] of $CONF_FILE (default: the exit
# key's config), comments stripped.
conf_value() {
  awk -v sec="$1" -v key="$2" '
    /^[[:space:]]*\[/ { cur = $0; gsub(/[][[:space:]]/, "", cur); next }
    cur == sec {
      line = $0; sub(/#.*/, "", line)
      split(line, kv, "=")
      k = kv[1]; gsub(/[[:space:]]/, "", k)
      if (k == key) { v = substr(line, index(line, "=") + 1); gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); print v; exit }
    }' "${CONF_FILE:-$EXIT_VPN_CONF}"
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
