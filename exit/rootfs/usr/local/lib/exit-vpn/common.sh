# Shared helpers for the exit-vpn init scripts (sourced, POSIX sh).
# Reads a standard wg-quick config (provider-agnostic, PLAN D16) given by EXIT_VPN_CONF.
set -eu

: "${EXIT_VPN_CONF:?EXIT_VPN_CONF must point to the WireGuard config}"
WG_IF=${WG_IF:-wg0}
EXIT_TABLE=${EXIT_TABLE:-100}
STATE_DIR=${STATE_DIR:-/var/lib/exit-vpn}

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

# first_ipv4 "<comma list>": the first IPv4 entry (IPv6 comes later; PLAN open question 6).
first_ipv4() { echo "$1" | tr ',' '\n' | tr -d ' ' | grep -E '^[0-9]+(\.[0-9]+){3}(/[0-9]+)?$' | head -n1; }

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
}
