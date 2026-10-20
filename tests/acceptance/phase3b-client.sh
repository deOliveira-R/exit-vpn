#!/usr/bin/env bash
# Phase 3b acceptance (PLAN T16-T20), run on the Mac in the tailnet (any VPN app off),
# after `exitctl proton login`. Selects the exit node for the leak loops and clears it
# on exit. Leaves slot a on a fresh key with NetShield 0, slot b on a fresh key.
set -uo pipefail
cd "$(dirname "$0")/../.."
NODE=${EXIT_NODE_NAME:-exit-proton}
VPS=${VPS_SSH:-fin-intel}; VPS_TS_NAME=${VPS_TS_NAME:-fin-intel}
API="http://$NODE:${API_PORT:-8080}"
EXITCTL=${EXITCTL:-helper/.venv/bin/exitctl}
PY=${PY:-helper/.venv/bin/python}
AD_DOMAINS="doubleclick.net googleadservices.com adservice.google.com"
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
j()   { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
api() { curl -s --max-time "${T:-40}" "$@"; }
trace() { curl -s --max-time "${1:-3}" https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p'; }
slot() { api "$API/v1/keys" | j "next(s for s in d['slots'] if s['slot']=='$1')$2"; }

vps_ip=$(tailscale ip -4 "$VPS_TS_NAME")
vps_alias_host=$(ssh -G "$VPS" | awk '$1=="hostname"{print $2}')
vps() { ssh -o HostName="$vps_ip" -o HostKeyAlias="$vps_alias_host" -o ConnectTimeout=10 "$VPS" "$@"; }
loop_pid=""
cleanup() {
  [ -n "$loop_pid" ] && kill "$loop_pid" 2>/dev/null
  tailscale set --exit-node= >/dev/null 2>&1; echo "(exit node cleared)"
}
trap cleanup EXIT

# Leak loop: the exit's IP every 0.5 s; the Oracle IP must never appear
leak_start() { : > "$1"; ( while :; do echo "$(date +%s) $(trace 2)" >> "$1"; sleep 0.5; done ) & loop_pid=$!; }
leak_stop()  { kill "$loop_pid" 2>/dev/null; wait "$loop_pid" 2>/dev/null; loop_pid=""; }
# Ad domains answered by Proton's resolver through the tunnel (NetShield acts there)
ads_blocked() {
  vps -n "sudo docker exec $NODE sh -c 'for d in $AD_DOMAINS; do dig +short +time=3 +tries=1 @10.2.0.1 \$d A | head -n1; done'" \
    | grep -c -E '^(0\.0\.0\.0|127\.0\.0\.1|)$'
}

$EXITCTL proton whoami --json | j 'd.get("account") or exit("not logged in: run exitctl proton login")' >/dev/null \
  || { echo "log in first: $EXITCTL proton login"; exit 1; }
tailscale set --exit-node="$NODE" --exit-node-allow-lan-access=true
for _ in $(seq 20); do [ -n "$(trace)" ] && break; sleep 1; done
oracle=$(vps -n 'curl -s --max-time 5 https://www.cloudflare.com/cdn-cgi/trace | sed -n s/^ip=//p')
echo "INFO slot a before: $(slot a '["source"], s["public_key"][:12], s["options"]')"
ads_before=$(ads_blocked)

# T16: rotate slot a with NetShield 2 (ads/trackers): new key live, no leak, filter on
log=$(mktemp); leak_start "$log"
pub_before=$(slot a '["public_key"]')
t0=$(date +%s)
out=$($EXITCTL proton rotate --slot a --netshield 2 --json); rc=$?
dt=$(( $(date +%s) - t0 ))
sleep 3; leak_stop
pub_after=$(slot a '["public_key"]'); ns=$(slot a '["options"].get("NetShield")')
ip=$(trace)
ads_after=$(ads_blocked)
deleted=$(echo "$out" | j 'd.get("deleted_old")' 2>/dev/null)
if grep -q " $oracle$" "$log"; then bad "T16 LEAK: Oracle IP seen during key rotation"
elif [ $rc -eq 0 ] && [ "$pub_after" != "$pub_before" ] && [ "$ns" = 2 ] && [ -n "$ip" ] && [ "$ip" != "$oracle" ]; then
  ok "T16 rotate slot a (NetShield 2) in ${dt}s: new key live, exit $ip, old certificate deleted: $deleted"
else bad "T16 rotate rc=$rc ns=$ns ip=$ip: $(echo "$out" | head -c 300)"; fi
gaps=$(awk '$2=="" {n++} END {print n+0}' "$log"); echo "INFO T16 ${gaps} failed samples (0.5 s each) during the swap"
[ "$ads_after" -gt "$ads_before" ] && ok "T16 NetShield 2 filters: $ads_before → $ads_after of 3 ad domains blocked" \
  || bad "T16 NetShield: $ads_before → $ads_after of 3 ad domains blocked"
gone=$($EXITCTL proton list --json | j "not any(c['id']=='$deleted' for c in d)")
[ "$deleted" = None ] || { [ "$gone" = True ] && ok "T16 old certificate $deleted gone from the account" || bad "T16 old certificate still listed"; }

# T19: renew slot a: expiry moves (or stays at the max), same key, no reconnect
exp_before=$(slot a '["meta"].get("expires_at")'); srv_before=$(api "$API/v1/status" | j 'd["server"]')
out=$($EXITCTL proton renew --slot a --json); rc=$?
exp_after=$(slot a '["meta"].get("expires_at")'); pub_now=$(slot a '["public_key"]')
srv_after=$(api "$API/v1/status" | j 'd["server"]')
if [ $rc -eq 0 ] && [ "$pub_now" = "$pub_after" ] && [ "$srv_after" = "$srv_before" ] && [ "$exp_after" -ge "$exp_before" ]; then
  ok "T19 renew slot a: expiry $exp_before → $exp_after, same key, same server"
else bad "T19 renew rc=$rc: $(echo "$out" | head -c 300)"; fi

# T17: an unregistered key → 409, previous key back, no leak
leak_start "$log"
bogus=$($PY -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())')
conf=$(printf '[Interface]\nPrivateKey = %s\nAddress = 10.2.0.2/32\nDNS = 10.2.0.1\n\n[Peer]\nPublicKey = %s\nEndpoint = 192.0.2.1:51820\n' \
  "$bogus" "$(api "$API/v1/status" | j 'd["pubkey"]')")
t0=$(date +%s)
code=$(python3 -c 'import json,sys; print(json.dumps({"conf": sys.stdin.read(), "meta": {"provider": "test-bogus"}}))' <<<"$conf" \
  | curl -s -o /dev/null -w '%{http_code}' --max-time 150 -X PUT -H 'content-type: application/json' --data-binary @- "$API/v1/keys/a")
dt=$(( $(date +%s) - t0 ))
unset bogus conf
for _ in $(seq 30); do [ -n "$(trace)" ] && break; sleep 1; done
sleep 2; leak_stop
pub_now=$(slot a '["public_key"]'); ip=$(trace)
if grep -q " $oracle$" "$log"; then bad "T17 LEAK: Oracle IP seen while testing the bogus key"
elif [ "$code" = 409 ] && [ "$pub_now" = "$pub_after" ] && [ -n "$ip" ]; then
  ok "T17 bogus key rejected (409) after ${dt}s, previous key back, exit $ip"
else bad "T17 code=$code key_back=$([ "$pub_now" = "$pub_after" ] && echo yes || echo no) ip=$ip"; fi
rm -f "$log"

# T20: rotate slot b (prober), then a find job works
out=$($EXITCTL proton rotate --slot b --netshield 0 --json); rc=$?
[ $rc -eq 0 ] && ok "T20a rotate slot b: $(slot b '["source"], s["meta"].get("device_name")')" \
  || bad "T20a rotate slot b: $(echo "$out" | head -c 300)"
job=$(api -X POST -H 'content-type: application/json' \
  -d '{"url":"https://www.cloudflare.com/cdn-cgi/trace","max_candidates":2,"mode":"http"}' "$API/v1/find" | j 'd["job_id"]')
for _ in $(seq 60); do r=$(api "$API/v1/find/$job"); [ "$(echo "$r" | j 'd["state"]')" != running ] && break; sleep 2; done
nok=$(echo "$r" | j 'len(d["ok"])')
[ "$nok" -ge 1 ] && ok "T20b find job after the prober key change: $nok ok" || bad "T20b find: $(echo "$r" | head -c 300)"

# Back to NetShield 0 on the exit (PLAN Q4)
$EXITCTL proton rotate --slot a --netshield 0 --json >/dev/null && echo "INFO slot a back to NetShield 0" \
  || echo "WARN could not rotate slot a back to NetShield 0"

# T18: the Proton session never reaches the VPS; no private key in responses or logs.
# The tokens go to the VPS on ssh's stdin (never argv) and are only compared there.
r=$( { api "$API/v1/keys"; api "$API/v1/status"; } | grep -c -i 'privatekey')
[ "$r" = 0 ] && ok "T18a no private key in /v1/keys or /v1/status" || bad "T18a private key text in API responses"
hits=$($PY -I -c '
import json, keyring
d = json.loads(keyring.get_password("exitctl", "proton:session"))
print("\n".join(v for v in (d.get("UID"), d.get("AccessToken"), d.get("RefreshToken")) if v))' \
  | vps "f=\$(mktemp); cat > \$f; [ \$(wc -l < \$f) -ge 2 ] || { echo no-tokens; rm -f \$f; exit; }
      n=0; for c in $NODE $NODE-prober; do sudo docker logs \$c 2>&1 | grep -q -F -f \$f && n=\$((n+1)); done
      m=\$(sudo sh -c \"grep -r -l -F -f \$f /opt/exit-node /var/lib/docker/volumes/exit-vpn_* 2>/dev/null\" | wc -l)
      rm -f \$f; echo \$((n+m))" 2>/dev/null)
logkeys=$(vps -n "for c in $NODE $NODE-prober; do sudo docker logs \$c 2>&1; done | grep -c -i 'privatekey'")
[ "${hits:-x}" = 0 ] && ok "T18b Proton session tokens: not in container logs, the deploy dir or the volumes" \
  || bad "T18b session token found on the VPS (${hits:-check failed})"
[ "$logkeys" = 0 ] && ok "T18c no private key text in container logs" || bad "T18c 'PrivateKey' in logs ($logkeys)"

echo "== $pass passed, $fail failed"; [ "$fail" -eq 0 ]
