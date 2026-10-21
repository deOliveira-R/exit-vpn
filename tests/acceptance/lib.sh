# Shared by the acceptance scripts (sourced, not run). What each script proves: README.md.
#
#   check reporting   ok / bad / info / warn, summary (exit status: 0 only if nothing failed)
#   cleanup           on_exit '<command>' runs at exit, last registered first, even on failure
#   JSON              j '<python expression over d>' reads stdin
#   controller API    api <curl args>, post <METHOD> <path> <json>, status
#   egress            trace [timeout] → "ip loc" of this machine's internet egress; egress_ip
#   VPS (from a Mac)  vps <command> over the tailnet; VPS_EGRESS: the VPS's own IP (a leak)
#   samples           sample_start <file> / sample_stop: egress IP every 0.5 s; leaked <file>
#   faults            drop_endpoint <ip> <tag> / clear_drops <tag>: silently drop wg0 traffic
#   exit node         use_exit_node: select it for this run, restore the previous one at exit
#
# Settings (environment): EXIT_NODE_NAME (exit-proton), API_PORT (8080), VPS_SSH (the VPS's ssh
# alias), VPS_TS_NAME (its tailnet name), EXITCTL.

set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SUITE=$(basename "$0" .sh)
NODE=${EXIT_NODE_NAME:-exit-proton}
API="http://$NODE:${API_PORT:-8080}"
VPS=${VPS_SSH:-fin-intel}
VPS_TS_NAME=${VPS_TS_NAME:-fin-intel}
EXITCTL=${EXITCTL:-$REPO/helper/.venv/bin/exitctl}
PY=${PY:-$REPO/helper/.venv/bin/python}

pass=0; fail=0
ok()   { echo "PASS $SUITE: $1"; pass=$((pass+1)); }
bad()  { echo "FAIL $SUITE: $1"; fail=$((fail+1)); }
info() { echo "INFO $SUITE: $1"; }
warn() { echo "WARN $SUITE: $1"; }
summary() { echo "== $SUITE: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; }
die()  { echo "STOP $SUITE: $1" >&2; exit 2; }

_cleanups=()
on_exit() { _cleanups=("$1" ${_cleanups[@]+"${_cleanups[@]}"}); }
_run_cleanups() { local c; for c in ${_cleanups[@]+"${_cleanups[@]}"}; do eval "$c"; done; }
trap _run_cleanups EXIT

j()      { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
api()    { curl -s --max-time "${T:-90}" "$@"; }
post()   { api -X "$1" -H 'content-type: application/json' -d "$3" "$API$2"; }
status() { api "$API/v1/status"; }

trace() {
  curl -s --max-time "${1:-5}" https://www.cloudflare.com/cdn-cgi/trace \
    | awk -F= '$1=="ip"{ip=$2} $1=="loc"{loc=$2} END{if (ip) print ip, loc}'
}
egress_ip() { trace "${1:-5}" | cut -d' ' -f1; }
wait_egress() { local _; for _ in $(seq "${1:-30}"); do [ -n "$(egress_ip 3)" ] && return 0; sleep 1; done; return 1; }

# The VPS over the tailnet (public-IP sessions were reset during deploys), with the host key
# checked against the ssh alias's known_hosts entry.
vps_connect() {
  vps_ip=$(tailscale ip -4 "$VPS_TS_NAME") || die "cannot resolve $VPS_TS_NAME in the tailnet"
  vps_host=$(ssh -G "$VPS" | awk '$1=="hostname"{print $2}')
  VPS_EGRESS=$(vps 'curl -s --max-time 5 https://www.cloudflare.com/cdn-cgi/trace | sed -n s/^ip=//p')
  [ -n "$VPS_EGRESS" ] || die "cannot read the VPS's own egress IP"
}
vps() { ssh -n -T -o HostName="$vps_ip" -o HostKeyAlias="$vps_host" -o ConnectTimeout=10 "$VPS" "$@"; }
vps_stdin() { ssh -T -o HostName="$vps_ip" -o HostKeyAlias="$vps_host" -o ConnectTimeout=10 "$VPS" "$@"; }

_sampler=""
sample_start() { : > "$1"; ( while :; do echo "$SECONDS $(egress_ip 2)" >> "$1"; sleep 0.5; done ) & _sampler=$!; }
sample_stop()  { [ -n "$_sampler" ] && { kill "$_sampler" 2>/dev/null; wait "$_sampler" 2>/dev/null; }; _sampler=""; }
leaked() { grep -q " $VPS_EGRESS$" "$1"; }
on_exit sample_stop

drop_endpoint() { vps "sudo docker exec $NODE nft insert rule inet killswitch output ip daddr $1 udp dport 51820 drop comment $2"; }
clear_drops() {
  vps "sudo docker exec $NODE sh -c 'for h in \$(nft -a list chain inet killswitch output | sed -n \"s/.*comment \\\"$1\\\".*handle \\([0-9]*\\)/\\1/p\"); do nft delete rule inet killswitch output handle \$h; done'" 2>/dev/null
}

# The exit node this Mac uses now is restored at exit. A Mac with none is refused: "restoring"
# none would leave its traffic going out directly after the test (a leak, not a cleanup).
# ALLOW_NO_EXIT=1 runs anyway and restores none.
use_exit_node() {
  local prev
  prev=$(tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); print(next((p["TailscaleIPs"][0] for p in (s.get("Peer") or {}).values() if p.get("ExitNode")), ""))')
  if [ -z "$prev" ] && [ "${ALLOW_NO_EXIT:-0}" != 1 ]; then
    die "this Mac uses no exit node, so the run would end by restoring none; select one first (tailscale set --exit-node=$NODE) or set ALLOW_NO_EXIT=1"
  fi
  on_exit "tailscale set --exit-node='$prev' >/dev/null 2>&1; echo '(exit node restored)'"
  tailscale set --exit-node="$NODE" --exit-node-allow-lan-access=true || die "cannot select $NODE"
  wait_egress 20
}

# exitctl must be logged in (the owner does it: password, 2FA and CAPTCHA prompts).
need_login() {
  "$EXITCTL" proton whoami --json 2>/dev/null | j 'd.get("account") or exit(1)' >/dev/null \
    || die "exitctl is not logged in: run $EXITCTL proton login"
}
