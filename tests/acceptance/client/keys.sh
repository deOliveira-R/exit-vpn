#!/usr/bin/env bash
# Key slots: rotating the exit's key (with new options) goes live without a leak and deletes
# the old certificate, renewing keeps the key, an unregistered key is rolled back, rotating
# the prober's key keeps find working, and the bootstrap files mirror the live keys. Needs
# `exitctl proton login`. Leaves slot a on a fresh key with NetShield 0, slot b on a fresh key.
. "$(dirname "$0")/../lib.sh"
AD_DOMAINS="doubleclick.net googleadservices.com adservice.google.com"
need_login
vps_connect
use_exit_node
slot() { api "$API/v1/keys" | python3 -c "import json,sys; s=next(x for x in json.load(sys.stdin)['slots'] if x['slot']=='$1'); print(s$2)"; }
# Ad domains answered by the VPN's resolver through the tunnel (NetShield acts there)
ads_blocked() {
  vps "sudo docker exec $NODE sh -c 'for d in $AD_DOMAINS; do echo x\$(dig +short +time=3 +tries=1 @10.2.0.1 \$d A | head -n1); done'" 2>/dev/null \
    | awk '$0=="x" || $0=="x0.0.0.0" || $0=="x127.0.0.1" {n++} END {print n+0}'
}
log=$(mktemp); on_exit "rm -f '$log'"
info "slot a before: $(slot a '["source"], s["public_key"][:12], s["options"].get("NetShield")')"
ads_before=$(ads_blocked)

# Rotate slot a with NetShield 2 (ads and trackers)
sample_start "$log"
pub_before=$(slot a '["public_key"]'); t0=$(date +%s)
out=$("$EXITCTL" proton rotate --slot a --netshield 2 --json); rc=$?
dt=$(( $(date +%s) - t0 )); sleep 3; sample_stop
pub_after=$(slot a '["public_key"]'); ns=$(slot a '["options"].get("NetShield")'); ip=$(egress_ip)
deleted=$(echo "$out" | j 'd.get("deleted_old")' 2>/dev/null)
if leaked "$log"; then bad "the VPS IP was seen during the key rotation"
elif [ $rc -eq 0 ] && [ "$pub_after" != "$pub_before" ] && [ "$ns" = 2 ] && [ -n "$ip" ] && [ "$ip" != "$VPS_EGRESS" ]; then
  ok "rotating the exit's key goes live without a leak (${dt}s, exit $ip)"
else bad "rotate slot a: rc=$rc NetShield=$ns ip=$ip: $(echo "$out" | head -c 300)"; fi
info "$(awk 'NF==1' "$log" | wc -l | tr -d ' ') failed samples (0.5 s each) during the swap"
ads_after=$(ads_blocked)
[ "$ads_after" -gt "$ads_before" ] && ok "the new key's options apply: NetShield 2 blocks $ads_after of 3 ad domains (was $ads_before)" \
  || bad "NetShield 2: $ads_before → $ads_after of 3 ad domains blocked"
if [ "$deleted" != None ]; then
  gone=$("$EXITCTL" proton list --json | j "not any(c['id']=='$deleted' for c in d)")
  [ "$gone" = True ] && ok "the old certificate is deleted from the account" || bad "the old certificate $deleted is still listed"
fi

# Renew slot a: the expiry moves (or stays at the maximum), same key, same server
exp_before=$(slot a '["meta"].get("expires_at")'); srv_before=$(status | j 'd["server"]')
out=$("$EXITCTL" proton renew --slot a --json); rc=$?
exp_after=$(slot a '["meta"].get("expires_at")'); pub_now=$(slot a '["public_key"]'); srv_after=$(status | j 'd["server"]')
if [ $rc -eq 0 ] && [ "$pub_now" = "$pub_after" ] && [ "$srv_after" = "$srv_before" ] && [ "$exp_after" -ge "$exp_before" ]; then
  ok "renewing keeps the key and the server: expiry $exp_before → $exp_after"
else bad "renew rc=$rc: $(echo "$out" | head -c 300)"; fi

# An unregistered key: 409, the previous key back, no leak
sample_start "$log"
bogus=$("$PY" -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())')
conf=$(printf '[Interface]\nPrivateKey = %s\nAddress = 10.2.0.2/32\nDNS = 10.2.0.1\n\n[Peer]\nPublicKey = %s\nEndpoint = 192.0.2.1:51820\n' \
  "$bogus" "$(status | j 'd["pubkey"]')")
t0=$(date +%s)
code=$(python3 -c 'import json,sys; print(json.dumps({"conf": sys.stdin.read(), "meta": {"provider": "test-bogus"}}))' <<<"$conf" \
  | curl -s -o /dev/null -w '%{http_code}' --max-time 150 -X PUT -H 'content-type: application/json' --data-binary @- "$API/v1/keys/a")
dt=$(( $(date +%s) - t0 )); unset bogus conf
wait_egress 30; sleep 2; sample_stop
pub_now=$(slot a '["public_key"]'); ip=$(egress_ip)
if leaked "$log"; then bad "the VPS IP was seen while trying the unregistered key"
elif [ "$code" = 409 ] && [ "$pub_now" = "$pub_after" ] && [ -n "$ip" ]; then
  ok "an unregistered key is rolled back: 409 after ${dt}s, previous key live, exit $ip"
else bad "unregistered key: HTTP $code, previous key back: $([ "$pub_now" = "$pub_after" ] && echo yes || echo no), ip '$ip'"; fi

# Rotate slot b (the prober's): a find job then goes through the new key
out=$("$EXITCTL" proton rotate --slot b --netshield 0 --json); rc=$?
[ $rc -eq 0 ] && ok "rotating the prober's key: $(slot b '["source"], s["meta"].get("device_name")')" || bad "rotate slot b: $(echo "$out" | head -c 300)"
job=$(post POST /v1/find '{"url":"https://www.cloudflare.com/cdn-cgi/trace","max_candidates":2,"mode":"http"}' | j 'd["job_id"]')
for _ in $(seq 60); do r=$(api "$API/v1/find/$job"); [ "$(echo "$r" | j 'd["state"]')" != running ] && break; sleep 2; done
# The trace page is too bare for a verdict ("inconclusive"); what matters is that the
# prober's tunnel carried traffic with the new key: an exit IP and no tunnel error
nok=$(echo "$r" | j 'sum(1 for x in d["results"] if x.get("exit_ip") and x["outcome"] != "error")')
[ "$nok" -ge 1 ] && ok "find works on the prober's new key ($nok of 2 probes through its tunnel)" || bad "find after the prober key change: $(echo "$r" | head -c 300)"

# The bootstrap files (used when the volumes are empty) mirror the live keys
same=$(vps 'for s in a b; do sudo cmp -s /var/lib/docker/volumes/exit-vpn_keys-$s/_data/wg.conf /opt/exit-node/secrets/proton_$s.conf && echo same; done' | grep -c same)
[ "$same" = 2 ] && ok "the bootstrap files mirror the live keys" || bad "$same of 2 bootstrap files match the live keys"

# The exit's usual options: NetShield 0
"$EXITCTL" proton rotate --slot a --netshield 0 --json >/dev/null && info "slot a back to NetShield 0" || warn "could not rotate slot a back to NetShield 0"

summary
