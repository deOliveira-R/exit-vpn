#!/bin/sh
# Prober netns: wg-probe (key B) carries ALL traffic, wg-quick style (fwmark), with an
# unreachable fallback and an nftables output policy of drop. The only packets allowed
# out of eth0 are wg-probe's own (marked) WireGuard packets and replies to exitnet.
set -eu
CONF=${PROBE_VPN_CONF:-/secrets/proton_b.conf}
# An uploaded key (slot b) takes precedence; POST /key/reload re-reads it.
# Same choice as keyfile.KeyFile.path(); IF/TABLE/port must match tunnel.ProbeTunnel.
KEY_DIR=${KEY_DIR_B:-/keys/b}
if [ -r "$KEY_DIR/wg.conf" ]; then CONF=$KEY_DIR/wg.conf; fi
IF=wg-probe; TABLE=200; MARK=0xca6c
EXITNET=${EXITNET_SUBNET:?}; EXIT_IP=${EXIT_IP:?}
log() { echo "exit-vpn[prober-init]: $*" >&2; }

val() { awk -v sec="$1" -v key="$2" '
  /^[[:space:]]*\[/ { cur=$0; gsub(/[][[:space:]]/,"",cur); next }
  cur==sec { line=$0; sub(/#.*/,"",line); split(line,kv,"="); k=kv[1]; gsub(/[[:space:]]/,"",k)
    if (k==key) { v=substr(line,index(line,"=")+1); gsub(/^[[:space:]]+|[[:space:]]+$/,"",v); print v; exit } }' "$CONF"; }
ipv4() { echo "$1" | tr ',' '\n' | tr -d ' ' | grep -E '^[0-9]+(\.[0-9]+){3}(/[0-9]+)?$' | head -n1; }
ADDR=$(ipv4 "$(val Interface Address)"); case "$ADDR" in */*) ;; *) ADDR="$ADDR/32";; esac

# Kill switch first: nothing may leave before the rules exist
nft -f - <<NFT
table inet prober_ks
delete table inet prober_ks
table inet prober_ks {
  chain output {
    type filter hook output priority -10; policy drop;
    oifname "lo" accept
    oifname "$IF" accept
    meta mark $MARK udp dport 51820 accept
    ip daddr $EXITNET accept
  }
  chain input {
    type filter hook input priority -10; policy accept;
    tcp dport 9000 iifname != "lo" ip saddr != $EXIT_IP drop
  }
}
table ip prober_nat
delete table ip prober_nat
table ip prober_nat {
  chain postrouting { type nat hook postrouting priority 100; policy accept; oifname "$IF" masquerade; }
}
NFT
ip route replace unreachable default metric 1000 table $TABLE
ip -6 route replace unreachable default metric 1000 table $TABLE
ip rule del priority 100 2>/dev/null || true; ip rule add priority 100 to "$EXITNET" lookup main
ip rule del priority 110 2>/dev/null || true; ip rule add priority 110 not fwmark $MARK lookup $TABLE
ip -6 rule del priority 110 2>/dev/null || true; ip -6 rule add priority 110 lookup $TABLE

ip link del $IF 2>/dev/null || true
ip link add $IF type wireguard
val Interface PrivateKey | wg set $IF private-key /dev/stdin fwmark $MARK
ip addr add "$ADDR" dev $IF
ip link set $IF mtu 1420 up
ip route replace default dev $IF table $TABLE
log "kill switch + $IF ready (address $ADDR, no peer until a probe)"

cd /opt/prober/src
exec /opt/prober/venv/bin/python -m prober
