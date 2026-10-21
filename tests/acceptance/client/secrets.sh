#!/usr/bin/env bash
# Secrets stay where they belong: no private key in the API or the container logs, and the
# provider session (it lives in the Mac's Keychain) is nowhere on the VPS. The session tokens
# go to the VPS on ssh's stdin, never in argv, and are only compared there. Needs
# `exitctl proton login`. Changes nothing.
. "$(dirname "$0")/../lib.sh"
need_login
vps_connect

r=$( { api "$API/v1/keys"; status; } | grep -c -E -i 'privatekey[[:space:]]*=')
[ "$r" = 0 ] && ok "no private key in /v1/keys or /v1/status" || bad "private key text in API responses"

hits=$("$PY" -I -c '
import json, keyring
d = json.loads(keyring.get_password("exitctl", "proton:session"))
print("\n".join(v for v in (d.get("UID"), d.get("AccessToken"), d.get("RefreshToken")) if v))' \
  | vps_stdin "f=\$(mktemp); cat > \$f; [ \$(wc -l < \$f) -ge 2 ] || { echo no-tokens; rm -f \$f; exit; }
      n=0; for c in $NODE $NODE-prober; do sudo docker logs \$c 2>&1 | grep -q -F -f \$f && n=\$((n+1)); done
      m=\$(sudo sh -c \"grep -r -l -F -f \$f /opt/exit-node /var/lib/docker/volumes/exit-vpn_* 2>/dev/null\" | wc -l)
      rm -f \$f; echo \$((n+m))" 2>/dev/null)
[ "${hits:-x}" = 0 ] && ok "the provider session is not on the VPS (logs, deploy dir, volumes)" \
  || bad "session token found on the VPS (${hits:-check failed})"

logkeys=$(vps "for c in $NODE $NODE-prober; do sudo docker logs \$c 2>&1; done | grep -c -E -i 'privatekey[[:space:]]*='")
[ "$logkeys" = 0 ] && ok "no private key text in the container logs" || bad "'PrivateKey' in the container logs ($logkeys)"

summary
